#!/usr/bin/env python3
"""
Deterministic cross-run master prospect store + upsert.

Reads one or more clean Google Maps datasets (the *.json files produced by
normalize.py), computes a stable master_id for each lead, and upserts them
into a persistent master dataset (data/master/master.csv + master.json) plus
a discovery-history log (data/master/discovery_history.csv).

No AI/fuzzy matching. Pure, rule-based Python, same spirit as normalize.py.

master_id algorithm
--------------------
master_id is assigned ONCE per business and retained forever -- it is never
recomputed from an incoming lead's own fields alone. Instead every incoming
lead is resolved against a deterministic identity index built from the
EXISTING master store:

    identity index: identity key -> master_id
    (one master record can own several keys, e.g. its domain, its phone,
    and its name+address key, all pointing at the same master_id)

Resolution for an incoming lead:
    1. Compute normalize.match_keys(lead) -- all reliable identifiers the
       lead currently carries, in priority order: domain > phone >
       name+address.
    2. Look up each key in the identity index.
       - No key matches anything -> brand-new business. Its master_id is
         assigned once via compute_master_id(), which reuses
         normalize.dedup_key()'s priority order (domain > phone >
         name+address) so a first-seen record with no website/phone still
         gets a stable id.
       - All matching keys point to the same existing master_id -> the lead
         resolves to that master_id (this is how a business keeps its
         master_id even after its website or phone disappears from a later
         scrape: it still matches on whichever other key survived).
       - Matching keys point to DIFFERENT existing master_ids -> identity
         conflict (see below). Resolved deterministically via the same
         domain > phone > name+address priority, and recorded in
         data/master/identity_conflicts.csv rather than silently merged.
    3. Every key the lead carries (including any newly-discovered
       website/phone) is added to the identity index under the resolved
       master_id, so a later scrape that reveals a website or phone for an
       existing name+address-only business attaches to that same record
       instead of minting a new one.

This makes master_id depend only on stable business identifiers matched
against the existing master store, never on run_id/search_id/scraped_at or
on which fields happen to be populated in a single incoming lead, so the
same business reappearing in a later run/search/city -- even with fewer
identifying fields than before -- produces the same master_id.

Conflict rule for merging fields on an existing master record
---------------------------------------------------------------
- Blank-fill only: an existing non-empty value is NEVER overwritten by an
  incoming blank/None value.
- An existing blank/None value IS filled by an incoming non-empty value.
- For two non-empty, DIFFERING values (rating, review_count, address,
  lat/long, etc.) the EXISTING (first-seen) value wins. This is a
  deterministic, documented rule -- not a guess -- and avoids flip-flopping
  the master record every run.
- scraped_at/search_keyword/search_location/search_id/run_id are treated as
  "latest snapshot" provenance and are always overwritten with the values
  from the most recently processed occurrence (full history of every
  occurrence lives in discovery_history.csv, not in the master record).

Usage:
    python3 update_master.py \
        --clean-json data/google-maps/run_1/*/clean/clean.json \
        --master-dir data/master
"""
from __future__ import annotations

import argparse
import csv
import glob
import hashlib
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from normalize import (  # noqa: E402
    CANONICAL_FIELDS,
    dedup_key,
    domain_key,
    match_keys,
    normalize_address_key,
    normalize_name_key,
    normalize_phone_key,
)

MASTER_FIELDS = CANONICAL_FIELDS + [
    "master_id",
    "first_seen_at",
    "last_seen_at",
    "source_count",
    "search_count",
]
# Fields where an existing non-empty value always wins over a differing
# incoming non-empty value (blank-fill still applies).
STICKY_FIELDS = [f for f in CANONICAL_FIELDS if f not in ("scraped_at", "search_keyword", "search_location", "search_id", "run_id")]
# Fields always refreshed to the latest occurrence's values.
LATEST_FIELDS = ["scraped_at", "search_keyword", "search_location", "search_id", "run_id"]

HISTORY_FIELDS = [
    "master_id",
    "run_id",
    "search_id",
    "search_keyword",
    "search_location",
    "source",
    "first_discovered_at",
]


