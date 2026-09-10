import copy
import csv
import json
import logging
import os
import sys
import tempfile
import unittest

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "scripts", "enrichment"),
)

import enrich_websites as ew  # noqa: E402
import evidence  # noqa: E402
import html_extract  # noqa: E402
import robots as robots_mod  # noqa: E402
import scrapling_client  # noqa: E402
import url_utils  # noqa: E402

from scrapling.engines.toolbelt.custom import Response as ScraplingResponse  # noqa: E402

# Scrapling logs an INFO line per constructed Response; keep test output clean.
logging.getLogger("scrapling").setLevel(logging.WARNING)

FIXED_TIME = "2026-09-09T00:00:00+00:00"
FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures", "website_enrichment")


def load_fixture(name):
    with open(os.path.join(FIXTURE_DIR, name), encoding="utf-8") as f:
        return f.read()


def fixed_now():
    return FIXED_TIME


def make_master_record(**overrides):
    base = {
        "master_id": "abc123",
        "business_name": "Acme Commercial HVAC",
        "category": "commercial HVAC contractors",
        "website": "https://acmehvac.example.com",
    }
    base.update(overrides)
    return base


def fake_response(status_code=200, text="", content_type="text/html; charset=utf-8", url=None):
    """Build a REAL Scrapling Response (its parser included) from fixture HTML.

    Nothing here touches the network: Scrapling's Response is constructed
    directly from a local HTML string, so tests exercise Scrapling's actual
    parsing/selector code path while staying fully offline.
    """
    return ScraplingResponse(
        url=url or "https://fixture.example/",
        content=text,
        status=status_code,
        reason="OK" if status_code < 400 else "ERROR",
        cookies={},
        headers={"Content-Type": content_type},
        request_headers={},
    )


class FakeFetcher(ew.Fetcher):
    """Replaces Scrapling's network fetch with a canned url -> Response|Exception
    map, so no test ever touches a real socket. Everything downstream (parsing,
    extraction) still runs through Scrapling itself."""

    def __init__(self, config, responses_by_url):
        self.config = config
        self.client = None
        self._responses = responses_by_url
        self.calls = []

    def get(self, url):
        self.calls.append(url)
        outcome = self._responses.get(url)
        if outcome is None:
            raise ConnectionError(f"no fixture registered for {url}")
        if isinstance(outcome, Exception):
            raise outcome
        return fake_response(
            status_code=outcome.status,
            text=ew.response_text(outcome),
            content_type=outcome.headers.get("Content-Type", "text/html"),
            url=url,
        )

    def get_text_or_none(self, url):
        try:
            resp = self.get(url)
        except Exception:
            return None
        if resp.status >= 400:
            return None
        return ew.response_text(resp)


FakeResponse = fake_response

HOMEPAGE_HTML = load_fixture("homepage.html")
SERVICES_HTML = load_fixture("services.html")
ABOUT_HTML = load_fixture("about.html")
RESIDENTIAL_HTML = load_fixture("residential_only.html")
INCIDENTAL_HTML = load_fixture("incidental_only.html")

MALFORMED_HTML = load_fixture("malformed.html")


class UrlUtilsTests(unittest.TestCase):
    def test_valid_url_accepted(self):
        self.assertTrue(url_utils.is_valid_url("https://example.com"))

    def test_missing_scheme_rejected(self):
        self.assertFalse(url_utils.is_valid_url("example.com"))

    def test_ftp_scheme_rejected(self):
        self.assertFalse(url_utils.is_valid_url("ftp://example.com"))

    def test_empty_and_none_rejected(self):
        self.assertFalse(url_utils.is_valid_url(""))
        self.assertFalse(url_utils.is_valid_url(None))

    def test_normalize_strips_fragment(self):
        self.assertEqual(
            url_utils.normalize_url("https://example.com/page#section"),
            "https://example.com/page",
        )

    def test_normalize_strips_tracking_params(self):
        result = url_utils.normalize_url(
            "https://example.com/page?utm_source=x&gclid=y&keep=1"
        )
        self.assertIn("keep=1", result)
        self.assertNotIn("utm_source", result)
        self.assertNotIn("gclid", result)

    def test_normalize_drops_trailing_slash(self):
        self.assertEqual(
            url_utils.normalize_url("https://example.com/page/"),
            "https://example.com/page",
        )
        self.assertEqual(url_utils.normalize_url("https://example.com/"), "https://example.com/")

    def test_normalize_lowercases_host(self):
        self.assertEqual(
            url_utils.normalize_url("https://Example.COM/Page"),
            "https://example.com/Page",
        )

    def test_same_domain_true_for_www_variant(self):
        self.assertTrue(url_utils.same_domain("https://www.example.com/a", "https://example.com/b"))

    def test_same_domain_false_for_external(self):
        self.assertFalse(url_utils.same_domain("https://example.com/a", "https://other.com/a"))

    def test_resolve_link_relative(self):
        self.assertEqual(
            url_utils.resolve_link("https://example.com/dir/page", "sub"),
            "https://example.com/dir/sub",
        )

    def test_resolve_link_rejects_mailto(self):
        self.assertIsNone(url_utils.resolve_link("https://example.com", "mailto:a@b.com"))

    def test_resolve_link_rejects_empty_and_fragment_only(self):
        self.assertIsNone(url_utils.resolve_link("https://example.com", ""))
        self.assertIsNone(url_utils.resolve_link("https://example.com", "#top"))


