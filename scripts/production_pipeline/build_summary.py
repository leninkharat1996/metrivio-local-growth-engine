#!/usr/bin/env python3
"""
Step 11A -- Production Pipeline machine-readable run summary.

Builds pipeline_summary.json from the stats each existing pipeline stage
ALREADY prints (the JSON each stage's own main() writes to stdout), plus a
handful of run-envelope facts (run_id, commit_sha, timestamps, how many
searches were requested, overall pipeline_status).

This script performs NO scoring, tiering, or qualification logic of its
own -- it only reads numbers back out of the stats already produced by
scripts/icp/qualify.py, scripts/enrichment/enrich_websites.py, and
scripts/final_icp/qualify_final.py, so the existing rules/scoring are
never re-implemented or second-guessed here.

Usage:
    python3 build_summary.py \
        --run-id <id> --commit-sha <sha> \
        --started-at <rfc3339> --completed-at <rfc3339> \
        --searches-requested <n> \
        --master-json data/master/master.json \
        --preliminary-stats icp_stats.json \
        --enrichment-stats enrichment_stats.json \
        --final-icp-stats final_icp_stats.json \
        --pipeline-status success \
        --out pipeline_summary.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys


def _load_json(path: str | None) -> dict:
    if not path or not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _master_total(master_json_path: str | None) -> int:
    if not master_json_path or not os.path.exists(master_json_path):
        return 0
    with open(master_json_path, encoding="utf-8") as f:
        data = json.load(f)
    records = data if isinstance(data, list) else data.get("results", [])
    return len(records)


def build_summary(
    run_id: str,
    commit_sha: str,
    started_at: str,
    completed_at: str,
    searches_requested: int,
    master_json_path: str | None,
    preliminary_stats_path: str | None,
    enrichment_stats_path: str | None,
    final_icp_stats_path: str | None,
    pipeline_status: str,
) -> dict:
    preliminary = _load_json(preliminary_stats_path)
    enrichment = _load_json(enrichment_stats_path)
    final_icp = _load_json(final_icp_stats_path)

    prelim_status_counts = preliminary.get("status_counts", {})
    final_status_counts = final_icp.get("status_counts", {})
    final_tier_counts = final_icp.get("tier_counts", {})

    return {
        "run_id": run_id,
        "commit_sha": commit_sha,
        "started_at": started_at,
        "completed_at": completed_at,
        "searches_requested": searches_requested,
        "master_total": _master_total(master_json_path),
        "preliminary_qualified": prelim_status_counts.get("qualified", 0),
        "preliminary_review": prelim_status_counts.get("review", 0),
        "preliminary_excluded": prelim_status_counts.get("excluded", 0),
        "websites_attempted": enrichment.get("records_with_websites", 0),
        "websites_successful": enrichment.get("successful", 0),
        "websites_partial": enrichment.get("partial", 0),
        "websites_blocked": enrichment.get("blocked", 0),
        "websites_failed": enrichment.get("failed", 0),
        "final_qualified": final_status_counts.get("qualified", 0),
        "final_review": final_status_counts.get("review", 0),
        "final_excluded": final_status_counts.get("excluded", 0),
        "final_tier_A_plus": final_tier_counts.get("A+", 0),
        "final_tier_A": final_tier_counts.get("A", 0),
        "final_tier_B": final_tier_counts.get("B", 0),
        "final_tier_C": final_tier_counts.get("C", 0),
        "final_tier_D": final_tier_counts.get("D", 0),
        "pipeline_status": pipeline_status,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--commit-sha", required=True)
    parser.add_argument("--started-at", required=True)
    parser.add_argument("--completed-at", required=True)
    parser.add_argument("--searches-requested", type=int, required=True)
    parser.add_argument("--master-json", default="data/master/master.json")
    parser.add_argument("--preliminary-stats", default=None, help="Path to JSON stats printed by scripts/icp/qualify.py")
    parser.add_argument("--enrichment-stats", default=None, help="Path to JSON stats printed by scripts/enrichment/enrich_websites.py")
    parser.add_argument("--final-icp-stats", default=None, help="Path to JSON stats printed by scripts/final_icp/qualify_final.py")
    parser.add_argument("--pipeline-status", required=True, choices=["success", "failed"])
    parser.add_argument("--out", default="pipeline_summary.json")
    args = parser.parse_args(argv)

    summary = build_summary(
        args.run_id,
        args.commit_sha,
        args.started_at,
        args.completed_at,
        args.searches_requested,
        args.master_json,
        args.preliminary_stats,
        args.enrichment_stats,
        args.final_icp_stats,
        args.pipeline_status,
    )

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)

    print(json.dumps(summary, indent=2))
    return summary


if __name__ == "__main__":
    main(sys.argv[1:])
