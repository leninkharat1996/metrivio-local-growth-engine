import copy
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "scripts", "icp"),
)

import qualify  # noqa: E402

RULES_PATH = os.path.join(
    os.path.dirname(__file__), "..", "config", "icp_rules.json"
)
RULES = qualify.load_rules(RULES_PATH)
FIXED_EVALUATED_AT = "2026-09-09T00:00:00+00:00"


def make_master_record(**overrides):
    base = {
        "master_id": "abc123",
        "business_name": "Acme Commercial HVAC",
        "category": "commercial HVAC contractors",
        "address": "123 Main St, Dallas, TX 75201",
        "city": "Dallas",
        "state": "TX",
        "country": "USA",
        "phone": "214-555-0100",
        "website": "https://acmehvac.com",
        "google_maps_url": "https://maps.google.com/?cid=1",
        "rating": 4.7,
        "review_count": 85,
        "latitude": 32.7,
        "longitude": -96.8,
        "source": "google_maps_scraper",
        "scraped_at": "2026-09-09T00:00:00+00:00",
        "search_keyword": "commercial hvac",
        "search_location": "Dallas, TX",
        "search_id": "dallas-tx_hvac",
        "run_id": "run_1",
        "first_seen_at": "2026-09-01T00:00:00+00:00",
        "last_seen_at": "2026-09-09T00:00:00+00:00",
        "source_count": 1,
        "search_count": 1,
    }
    base.update(overrides)
    return base


def qualify_one(**overrides):
    record = make_master_record(**overrides)
    return qualify.qualify_record(record, RULES, FIXED_EVALUATED_AT)


class ScoringScenarioTests(unittest.TestCase):
    def test_obvious_commercial_hvac_scores_a_or_a_plus(self):
        result = qualify_one(
            business_name="Acme Commercial HVAC",
            category="commercial HVAC contractors",
            rating=4.8,
            review_count=120,
            website="https://acmehvac.com",
        )
        self.assertIn(result["icp_tier"], ("A", "A+"))
        self.assertEqual(result["icp_exclusions"], [])

    def test_residential_only_hvac_is_tier_d(self):
        result = qualify_one(
            business_name="Joe's Residential HVAC",
            category="residential heating and air home services",
        )
        self.assertEqual(result["icp_tier"], "D")
        self.assertTrue(any("residential-only" in r for r in result["icp_exclusions"]))

    def test_mixed_residential_and_commercial_not_auto_rejected(self):
        result = qualify_one(
            business_name="Metro Heating and Air",
            category="residential and commercial HVAC services",
        )
        self.assertNotEqual(result["icp_tier"], "D")
        self.assertEqual(result["icp_exclusions"], [])

    def test_distributor_is_tier_d(self):
        result = qualify_one(
            business_name="Statewide HVAC Distributor",
            category="HVAC parts distributor and wholesale supply",
        )
        self.assertEqual(result["icp_tier"], "D")
        self.assertTrue(len(result["icp_exclusions"]) >= 1)

    def test_non_us_country_is_tier_d(self):
        result = qualify_one(
            business_name="Toronto Commercial HVAC",
            category="commercial HVAC contractors",
            country="Canada",
        )
        self.assertEqual(result["icp_tier"], "D")
        self.assertTrue(any("country" in r for r in result["icp_exclusions"]))

    def test_plumbing_only_is_tier_d(self):
        result = qualify_one(
            business_name="Dallas Plumbing Co",
            category="plumbing services",
        )
        self.assertEqual(result["icp_tier"], "D")
        self.assertTrue(any("plumbing_only" in r for r in result["icp_exclusions"]))

    def test_electrical_only_is_tier_d(self):
        result = qualify_one(
            business_name="Bright Spark Electrician",
            category="residential electrical contractor",
        )
        self.assertEqual(result["icp_tier"], "D")

    def test_plumbing_with_hvac_signal_is_not_hard_excluded(self):
        # A plumbing & HVAC combo shop is not a "plumbing-only" business.
        result = qualify_one(
            business_name="Dallas Plumbing and HVAC",
            category="plumbing and commercial HVAC services",
        )
        self.assertNotIn(
            "plumbing_only",
            " ".join(result["icp_exclusions"]),
        )

    def test_insufficient_evidence_does_not_score_a_plus(self):
        result = qualify_one(
            business_name="XYZ Enterprises",
            category="",
            website="",
            rating=None,
            review_count=None,
        )
        self.assertIn(result["icp_tier"], ("C", "B", "D"))
        self.assertNotIn(result["icp_tier"], ("A", "A+"))

    def test_strong_commercial_signals_increase_score_over_baseline(self):
        baseline = qualify_one(
            business_name="Plain HVAC Co",
            category="hvac",
            website="",
            rating=None,
            review_count=None,
        )
        boosted = qualify_one(
            business_name="Plain HVAC Co",
            category="commercial hvac industrial hvac mechanical contractor",
            website="",
            rating=None,
            review_count=None,
        )
        self.assertGreater(boosted["icp_score"], baseline["icp_score"])

    def test_hard_exclusion_overrides_strong_positive_signals(self):
        result = qualify_one(
            business_name="Best Commercial HVAC Manufacturer",
            category="commercial HVAC industrial mechanical contractor manufacturer",
            rating=4.9,
            review_count=500,
            website="https://example.com",
        )
        self.assertEqual(result["icp_tier"], "D")
        self.assertTrue(len(result["icp_exclusions"]) >= 1)

    def test_government_entity_is_tier_d(self):
        result = qualify_one(
            business_name="City of Dallas Facilities Department",
            category="government facilities management",
        )
        self.assertEqual(result["icp_tier"], "D")


