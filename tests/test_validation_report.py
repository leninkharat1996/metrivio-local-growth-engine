import json
import os
import tempfile
import unittest

from scripts.production_pipeline.validation_report import (
    SECTION_FOOTER,
    SECTION_HEADER,
    build_validation_record,
    build_validation_records,
    format_record_lines,
    render_section,
    run,
)

FINAL_RECORD = {
    "master_id": "abc123",
    "business_name": "Example Commercial HVAC",
    "website": "https://example-hvac.com",
    "city": "Dallas",
    "state": "TX",
    "country": "US",
    "final_icp_status": "qualified",
    "final_icp_tier": "B",
    "final_icp_score": 47,
    "final_icp_confidence": "high",
    "qualification_reasons": ["Path B: 1 HVAC-specific direct-service category(y/ies)..."],
    "exclusion_reasons": [],
    "commercial_service_signals": ["commercial_hvac", "mechanical_contractor"],
    "hvac_direct_signals": ["commercial_hvac"],
    "commercial_vertical_signals": ["property_management"],
    "high_value_service_signals": ["commercial_hvac"],
    "residential_signals": {"present": False, "keywords": []},
    "incidental_signals": [],
    "evidence_source": "website_enrichment",
    "website_status": "success",
    "preliminary_icp_status": "qualified",
    "preliminary_icp_confidence": "medium",
    "preliminary_icp_score": 32,
    "score_breakdown": {},
    "final_icp_evaluated_at": "2026-01-01T00:10:00Z",
}

MASTER_RECORD = {
    "master_id": "abc123",
    "business_name": "Example Commercial HVAC",
    "first_seen_at": "2026-01-01T00:00:00Z",
    "last_seen_at": "2026-01-01T00:00:00Z",
    "source_count": 1,
    "search_count": 1,
}


class BuildValidationRecordTests(unittest.TestCase):
    def test_fields_are_read_verbatim_from_final_icp_and_master(self):
        record = build_validation_record(FINAL_RECORD, MASTER_RECORD, run_started_at=None)

        # 2. required fields are present
        for field in (
            "master_id", "business_name", "city", "state", "website",
            "final_icp_status", "final_icp_tier", "final_icp_score", "final_icp_confidence",
            "preliminary_icp_status", "preliminary_icp_score", "preliminary_icp_confidence",
            "hvac_direct_signals", "commercial_vertical_signals", "commercial_service_signals",
            "residential_present", "residential_keywords",
            "qualification_reasons", "exclusion_reasons",
            "website_status",
            "first_seen_at", "last_seen_at", "source_count", "search_count",
        ):
            self.assertIn(field, record)

        # 3. evidence comes verbatim from the existing classifier output
        self.assertEqual(record["hvac_direct_signals"], FINAL_RECORD["hvac_direct_signals"])
        self.assertEqual(record["commercial_vertical_signals"], FINAL_RECORD["commercial_vertical_signals"])
        self.assertEqual(record["commercial_service_signals"], FINAL_RECORD["commercial_service_signals"])
        self.assertEqual(record["final_icp_status"], FINAL_RECORD["final_icp_status"])
        self.assertEqual(record["final_icp_tier"], FINAL_RECORD["final_icp_tier"])
        self.assertEqual(record["final_icp_score"], FINAL_RECORD["final_icp_score"])

        # 4. provenance is preserved from the master record, unchanged
        self.assertEqual(record["master_id"], MASTER_RECORD["master_id"])
        self.assertEqual(record["first_seen_at"], MASTER_RECORD["first_seen_at"])
        self.assertEqual(record["last_seen_at"], MASTER_RECORD["last_seen_at"])
        self.assertEqual(record["source_count"], MASTER_RECORD["source_count"])
        self.assertEqual(record["search_count"], MASTER_RECORD["search_count"])

    def test_no_master_record_leaves_provenance_none_not_fabricated(self):
        record = build_validation_record(FINAL_RECORD, None, run_started_at=None)
        self.assertIsNone(record["first_seen_at"])
        self.assertIsNone(record["last_seen_at"])
        self.assertIsNone(record["source_count"])
        self.assertIsNone(record["search_count"])
        self.assertIsNone(record["newly_discovered_this_run"])

    def test_newly_discovered_derived_from_existing_first_seen_at(self):
        newly = build_validation_record(FINAL_RECORD, MASTER_RECORD, run_started_at="2026-01-01T00:00:00Z")
        self.assertTrue(newly["newly_discovered_this_run"])

        older_master = dict(MASTER_RECORD, first_seen_at="2025-12-01T00:00:00Z")
        not_new = build_validation_record(FINAL_RECORD, older_master, run_started_at="2026-01-01T00:00:00Z")
        self.assertFalse(not_new["newly_discovered_this_run"])

    def test_unknown_when_run_started_at_omitted(self):
        record = build_validation_record(FINAL_RECORD, MASTER_RECORD, run_started_at=None)
        self.assertIsNone(record["newly_discovered_this_run"])


