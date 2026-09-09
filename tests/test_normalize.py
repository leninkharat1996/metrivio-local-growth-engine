import csv
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "scripts", "google_maps_scraper"),
)

import build_manifest  # noqa: E402
import normalize  # noqa: E402
import search_plan  # noqa: E402


class NormalizationTests(unittest.TestCase):
    def test_normalize_name_key_strips_suffix_and_punctuation(self):
        self.assertEqual(
            normalize.normalize_name_key("Joe's HVAC, LLC."),
            normalize.normalize_name_key("Joes HVAC"),
        )

    def test_normalize_address_key_strips_street_type(self):
        self.assertEqual(
            normalize.normalize_address_key("123 Main Street"),
            normalize.normalize_address_key("123 Main St"),
        )

    def test_normalize_phone_key_strips_country_code(self):
        self.assertEqual(normalize.normalize_phone_key("+1 (214) 555-0100"), "2145550100")
        self.assertEqual(normalize.normalize_phone_key("214-555-0100"), "2145550100")

    def test_domain_key_normalizes_www_and_scheme(self):
        self.assertEqual(normalize.domain_key("https://www.example.com/hvac"), "example.com")
        self.assertEqual(normalize.domain_key("example.com"), "example.com")

    def test_parse_city_state_country_basic_us_address(self):
        city, state, country = normalize.parse_city_state_country(
            "123 Main St, Dallas, TX 75201"
        )
        self.assertEqual(city, "Dallas")
        self.assertEqual(state, "TX")
        self.assertEqual(country, "USA")

    def test_parse_city_state_country_empty_address(self):
        self.assertEqual(normalize.parse_city_state_country(""), ("", "", ""))


class DedupTests(unittest.TestCase):
    def _lead(self, **overrides):
        base = {
            "business_name": "Joe's HVAC",
            "category": "hvac",
            "address": "123 Main St, Dallas, TX 75201",
            "city": "Dallas",
            "state": "TX",
            "country": "USA",
            "phone": "214-555-0100",
            "website": "https://joeshvac.com",
            "google_maps_url": "https://maps.google.com/?cid=1",
            "rating": 4.5,
            "review_count": 10,
            "latitude": 32.7,
            "longitude": -96.8,
            "source": "google_maps_scraper",
            "scraped_at": "2026-09-09T00:00:00+00:00",
            "search_keyword": "hvac",
            "search_location": "Dallas, TX",
            "search_id": "dallas-tx_hvac",
            "run_id": "run_test",
        }
        base.update(overrides)
        return base

    def test_duplicate_by_website_domain(self):
        a = self._lead(website="https://joeshvac.com")
        b = self._lead(website="https://www.joeshvac.com", phone="")
        self.assertEqual(normalize.dedup_key(a), normalize.dedup_key(b))

    def test_duplicate_by_phone(self):
        a = self._lead(website="", phone="214-555-0100")
        b = self._lead(website="", phone="+1 214 555 0100")
        self.assertEqual(normalize.dedup_key(a), normalize.dedup_key(b))

    def test_duplicate_by_name_and_address_fallback(self):
        a = self._lead(website="", phone="", business_name="Joe's HVAC LLC")
        b = self._lead(website="", phone="", business_name="Joes HVAC")
        self.assertEqual(normalize.dedup_key(a), normalize.dedup_key(b))

    def test_distinct_businesses_not_merged(self):
        a = self._lead()
        b = self._lead(
            business_name="Different Co",
            address="999 Other Ave, Fort Worth, TX 76102",
            phone="817-555-0199",
            website="https://different.co",
        )
        self.assertNotEqual(normalize.dedup_key(a), normalize.dedup_key(b))

    def test_normalize_and_dedup_prefers_complete_record(self):
        raw_rows = [
            {"title": "Joe's HVAC", "phone": "", "website": "", "address": "123 Main St, Dallas, TX"},
            {"title": "Joe's HVAC LLC", "phone": "214-555-0100", "website": "", "address": "123 Main St, Dallas, TX"},
        ]
        leads, stats = normalize.normalize_and_dedup(
            raw_rows, "hvac", "Dallas, TX", "2026-09-09T00:00:00+00:00", "dallas-tx_hvac", "run_test"
        )
        self.assertEqual(stats["clean_records"], 1)
        self.assertEqual(stats["duplicates_merged"], 1)
        self.assertEqual(leads[0]["phone"], "214-555-0100")

    def test_normalize_and_dedup_attaches_provenance_fields(self):
        raw_rows = [
            {"title": "Joe's HVAC", "phone": "214-555-0100", "address": "123 Main St, Dallas, TX"},
        ]
        leads, _ = normalize.normalize_and_dedup(
            raw_rows, "hvac", "Dallas, TX", "2026-09-09T00:00:00+00:00", "dallas-tx_hvac", "run_test"
        )
        self.assertEqual(leads[0]["search_keyword"], "hvac")
        self.assertEqual(leads[0]["search_location"], "Dallas, TX")
        self.assertEqual(leads[0]["search_id"], "dallas-tx_hvac")
        self.assertEqual(leads[0]["run_id"], "run_test")
        self.assertEqual(leads[0]["source"], "google_maps_scraper")
        self.assertIn("scraped_at", leads[0])

    def test_missing_phone_and_website_flagged_partial(self):
        raw_rows = [
            {"title": "No Contact Biz", "address": "1 Nowhere Rd, Austin, TX"},
        ]
        leads, stats = normalize.normalize_and_dedup(raw_rows, "hvac", "Austin, TX", "2026-09-09T00:00:00+00:00")
        self.assertEqual(stats["clean_records"], 1)
        self.assertEqual(stats["partial_records_missing_phone_and_website"], 1)

    def test_malformed_record_missing_name_is_skipped(self):
        raw_rows = [
            {"title": "", "address": "1 Nowhere Rd, Austin, TX"},
            {"title": "Valid Biz", "address": "2 Somewhere Rd, Austin, TX", "phone": "512-555-0100"},
        ]
        leads, stats = normalize.normalize_and_dedup(raw_rows, "hvac", "Austin, TX", "2026-09-09T00:00:00+00:00")
        self.assertEqual(stats["malformed_skipped"], 1)
        self.assertEqual(stats["clean_records"], 1)


