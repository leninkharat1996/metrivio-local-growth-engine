import json
import os
import tempfile
import unittest

from scripts.production_pipeline.build_summary import build_summary


class BuildSummaryTests(unittest.TestCase):
    def test_build_summary_reads_stage_stats_without_recomputing_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            master_path = os.path.join(tmp, "master.json")
            with open(master_path, "w") as f:
                json.dump([{"master_id": "a"}, {"master_id": "b"}], f)

            prelim_path = os.path.join(tmp, "icp_stats.json")
            with open(prelim_path, "w") as f:
                json.dump({"status_counts": {"qualified": 1, "review": 1, "excluded": 0}}, f)

            enrichment_path = os.path.join(tmp, "enrichment_stats.json")
            with open(enrichment_path, "w") as f:
                json.dump(
                    {
                        "records_with_websites": 2,
                        "successful": 1,
                        "partial": 1,
                        "blocked": 0,
                        "failed": 0,
                    },
                    f,
                )

            final_icp_path = os.path.join(tmp, "final_icp_stats.json")
            with open(final_icp_path, "w") as f:
                json.dump(
                    {
                        "status_counts": {"qualified": 1, "review": 0, "excluded": 1},
                        "tier_counts": {"A+": 0, "A": 1, "B": 0, "C": 0, "D": 1},
                    },
                    f,
                )

            summary = build_summary(
                run_id="123",
                commit_sha="abcdef",
                started_at="2026-01-01T00:00:00Z",
                completed_at="2026-01-01T00:05:00Z",
                searches_requested=1,
                master_json_path=master_path,
                preliminary_stats_path=prelim_path,
                enrichment_stats_path=enrichment_path,
                final_icp_stats_path=final_icp_path,
                pipeline_status="success",
            )

        self.assertEqual(summary["master_total"], 2)
        self.assertEqual(summary["preliminary_qualified"], 1)
        self.assertEqual(summary["preliminary_review"], 1)
        self.assertEqual(summary["websites_attempted"], 2)
        self.assertEqual(summary["websites_successful"], 1)
        self.assertEqual(summary["final_qualified"], 1)
        self.assertEqual(summary["final_excluded"], 1)
        self.assertEqual(summary["final_tier_A"], 1)
        self.assertEqual(summary["final_tier_D"], 1)
        self.assertEqual(summary["pipeline_status"], "success")
        self.assertEqual(summary["run_id"], "123")

    def test_missing_stats_files_default_to_zero_not_fabricated(self):
        summary = build_summary(
            run_id="1",
            commit_sha="sha",
            started_at="t0",
            completed_at="t1",
            searches_requested=0,
            master_json_path="/nonexistent/master.json",
            preliminary_stats_path=None,
            enrichment_stats_path=None,
            final_icp_stats_path=None,
            pipeline_status="failed",
        )
        self.assertEqual(summary["master_total"], 0)
        self.assertEqual(summary["final_qualified"], 0)
        self.assertEqual(summary["pipeline_status"], "failed")


if __name__ == "__main__":
    unittest.main()