class DeterminismTests(unittest.TestCase):
    def test_same_input_produces_byte_identical_output(self):
        record = make_master_record()
        result_a = qualify.qualify_record(copy.deepcopy(record), RULES, FIXED_EVALUATED_AT)
        result_b = qualify.qualify_record(copy.deepcopy(record), RULES, FIXED_EVALUATED_AT)
        self.assertEqual(json.dumps(result_a, sort_keys=True), json.dumps(result_b, sort_keys=True))

    def test_qualify_all_output_is_byte_identical_across_runs(self):
        records = [
            make_master_record(master_id="id1", business_name="Acme Commercial HVAC"),
            make_master_record(master_id="id2", business_name="Joe's Residential HVAC", category="residential heating"),
        ]
        results_a = qualify.qualify_all(copy.deepcopy(records), RULES, FIXED_EVALUATED_AT)
        results_b = qualify.qualify_all(copy.deepcopy(records), RULES, FIXED_EVALUATED_AT)
        self.assertEqual(json.dumps(results_a, sort_keys=True), json.dumps(results_b, sort_keys=True))

    def test_output_order_independent_of_input_order(self):
        records = [
            make_master_record(master_id="bbb"),
            make_master_record(master_id="aaa"),
        ]
        results = qualify.qualify_all(records, RULES, FIXED_EVALUATED_AT)
        self.assertEqual([r["master_id"] for r in results], ["aaa", "bbb"])


class DataIntegrityTests(unittest.TestCase):
    def test_master_id_preserved_exactly(self):
        result = qualify_one(master_id="deadbeef1234abcd")
        self.assertEqual(result["master_id"], "deadbeef1234abcd")

    def test_required_output_fields_present(self):
        result = qualify_one()
        for field in qualify.OUTPUT_FIELDS:
            self.assertIn(field, result)

    def test_original_master_data_not_mutated_on_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            master_dir = os.path.join(tmp, "master")
            out_dir = os.path.join(tmp, "icp")
            os.makedirs(master_dir)
            master_json_path = os.path.join(master_dir, "master.json")
            records = [make_master_record(master_id="id1")]
            with open(master_json_path, "w", encoding="utf-8") as f:
                json.dump(records, f, indent=2)
            original_bytes = open(master_json_path, "rb").read()

            qualify.run(master_json_path, RULES_PATH, out_dir, evaluated_at=FIXED_EVALUATED_AT)

            self.assertEqual(open(master_json_path, "rb").read(), original_bytes)

    def test_run_writes_output_to_separate_directory_not_master_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            master_dir = os.path.join(tmp, "master")
            out_dir = os.path.join(tmp, "icp")
            os.makedirs(master_dir)
            master_json_path = os.path.join(master_dir, "master.json")
            with open(master_json_path, "w", encoding="utf-8") as f:
                json.dump([make_master_record(master_id="id1")], f)

            stats = qualify.run(master_json_path, RULES_PATH, out_dir, evaluated_at=FIXED_EVALUATED_AT)

            self.assertTrue(os.path.exists(stats["artifacts"]["icp_json"]))
            self.assertTrue(os.path.exists(stats["artifacts"]["icp_csv"]))
            self.assertFalse(os.path.exists(os.path.join(master_dir, "icp_qualified.json")))
            self.assertEqual(stats["records_evaluated"], 1)


class ReasonsAndExclusionsTests(unittest.TestCase):
    def test_reasons_list_is_human_readable_strings(self):
        result = qualify_one()
        self.assertTrue(len(result["icp_reasons"]) > 0)
        for reason in result["icp_reasons"]:
            self.assertIsInstance(reason, str)

    def test_exclusions_empty_for_qualifying_business(self):
        result = qualify_one(
            business_name="Acme Commercial HVAC",
            category="commercial HVAC contractors",
        )
        self.assertEqual(result["icp_exclusions"], [])

    def test_employee_count_bonus_only_when_present_never_required(self):
        without = qualify_one(business_name="Plain HVAC", category="hvac")
        with_small = qualify_one(business_name="Plain HVAC", category="hvac", employee_count=8)
        self.assertGreater(with_small["icp_score"], without["icp_score"])
        # Field absent entirely -> no fabricated bonus, no crash.
        self.assertNotIn("employee_count", without)


if __name__ == "__main__":
    unittest.main()
