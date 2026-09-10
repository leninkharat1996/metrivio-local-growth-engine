#!/usr/bin/env python3
"""
Step 11A -- Final ICP record-level validation report.

Pure observability/reporting addition: it reads the already-generated
Final ICP output (data/final_icp/final_icp_qualified.json, produced by
scripts/final_icp/qualify_final.py) and the persistent master store
(data/master/master.json, produced by scripts/google_maps_scraper/
update_master.py) READ-ONLY, and prints one compact, human-readable line
block per Final ICP record to stdout -- so a reviewer can validate
individual leads directly from the GitHub Actions log even when the
workflow's artifact cannot be downloaded.

This module makes NO qualification/scoring/exclusion decisions of its
own. Every status, tier, score, confidence, evidence category, exclusion
reason, and website status value it prints is read verbatim from the
existing Final ICP record (scripts/final_icp/qualify_final.py's
OUTPUT_FIELDS) or from the existing master record's provenance fields
(master_id, first_seen_at, last_seen_at, source_count, search_count).
It never re-derives or second-guesses those values.

"newly_discovered_this_run" is the one inference this module makes, and
it is derived purely from data the master store already carries: a
record is considered newly discovered in the current run when its
existing first_seen_at timestamp falls at or after the run's own
started_at timestamp (passed in via --run-started-at). When
--run-started-at is omitted, newly_discovered_this_run is left as None
(unknown) rather than guessed.

Usage:
    python3 validation_report.py \
        --final-icp-json data/final_icp/final_icp_qualified.json \
        --master-json data/master/master.json \
        --run-started-at 2026-01-01T00:00:00Z \
        --out-dir data/final_icp \
        --json-out final_icp_validation.json \
        --csv-out final_icp_validation.csv
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys

SECTION_HEADER = "===== FINAL ICP VALIDATION RECORDS ====="
SECTION_FOOTER = "===== END FINAL ICP VALIDATION RECORDS ====="

# Provenance fields read verbatim from the existing master record. Never
# invented, never recomputed -- exactly what update_master.py already
# stores per business.
PROVENANCE_FIELDS = ["master_id", "first_seen_at", "last_seen_at", "source_count", "search_count"]

OUTPUT_FIELDS = [
    "master_id",
    "business_name",
    "city",
    "state",
    "website",
    "final_icp_status",
    "final_icp_tier",
    "final_icp_score",
    "final_icp_confidence",
    "preliminary_icp_status",
    "preliminary_icp_score",
    "preliminary_icp_confidence",
    "hvac_direct_signals",
    "commercial_vertical_signals",
    "commercial_service_signals",
    "residential_present",
    "residential_keywords",
    "qualification_reasons",
    "exclusion_reasons",
    "website_status",
    "evidence_source",
    "first_seen_at",
    "last_seen_at",
    "source_count",
    "search_count",
    "newly_discovered_this_run",
]


def load_json_list(path: str | None) -> list[dict]:
    if not path or not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, list) else data.get("results", [])


def index_master(path: str | None) -> dict[str, dict]:
    return {r.get("master_id"): r for r in load_json_list(path) if r.get("master_id")}


def _newly_discovered(first_seen_at: str | None, run_started_at: str | None) -> bool | None:
    if not first_seen_at or not run_started_at:
        return None
    # RFC3339 UTC ('...Z') timestamps sort lexicographically the same as
    # chronologically, matching how first_seen_at/last_seen_at are already
    # compared elsewhere in this codebase (update_master.py's min()/max()).
    return first_seen_at >= run_started_at


def build_validation_record(final_record: dict, master_record: dict | None, run_started_at: str | None) -> dict:
    residential = final_record.get("residential_signals") or {}
    first_seen_at = (master_record or {}).get("first_seen_at")

    return {
        "master_id": final_record.get("master_id"),
        "business_name": final_record.get("business_name"),
        "city": final_record.get("city"),
        "state": final_record.get("state"),
        "website": final_record.get("website"),
        "final_icp_status": final_record.get("final_icp_status"),
        "final_icp_tier": final_record.get("final_icp_tier"),
        "final_icp_score": final_record.get("final_icp_score"),
        "final_icp_confidence": final_record.get("final_icp_confidence"),
        "preliminary_icp_status": final_record.get("preliminary_icp_status"),
        "preliminary_icp_score": final_record.get("preliminary_icp_score"),
        "preliminary_icp_confidence": final_record.get("preliminary_icp_confidence"),
        "hvac_direct_signals": final_record.get("hvac_direct_signals") or [],
        "commercial_vertical_signals": final_record.get("commercial_vertical_signals") or [],
        "commercial_service_signals": final_record.get("commercial_service_signals") or [],
        "residential_present": bool(residential.get("present")),
        "residential_keywords": residential.get("keywords") or [],
        "qualification_reasons": final_record.get("qualification_reasons") or [],
        "exclusion_reasons": final_record.get("exclusion_reasons") or [],
        "website_status": final_record.get("website_status"),
        "evidence_source": final_record.get("evidence_source"),
        "first_seen_at": first_seen_at,
        "last_seen_at": (master_record or {}).get("last_seen_at"),
        "source_count": (master_record or {}).get("source_count"),
        "search_count": (master_record or {}).get("search_count"),
        "newly_discovered_this_run": _newly_discovered(first_seen_at, run_started_at),
    }


def build_validation_records(
    final_records: list[dict], master_by_id: dict[str, dict], run_started_at: str | None = None
) -> list[dict]:
    records = [
        build_validation_record(final_record, master_by_id.get(final_record.get("master_id")), run_started_at)
        for final_record in final_records
    ]
    records.sort(key=lambda r: (r.get("master_id") or ""))
    return records


def sanitize_for_log(value):
    """Make an externally sourced value safe to print as part of a GitHub
    Actions log line.

    GitHub Actions interprets any stdout line that *starts* with
    ``::name::`` (optionally after leading whitespace) as a workflow
    command (``::error::``, ``::add-mask::``, ``::stop-commands::``, ...).
    A record field (business name, website, evidence text, etc.) is
    external, untrusted data and must never be able to (a) introduce a new
    line via an embedded newline, or (b) itself begin with a ``::``
    command prefix. This is display-only sanitization for the printed
    report section -- it must never be applied to the JSON/CSV output,
    which preserves the original field values verbatim.
    """
    if value is None:
        return value
    text = str(value)
    # (a) collapse embedded newlines so a value can never spawn extra log
    # lines of its own.
    text = text.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
    # (b) neutralize a leading "::" workflow-command prefix (allowing for
    # leading whitespace) without mangling "::" that appears mid-string.
    lstripped = text.lstrip()
    if lstripped.startswith("::"):
        idx = len(text) - len(lstripped)
        text = text[:idx] + ": " + lstripped[1:]
    return text


def _location(record: dict) -> str:
    city = sanitize_for_log(record.get("city")) or ""
    state = sanitize_for_log(record.get("state")) or ""
    if city and state:
        return f"{city}, {state}"
    return city or state or "location unknown"


def _join(values) -> str:
    values = [sanitize_for_log(v) for v in (values or [])]
    return ", ".join(values) if values else "none"


def format_record_lines(record: dict) -> list[str]:
    status = sanitize_for_log(record.get("final_icp_status") or "unknown").upper()
    name = sanitize_for_log(record.get("business_name")) or "(unnamed business)"
    tier = sanitize_for_log(record.get("final_icp_tier")) or "-"
    score = record.get("final_icp_score")
    confidence = sanitize_for_log(record.get("final_icp_confidence")) or "-"
    website = sanitize_for_log(record.get("website")) or "none"
    website_status = sanitize_for_log(record.get("website_status")) or "n/a"
    evidence_source = sanitize_for_log(record.get("evidence_source")) or "n/a"
    preliminary_status = sanitize_for_log(record.get("preliminary_icp_status")) or "n/a"
    preliminary_confidence = sanitize_for_log(record.get("preliminary_icp_confidence")) or "n/a"

    lines = [
        f"[{status}] {name} | {_location(record)} | Tier {tier} | Score {score} | Confidence {confidence}",
        f"  Website: {website} | Website status: {website_status} "
        f"| Evidence source: {evidence_source}",
        f"  Preliminary: {preliminary_status} "
        f"| Score {record.get('preliminary_icp_score')} | Confidence {preliminary_confidence}",
        f"  HVAC-direct: {_join(record.get('hvac_direct_signals'))} "
        f"| Vertical: {_join(record.get('commercial_vertical_signals'))} "
        f"| Service: {_join(record.get('commercial_service_signals'))}",
        f"  Residential: {'present (' + _join(record.get('residential_keywords')) + ')' if record.get('residential_present') else 'none'}",
    ]

    reasons = [
        sanitize_for_log(r)
        for r in (record.get("exclusion_reasons") or []) + (record.get("qualification_reasons") or [])
    ]
    lines.append(f"  Reason: {'; '.join(reasons) if reasons else 'n/a'}")

    newly = record.get("newly_discovered_this_run")
    newly_text = "unknown" if newly is None else ("yes" if newly else "no")
    master_id = sanitize_for_log(record.get("master_id")) or "n/a"
    first_seen = sanitize_for_log(record.get("first_seen_at")) or "n/a"
    last_seen = sanitize_for_log(record.get("last_seen_at")) or "n/a"
    lines.append(
        f"  Provenance: master_id={master_id} "
        f"| first_seen={first_seen} | last_seen={last_seen} "
        f"| source_count={record.get('source_count')} | search_count={record.get('search_count')} "
        f"| newly_discovered_this_run={newly_text}"
    )
    return lines


def render_section(records: list[dict]) -> str:
    lines = [SECTION_HEADER]
    if not records:
        lines.append("(no Final ICP records to report)")
    else:
        for record in records:
            lines.extend(format_record_lines(record))
            lines.append("")
    lines.append(SECTION_FOOTER)
    return "\n".join(lines)


def write_outputs(records: list[dict], out_dir: str, json_name: str, csv_name: str) -> tuple[str, str]:
    os.makedirs(out_dir, exist_ok=True)
    json_path = os.path.join(out_dir, json_name)
    csv_path = os.path.join(out_dir, csv_name)

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(records, f, indent=2, ensure_ascii=False)

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in records:
            flat = dict(row)
            for list_field in (
                "hvac_direct_signals", "commercial_vertical_signals", "commercial_service_signals",
                "residential_keywords", "qualification_reasons", "exclusion_reasons",
            ):
                flat[list_field] = " | ".join(row.get(list_field) or [])
            writer.writerow({k: ("" if flat.get(k) is None else flat.get(k)) for k in OUTPUT_FIELDS})

    return json_path, csv_path


def run(
    final_icp_json_path: str,
    master_json_path: str,
    out_dir: str,
    json_name: str = "final_icp_validation.json",
    csv_name: str = "final_icp_validation.csv",
    run_started_at: str | None = None,
) -> dict:
    final_records = load_json_list(final_icp_json_path)
    master_by_id = index_master(master_json_path)

    records = build_validation_records(final_records, master_by_id, run_started_at)
    json_path, csv_path = write_outputs(records, out_dir, json_name, csv_name)

    return {
        "records_reported": len(records),
        "section": render_section(records),
        "artifacts": {"validation_json": json_path, "validation_csv": csv_path},
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--final-icp-json", default="data/final_icp/final_icp_qualified.json")
    parser.add_argument("--master-json", default="data/master/master.json")
    parser.add_argument("--out-dir", default="data/final_icp")
    parser.add_argument("--json-out", default="final_icp_validation.json")
    parser.add_argument("--csv-out", default="final_icp_validation.csv")
    parser.add_argument(
        "--run-started-at",
        default=None,
        help="RFC3339 timestamp the current pipeline run started at, used only to flag "
        "newly_discovered_this_run from the existing first_seen_at provenance field.",
    )
    args = parser.parse_args(argv)

    result = run(
        args.final_icp_json,
        args.master_json,
        args.out_dir,
        args.json_out,
        args.csv_out,
        args.run_started_at,
    )

    print(result["section"])
    return result


if __name__ == "__main__":
    main(sys.argv[1:])
