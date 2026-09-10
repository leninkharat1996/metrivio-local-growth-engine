#!/usr/bin/env python3
"""
Step 11B -- Final ICP record-level validation/observability report.

The production pipeline (scripts/final_icp/qualify_final.py) already
computes, per business, every field a human needs to audit a qualification
decision: status, tier, score, confidence, the exact evidence categories
that drove it, the exclusion/rescue reason (if any), and the preliminary
(Google Maps name/category) status it started from. That is all written to
data/final_icp/final_icp_qualified.json -- but the only way to see it today
is to download the workflow's uploaded GitHub Actions artifact, which is
not reachable from this environment (Azure Blob artifact storage is not
reachable through the outbound proxy).

This script makes NO qualification decisions and computes NO new evidence.
It only:
  1. Reads data/final_icp/final_icp_qualified.json (read-only) -- the
     existing Final ICP decision for every record, verbatim.
  2. Left-joins in existing provenance fields already carried on the
     matching data/master/master.json record (read-only): first_seen_at,
     last_seen_at, source_count, search_count, and the master record's
     most-recently-attached run_id/search_id.
  3. Derives a purely descriptive "new / updated / historical" label for
     the CURRENT run from those existing fields (a record whose master
     row's run_id matches the run_id passed via --current-run-id is
     "touched this run"; among those, first_seen_at == last_seen_at means
     it was first discovered this run ("new"), otherwise it already
     existed and was refreshed ("updated"); anything else is
     "historical" -- present in the cumulative master store from an
     earlier run). This label is NOT written back anywhere and does not
     affect qualification in any way.
  4. Prints a compact, human-readable "===== FINAL ICP VALIDATION
     RECORDS =====" section to stdout (this IS the primary deliverable --
     it lands directly in GitHub Actions logs, no artifact download
     needed) and writes the same data as
     data/final_icp/final_icp_validation.json/.csv for anyone who *can*
     reach artifacts.

Never prints: raw HTML, environment variables, secrets, tokens, or
cookies -- only the existing, already-extracted business/evidence fields
enumerated in VALIDATION_FIELDS below.

Usage:
    python3 scripts/production_pipeline/build_validation_report.py \
        --final-icp-json data/final_icp/final_icp_qualified.json \
        --master-json data/master/master.json \
        --current-run-id run_12345_1 \
        --out-dir data/final_icp
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys

VALIDATION_FIELDS = [
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
    "preliminary_icp_confidence",
    "preliminary_icp_score",
    "hvac_direct_signals",
    "commercial_vertical_signals",
    "commercial_service_signals",
    "residential_signals",
    "incidental_signals",
    "qualification_reasons",
    "exclusion_reasons",
    "evidence_source",
    "website_status",
    "first_seen_at",
    "last_seen_at",
    "source_count",
    "search_count",
    "master_run_id",
    "master_search_id",
    "run_status",
]

LIST_OR_DICT_FIELDS = (
    "hvac_direct_signals",
    "commercial_vertical_signals",
    "commercial_service_signals",
    "residential_signals",
    "incidental_signals",
    "qualification_reasons",
    "exclusion_reasons",
)

RUN_STATUS_NEW = "new"
RUN_STATUS_UPDATED = "updated"
RUN_STATUS_HISTORICAL = "historical"
RUN_STATUS_UNKNOWN = "unknown"

PROVENANCE_FIELDS = (
    "first_seen_at",
    "last_seen_at",
    "source_count",
    "search_count",
    "master_run_id",
    "master_search_id",
    "run_status",
)


# ---------------------------------------------------------------------------
# Loading (read-only -- same pattern as scripts/final_icp/qualify_final.py)
# ---------------------------------------------------------------------------

def load_json_list(path: str) -> list[dict]:
    if not path or not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, list) else data.get("results", [])


def index_master(path: str) -> dict[str, dict]:
    return {r.get("master_id"): r for r in load_json_list(path) if r.get("master_id")}


# ---------------------------------------------------------------------------
# Provenance (existing master-store fields only -- no new evidence)
# ---------------------------------------------------------------------------

def derive_run_status(master_record: dict, current_run_id: str | None) -> str:
    if not master_record:
        return RUN_STATUS_UNKNOWN
    if not current_run_id:
        return RUN_STATUS_UNKNOWN
    if master_record.get("run_id") != current_run_id:
        return RUN_STATUS_HISTORICAL
    first_seen = master_record.get("first_seen_at")
    last_seen = master_record.get("last_seen_at")
    if first_seen and first_seen == last_seen:
        return RUN_STATUS_NEW
    return RUN_STATUS_UPDATED


def build_provenance(master_record: dict | None, current_run_id: str | None) -> dict:
    master_record = master_record or {}
    return {
        "first_seen_at": master_record.get("first_seen_at"),
        "last_seen_at": master_record.get("last_seen_at"),
        "source_count": master_record.get("source_count"),
        "search_count": master_record.get("search_count"),
        "master_run_id": master_record.get("run_id"),
        "master_search_id": master_record.get("search_id"),
        "run_status": derive_run_status(master_record, current_run_id),
    }


# ---------------------------------------------------------------------------
# Per-record validation row (existing Final ICP fields, verbatim, plus
# provenance -- no new evidence, no re-scoring, no re-classification)
# ---------------------------------------------------------------------------

def build_validation_record(final_icp_record: dict, master_record: dict | None, current_run_id: str | None) -> dict:
    row = {field: final_icp_record.get(field) for field in VALIDATION_FIELDS if field not in PROVENANCE_FIELDS}
    row.update(build_provenance(master_record, current_run_id))
    return row


def build_validation_records(
    final_icp_records: list[dict], master_index: dict[str, dict], current_run_id: str | None
) -> list[dict]:
    rows = [
        build_validation_record(r, master_index.get(r.get("master_id")), current_run_id)
        for r in final_icp_records
    ]
    rows.sort(key=lambda r: (r.get("master_id") or ""))
    return rows


# ---------------------------------------------------------------------------
# GitHub Actions log output (the mandatory deliverable)
# ---------------------------------------------------------------------------

def _fmt_list(value) -> str:
    if not value:
        return "none"
    if isinstance(value, dict):
        present = value.get("present")
        keywords = value.get("keywords") or []
        return f"present={bool(present)} keywords={keywords}"
    if isinstance(value, list) and value and isinstance(value[0], dict):
        return str([f"{i.get('category')}:{i.get('keyword')}" for i in value])
    return str(value)


def format_validation_record(row: dict) -> str:
    status = (row.get("final_icp_status") or "unknown").upper()
    name = row.get("business_name") or "(no business_name on file)"
    city = row.get("city") or "?"
    state = row.get("state") or "?"
    tier = row.get("final_icp_tier") or "?"
    score = row.get("final_icp_score")
    confidence = row.get("final_icp_confidence") or "?"

    lines = [
        f"[{status}] {name} | {city}, {state} | Tier {tier} | Score {score} | Confidence {confidence}",
        f"  master_id={row.get('master_id')} website={row.get('website') or '(none)'}",
        f"  preliminary: status={row.get('preliminary_icp_status')} "
        f"confidence={row.get('preliminary_icp_confidence')} score={row.get('preliminary_icp_score')}",
        f"  hvac_direct={_fmt_list(row.get('hvac_direct_signals'))} "
        f"commercial_vertical={_fmt_list(row.get('commercial_vertical_signals'))} "
        f"service={_fmt_list(row.get('commercial_service_signals'))}",
        f"  residential={_fmt_list(row.get('residential_signals'))} "
        f"incidental={_fmt_list(row.get('incidental_signals'))}",
        f"  evidence_source={row.get('evidence_source')} website_status={row.get('website_status')}",
    ]
    if row.get("qualification_reasons"):
        lines.append(f"  qualification_reasons={row.get('qualification_reasons')}")
    if row.get("exclusion_reasons"):
        lines.append(f"  exclusion_reasons/rescue={row.get('exclusion_reasons')}")
    lines.append(
        f"  provenance: first_seen={row.get('first_seen_at')} last_seen={row.get('last_seen_at')} "
        f"source_count={row.get('source_count')} search_count={row.get('search_count')} "
        f"run_status={row.get('run_status')}"
    )
    return "\n".join(lines)


def print_validation_log(rows: list[dict], out=None) -> None:
    out = out or sys.stdout
    print("===== FINAL ICP VALIDATION RECORDS =====", file=out)
    if not rows:
        print("(no records evaluated by Final ICP -- nothing to validate)", file=out)
    else:
        for row in rows:
            print(format_validation_record(row), file=out)
            print("", file=out)
    print(f"===== END FINAL ICP VALIDATION RECORDS ({len(rows)} record(s)) =====", file=out)


# ---------------------------------------------------------------------------
# Output artifacts
# ---------------------------------------------------------------------------

def write_outputs(rows: list[dict], out_dir: str) -> tuple[str, str]:
    os.makedirs(out_dir, exist_ok=True)
    json_path = os.path.join(out_dir, "final_icp_validation.json")
    csv_path = os.path.join(out_dir, "final_icp_validation.csv")

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(rows, f, indent=2, ensure_ascii=False, sort_keys=False)

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=VALIDATION_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            flat = dict(row)
            for field in LIST_OR_DICT_FIELDS:
                default = {} if field == "residential_signals" else []
                flat[field] = json.dumps(row.get(field) if row.get(field) is not None else default, ensure_ascii=False)
            writer.writerow({k: ("" if flat.get(k) is None else flat.get(k)) for k in VALIDATION_FIELDS})

    return json_path, csv_path


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def run(
    final_icp_json_path: str,
    master_json_path: str,
    current_run_id: str | None,
    out_dir: str,
    log_out=None,
) -> dict:
    final_icp_records = load_json_list(final_icp_json_path)
    master_index = index_master(master_json_path)

    rows = build_validation_records(final_icp_records, master_index, current_run_id)

    print_validation_log(rows, out=log_out)

    json_path, csv_path = write_outputs(rows, out_dir)

    run_status_counts: dict[str, int] = {}
    for row in rows:
        run_status_counts[row["run_status"]] = run_status_counts.get(row["run_status"], 0) + 1

    return {
        "records_validated": len(rows),
        "run_status_counts": run_status_counts,
        "artifacts": {"validation_json": json_path, "validation_csv": csv_path},
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--final-icp-json", default="data/final_icp/final_icp_qualified.json", help="Path to the existing Final ICP output JSON (read-only)")
    parser.add_argument("--master-json", default="data/master/master.json", help="Path to the master prospect store JSON (read-only)")
    parser.add_argument(
        "--current-run-id",
        default=None,
        help="The current collection run's run_id, in the same format leads carry "
        "(e.g. run_<github.run_id>_<github.run_attempt>). Used only to label each "
        "record new/updated/historical for this run; never changes master-store "
        "behavior.",
    )
    parser.add_argument("--out-dir", default="data/final_icp", help="Directory to write the validation artifact (default: data/final_icp)")
    args = parser.parse_args(argv)

    stats = run(args.final_icp_json, args.master_json, args.current_run_id, args.out_dir)
    print(json.dumps(stats, indent=2))
    return stats


if __name__ == "__main__":
    main(sys.argv[1:])