class HtmlExtractTests(unittest.TestCase):
    def test_title_extracted(self):
        result = html_extract.extract_page(HOMEPAGE_HTML, max_text_length=5000)
        self.assertIn("Acme Commercial HVAC", result["title"])

    def test_meta_description_extracted(self):
        result = html_extract.extract_page(HOMEPAGE_HTML, max_text_length=5000)
        self.assertIn("Commercial HVAC service", result["meta_description"])

    def test_visible_text_normalized_whitespace(self):
        result = html_extract.extract_page(HOMEPAGE_HTML, max_text_length=5000)
        self.assertNotIn("\n", result["body_text"])
        self.assertNotIn("  ", result["body_text"])

    def test_body_text_length_capped(self):
        big_html = "<html><body><p>" + ("word " * 10000) + "</p></body></html>"
        result = html_extract.extract_page(big_html, max_text_length=100)
        self.assertLessEqual(len(result["body_text"]), 100)

    def test_headings_extracted(self):
        result = html_extract.extract_page(HOMEPAGE_HTML, max_text_length=5000)
        self.assertTrue(any("Commercial HVAC Service" in h for h in result["headings"]))

    def test_links_discovered_with_text(self):
        result = html_extract.extract_page(HOMEPAGE_HTML, max_text_length=5000)
        hrefs = [link["href"] for link in result["links"]]
        self.assertIn("/services", hrefs)
        self.assertIn("/about", hrefs)

    def test_script_and_style_text_excluded(self):
        html = "<html><body><script>var x = 'commercial hvac secret';</script><p>Visible text</p></body></html>"
        result = html_extract.extract_page(html, max_text_length=5000)
        self.assertNotIn("secret", result["body_text"])
        self.assertIn("Visible text", result["body_text"])

    def test_malformed_html_does_not_raise(self):
        result = html_extract.extract_page(MALFORMED_HTML, max_text_length=5000)
        self.assertIn("Unclosed paragraph", result["body_text"])

    def test_no_meta_description_is_none(self):
        result = html_extract.extract_page("<html><head><title>T</title></head><body></body></html>", max_text_length=5000)
        self.assertIsNone(result["meta_description"])


class EvidenceExtractionTests(unittest.TestCase):
    def test_commercial_hvac_evidence_found(self):
        items = evidence.extract_evidence("https://x.example/", "We specialize in commercial HVAC and rooftop unit replacement.", 200)
        categories = {i["category"] for i in items}
        self.assertIn("commercial_hvac", categories)
        self.assertIn("rtu_rooftop", categories)

    def test_commercial_service_evidence_found(self):
        items = evidence.extract_evidence("https://x.example/", "Ask about our preventive maintenance agreements.", 200)
        categories = {i["category"] for i in items}
        self.assertIn("preventive_maintenance", categories)

    def test_commercial_vertical_evidence_found(self):
        items = evidence.extract_evidence(
            "https://x.example/", "We serve warehouses, manufacturing facilities and office buildings.", 200
        )
        categories = {i["category"] for i in items}
        self.assertIn("commercial_vertical", categories)

    def test_residential_evidence_found(self):
        items = evidence.extract_evidence("https://x.example/", "Residential heating and air conditioning. Home comfort.", 200)
        categories = {i["category"] for i in items}
        self.assertIn("residential", categories)

    def test_residential_only_indicator_found(self):
        items = evidence.extract_evidence("https://x.example/", "Residential service only for homeowners.", 200)
        categories = {i["category"] for i in items}
        self.assertIn("residential_only_indicator", categories)

    def test_no_match_returns_empty(self):
        items = evidence.extract_evidence("https://x.example/", "Just a plain sentence about nothing relevant.", 200)
        self.assertEqual(items, [])

    def test_snippet_contains_keyword_context(self):
        items = evidence.extract_evidence(
            "https://x.example/", "Long preamble text. Commercial HVAC service and maintenance. Long tail text.", 200
        )
        match = next(i for i in items if i["category"] == "commercial_hvac")
        self.assertIn("commercial hvac", match["snippet"].lower())

    def test_snippet_length_capped(self):
        items = evidence.extract_evidence("https://x.example/", "commercial hvac " + ("x" * 5000), 50)
        for item in items:
            self.assertLessEqual(len(item["snippet"]), 50)

    def test_evidence_sort_is_deterministic(self):
        items = [
            {"category": "residential", "keyword": "residential", "page_url": "https://x/b", "snippet": "s"},
            {"category": "commercial_hvac", "keyword": "commercial hvac", "page_url": "https://x/a", "snippet": "s"},
        ]
        sorted_a = evidence.sort_evidence(list(items))
        sorted_b = evidence.sort_evidence(list(reversed(items)))
        self.assertEqual(sorted_a, sorted_b)
        self.assertEqual(sorted_a[0]["category"], "commercial_hvac")

    def test_dedupe_evidence_removes_exact_repeats(self):
        items = [
            {"category": "residential", "keyword": "residential", "page_url": "https://x/a", "snippet": "s1"},
            {"category": "residential", "keyword": "residential", "page_url": "https://x/a", "snippet": "s2"},
        ]
        deduped = evidence.dedupe_evidence(items)
        self.assertEqual(len(deduped), 1)
        self.assertEqual(deduped[0]["snippet"], "s1")

    def test_bare_vertical_mention_is_incidental_not_customer_vertical(self):
        # A standalone building-type word with no HVAC/heating/cooling/
        # service context in the same sentence must NOT count as real
        # customer-vertical evidence.
        items = evidence.extract_evidence(
            "https://x.example/", "Our office is located downtown near the warehouse district.", 200
        )
        vertical_items = [i for i in items if i["category"] == "commercial_vertical"]
        self.assertTrue(vertical_items)
        for item in vertical_items:
            self.assertEqual(item["evidence_type"], "incidental")

    def test_vertical_mention_with_hvac_context_is_customer_vertical(self):
        items = evidence.extract_evidence(
            "https://x.example/", "We provide commercial HVAC service for offices and warehouses.", 200
        )
        vertical_items = [i for i in items if i["category"] == "commercial_vertical"]
        self.assertTrue(vertical_items)
        for item in vertical_items:
            self.assertEqual(item["evidence_type"], "customer_vertical")

    def test_broadened_commercial_hvac_phrases_detected(self):
        for phrase in (
            "we offer hvac for businesses",
            "our business hvac division",
            "commercial heating and cooling specialists",
            "commercial property hvac maintenance",
            "packaged rooftop units installed",
            "vrf system design",
        ):
            items = evidence.extract_evidence("https://x.example/", f"We provide {phrase}.", 200)
            self.assertTrue(items, f"expected evidence for phrase: {phrase!r}")

    def test_rollup_counts_group_correctly(self):
        items = evidence.extract_evidence(
            "https://x.example/",
            "commercial hvac warehouse residential preventive maintenance",
            200,
        )
        counts = evidence.rollup_counts(items)
        self.assertGreaterEqual(counts["commercial_hvac_signals"], 1)
        self.assertGreaterEqual(counts["commercial_service_signals"], 1)
        self.assertGreaterEqual(counts["commercial_vertical_signals"], 1)
        self.assertGreaterEqual(counts["residential_signals"], 1)


