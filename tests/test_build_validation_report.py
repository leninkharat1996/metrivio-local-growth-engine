import contextlib
import io
import json
import os
import tempfile
import unittest

from scripts.production_pipeline import build_validation_report as bvr

CURRENT_RUN_ID = "run_999_1"


def make_final_icp_record(**overrides):
    base = {
        "master_id": "m1",
        "business_name": "Acme Commercial HVAC",
        "website": "https://acmehvac.com",
        "city": "Dallas",
        "state": "TX",
        "country": "USA",
        "final_icp_status": "qualified",
        "final_icp_tier": "B",
        "final_icp_score": 47,
        "final_icp_confidence": "high",
        "qualification_reasons": ["Path A: 2 HVAC-specific direct-service category(ies)"],
        "exclusion_reasons": [],
        "commercial_service_signals": ["commercial_hvac_install", "commercial_hvac_maintenance"],
        "hvac_direct_signals": ["commercial_hvac_install", "commercial_hvac_maintenance"],
        "commercial_vertical_signals": ["office building", "warehouse"],
        "high_value_service_signals": ["commercial_hvac_install", "commercial_hvac_maintenance"],
        "residential_signals": {"present": False, "keywords": []},
        "incidental_signals": [],
        "evidence_source": "website_enrichment",
        "website_status": "success",
        "preliminary_icp_status": "qualified",
        "preliminary_icp_confidence": "high",
        "preliminary_icp_score": 60,
        "score_breakdown": {"bucket1_hvac_direct": 30, "bucket2_service": 8, "bucket3_supporting": 9, "bucket4_modifiers": 0},
        "final_icp_evaluated_at": "2026-09-10T00:00:00+00:00",
    }
    base.update(overrides)
    return base


def make_master_record(**overrides):
    base = {
        "master_id": "m1",
        "business_name": "Acme Commercial HVAC",
        "run_id": CURRENT_RUN_ID,
        "search_id": "s1",
        "first_seen_at": "2026-09-10T00:00:00Z",
        "last_seen_at": "2026-09-10T00:00:00Z",
        "source_count": 1,
        "search_count": 1,
    }
    base.update(overrides)
    return base


