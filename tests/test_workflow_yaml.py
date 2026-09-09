import os
import unittest

import yaml

WORKFLOW_PATH = os.path.join(
    os.path.dirname(__file__), "..", ".github", "workflows", "google-maps-scraper.yml"
)


def load_workflow():
    with open(WORKFLOW_PATH) as f:
        # PyYAML parses the bare `on:` key as the boolean True; work with
        # the raw mapping either way by trying both keys.
        return yaml.safe_load(f)


class WorkflowYamlTests(unittest.TestCase):
    def setUp(self):
        self.workflow = load_workflow()

    def test_yaml_parses(self):
        self.assertIsInstance(self.workflow, dict)

    def test_concurrency_group_serializes_master_store_writers(self):
        concurrency = self.workflow.get("concurrency")
        self.assertIsNotNone(concurrency, "expected a top-level concurrency block")
        self.assertEqual(concurrency.get("group"), "google-maps-master-store")
        # cancel-in-progress must be False: this queues runs one after
        # another rather than killing an in-flight master-store update.
        self.assertIs(concurrency.get("cancel-in-progress"), False)

    def test_job_has_actions_read_permission_for_artifact_fallback_lookup(self):
        job = self.workflow["jobs"]["scrape"]
        permissions = job.get("permissions")
        self.assertIsNotNone(permissions, "expected explicit job permissions")
        self.assertEqual(permissions.get("contents"), "read")
        self.assertEqual(permissions.get("actions"), "read")

    def _steps(self):
        return self.workflow["jobs"]["scrape"]["steps"]

    def _step_named(self, name):
        for step in self._steps():
            if step.get("name") == name:
                return step
        self.fail(f"no step named {name!r} found")

    def test_cache_restore_step_precedes_source_resolution_step(self):
        steps = self._steps()
        names = [s.get("name") for s in steps]
        restore_idx = names.index("Restore master store from cache (best-effort)")
        resolve_idx = names.index(
            "Resolve master store source (cache, artifact fallback, or empty)"
        )
        update_idx = names.index("Update master prospect store")
        self.assertLess(restore_idx, resolve_idx)
        self.assertLess(resolve_idx, update_idx)

    def test_source_resolution_step_has_gh_token_for_api_access(self):
        step = self._step_named(
            "Resolve master store source (cache, artifact fallback, or empty)"
        )
        env = step.get("env", {})
        self.assertIn("GH_TOKEN", env)

    def test_master_store_upload_includes_identity_conflicts_and_all_prior_files(self):
        step = self._step_named("Upload master snapshot + discovery history")
        path = step["with"]["path"]
        for expected in (
            "data/master/master.csv",
            "data/master/master.json",
            "data/master/discovery_history.csv",
            "data/master/identity_conflicts.csv",
        ):
            self.assertIn(expected, path)
        # Zero conflicts (or any single missing file) must not fail the run.
        self.assertEqual(step["with"]["if-no-files-found"], "warn")

    def test_cache_save_step_uses_a_fresh_key_per_run(self):
        step = self._step_named("Save master snapshot to cache (best-effort, for the next run)")
        self.assertIn("${{ github.run_id }}", step["with"]["key"])


if __name__ == "__main__":
    unittest.main()