class RobotsTests(unittest.TestCase):
    def test_missing_robots_txt_is_allowed(self):
        r = robots_mod.SimpleRobots(lambda url: None, "TestBot/1.0")
        self.assertTrue(r.is_allowed("https://example.com/anything"))

    def test_disallow_rule_blocks_path(self):
        robots_txt = "User-agent: *\nDisallow: /private\n"
        r = robots_mod.SimpleRobots(lambda url: robots_txt, "TestBot/1.0")
        self.assertFalse(r.is_allowed("https://example.com/private/page"))
        self.assertTrue(r.is_allowed("https://example.com/public"))

    def test_fetch_exception_fails_open(self):
        def raiser(url):
            raise RuntimeError("boom")
        r = robots_mod.SimpleRobots(raiser, "TestBot/1.0")
        self.assertTrue(r.is_allowed("https://example.com/anything"))

    def test_allow_overrides_more_specific_disallow_when_longer_match(self):
        robots_txt = "User-agent: *\nDisallow: /shop\nAllow: /shop/public\n"
        r = robots_mod.SimpleRobots(lambda url: robots_txt, "TestBot/1.0")
        self.assertFalse(r.is_allowed("https://example.com/shop/private"))
        self.assertTrue(r.is_allowed("https://example.com/shop/public"))