def compute_master_id(lead: dict) -> str:
    key = dedup_key(lead)
    digest = hashlib.sha256(":".join(str(part) for part in key).encode("utf-8")).hexdigest()
    return digest[:16]


CONFLICT_FIELDS = [
    "master_id",
    "resolved_via_key",
    "conflicting_master_ids",
    "business_name",
    "run_id",
    "search_id",
    "detected_at",
]


def build_identity_index(master: dict[str, dict]) -> dict[tuple, str]:
    """Deterministic identity key -> master_id index, rebuilt from the
    existing master store (a master record may own several keys)."""
    index: dict[tuple, str] = {}
    for master_id, record in master.items():
        for key in match_keys(record):
            index[key] = master_id
    return index


def resolve_master_id(lead: dict, index: dict[tuple, str]) -> tuple[str | None, dict | None]:
    """Resolve an incoming lead against the existing identity index.

    Priority: domain > phone > name+address, matching normalize.dedup_key()
    and normalize.match_keys(). Returns (master_id_or_None, conflict_or_None).

    master_id is None only when none of the lead's present keys match
    anything in the index (brand-new business).

    A present-but-STRONGER key (domain, or phone when domain is absent)
    that does NOT match anything in the index is trusted on its own: a
    lower-priority key incidentally matching a different existing master
    (e.g. a coincidentally shared phone number) is NOT used to attach this
    lead to that unrelated master. name+address is always checked as a
    last-resort corroboration, so a business first seen with only a
    name+address key still gets recognized once a website or phone is
    later discovered for it.

    A conflict is reported -- never used to silently merge records -- when
    a present higher-priority key DOES match one master while a present
    lower-priority key matches a DIFFERENT master. Resolution still picks
    the higher-priority key's master deterministically.
    """
    domain = domain_key(lead.get("website", ""))
    phone = normalize_phone_key(lead.get("phone", ""))
    name_key = normalize_name_key(lead.get("business_name", ""))
    address_key = normalize_address_key(lead.get("address", ""))

    domain_key_tuple = ("domain", domain) if domain else None
    phone_key_tuple = ("phone", phone) if phone else None
    name_address_key_tuple = ("name_address", name_key, address_key) if name_key and address_key else None

    domain_match = index.get(domain_key_tuple) if domain_key_tuple else None
    phone_match = index.get(phone_key_tuple) if phone_key_tuple else None
    name_address_match = index.get(name_address_key_tuple) if name_address_key_tuple else None

    def _conflict(resolved_key: tuple, resolved_id: str, other_id: str) -> dict:
        return {
            "resolved_via_key": ":".join(str(part) for part in resolved_key),
            "conflicting_master_ids": [other_id],
        }

    if domain_key_tuple is not None:
        if domain_match is not None:
            conflict = None
            if phone_match is not None and phone_match != domain_match:
                conflict = _conflict(domain_key_tuple, domain_match, phone_match)
            return domain_match, conflict
        # Domain present but brand-new to us: trust it over an incidental
        # phone collision; only name+address may still attach this lead to
        # a pre-existing record (the "website discovered later" case).
        if name_address_match is not None:
            return name_address_match, None
        return None, None

    if phone_key_tuple is not None:
        if phone_match is not None:
            conflict = None
            if name_address_match is not None and name_address_match != phone_match:
                conflict = _conflict(phone_key_tuple, phone_match, name_address_match)
            return phone_match, conflict
        if name_address_match is not None:
            return name_address_match, None
        return None, None

    if name_address_match is not None:
        return name_address_match, None
    return None, None


def _is_blank(value) -> bool:
    return value is None or value == ""


def load_clean_leads(paths: list[str]) -> list[dict]:
    leads = []
    for path in paths:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        leads.extend(data if isinstance(data, list) else data.get("results", []))
    return leads


def load_master(master_json_path: str) -> dict[str, dict]:
    if not os.path.exists(master_json_path):
        return {}
    with open(master_json_path, encoding="utf-8") as f:
        records = json.load(f)
    return {r["master_id"]: r for r in records}


