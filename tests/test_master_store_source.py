import json
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "scripts", "google_maps_scraper"),
)

from master_store_source import (  # noqa: E402
    SOURCE_ARTIFACT,
    SOURCE_CACHE,
    SOURCE_EMPTY,
    find_latest_master_store_artifact,
    select_master_store_source,
)

SCRIPT_PATH = os.path.join(
    os.path.dirname(__file__), "..", "scripts", "google_maps_scraper", "master_store_source.py"
)


class SelectMasterStoreSourceTests(unittest.TestCase):
    def test_cache_restored_wins_regardless_of_artifact_availability(self):
        self.assertEqual(select_master_store_source(True, True), SOURCE_CACHE)
        self.assertEqual(select_master_store_source(True, False), SOURCE_CACHE)

    def test_falls_back_to_artifact_when_cache_missed(self):
        self.assertEqual(select_master_store_source(False, True), SOURCE_ARTIFACT)

    def test_empty_when_neither_cache_nor_artifact_available(self):
        self.assertEqual(select_master_store_source(False, False), SOURCE_EMPTY)


class FindLatestMasterStoreArtifactTests(unittest.TestCase):
    def test_no_runs_returns_none(self):
        self.assertIsNone(find_latest_master_store_artifact([]))

    def test_no_matching_artifact_in_any_run_returns_none(self):
        runs = [
            {"id": 3, "artifacts": [{"name": "google-maps-clean-csv-run_3"}]},
            {"id": 2, "artifacts": [{"name": "google-maps-manifest-run_2"}]},
        ]
        self.assertIsNone(find_latest_master_store_artifact(runs))

    def test_picks_most_recent_run_with_a_master_store_artifact(self):
        # Runs are given newest-first, as the GitHub API returns them; the
        # most recent run (id=5) has no master-store artifact, so run 4
        # (the next-most-recent) should be selected, not run 2 even though
        # it also has one.
        runs = [
            {"id": 5, "artifacts": [{"name": "google-maps-manifest-run_5"}]},
            {"id": 4, "artifacts": [{"name": "google-maps-master-store-run_4"}]},
            {"id": 2, "artifacts": [{"name": "google-maps-master-store-run_2"}]},
        ]
        self.assertEqual(
            find_latest_master_store_artifact(runs),
            (4, "google-maps-master-store-run_4"),
        )

    def test_does_not_use_artifacts_from_unrelated_workflows(self):
        # A run's artifact list here only ever contains artifacts uploaded
        # by that run; a run with only unrelated artifact names must be
        # skipped in favor of a run that actually has a master-store one.
        runs = [
            {"id": 9, "artifacts": [{"name": "some-other-workflow-artifact"}]},
            {"id": 8, "artifacts": [{"name": "google-maps-master-store-run_8"}]},
        ]
        self.assertEqual(
            find_latest_master_store_artifact(runs),
            (8, "google-maps-master-store-run_8"),
        )

    def test_ignores_runs_with_no_artifacts_key(self):
        runs = [
            {"id": 7},
            {"id": 6, "artifacts": [{"name": "google-maps-master-store-run_6"}]},
        ]
        self.assertEqual(
            find_latest_master_store_artifact(runs),
            (6, "google-maps-master-store-run_6"),
        )


class DeterministicDecisionFlowTests(unittest.TestCase):
    """
    End-to-end (still pure/offline) exercises of the same decision flow the
    workflow step performs: check cache -> search runs -> select source.
    """

    def test_cache_hit_flow_never_consults_runs(self):
        cache_restored = True
        # In the real step this branch is never reached when cache_restored
        # is True, but if it were, the "no runs" case still isn't consulted
        # because select_master_store_source short-circuits on cache.
        source = select_master_store_source(cache_restored, artifact_found=False)
        self.assertEqual(source, SOURCE_CACHE)

    def test_artifact_fallback_flow_when_cache_misses(self):
        cache_restored = False
        runs = [
            {"id": 10, "artifacts": [{"name": "google-maps-master-store-run_10"}]},
        ]
        found = find_latest_master_store_artifact(runs)
        self.assertIsNotNone(found)
        source = select_master_store_source(cache_restored, artifact_found=found is not None)
        self.assertEqual(source, SOURCE_ARTIFACT)
        self.assertEqual(found, (10, "google-maps-master-store-run_10"))

    def test_empty_initialization_when_no_prior_artifact_exists(self):
        cache_restored = False
        runs = []  # no successful runs at all -- first-ever master run
        found = find_latest_master_store_artifact(runs)
        self.assertIsNone(found)
        source = select_master_store_source(cache_restored, artifact_found=found is not None)
        self.assertEqual(source, SOURCE_EMPTY)


class CliTests(unittest.TestCase):
    """
    Exercises the same subcommands the workflow step actually invokes
    (single-line `python3 master_store_source.py <subcommand> ...`), so the
    exit-code/stdout contract the bash `run:` step relies on is covered by
    tests, without any live GitHub Actions calls.
    """

    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, SCRIPT_PATH, *args],
            capture_output=True,
            text=True,
        )

    def test_has_master_artifact_exit_code_drives_the_bash_loop_break(self):
        matching = json.dumps([{"name": "google-maps-master-store-run_1"}])
        non_matching = json.dumps([{"name": "google-maps-manifest-run_1"}])
        self.assertEqual(self.run_cli("has-master-artifact", matching).returncode, 0)
        self.assertEqual(self.run_cli("has-master-artifact", non_matching).returncode, 1)

    def test_append_run_then_find_latest_round_trips(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write("[]")
            entries_path = f.name
        try:
            self.assertEqual(
                self.run_cli(
                    "append-run",
                    entries_path,
                    "42",
                    json.dumps([{"name": "google-maps-master-store-run_42"}]),
                ).returncode,
                0,
            )
            result = self.run_cli("find-latest", entries_path)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout.strip(), "42 google-maps-master-store-run_42")
        finally:
            os.unlink(entries_path)

    def test_find_latest_with_no_matches_prints_empty_line(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write(json.dumps([{"id": 1, "artifacts": [{"name": "unrelated"}]}]))
            entries_path = f.name
        try:
            result = self.run_cli("find-latest", entries_path)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout.strip(), "")
        finally:
            os.unlink(entries_path)

    def test_select_source_cli_matches_python_api(self):
        result = self.run_cli("select-source", "false", "true")
        self.assertEqual(result.stdout.strip(), SOURCE_ARTIFACT)

    def test_find_latest_on_missing_entries_file_fails_loudly(self):
        # This models "the search step itself couldn't produce its entries
        # file" -- e.g. an earlier gh api call in the workflow died. The
        # CLI must exit non-zero (which, under the workflow's `set -e`,
        # fails the job) rather than silently behaving like "no artifact
        # found" and falling through to an empty master store.
        result = self.run_cli("find-latest", "/nonexistent/entries.json")
        self.assertNotEqual(result.returncode, 0)

    def test_unknown_subcommand_fails_loudly(self):
        result = self.run_cli("not-a-real-subcommand")
        self.assertEqual(result.returncode, 2)


if __name__ == "__main__":
    unittest.main()