class CrawlAndEnrichmentTests(unittest.TestCase):
    def setUp(self):
        self.config = dict(ew.DEFAULT_CONFIG)
        self.config["max_pages_per_domain"] = 3

    def _fetcher(self, responses_by_url):
        return FakeFetcher(self.config, responses_by_url)

    def test_no_website_field_makes_no_http_call(self):
        record = make_master_record(website="")
        fetcher = self._fetcher({})
        robots = robots_mod.SimpleRobots(lambda u: None, self.config["user_agent"])
        result = ew.enrich_record(record, self.config, fetcher, robots, fixed_now)
        self.assertEqual(result["website_status"], ew.STATUS_NO_WEBSITE)
        self.assertEqual(fetcher.calls, [])

    def test_obvious_plumbing_only_business_skips_crawl(self):
        record = make_master_record(business_name="Joe's Plumbing Co", category="plumber")
        fetcher = self._fetcher({})
        robots = robots_mod.SimpleRobots(lambda u: None, self.config["user_agent"])
        result = ew.enrich_record(record, self.config, fetcher, robots, fixed_now)
        self.assertEqual(result["website_status"], ew.STATUS_SKIPPED_NON_HVAC_TRADE)
        self.assertEqual(fetcher.calls, [])

    def test_obvious_electrical_only_business_skips_crawl(self):
        record = make_master_record(business_name="Bright Spark Electricians", category="electrician")
        fetcher = self._fetcher({})
        robots = robots_mod.SimpleRobots(lambda u: None, self.config["user_agent"])
        result = ew.enrich_record(record, self.config, fetcher, robots, fixed_now)
        self.assertEqual(result["website_status"], ew.STATUS_SKIPPED_NON_HVAC_TRADE)
        self.assertEqual(fetcher.calls, [])

    def test_obvious_handyman_only_business_skips_crawl(self):
        record = make_master_record(business_name="All-Around Handyman", category="handyman")
        fetcher = self._fetcher({})
        robots = robots_mod.SimpleRobots(lambda u: None, self.config["user_agent"])
        result = ew.enrich_record(record, self.config, fetcher, robots, fixed_now)
        self.assertEqual(result["website_status"], ew.STATUS_SKIPPED_NON_HVAC_TRADE)
        self.assertEqual(fetcher.calls, [])

    def test_hvac_manufacturer_skips_crawl_even_with_hvac_in_name(self):
        record = make_master_record(business_name="Acme HVAC Manufacturing", category="HVAC equipment manufacturer")
        fetcher = self._fetcher({})
        robots = robots_mod.SimpleRobots(lambda u: None, self.config["user_agent"])
        result = ew.enrich_record(record, self.config, fetcher, robots, fixed_now)
        self.assertEqual(result["website_status"], ew.STATUS_SKIPPED_NON_HVAC_TRADE)
        self.assertEqual(fetcher.calls, [])

    def test_plumbing_company_with_hvac_in_name_is_not_skipped(self):
        # "Must not filter a business whose name contains plumbing/electrical
        # if it also has HVAC in its name/category" -- crawling proceeds
        # normally (network call happens; fixture 404s are fine here, only
        # the skip decision itself is under test).
        record = make_master_record(
            business_name="Ace Plumbing & HVAC", category="plumbing and HVAC contractor", website=""
        )
        fetcher = self._fetcher({})
        robots = robots_mod.SimpleRobots(lambda u: None, self.config["user_agent"])
        result = ew.enrich_record(record, self.config, fetcher, robots, fixed_now)
        # No website on this record, so it hits the no_website path, not the
        # pre-enrichment skip path -- proving the HVAC rescue prevented an
        # early skip decision before even reaching the website check.
        self.assertEqual(result["website_status"], ew.STATUS_NO_WEBSITE)

    def test_pre_filter_function_rescued_by_hvac_signal(self):
        self.assertIsNone(ew.is_obvious_non_hvac_trade("Ace Plumbing & HVAC", "plumbing and HVAC contractor"))

    def test_pre_filter_function_flags_plumbing_only(self):
        self.assertIsNotNone(ew.is_obvious_non_hvac_trade("Joe's Plumbing", "plumber"))

    def test_pre_filter_function_flags_hvac_distributor_unconditionally(self):
        self.assertIsNotNone(ew.is_obvious_non_hvac_trade("Metro HVAC Distributor", "HVAC wholesale distribution"))

    def test_pre_filter_function_keeps_ambiguous_business(self):
        # A generic/ambiguous business name/category (no trade keyword hit
        # at all) must never be skipped -- bias toward keeping ambiguous
        # cases so real HVAC prospects are never silently dropped.
        self.assertIsNone(ew.is_obvious_non_hvac_trade("Dallas Comfort Systems", "mechanical services"))

    def test_invalid_url_makes_no_http_call(self):
        record = make_master_record(website="not-a-url")
        fetcher = self._fetcher({})
        robots = robots_mod.SimpleRobots(lambda u: None, self.config["user_agent"])
        result = ew.enrich_record(record, self.config, fetcher, robots, fixed_now)
        self.assertEqual(result["website_status"], ew.STATUS_INVALID_URL)
        self.assertEqual(fetcher.calls, [])

    def test_successful_crawl_extracts_homepage_and_links(self):
        website = "https://acmehvac.example.com"
        responses = {
            website: fake_response(text=HOMEPAGE_HTML),
            "https://acmehvac.example.com/services": fake_response(text=SERVICES_HTML),
            "https://acmehvac.example.com/about": fake_response(text=ABOUT_HTML),
            "https://acmehvac.example.com/contact": fake_response(text="<html><body>Contact us</body></html>"),
            "https://acmehvac.example.com/robots.txt": fake_response(text=""),
        }
        record = make_master_record(website=website)
        fetcher = self._fetcher(responses)
        robots = robots_mod.SimpleRobots(lambda u: fetcher.get_text_or_none(u), self.config["user_agent"])
        result = ew.enrich_record(record, self.config, fetcher, robots, fixed_now)

        self.assertEqual(result["website_status"], ew.STATUS_SUCCESS)
        self.assertEqual(result["pages_crawled"], 3)
        self.assertIn("Acme Commercial HVAC", result["homepage_title"])
        self.assertTrue(result["evidence"])
        self.assertGreaterEqual(result["commercial_hvac_signals"], 1)

    def test_same_domain_restriction_enforced(self):
        website = "https://acmehvac.example.com"
        responses = {
            website: fake_response(text=HOMEPAGE_HTML),
        }
        record = make_master_record(website=website)
        fetcher = self._fetcher(responses)
        robots = robots_mod.SimpleRobots(lambda u: None, self.config["user_agent"])
        ew.enrich_record(record, self.config, fetcher, robots, fixed_now)
        self.assertFalse(any("external-site.example" in c for c in fetcher.calls))

    def test_page_limit_enforced(self):
        self.config["max_pages_per_domain"] = 1
        website = "https://acmehvac.example.com"
        responses = {website: fake_response(text=HOMEPAGE_HTML)}
        record = make_master_record(website=website)
        fetcher = self._fetcher(responses)
        robots = robots_mod.SimpleRobots(lambda u: None, self.config["user_agent"])
        result = ew.enrich_record(record, self.config, fetcher, robots, fixed_now)
        self.assertEqual(result["pages_crawled"], 1)

    def test_tracking_params_stripped_from_discovered_links(self):
        website = "https://acmehvac.example.com"
        responses = {
            website: fake_response(text=HOMEPAGE_HTML),
            "https://acmehvac.example.com/services": fake_response(text=SERVICES_HTML),
            "https://acmehvac.example.com/about": fake_response(text=ABOUT_HTML),
            "https://acmehvac.example.com/contact": fake_response(text="<html><body>Contact</body></html>"),
        }
        self.config["max_pages_per_domain"] = 5
        record = make_master_record(website=website)
        fetcher = self._fetcher(responses)
        robots = robots_mod.SimpleRobots(lambda u: None, self.config["user_agent"])
        ew.enrich_record(record, self.config, fetcher, robots, fixed_now)
        self.assertIn("https://acmehvac.example.com/contact", fetcher.calls)
        self.assertNotIn("https://acmehvac.example.com/contact?utm_source=google&utm_medium=cpc", fetcher.calls)

    def test_duplicate_urls_not_fetched_twice(self):
        html_with_dupe_links = (
            "<html><body><a href='/services'>Services</a>"
            "<a href='/services'>Services Again</a></body></html>"
        )
        website = "https://acmehvac.example.com"
        responses = {
            website: fake_response(text=html_with_dupe_links),
            "https://acmehvac.example.com/services": fake_response(text=SERVICES_HTML),
        }
        self.config["max_pages_per_domain"] = 5
        record = make_master_record(website=website)
        fetcher = self._fetcher(responses)
        robots = robots_mod.SimpleRobots(lambda u: None, self.config["user_agent"])
        ew.enrich_record(record, self.config, fetcher, robots, fixed_now)
        self.assertEqual(fetcher.calls.count("https://acmehvac.example.com/services"), 1)

    def test_http_error_status_recorded(self):
        website = "https://brokenhvac.example.com"
        responses = {website: fake_response(status_code=500, text="")}
        record = make_master_record(website=website)
        fetcher = self._fetcher(responses)
        robots = robots_mod.SimpleRobots(lambda u: None, self.config["user_agent"])
        result = ew.enrich_record(record, self.config, fetcher, robots, fixed_now)
        self.assertEqual(result["website_status"], ew.STATUS_HTTP_ERROR)
        self.assertEqual(result["http_status"], 500)
        self.assertEqual(result["pages_crawled"], 0)

    def test_blocked_status_for_403(self):
        website = "https://blockedhvac.example.com"
        responses = {website: fake_response(status_code=403, text="")}
        record = make_master_record(website=website)
        fetcher = self._fetcher(responses)
        robots = robots_mod.SimpleRobots(lambda u: None, self.config["user_agent"])
        result = ew.enrich_record(record, self.config, fetcher, robots, fixed_now)
        self.assertEqual(result["website_status"], ew.STATUS_BLOCKED)

    def test_timeout_handling(self):
        website = "https://slowhvac.example.com"
        responses = {website: TimeoutError("timed out")}
        record = make_master_record(website=website)
        fetcher = self._fetcher(responses)
        robots = robots_mod.SimpleRobots(lambda u: None, self.config["user_agent"])
        result = ew.enrich_record(record, self.config, fetcher, robots, fixed_now)
        self.assertEqual(result["website_status"], ew.STATUS_TIMEOUT)

    def test_connection_error_handling(self):
        website = "https://unreachablehvac.example.com"
        responses = {website: ConnectionError("refused")}
        record = make_master_record(website=website)
        fetcher = self._fetcher(responses)
        robots = robots_mod.SimpleRobots(lambda u: None, self.config["user_agent"])
        result = ew.enrich_record(record, self.config, fetcher, robots, fixed_now)
        self.assertEqual(result["website_status"], ew.STATUS_CONNECTION_ERROR)

    def test_non_html_content_type_handled(self):
        website = "https://pdfhvac.example.com"
        responses = {website: fake_response(text="%PDF-1.4", content_type="application/pdf")}
        record = make_master_record(website=website)
        fetcher = self._fetcher(responses)
        robots = robots_mod.SimpleRobots(lambda u: None, self.config["user_agent"])
        result = ew.enrich_record(record, self.config, fetcher, robots, fixed_now)
        self.assertEqual(result["website_status"], ew.STATUS_NON_HTML)

    def test_partial_crawl_when_a_subpage_fails(self):
        website = "https://acmehvac.example.com"
        responses = {
            website: fake_response(text=HOMEPAGE_HTML),
            "https://acmehvac.example.com/services": fake_response(status_code=500, text=""),
            "https://acmehvac.example.com/about": fake_response(text=ABOUT_HTML),
        }
        record = make_master_record(website=website)
        fetcher = self._fetcher(responses)
        robots = robots_mod.SimpleRobots(lambda u: None, self.config["user_agent"])
        result = ew.enrich_record(record, self.config, fetcher, robots, fixed_now)
        self.assertEqual(result["website_status"], ew.STATUS_PARTIAL)
        self.assertGreaterEqual(result["pages_failed"], 1)
        self.assertGreaterEqual(result["pages_crawled"], 1)

    def test_broken_page_does_not_crash_batch(self):
        good_record = make_master_record(master_id="id1", website="https://good.example.com")
        bad_record = make_master_record(master_id="id2", website="https://bad.example.com")
        responses = {
            "https://good.example.com": fake_response(text=HOMEPAGE_HTML),
            "https://good.example.com/services": fake_response(text=SERVICES_HTML),
            "https://good.example.com/about": fake_response(text=ABOUT_HTML),
            "https://good.example.com/contact": fake_response(text="<html><body>Contact us</body></html>"),
            "https://bad.example.com": RuntimeError("weird failure"),
        }
        fetcher = self._fetcher(responses)
        results = ew.enrich_all([good_record, bad_record], self.config, fetcher, now_fn=fixed_now)
        statuses = {r["master_id"]: r["website_status"] for r in results}
        self.assertEqual(statuses["id1"], ew.STATUS_SUCCESS)
        self.assertEqual(statuses["id2"], ew.STATUS_FAILED)

    def test_malformed_html_does_not_crash_crawl(self):
        website = "https://brokenmarkup.example.com"
        responses = {website: fake_response(text=MALFORMED_HTML)}
        record = make_master_record(website=website)
        fetcher = self._fetcher(responses)
        robots = robots_mod.SimpleRobots(lambda u: None, self.config["user_agent"])
        result = ew.enrich_record(record, self.config, fetcher, robots, fixed_now)
        self.assertIn(result["website_status"], (ew.STATUS_SUCCESS, ew.STATUS_PARTIAL))

    def test_residential_only_evidence_collected(self):
        website = "https://homehvac.example.com"
        responses = {website: fake_response(text=RESIDENTIAL_HTML)}
        record = make_master_record(website=website)
        fetcher = self._fetcher(responses)
        robots = robots_mod.SimpleRobots(lambda u: None, self.config["user_agent"])
        result = ew.enrich_record(record, self.config, fetcher, robots, fixed_now)
        categories = {e["category"] for e in result["evidence"]}
        self.assertIn("residential", categories)
        self.assertIn("residential_only_indicator", categories)
        self.assertGreaterEqual(result["residential_signals"], 1)

    def test_deterministic_evidence_ordering_across_runs(self):
        website = "https://acmehvac.example.com"
        responses = {
            website: fake_response(text=HOMEPAGE_HTML),
            "https://acmehvac.example.com/services": fake_response(text=SERVICES_HTML),
            "https://acmehvac.example.com/about": fake_response(text=ABOUT_HTML),
        }
        record = make_master_record(website=website)

        fetcher_a = self._fetcher(responses)
        robots_a = robots_mod.SimpleRobots(lambda u: None, self.config["user_agent"])
        result_a = ew.enrich_record(copy.deepcopy(record), self.config, fetcher_a, robots_a, fixed_now)

        fetcher_b = self._fetcher(responses)
        robots_b = robots_mod.SimpleRobots(lambda u: None, self.config["user_agent"])
        result_b = ew.enrich_record(copy.deepcopy(record), self.config, fetcher_b, robots_b, fixed_now)

        self.assertEqual(
            json.dumps(result_a["evidence"], sort_keys=True),
            json.dumps(result_b["evidence"], sort_keys=True),
        )

    def test_robots_disallow_skips_page(self):
        website = "https://robothvac.example.com"
        robots_txt = "User-agent: *\nDisallow: /services\n"
        responses = {
            website: fake_response(text=HOMEPAGE_HTML),
            "https://robothvac.example.com/services": fake_response(text=SERVICES_HTML),
            "https://robothvac.example.com/about": fake_response(text=ABOUT_HTML),
            "https://robothvac.example.com/robots.txt": fake_response(text=robots_txt),
        }
        self.config["max_pages_per_domain"] = 5
        record = make_master_record(website=website)
        fetcher = self._fetcher(responses)
        robots = robots_mod.SimpleRobots(lambda u: fetcher.get_text_or_none(u), self.config["user_agent"])
        result = ew.enrich_record(record, self.config, fetcher, robots, fixed_now)
        crawled_urls = [p for p in fetcher.calls if p.endswith("/services")]
        self.assertEqual(crawled_urls, [])
        self.assertNotEqual(result["website_status"], ew.STATUS_FAILED)


