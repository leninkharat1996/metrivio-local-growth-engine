import os
import unittest

import yaml

WORKFLOW_PATH = os.path.join(
    os.path.dirname(__file__), "..", ".github", "workflows", "production-pipeline.yml"
)


def load_workflow():
    with open(WORKFLOW_PATH) as f:
        return yaml.safe_load(f)


def _steps(workflow):
    return workflow["jobs"]["run-pipeline"]["steps"]


def _step_by_id(workflow, step_id):
    for step in _steps(workflow):
        if step.get("id") == step_id:
            return step
    return None


class ValidationReportYamlTests(unittest.TestCase):
    def setUp(self):
        self.workflow = load_workflow()

    def test_yaml_still_parses(self):
        self.assertIsInstance(self.workflow, dict)

    def test_stage4b_validation_step_exists_and_invokes_the_new_script(self):
        step = _step_by_id(self.workflow, "stage4b")
        self.assertIsNotNone(step, "expected a step with id: stage4b")
        self.assertIn("scripts/production_pipeline/build_validation_report.py", step["run"])
        self.assertIn("--final-icp-json", step["run"])
        self.assertIn("--master-json", step["run"])
        self.assertIn("--current-run-id", step["run"])

    def test_stage4b_runs_after_stage4(self):
        steps = _steps(self.workflow)
        ids = [s.get("id") for s in steps]
        self.assertIn("stage4", ids)
        self.assertIn("stage4b", ids)
        self.assertLess(ids.index("stage4"), ids.index("stage4b"))

    def test_validation_artifacts_are_uploaded(self):
        upload_step = None
        for step in _steps(self.workflow):
            if step.get("uses", "").startswith("actions/upload-artifact"):
                upload_step = step
        self.assertIsNotNone(upload_step)
        upload_paths = upload_step["with"]["path"]
        self.assertIn("data/final_icp/final_icp_validation.json", upload_paths)
        self.assertIn("data/final_icp/final_icp_validation.csv", upload_paths)

    def test_final_failure_gate_checks_stage4b_outcome(self):
        gate_step = None
        for step in _steps(self.workflow):
            if step.get("name", "").startswith("Fail the workflow if any stage did not succeed"):
                gate_step = step
        self.assertIsNotNone(gate_step)
        self.assertIn("steps.stage4b.outcome", gate_step["run"])

    def test_stage4b_does_not_touch_scoring_or_rules_inputs(self):
        step = _step_by_id(self.workflow, "stage4b")
        # The observability step must never pass --rules or otherwise
        # reference the classifier's config -- it only reads Stage 4's
        # already-decided output.
        self.assertNotIn("--rules", step["run"])
        self.assertNotIn("final_icp_rules.json", step["run"])


if __name__ == "__main__":
    unittest.main()
