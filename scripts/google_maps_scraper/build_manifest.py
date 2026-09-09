#!/usr/bin/env python3
"""
Build a machine-readable manifest for a Google Maps collection run.

Combines the (already validated) search plan with the per-search result
records emitted while executing it, and reports overall run status. Pure/
deterministic: no network calls, no scraping here.

Result records (one per line of a --results-file, JSON Lines) look like:

    {
      "search_id": "dallas-tx_commercial-hvac-contractors",
      "status": "success",
      "stats": { ...normalize.py stats block... },
      "artifacts": {"raw_csv": "...", "clean_csv": "...", "clean_json": "..."}
    }

or, on failure:

    {"search_id": "...", "status": "failed", "error": "..."}

Usage:
    python3 build_manifest.py --run-id RUN123 --plan-file plan.json \
        --results-file results.jsonl --out manifest.json
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone


def load_jsonl(path: str) -> list[dict]:
    records = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    records.append(json.loads(line))
    except FileNotFoundError:
        pass
    return records


def build_manifest(run_id: str, plan_entries: list[dict], results: list[dict], timestamp: str | None = None) -> dict:
    timestamp = timestamp or datetime.now(timezone.utc).isoformat()
    results_by_id = {r["search_id"]: r for r in results if r.get("search_id")}

    requested = []
    successful = []
    failed = []
    invalid = []

    for entry in plan_entries:
        if not entry.get("valid"):
            invalid.append(
                {
                    "line_no": entry.get("line_no"),
                    "raw": entry.get("raw"),
                    "error": entry.get("error"),
                }
            )
            continue

        search_id = entry["search_id"]
        requested.append(
            {
                "search_id": search_id,
                "keyword": entry["keyword"],
                "location": entry["location"],
                "depth": entry["depth"],
            }
        )

        result = results_by_id.get(search_id)
        if result is None:
            failed.append(
                {
                    "search_id": search_id,
                    "keyword": entry["keyword"],
                    "location": entry["location"],
                    "error": "search did not produce a result record (not attempted or crashed silently)",
                }
            )
            continue

        if result.get("status") == "success":
            stats = result.get("stats", {})
            successful.append(
                {
                    "search_id": search_id,
                    "keyword": entry["keyword"],
                    "location": entry["location"],
                    "raw_record_count": stats.get("raw_records"),
                    "clean_record_count": stats.get("clean_records"),
                    "malformed_count": stats.get("malformed_skipped"),
                    "duplicate_count": stats.get("duplicates_merged"),
                    "partial_count": stats.get("partial_records_missing_phone_and_website"),
                    "artifacts": result.get("artifacts", {}),
                }
            )
        else:
            failed.append(
                {
                    "search_id": search_id,
                    "keyword": entry["keyword"],
                    "location": entry["location"],
                    "error": result.get("error", "unknown error"),
                }
            )

    if not requested:
        overall_status = "failed"
    elif invalid or failed:
        overall_status = "partial_failure" if successful else "failed"
    else:
        overall_status = "success"

    return {
        "run_id": run_id,
        "generated_at": timestamp,
        "overall_status": overall_status,
        "requested_searches": requested,
        "invalid_search_lines": invalid,
        "successful_searches": successful,
        "failed_searches": failed,
        "totals": {
            "requested": len(requested) + len(invalid),
            "valid": len(requested),
            "invalid": len(invalid),
            "successful": len(successful),
            "failed": len(failed),
        },
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--plan-file", required=True, help="JSON array from search_plan.py")
    parser.add_argument("--results-file", required=True, help="JSON Lines of per-search results")
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)

    with open(args.plan_file, encoding="utf-8") as f:
        plan_entries = json.load(f)
    results = load_jsonl(args.results_file)

    manifest = build_manifest(args.run_id, plan_entries, results)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")
    print(json.dumps(manifest, indent=2))
    return 0 if manifest["overall_status"] == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