def load_history_keys(history_csv_path: str) -> set[tuple]:
    """Existing (master_id, run_id, search_id) triples, for idempotent history."""
    keys = set()
    if not os.path.exists(history_csv_path):
        return keys
    with open(history_csv_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            keys.add((row["master_id"], row["run_id"], row["search_id"]))
    return keys


def load_history_rows(history_csv_path: str) -> list[dict]:
    if not os.path.exists(history_csv_path):
        return []
    with open(history_csv_path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def merge_lead_into_master(existing: dict | None, lead: dict, master_id: str) -> dict:
    """Deterministically merge one incoming clean lead into a master record."""
    scraped_at = lead.get("scraped_at") or datetime.now(timezone.utc).isoformat()

    if existing is None:
        record = {k: lead.get(k) for k in CANONICAL_FIELDS}
        record["master_id"] = master_id
        record["first_seen_at"] = scraped_at
        record["last_seen_at"] = scraped_at
        record["source_count"] = 1
        record["search_count"] = 1
        return record

    record = dict(existing)

    for field in STICKY_FIELDS:
        incoming = lead.get(field)
        current = record.get(field)
        if _is_blank(current) and not _is_blank(incoming):
            record[field] = incoming
        # else: keep existing value (blank stays blank if incoming also
        # blank; differing non-empty values keep the existing/first-seen one)

    for field in LATEST_FIELDS:
        incoming = lead.get(field)
        if not _is_blank(incoming):
            record[field] = incoming

    existing_first = record.get("first_seen_at") or scraped_at
    existing_last = record.get("last_seen_at") or scraped_at
    record["first_seen_at"] = min(existing_first, scraped_at)
    record["last_seen_at"] = max(existing_last, scraped_at)

    return record


def update_master(
    leads: list[dict],
    existing_master: dict[str, dict],
    existing_history_keys: set[tuple],
    existing_history_rows: list[dict],
    existing_conflicts: list[dict] | None = None,
) -> tuple[list[dict], list[dict], dict]:
    """Pure function: returns (master_records, history_rows, stats)."""
    master = dict(existing_master)
    history_rows = list(existing_history_rows)
    history_keys = set(existing_history_keys)
    conflicts = list(existing_conflicts or [])

    identity_index = build_identity_index(master)

    new_records = 0
    updated_records = 0
    new_history_events = 0

    # Track distinct sources / search_ids per master_id across full history
    # (existing history + this batch) so counts stay correct on reprocessing.
    sources_seen: dict[str, set] = {}
    searches_seen: dict[str, set] = {}
    for row in history_rows:
        sources_seen.setdefault(row["master_id"], set()).add(row.get("source", ""))
        searches_seen.setdefault(row["master_id"], set()).add(row.get("search_id", ""))

    for lead in leads:
        resolved_id, conflict = resolve_master_id(lead, identity_index)
        master_id = resolved_id if resolved_id is not None else compute_master_id(lead)

        if conflict:
            conflicts.append(
                {
                    "master_id": master_id,
                    "resolved_via_key": conflict["resolved_via_key"],
                    "conflicting_master_ids": "|".join(conflict["conflicting_master_ids"]),
                    "business_name": lead.get("business_name") or "",
                    "run_id": lead.get("run_id") or "",
                    "search_id": lead.get("search_id") or "",
                    "detected_at": lead.get("scraped_at") or "",
                }
            )

        existing = master.get(master_id)
        if existing is None:
            new_records += 1
        else:
            updated_records += 1

        record = merge_lead_into_master(existing, lead, master_id)
        master[master_id] = record

        # A newly-discovered identifier (e.g. a website found on a later
        # scrape of a name+address-only business) must attach to this same
        # master_id going forward, not mint a new record.
        for key in match_keys(lead):
            identity_index[key] = master_id

        scraped_at = lead.get("scraped_at") or ""
        run_id = lead.get("run_id") or ""
        search_id = lead.get("search_id") or ""
        history_key = (master_id, run_id, search_id)
        if history_key not in history_keys:
            history_keys.add(history_key)
            history_rows.append(
                {
                    "master_id": master_id,
                    "run_id": run_id,
                    "search_id": search_id,
                    "search_keyword": lead.get("search_keyword") or "",
                    "search_location": lead.get("search_location") or "",
                    "source": lead.get("source") or "",
                    "first_discovered_at": scraped_at,
                }
            )
            new_history_events += 1

        sources_seen.setdefault(master_id, set()).add(lead.get("source") or "")
        searches_seen.setdefault(master_id, set()).add(search_id)

    for master_id, record in master.items():
        record["source_count"] = len({s for s in sources_seen.get(master_id, set()) if s})
        record["search_count"] = len({s for s in searches_seen.get(master_id, set()) if s})

    stats = {
        "leads_processed": len(leads),
        "new_master_records": new_records,
        "updated_master_records": updated_records,
        "total_master_records": len(master),
        "new_history_events": new_history_events,
        "total_history_events": len(history_rows),
        "new_identity_conflicts": len(conflicts) - len(existing_conflicts or []),
        "total_identity_conflicts": len(conflicts),
        "identity_conflicts": conflicts,
    }
    return list(master.values()), history_rows, stats


def write_master(records: list[dict], master_dir: str) -> tuple[str, str]:
    records = sorted(records, key=lambda r: r["master_id"])
    csv_path = os.path.join(master_dir, "master.csv")
    json_path = os.path.join(master_dir, "master.json")

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=MASTER_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for record in records:
            writer.writerow({k: ("" if record.get(k) is None else record.get(k)) for k in MASTER_FIELDS})

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump([{k: r.get(k) for k in MASTER_FIELDS} for r in records], f, indent=2, ensure_ascii=False)

    return csv_path, json_path


def write_history(rows: list[dict], master_dir: str) -> str:
    rows = sorted(rows, key=lambda r: (r["master_id"], r["run_id"], r["search_id"]))
    history_path = os.path.join(master_dir, "discovery_history.csv")
    with open(history_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=HISTORY_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in HISTORY_FIELDS})
    return history_path


def load_conflicts(conflicts_csv_path: str) -> list[dict]:
    if not os.path.exists(conflicts_csv_path):
        return []
    with open(conflicts_csv_path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_conflicts(rows: list[dict], master_dir: str) -> str:
    rows = sorted(rows, key=lambda r: (r["master_id"], r["run_id"], r["search_id"]))
    conflicts_path = os.path.join(master_dir, "identity_conflicts.csv")
    with open(conflicts_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CONFLICT_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in CONFLICT_FIELDS})
    return conflicts_path


def run(clean_json_paths: list[str], master_dir: str) -> dict:
    os.makedirs(master_dir, exist_ok=True)
    leads = load_clean_leads(clean_json_paths)

    master_json_path = os.path.join(master_dir, "master.json")
    history_csv_path = os.path.join(master_dir, "discovery_history.csv")
    conflicts_csv_path = os.path.join(master_dir, "identity_conflicts.csv")

    existing_master = load_master(master_json_path)
    existing_history_rows = load_history_rows(history_csv_path)
    existing_history_keys = load_history_keys(history_csv_path)
    existing_conflicts = load_conflicts(conflicts_csv_path)

    records, history_rows, stats = update_master(
        leads, existing_master, existing_history_keys, existing_history_rows, existing_conflicts
    )

    master_csv, master_json = write_master(records, master_dir)
    history_csv = write_history(history_rows, master_dir)
    conflicts_csv = write_conflicts(stats.pop("identity_conflicts"), master_dir)

    stats["artifacts"] = {
        "master_csv": master_csv,
        "master_json": master_json,
        "discovery_history_csv": history_csv,
        "identity_conflicts_csv": conflicts_csv,
    }
    return stats


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--clean-json",
        action="append",
        required=True,
        help="Path or glob to a clean *.json file from normalize.py. May be repeated.",
    )
    parser.add_argument("--master-dir", default="data/master", help="Directory for the master store (default: data/master)")
    args = parser.parse_args(argv)

    paths: list[str] = []
    for pattern in args.clean_json:
        matched = sorted(glob.glob(pattern))
        paths.extend(matched if matched else [pattern])

    stats = run(paths, args.master_dir)
    print(json.dumps(stats, indent=2))
    return stats


if __name__ == "__main__":
    main(sys.argv[1:])