class BuildValidationRecordsTests(unittest.TestCase):
    def test_every_final_icp_record_is_represented(self):
        second = dict(FINAL_RECORD, master_id="zzz999", business_name="Second Business")
        records = build_validation_records([FINAL_RECORD, second], {"abc123": MASTER_RECORD}, run_started_at=None)
        self.assertEqual(len(records), 2)
        ids = {r["master_id"] for r in records}
        self.assertEqual(ids, {"abc123", "zzz999"})

    def test_empty_final_icp_records_produce_empty_list(self):
        records = build_validation_records([], {"abc123": MASTER_RECORD}, run_started_at=None)
        self.assertEqual(records, [])


class RenderSectionTests(unittest.TestCase):
    def test_section_header_and_footer_present(self):
        section = render_section([build_validation_record(FINAL_RECORD, MASTER_RECORD, None)])
        self.assertIn(SECTION_HEADER, section)
        self.assertIn(SECTION_FOOTER, section)
        self.assertIn("[QUALIFIED]", section)
        self.assertIn("Example Commercial HVAC", section)
        self.assertIn("Dallas, TX", section)
        self.assertIn("Tier B", section)
        self.assertIn("Score 47", section)

    # 5. empty results work
    def test_empty_records_render_placeholder_section(self):
        section = render_section([])
        self.assertIn(SECTION_HEADER, section)
        self.assertIn(SECTION_FOOTER, section)
        self.assertIn("no Final ICP records", section)

    def test_format_record_lines_no_html_dumped(self):
        html_like = dict(FINAL_RECORD, qualification_reasons=["<html><body>page</body></html>"])
        lines = format_record_lines(build_validation_record(html_like, MASTER_RECORD, None))
        joined = "\n".join(lines)
        # The reason text itself may quote a short phrase, but nothing raw
        # HTML-page-sized should ever be emitted; this guards against a
        # future accidental dump of a body_text/homepage field.
        self.assertNotIn("<script", joined.lower())
        self.assertNotIn("<!doctype", joined.lower())

    # 6. no secrets are printed
    def test_no_secret_like_fields_ever_printed(self):
        record = build_validation_record(FINAL_RECORD, MASTER_RECORD, None)
        section = render_section([record])
        lowered = section.lower()
        for forbidden in ("token", "secret", "password", "api_key", "authorization", "gh_token", "cookie"):
            self.assertNotIn(forbidden, lowered)


class RunEndToEndTests(unittest.TestCase):
    def test_run_writes_json_csv_and_returns_section(self):
        with tempfile.TemporaryDirectory() as tmp:
            final_icp_path = os.path.join(tmp, "final_icp_qualified.json")
            with open(final_icp_path, "w") as f:
                json.dump([FINAL_RECORD], f)

            master_path = os.path.join(tmp, "master.json")
            with open(master_path, "w") as f:
                json.dump([MASTER_RECORD], f)

            out_dir = os.path.join(tmp, "out")
            result = run(final_icp_path, master_path, out_dir, run_started_at="2025-01-01T00:00:00Z")

            self.assertEqual(result["records_reported"], 1)
            self.assertIn(SECTION_HEADER, result["section"])

            json_path = result["artifacts"]["validation_json"]
            csv_path = result["artifacts"]["validation_csv"]
            self.assertTrue(os.path.exists(json_path))
            self.assertTrue(os.path.exists(csv_path))

            with open(json_path) as f:
                written = json.load(f)
            self.assertEqual(len(written), 1)
            self.assertEqual(written[0]["master_id"], "abc123")

    def test_run_with_missing_final_icp_file_produces_empty_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = os.path.join(tmp, "out")
            result = run(
                os.path.join(tmp, "does_not_exist.json"),
                os.path.join(tmp, "master.json"),
                out_dir,
            )
            self.assertEqual(result["records_reported"], 0)
            self.assertIn("no Final ICP records", result["section"])


if __name__ == "__main__":
    unittest.main()
