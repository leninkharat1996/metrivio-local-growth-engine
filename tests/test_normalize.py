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

import normalize  # noqa: E402


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
        leads, stats = normalize.normalize_and_dedup(raw_rows, "hvac", "Dallas, TX", "2026-09-09T00:00:00+00:00")
        self.assertEqual(stats["clean_records"], 1)
        self.assertEqual(stats["duplicates_merged"], 1)
        self.assertEqual(leads[0]["phone"], "214-555-0100")

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


if __name__ == "__main__":
    unittest.main()