class BuildValidationRecordTests(unittest.TestCase):
    # 1. every final ICP record appears in the validation report
    def test_every_final_icp_record_appears(self):
        records = [make_final_icp_record(master_id="m1"), make_final_icp_record(master_id="m2", business_name="Other Co")]
        master_index = {"m1": make_master_record(master_id="m1"), "m2": make_master_record(master_id="m2")}
        rows = bvr.build_validation_records(records, master_index, CURRENT_RUN_ID)
        self.assertEqual({r["master_id"] for r in rows}, {"m1", "m2"})
        self.assertEqual(len(rows), 2)

    # 2. required fields are present
    def test_required_fields_present_on_every_row(self):
        records = [make_final_icp_record()]
        master_index = {"m1": make_master_record()}
        rows = bvr.build_validation_records(records, master_index, CURRENT_RUN_ID)
        self.assertEqual(len(rows), 1)
        for field in bvr.VALIDATION_FIELDS:
            self.assertIn(field, rows[0], f"missing required field: {field}")

    # 3. existing evidence is preserved exactly (not recomputed / mutated)
    def test_existing_evidence_preserved_exactly(self):
        record = make_final_icp_record(
            hvac_direct_signals=["commercial_hvac_install"],
            commercial_vertical_signals=["office building"],
            commercial_service_signals=["commercial_hvac_install", "commercial_hvac_maintenance"],
            residential_signals={"present": True, "keywords": ["residential ac repair"]},
            incidental_signals=[{"category": "building_type", "keyword": "warehouse"}],
            qualification_reasons=["Path B: corroborated by high preliminary confidence"],
            exclusion_reasons=["hard exclusion: residential-only ..."],
        )
        master_index = {"m1": make_master_record()}
        rows = bvr.build_validation_records([record], master_index, CURRENT_RUN_ID)
        row = rows[0]
        self.assertEqual(row["hvac_direct_signals"], record["hvac_direct_signals"])
        self.assertEqual(row["commercial_vertical_signals"], record["commercial_vertical_signals"])
        self.assertEqual(row["commercial_service_signals"], record["commercial_service_signals"])
        self.assertEqual(row["residential_signals"], record["residential_signals"])
        self.assertEqual(row["incidental_signals"], record["incidental_signals"])
        self.assertEqual(row["qualification_reasons"], record["qualification_reasons"])
        self.assertEqual(row["exclusion_reasons"], record["exclusion_reasons"])
        # Also verify the final decision fields themselves are untouched.
        self.assertEqual(row["final_icp_status"], record["final_icp_status"])
        self.assertEqual(row["final_icp_tier"], record["final_icp_tier"])
        self.assertEqual(row["final_icp_score"], record["final_icp_score"])
        self.assertEqual(row["final_icp_confidence"], record["final_icp_confidence"])
        self.assertEqual(row["preliminary_icp_status"], record["preliminary_icp_status"])
        self.assertEqual(row["preliminary_icp_confidence"], record["preliminary_icp_confidence"])
        self.assertEqual(row["preliminary_icp_score"], record["preliminary_icp_score"])

    # 4. provenance is preserved
    def test_provenance_preserved_from_master_record(self):
        master = make_master_record(
            first_seen_at="2026-01-01T00:00:00Z",
            last_seen_at="2026-09-10T00:00:00Z",
            source_count=3,
            search_count=2,
            search_id="s42",
        )
        rows = bvr.build_validation_records([make_final_icp_record()], {"m1": master}, CURRENT_RUN_ID)
        row = rows[0]
        self.assertEqual(row["first_seen_at"], "2026-01-01T00:00:00Z")
        self.assertEqual(row["last_seen_at"], "2026-09-10T00:00:00Z")
        self.assertEqual(row["source_count"], 3)
        self.assertEqual(row["search_count"], 2)
        self.assertEqual(row["master_search_id"], "s42")
        self.assertEqual(row["master_run_id"], CURRENT_RUN_ID)

    # 4b. missing master record (shouldn't happen in practice, but must not crash)
    def test_missing_master_record_yields_null_provenance_not_a_crash(self):
        rows = bvr.build_validation_records([make_final_icp_record(master_id="ghost")], {}, CURRENT_RUN_ID)
        row = rows[0]
        self.assertIsNone(row["first_seen_at"])
        self.assertIsNone(row["source_count"])
        self.assertEqual(row["run_status"], bvr.RUN_STATUS_UNKNOWN)

    # 5. new vs updated/historical status represented correctly
    def test_run_status_new_when_first_seen_equals_last_seen_on_current_run(self):
        master = make_master_record(first_seen_at="2026-09-10T00:00:00Z", last_seen_at="2026-09-10T00:00:00Z")
        rows = bvr.build_validation_records([make_final_icp_record()], {"m1": master}, CURRENT_RUN_ID)
        self.assertEqual(rows[0]["run_status"], bvr.RUN_STATUS_NEW)

    def test_run_status_updated_when_touched_this_run_but_seen_before(self):
        master = make_master_record(first_seen_at="2026-01-01T00:00:00Z", last_seen_at="2026-09-10T00:00:00Z")
        rows = bvr.build_validation_records([make_final_icp_record()], {"m1": master}, CURRENT_RUN_ID)
        self.assertEqual(rows[0]["run_status"], bvr.RUN_STATUS_UPDATED)

    def test_run_status_historical_when_master_run_id_is_a_different_run(self):
        master = make_master_record(run_id="run_111_1")
        rows = bvr.build_validation_records([make_final_icp_record()], {"m1": master}, CURRENT_RUN_ID)
        self.assertEqual(rows[0]["run_status"], bvr.RUN_STATUS_HISTORICAL)

    def test_run_status_unknown_when_no_current_run_id_supplied(self):
        master = make_master_record()
        rows = bvr.build_validation_records([make_final_icp_record()], {"m1": master}, None)
        self.assertEqual(rows[0]["run_status"], bvr.RUN_STATUS_UNKNOWN)

    # 6. empty result set works
    def test_empty_final_icp_records_produces_empty_report_without_error(self):
        rows = bvr.build_validation_records([], {}, CURRENT_RUN_ID)
        self.assertEqual(rows, [])
        buf = io.StringIO()
        bvr.print_validation_log(rows, out=buf)
        output = buf.getvalue()
        self.assertIn("===== FINAL ICP VALIDATION RECORDS =====", output)
        self.assertIn("0 record(s)", output)

    def test_run_with_empty_inputs_writes_valid_empty_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            final_icp_path = os.path.join(tmp, "final_icp_qualified.json")
            with open(final_icp_path, "w", encoding="utf-8") as f:
                json.dump([], f)
            master_path = os.path.join(tmp, "master.json")
            with open(master_path, "w", encoding="utf-8") as f:
                json.dump([], f)
            out_dir = os.path.join(tmp, "out")

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                stats = bvr.run(final_icp_path, master_path, CURRENT_RUN_ID, out_dir)

            self.assertEqual(stats["records_validated"], 0)
            json_path = os.path.join(out_dir, "final_icp_validation.json")
            csv_path = os.path.join(out_dir, "final_icp_validation.csv")
            self.assertTrue(os.path.exists(json_path))
            self.assertTrue(os.path.exists(csv_path))
            with open(json_path, encoding="utf-8") as f:
                self.assertEqual(json.load(f), [])
            with open(csv_path, encoding="utf-8") as f:
                header = f.readline().strip().split(",")
            self.assertEqual(header, bvr.VALIDATION_FIELDS)

    # 7. no secret/environment-variable leakage
    def test_no_secret_or_env_var_leakage_in_log_output(self):
        secret_value = "sk-super-secret-token-should-never-appear"
        old_value = os.environ.get("METRIVIO_TEST_FAKE_SECRET")
        os.environ["METRIVIO_TEST_FAKE_SECRET"] = secret_value
        try:
            rows = bvr.build_validation_records(
                [make_final_icp_record()], {"m1": make_master_record()}, CURRENT_RUN_ID
            )
            buf = io.StringIO()
            bvr.print_validation_log(rows, out=buf)
            output = buf.getvalue()
            self.assertNotIn(secret_value, output)
            self.assertNotIn("METRIVIO_TEST_FAKE_SECRET", output)
            # Never print raw HTML either -- the source data has none, but
            # guard the property explicitly.
            self.assertNotIn("<html", output.lower())
            self.assertNotIn("<!doctype", output.lower())
        finally:
            if old_value is None:
                os.environ.pop("METRIVIO_TEST_FAKE_SECRET", None)
            else:
                os.environ["METRIVIO_TEST_FAKE_SECRET"] = old_value

    def test_does_not_read_or_print_any_environ_values(self):
        # The module must never call os.environ / os.getenv at all -- it
        # only ever consumes the final ICP + master JSON files it is
        # pointed at.
        source = open(
            os.path.join(os.path.dirname(__file__), "..", "scripts", "production_pipeline", "build_validation_report.py"),
            encoding="utf-8",
        ).read()
        self.assertNotIn("os.environ", source)
        self.assertNotIn("os.getenv", source)

    # 8 (existing suite) is verified by running the full test suite
    # separately -- see task report.

    def test_csv_round_trip_preserves_list_fields_as_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows = bvr.build_validation_records(
                [make_final_icp_record()], {"m1": make_master_record()}, CURRENT_RUN_ID
            )
            json_path, csv_path = bvr.write_outputs(rows, tmp)
            with open(csv_path, newline="", encoding="utf-8") as f:
                import csv as csv_mod

                reader = csv_mod.DictReader(f)
                csv_rows = list(reader)
            self.assertEqual(len(csv_rows), 1)
            self.assertEqual(
                json.loads(csv_rows[0]["hvac_direct_signals"]),
                rows[0]["hvac_direct_signals"],
            )
            self.assertEqual(
                json.loads(csv_rows[0]["residential_signals"]),
                rows[0]["residential_signals"],
            )

    def test_format_validation_record_includes_key_fields_without_raw_html(self):
        row = bvr.build_validation_records(
            [make_final_icp_record()], {"m1": make_master_record()}, CURRENT_RUN_ID
        )[0]
        text = bvr.format_validation_record(row)
        self.assertIn("QUALIFIED", text)
        self.assertIn("Acme Commercial HVAC", text)
        self.assertIn("Dallas, TX", text)
        self.assertIn("Tier B", text)
        self.assertIn("Score 47", text)
        self.assertIn("Confidence high", text)
        self.assertNotIn("<html", text.lower())


if __name__ == "__main__":
    unittest.main()
