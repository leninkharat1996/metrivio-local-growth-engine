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
Reuses normalize.dedup_key()'s priority order (domain > phone >
name+address) so identity is consistent with in-run dedup, then hashes it:

    master_id = sha256(":".join(dedup_key(lead))).hexdigest()[:16]

This makes master_id depend only on stable business identifiers, never on
run_id/search_id/scraped_at, so the same business reappearing in a later
run/search/city produces the same master_id.

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

from normalize import CANONICAL_FIELDS, dedup_key  # noqa: E402

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
) -> tuple[list[dict], list[dict], dict]:
    """Pure function: returns (master_records, history_rows, stats)."""
    master = dict(existing_master)
    history_rows = list(existing_history_rows)
    history_keys = set(existing_history_keys)

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
        master_id = compute_master_id(lead)
        existing = master.get(master_id)
        if existing is None:
            new_records += 1
        else:
            updated_records += 1

        record = merge_lead_into_master(existing, lead, master_id)
        master[master_id] = record

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


def run(clean_json_paths: list[str], master_dir: str) -> dict:
    os.makedirs(master_dir, exist_ok=True)
    leads = load_clean_leads(clean_json_paths)

    master_json_path = os.path.join(master_dir, "master.json")
    history_csv_path = os.path.join(master_dir, "discovery_history.csv")

    existing_master = load_master(master_json_path)
    existing_history_rows = load_history_rows(history_csv_path)
    existing_history_keys = load_history_keys(history_csv_path)

    records, history_rows, stats = update_master(
        leads, existing_master, existing_history_keys, existing_history_rows
    )

    master_csv, master_json = write_master(records, master_dir)
    history_csv = write_history(history_rows, master_dir)

    stats["artifacts"] = {
        "master_csv": master_csv,
        "master_json": master_json,
        "discovery_history_csv": history_csv,
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
