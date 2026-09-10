import os
import unittest

import yaml

WORKFLOW_PATH = os.path.join(
    os.path.dirname(__file__), "..", ".github", "workflows", "production-pipeline.yml"
)


def load_workflow():
    with open(WORKFLOW_PATH) as f:
        # PyYAML parses the bare `on:` key as the boolean True; work with
        # the raw mapping either way by trying both keys.
        return yaml.safe_load(f)


class ProductionPipelineYamlTests(unittest.TestCase):
    def setUp(self):
        self.workflow = load_workflow()

    def _on(self):
        return self.workflow.get("on", self.workflow.get(True))

    def test_yaml_parses(self):
        self.assertIsInstance(self.workflow, dict)

    # 1. workflow_dispatch exists
    def test_workflow_dispatch_trigger_exists(self):
        on = self._on()
        self.assertIn("workflow_dispatch", on)

    # 2. required inputs exist
    def test_required_inputs_exist(self):
        inputs = self._on()["workflow_dispatch"]["inputs"]
        self.assertIn("search_plan", inputs)
        self.assertTrue(inputs["search_plan"]["required"])

        self.assertIn("max_pages_per_domain", inputs)
        self.assertEqual(inputs["max_pages_per_domain"]["default"], "5")

        self.assertIn("request_timeout", inputs)
        self.assertEqual(inputs["request_timeout"]["default"], "20")

        self.assertIn("user_agent", inputs)
        self.assertIn("MetrivioLocalGrowthEngine", inputs["user_agent"]["default"])
        # Do not impersonate browsers.
        self.assertNotIn("Mozilla", inputs["user_agent"]["default"])

    # 3. no push trigger
    def test_no_push_trigger(self):
        on = self._on()
        self.assertNotIn("push", on)

    # 4. no schedule trigger
    def test_no_schedule_trigger(self):
        on = self._on()
        self.assertNotIn("schedule", on)

    def _steps(self):
        return self.workflow["jobs"]["run-pipeline"]["steps"]

    def _step_id_index(self, step_id):
        for idx, step in enumerate(self._steps()):
            if step.get("id") == step_id:
                return idx
        self.fail(f"no step with id {step_id!r} found")

    def _step_name_index(self, name):
        for idx, step in enumerate(self._steps()):
            if step.get("name") == name:
                return idx
        self.fail(f"no step named {name!r} found")

    def _python_setup_index(self):
        for idx, step in enumerate(self._steps()):
            if step.get("uses", "").startswith("actions/setup-python"):
                return idx
        self.fail("no actions/setup-python step found")

    # Dependency installation: requirements.txt is installed, after Python
    # setup and before Stage 3 (the first stage needing a third-party dep).
    def test_requirements_txt_is_installed(self):
        install_idx = self._step_name_index("Install repository Python dependencies")
        install_step = self._steps()[install_idx]
        run_text = install_step["run"]
        self.assertIn("pip install -r requirements.txt", run_text)
        # Must not pin/introduce a different dependency source or version.
        self.assertNotIn("pip install scrapling", run_text)

    def test_dependency_installation_after_python_setup_and_before_stage3(self):
        python_idx = self._python_setup_index()
        install_idx = self._step_name_index("Install repository Python dependencies")
        stage3_idx = self._step_id_index("stage3")
        self.assertLess(python_idx, install_idx)
        self.assertLess(install_idx, stage3_idx)

    def test_stage1_dispatch_uses_ref_name_not_sha(self):
        stage1_trigger = next(s for s in self._steps() if s.get("id") == "stage1_trigger")
        run_text = stage1_trigger["run"]
        self.assertIn('--ref "${{ github.ref_name }}"', run_text)
        self.assertNotIn('--ref "${{ github.sha }}"', run_text)

    # 5. correct pipeline stage ordering
    def test_stage_order(self):
        trigger_idx = self._step_id_index("stage1_trigger")
        wait_idx = self._step_id_index("stage1_wait")
        master_idx = self._step_id_index("stage1_master")
        stage2_idx = self._step_id_index("stage2")
        stage3_idx = self._step_id_index("stage3")
        stage4_idx = self._step_id_index("stage4")

        self.assertLess(trigger_idx, wait_idx)
        self.assertLess(wait_idx, master_idx)
        self.assertLess(master_idx, stage2_idx)
        self.assertLess(stage2_idx, stage3_idx)
        # 7. final ICP invoked after enrichment
        self.assertLess(stage3_idx, stage4_idx)

    # 6. existing scripts are invoked rather than duplicated
    def test_existing_scripts_invoked(self):
        steps = self._steps()
        run_text = "\n".join(step.get("run", "") for step in steps)

        self.assertIn("scripts/icp/qualify.py", run_text)
        self.assertIn("scripts/enrichment/enrich_websites.py", run_text)
        self.assertIn("scripts/final_icp/qualify_final.py", run_text)
        self.assertIn("google-maps-scraper.yml", run_text)

        # The existing Go scraper build/invocation must not be duplicated here.
        self.assertNotIn("go build", run_text)
        self.assertNotIn("google-maps-scraper -input", run_text)

    def test_enrichment_stage_passes_required_overrides(self):
        stage3 = next(s for s in self._steps() if s.get("id") == "stage3")
        run_text = stage3["run"]
        self.assertIn("inputs.max_pages_per_domain", run_text)
        self.assertIn("inputs.request_timeout", run_text)
        self.assertIn("inputs.user_agent", run_text)

    def test_final_icp_stage_uses_all_required_inputs(self):
        stage4 = next(s for s in self._steps() if s.get("id") == "stage4")
        run_text = stage4["run"]
        self.assertIn("--master-json", run_text)
        self.assertIn("--preliminary-json", run_text)
        self.assertIn("--enrichment-json", run_text)
        self.assertIn("config/final_icp_rules.json", run_text)

    # 8. artifact upload exists
    def test_artifact_upload_exists(self):
        steps = self._steps()
        upload_steps = [s for s in steps if s.get("uses", "").startswith("actions/upload-artifact")]
        self.assertTrue(upload_steps, "expected an actions/upload-artifact step")
        upload = upload_steps[0]
        self.assertIn("metrivio-production-pipeline-${{ github.run_id }}", upload["with"]["name"])
        path = upload["with"]["path"]
        for expected in (
            "data/final_icp/final_icp_qualified.json",
            "data/final_icp/final_icp_qualified.csv",
            "data/enrichment/website_enrichment.json",
            "data/enrichment/website_enrichment.csv",
            "data/icp/icp_qualified.json",
            "data/icp/icp_qualified.csv",
            "pipeline_summary.json",
        ):
            self.assertIn(expected, path)
        # No raw HTML should be uploaded.
        self.assertNotIn(".html", path)

    def test_job_permissions_are_minimal(self):
        job = self.workflow["jobs"]["run-pipeline"]
        permissions = job.get("permissions")
        self.assertIsNotNone(permissions)
        self.assertEqual(permissions.get("contents"), "read")

    def test_failure_check_step_exists_and_runs_always(self):
        steps = self._steps()
        fail_steps = [
            s for s in steps
            if "Fail the workflow" in (s.get("name") or "")
        ]
        self.assertTrue(fail_steps, "expected a final failure-gating step")
        self.assertEqual(fail_steps[0].get("if"), "always()")

    # 10. no network/AI/paid-service logic added outside existing components
    def test_no_disallowed_terms_introduced(self):
        run_text = "\n".join(step.get("run", "") for step in self._steps())
        disallowed = [
            "openai",
            "anthropic",
            "gpt-",
            "chatgpt",
            "proxy=",
            "stealthy",
            "captcha",
            "linkedin",
        ]
        lowered = run_text.lower()
        for term in disallowed:
            self.assertNotIn(term, lowered, f"unexpected term {term!r} found in workflow run steps")


if __name__ == "__main__":
    unittest.main()