class OutputAndSchemaTests(unittest.TestCase):
    def setUp(self):
        self.config = dict(ew.DEFAULT_CONFIG)
        self.config["max_pages_per_domain"] = 2

    def test_output_schema_shape(self):
        record = make_master_record(website="")
        fetcher = FakeFetcher(self.config, {})
        results = ew.enrich_all([record], self.config, fetcher, now_fn=fixed_now)
        for field in ew.OUTPUT_FIELDS:
            self.assertIn(field, results[0])

    def test_run_writes_json_and_csv_without_touching_master_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            master_dir = os.path.join(tmp, "master")
            out_dir = os.path.join(tmp, "enrichment")
            os.makedirs(master_dir)
            master_json_path = os.path.join(master_dir, "master.json")
            records = [make_master_record(master_id="id1", website="")]
            with open(master_json_path, "w", encoding="utf-8") as f:
                json.dump(records, f, indent=2)
            original_bytes = open(master_json_path, "rb").read()

            import unittest.mock as mock

            class NoNetFetcher(ew.Fetcher):
                def __init__(self, config):
                    self.config = config

                def get(self, url):
                    raise AssertionError("no HTTP call expected for a website-less record")

                def get_text_or_none(self, url):
                    return None

            with mock.patch.object(ew, "Fetcher", NoNetFetcher):
                stats = ew.run(master_json_path, out_dir, self.config, now_fn=fixed_now)

            self.assertEqual(open(master_json_path, "rb").read(), original_bytes)
            self.assertTrue(os.path.exists(stats["artifacts"]["enrichment_json"]))
            self.assertTrue(os.path.exists(stats["artifacts"]["enrichment_csv"]))
            self.assertEqual(stats["records_seen"], 1)
            self.assertEqual(stats["no_website"], 1)

    def test_csv_has_expected_header(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = os.path.join(tmp, "enrichment")
            results = [ew.enrich_record(make_master_record(website=""), self.config, FakeFetcher(self.config, {}), robots_mod.SimpleRobots(lambda u: None, "bot"), fixed_now)]
            _, csv_path = ew.write_outputs(results, out_dir, {})
            with open(csv_path, encoding="utf-8") as f:
                reader = csv.DictReader(f)
                self.assertEqual(reader.fieldnames, ew.OUTPUT_FIELDS)

    def test_limit_restricts_records_processed(self):
        records = [make_master_record(master_id=f"id{i}", website="") for i in range(5)]
        fetcher = FakeFetcher(self.config, {})
        results = ew.enrich_all(records, self.config, fetcher, now_fn=fixed_now, limit=2)
        self.assertEqual(len(results), 2)

    def test_stats_counts_no_website_and_invalid_url(self):
        records = [
            make_master_record(master_id="id1", website=""),
            make_master_record(master_id="id2", website="not-a-url"),
        ]
        fetcher = FakeFetcher(self.config, {})
        results = ew.enrich_all(records, self.config, fetcher, now_fn=fixed_now)
        stats = ew.compute_stats(records, results)
        self.assertEqual(stats["no_website"], 1)
        self.assertEqual(stats["invalid_url"], 1)

    def test_result_order_independent_of_input_order(self):
        records = [
            make_master_record(master_id="bbb", website=""),
            make_master_record(master_id="aaa", website=""),
        ]
        fetcher = FakeFetcher(self.config, {})
        results = ew.enrich_all(records, self.config, fetcher, now_fn=fixed_now)
        self.assertEqual([r["master_id"] for r in results], ["aaa", "bbb"])


INCIDENTAL_TEXT = (
    "We install commercial hvac products from leading manufacturers, and we "
    "carry chiller and boiler replacement parts for the trade. We sell rooftop "
    "unit components from top brands."
)


class ScraplingIntegrationTests(unittest.TestCase):
    """Confirms the fetch/parse layer really is Scrapling (offline)."""

    def test_parse_html_returns_scrapling_selector(self):
        selector = scrapling_client.parse_html(HOMEPAGE_HTML, "https://acmehvac.example.com/")
        self.assertIsInstance(selector, scrapling_client.get_selector_class())
        self.assertIn("Acme Commercial HVAC", str(selector.css("title::text")[0]))

    def test_extract_page_accepts_a_parsed_scrapling_response(self):
        response = fake_response(text=SERVICES_HTML, url="https://acmehvac.example.com/services")
        extracted = html_extract.extract_page(response, max_text_length=5000)
        self.assertIn("Our Services", extracted["title"])
        self.assertIn("Design Build Mechanical Services", extracted["headings"])

    def test_scrapling_fetcher_never_uses_stealth_or_proxies(self):
        client = scrapling_client.ScraplingFetcher(timeout=7, user_agent="MetrivioBot/1.0")
        self.assertEqual(client.headers(), {"User-Agent": "MetrivioBot/1.0"})
        self.assertEqual(client.timeout, 7)
        source_path = os.path.join(
            os.path.dirname(__file__), "..", "scripts", "enrichment", "scrapling_client.py"
        )
        with open(source_path, encoding="utf-8") as f:
            source = f.read()
        self.assertNotIn("StealthyFetcher(", source)
        self.assertNotIn("proxy=", source)
        self.assertIn("stealthy_headers=False", source)

    def test_boilerplate_nav_and_footer_text_excluded_from_body_text(self):
        extracted = html_extract.extract_page(HOMEPAGE_HTML, max_text_length=20000)
        self.assertNotIn("Licensed and insured", extracted["body_text"])
        self.assertIn("rooftop units", extracted["body_text"])


class EvidenceTypeTests(unittest.TestCase):
    def test_direct_service_evidence_labelled(self):
        items = evidence.extract_evidence(
            "https://x.example/",
            "We provide commercial HVAC maintenance and RTU replacement.",
            220,
        )
        match = next(i for i in items if i["category"] == "commercial_hvac")
        self.assertEqual(match["evidence_type"], "direct_service")

    def test_incidental_product_mention_labelled(self):
        items = evidence.extract_evidence(
            "https://x.example/",
            "We install commercial hvac products from leading manufacturers.",
            220,
        )
        match = next(i for i in items if i["category"] == "commercial_hvac")
        self.assertEqual(match["evidence_type"], "incidental")

    def test_customer_vertical_labelled(self):
        # A vertical/building-type word only counts as real customer_vertical
        # evidence when it co-occurs, in the same sentence, with an
        # HVAC/heating/cooling/service context word -- see vocabulary.py's
        # VERTICAL_CONTEXT_MARKERS and evidence.py:classify_evidence_type.
        items = evidence.extract_evidence(
            "https://x.example/", "We provide HVAC service for warehouses and office buildings.", 220
        )
        match = next(i for i in items if i["category"] == "commercial_vertical")
        self.assertEqual(match["evidence_type"], "customer_vertical")

    def test_customer_vertical_without_hvac_context_is_incidental(self):
        # A bare/standalone building-type mention with no HVAC context in
        # the same sentence must NOT count as real commercial evidence.
        items = evidence.extract_evidence(
            "https://x.example/", "We serve warehouses and office buildings.", 220
        )
        match = next(i for i in items if i["category"] == "commercial_vertical")
        self.assertEqual(match["evidence_type"], "incidental")

    def test_residential_labelled(self):
        items = evidence.extract_evidence(
            "https://x.example/", "Residential service only for homeowners.", 220
        )
        self.assertTrue(items)
        self.assertTrue(all(i["evidence_type"] == "residential" for i in items))

    def test_every_item_has_full_evidence_shape(self):
        items = evidence.extract_evidence(
            "https://x.example/", "We provide commercial HVAC maintenance.", 220
        )
        self.assertTrue(items)
        for item in items:
            self.assertEqual(
                sorted(item),
                ["category", "evidence_type", "keyword", "page_url", "snippet"],
            )
            self.assertIn(item["evidence_type"], evidence.EVIDENCE_TYPES)

    def test_incidental_only_site_yields_no_direct_service_evidence(self):
        items = evidence.extract_evidence("https://x.example/", INCIDENTAL_TEXT, 220)
        self.assertTrue(items)
        self.assertNotIn("direct_service", {i["evidence_type"] for i in items})


class CliAndDeterminismTests(unittest.TestCase):
    def setUp(self):
        self.config = dict(ew.DEFAULT_CONFIG)
        self.config["max_pages_per_domain"] = 3
        self.responses = {
            "https://acmehvac.example.com": fake_response(text=HOMEPAGE_HTML),
            "https://acmehvac.example.com/services": fake_response(text=SERVICES_HTML),
            "https://acmehvac.example.com/about": fake_response(text=ABOUT_HTML),
        }

    def test_final_url_and_pages_attempted_recorded(self):
        record = make_master_record(website="https://acmehvac.example.com")
        fetcher = FakeFetcher(self.config, self.responses)
        robots = robots_mod.SimpleRobots(lambda u: None, self.config["user_agent"])
        result = ew.enrich_record(record, self.config, fetcher, robots, fixed_now)
        self.assertEqual(result["final_url"], "https://acmehvac.example.com")
        self.assertGreaterEqual(result["pages_attempted"], result["pages_crawled"])

    def test_blocked_homepage_stops_crawling_that_domain(self):
        website = "https://blocked.example.com"
        fetcher = FakeFetcher(
            self.config, {website: fake_response(status_code=403, text="forbidden")}
        )
        robots = robots_mod.SimpleRobots(lambda u: None, self.config["user_agent"])
        result = ew.enrich_record(
            make_master_record(website=website), self.config, fetcher, robots, fixed_now
        )
        self.assertEqual(result["website_status"], ew.STATUS_BLOCKED)
        self.assertEqual(fetcher.calls, [website])

    def _run_once(self, tmp):
        master_json_path = os.path.join(tmp, "master.json")
        with open(master_json_path, "w", encoding="utf-8") as f:
            json.dump([make_master_record(website="https://acmehvac.example.com")], f)
        out_dir = os.path.join(tmp, "enrichment")
        ew.run(
            master_json_path,
            out_dir,
            self.config,
            now_fn=fixed_now,
            fetcher=FakeFetcher(self.config, self.responses),
        )
        return out_dir

    def test_run_twice_produces_identical_output_files(self):
        outputs = []
        for _ in range(2):
            with tempfile.TemporaryDirectory() as tmp:
                out_dir = self._run_once(tmp)
                with open(os.path.join(out_dir, "website_enrichment.json"), encoding="utf-8") as f:
                    outputs.append(f.read())
        self.assertEqual(outputs[0], outputs[1])

    def test_output_json_has_stats_block_and_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = self._run_once(tmp)
            with open(os.path.join(out_dir, "website_enrichment.json"), encoding="utf-8") as f:
                payload = json.load(f)
        self.assertIn("stats", payload)
        self.assertIn("records", payload)
        for key in (
            "records_seen",
            "records_with_websites",
            "no_website",
            "successful",
            "partial",
            "blocked",
            "failed",
            "pages_attempted",
            "pages_successful",
            "pages_failed",
        ):
            self.assertIn(key, payload["stats"])

    def test_cli_main_never_writes_to_master_json(self):
        import unittest.mock as mock

        responses = self.responses

        class NoNetFetcher(ew.Fetcher):
            def __init__(self, config, client=None):
                self.config = config
                self.client = None
                self.calls = []

            def get(self, url):
                self.calls.append(url)
                outcome = responses.get(url)
                if outcome is None:
                    raise ConnectionError(url)
                return fake_response(
                    status_code=outcome.status, text=ew.response_text(outcome), url=url
                )

            def get_text_or_none(self, url):
                return None

        with tempfile.TemporaryDirectory() as tmp:
            master_json_path = os.path.join(tmp, "master.json")
            with open(master_json_path, "w", encoding="utf-8") as f:
                json.dump([make_master_record(website="https://acmehvac.example.com")], f)
            with open(master_json_path, "rb") as f:
                original_bytes = f.read()
            original_mtime = os.path.getmtime(master_json_path)
            out_dir = os.path.join(tmp, "enrichment")

            with mock.patch.object(ew, "Fetcher", NoNetFetcher):
                ew.main(
                    ["--master-json", master_json_path, "--out-dir", out_dir, "--max-pages", "3"]
                )

            with open(master_json_path, "rb") as f:
                self.assertEqual(f.read(), original_bytes)
            self.assertEqual(os.path.getmtime(master_json_path), original_mtime)
            self.assertTrue(os.path.exists(os.path.join(out_dir, "website_enrichment.csv")))



if __name__ == "__main__":
    unittest.main()