class OutputWriterTests(unittest.TestCase):
    def test_write_outputs_produces_valid_csv_and_json(self):
        leads = [
            {
                "business_name": "Joe's HVAC",
                "category": "hvac",
                "address": "123 Main St, Dallas, TX 75201",
                "city": "Dallas",
                "state": "TX",
                "country": "USA",
                "phone": "214-555-0100",
                "website": "https://joeshvac.com",
                "google_maps_url": "https://maps.google.com/?cid=1",
                "rating": 4.5,
                "review_count": 10,
                "latitude": 32.7,
                "longitude": -96.8,
                "source": "google_maps_scraper",
                "scraped_at": "2026-09-09T00:00:00+00:00",
                "search_keyword": "hvac",
                "search_location": "Dallas, TX",
                "search_id": "dallas-tx_hvac",
                "run_id": "run_test",
            }
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            prefix = os.path.join(tmpdir, "out")
            csv_path, json_path = normalize.write_outputs(leads, prefix)

            with open(csv_path, newline="", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(len(rows), 1)
            self.assertEqual(set(rows[0].keys()), set(normalize.CANONICAL_FIELDS))
            self.assertEqual(rows[0]["business_name"], "Joe's HVAC")

            with open(json_path, encoding="utf-8") as f:
                data = json.load(f)
            self.assertEqual(len(data), 1)
            self.assertEqual(set(data[0].keys()), set(normalize.CANONICAL_FIELDS))


class BuildQueryTests(unittest.TestCase):
    def test_build_query_line_combines_keyword_and_location(self):
        sys.path.insert(
            0,
            os.path.join(os.path.dirname(__file__), "..", "scripts", "google_maps_scraper"),
        )
        import build_query

        self.assertEqual(
            build_query.build_query_line("commercial HVAC contractors", "Dallas, TX"),
            "commercial HVAC contractors in Dallas, TX",
        )

    def test_build_query_line_rejects_empty_keyword(self):
        import build_query

        with self.assertRaises(ValueError):
            build_query.build_query_line("", "Dallas, TX")


class SearchPlanTests(unittest.TestCase):
    def test_parses_multiline_plan_with_explicit_depth(self):
        text = (
            "commercial HVAC contractors | Dallas, TX | 3\n"
            "commercial HVAC companies | Dallas, TX | 3\n"
        )
        entries = search_plan.parse_plan(text)
        self.assertEqual(len(entries), 2)
        self.assertTrue(all(e["valid"] for e in entries))
        self.assertEqual(entries[0]["keyword"], "commercial HVAC contractors")
        self.assertEqual(entries[0]["location"], "Dallas, TX")
        self.assertEqual(entries[0]["depth"], 3)

    def test_blank_lines_and_comments_are_skipped(self):
        text = "\n# a comment\n  \nkeyword | location | 2\n"
        entries = search_plan.parse_plan(text)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["keyword"], "keyword")

    def test_missing_depth_falls_back_to_default(self):
        entries = search_plan.parse_plan("keyword | location", default_depth=7)
        self.assertTrue(entries[0]["valid"])
        self.assertEqual(entries[0]["depth"], 7)

    def test_malformed_line_missing_pipe_is_invalid(self):
        entries = search_plan.parse_plan("just a keyword with no separators")
        self.assertFalse(entries[0]["valid"])
        self.assertIsNotNone(entries[0]["error"])

    def test_malformed_line_empty_keyword_is_invalid(self):
        entries = search_plan.parse_plan(" | Dallas, TX | 3")
        self.assertFalse(entries[0]["valid"])
        self.assertIn("keyword", entries[0]["error"])

    def test_malformed_line_empty_location_is_invalid(self):
        entries = search_plan.parse_plan("keyword | | 3")
        self.assertFalse(entries[0]["valid"])
        self.assertIn("location", entries[0]["error"])

    def test_malformed_line_non_numeric_depth_is_invalid(self):
        entries = search_plan.parse_plan("keyword | Dallas, TX | deep")
        self.assertFalse(entries[0]["valid"])
        self.assertIn("depth", entries[0]["error"])

    def test_malformed_line_zero_depth_is_invalid(self):
        entries = search_plan.parse_plan("keyword | Dallas, TX | 0")
        self.assertFalse(entries[0]["valid"])

    def test_valid_and_invalid_lines_mixed_keep_line_numbers(self):
        text = "keyword | Dallas, TX | 3\nbad line\nother | Austin, TX | 2\n"
        entries = search_plan.parse_plan(text)
        self.assertEqual([e["line_no"] for e in entries], [1, 2, 3])
        self.assertTrue(entries[0]["valid"])
        self.assertFalse(entries[1]["valid"])
        self.assertTrue(entries[2]["valid"])

    def test_search_id_is_deterministic_slug_of_location_and_keyword(self):
        entries = search_plan.parse_plan("commercial HVAC contractors | Dallas, TX | 3")
        self.assertEqual(entries[0]["search_id"], "dallas-tx_commercial-hvac-contractors")

    def test_search_id_is_stable_across_repeated_parsing(self):
        text = "commercial HVAC contractors | Dallas, TX | 3"
        first = search_plan.parse_plan(text)[0]["search_id"]
        second = search_plan.parse_plan(text)[0]["search_id"]
        self.assertEqual(first, second)

    def test_duplicate_search_lines_get_distinct_search_ids(self):
        text = "hvac | Dallas, TX | 3\nhvac | Dallas, TX | 5\n"
        entries = search_plan.parse_plan(text)
        ids = [e["search_id"] for e in entries]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(ids[0], "dallas-tx_hvac")
        self.assertEqual(ids[1], "dallas-tx_hvac-2")

    def test_invalid_lines_never_get_a_search_id(self):
        entries = search_plan.parse_plan("bad line with no pipe")
        self.assertNotIn("search_id", entries[0])


class BuildManifestTests(unittest.TestCase):
    def _plan(self):
        return [
            {
                "line_no": 1,
                "raw": "hvac | Dallas, TX | 3",
                "valid": True,
                "keyword": "hvac",
                "location": "Dallas, TX",
                "depth": 3,
                "search_id": "dallas-tx_hvac",
                "error": None,
            },
            {
                "line_no": 2,
                "raw": "plumbing | Dallas, TX | 3",
                "valid": True,
                "keyword": "plumbing",
                "location": "Dallas, TX",
                "depth": 3,
                "search_id": "dallas-tx_plumbing",
                "error": None,
            },
            {
                "line_no": 3,
                "raw": "bad line",
                "valid": False,
                "error": "expected 'keyword | location'",
            },
        ]

    def test_manifest_marks_run_fully_successful(self):
        plan = self._plan()[:2]
        results = [
            {"search_id": "dallas-tx_hvac", "status": "success", "stats": {"raw_records": 10, "clean_records": 9, "malformed_skipped": 1, "duplicates_merged": 0, "partial_records_missing_phone_and_website": 2}},
            {"search_id": "dallas-tx_plumbing", "status": "success", "stats": {"raw_records": 5, "clean_records": 5, "malformed_skipped": 0, "duplicates_merged": 0, "partial_records_missing_phone_and_website": 0}},
        ]
        manifest = build_manifest.build_manifest("run_test", plan, results, timestamp="2026-09-09T00:00:00Z")
        self.assertEqual(manifest["overall_status"], "success")
        self.assertEqual(manifest["totals"]["successful"], 2)
        self.assertEqual(manifest["totals"]["failed"], 0)
        self.assertEqual(manifest["successful_searches"][0]["clean_record_count"], 9)

    def test_manifest_reports_partial_failure_when_one_search_fails(self):
        plan = self._plan()[:2]
        results = [
            {"search_id": "dallas-tx_hvac", "status": "success", "stats": {"raw_records": 10, "clean_records": 9, "malformed_skipped": 1, "duplicates_merged": 0, "partial_records_missing_phone_and_website": 0}},
            {"search_id": "dallas-tx_plumbing", "status": "failed", "error": "scraper exited with status 1"},
        ]
        manifest = build_manifest.build_manifest("run_test", plan, results, timestamp="2026-09-09T00:00:00Z")
        self.assertEqual(manifest["overall_status"], "partial_failure")
        self.assertEqual(len(manifest["successful_searches"]), 1)
        self.assertEqual(len(manifest["failed_searches"]), 1)
        self.assertEqual(manifest["failed_searches"][0]["search_id"], "dallas-tx_plumbing")
        self.assertEqual(manifest["failed_searches"][0]["error"], "scraper exited with status 1")

    def test_manifest_reports_failed_when_all_searches_fail(self):
        plan = self._plan()[:2]
        results = [
            {"search_id": "dallas-tx_hvac", "status": "failed", "error": "boom"},
            {"search_id": "dallas-tx_plumbing", "status": "failed", "error": "boom"},
        ]
        manifest = build_manifest.build_manifest("run_test", plan, results, timestamp="2026-09-09T00:00:00Z")
        self.assertEqual(manifest["overall_status"], "failed")

    def test_manifest_includes_invalid_lines_and_still_reports_partial_failure(self):
        plan = self._plan()
        results = [
            {"search_id": "dallas-tx_hvac", "status": "success", "stats": {"raw_records": 1, "clean_records": 1, "malformed_skipped": 0, "duplicates_merged": 0, "partial_records_missing_phone_and_website": 0}},
            {"search_id": "dallas-tx_plumbing", "status": "success", "stats": {"raw_records": 1, "clean_records": 1, "malformed_skipped": 0, "duplicates_merged": 0, "partial_records_missing_phone_and_website": 0}},
        ]
        manifest = build_manifest.build_manifest("run_test", plan, results, timestamp="2026-09-09T00:00:00Z")
        self.assertEqual(len(manifest["invalid_search_lines"]), 1)
        self.assertEqual(manifest["overall_status"], "partial_failure")

    def test_manifest_missing_result_record_counts_as_failed(self):
        plan = self._plan()[:1]
        manifest = build_manifest.build_manifest("run_test", plan, [], timestamp="2026-09-09T00:00:00Z")
        self.assertEqual(manifest["overall_status"], "failed")
        self.assertEqual(len(manifest["failed_searches"]), 1)

    def test_manifest_with_no_requested_searches_is_failed(self):
        manifest = build_manifest.build_manifest("run_test", [], [], timestamp="2026-09-09T00:00:00Z")
        self.assertEqual(manifest["overall_status"], "failed")

    def test_manifest_carries_run_id_and_timestamp(self):
        manifest = build_manifest.build_manifest("run_test", self._plan()[:1], [
            {"search_id": "dallas-tx_hvac", "status": "success", "stats": {"raw_records": 1, "clean_records": 1, "malformed_skipped": 0, "duplicates_merged": 0, "partial_records_missing_phone_and_website": 0}}
        ], timestamp="2026-09-09T00:00:00Z")
        self.assertEqual(manifest["run_id"], "run_test")
        self.assertEqual(manifest["generated_at"], "2026-09-09T00:00:00Z")


if __name__ == "__main__":
    unittest.main()
