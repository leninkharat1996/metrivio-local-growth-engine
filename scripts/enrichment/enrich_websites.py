#!/usr/bin/env python3
"""
Step 9 -- Website Enrichment (evidence collection, NOT ICP qualification)

Reads the persistent master prospect store (data/master/master.json,
produced by scripts/google_maps_scraper/update_master.py) READ-ONLY and,
for every record that already has a website field, crawls the homepage
plus a small number of same-domain internal pages to collect factual,
publicly-visible website evidence: page titles/descriptions, headings,
body text (capped), and deterministic keyword matches against a fixed
HVAC/commercial-services vocabulary (scripts/enrichment/vocabulary.py).

This is explicitly an EVIDENCE-COLLECTION layer, matching the existing
preliminary ICP layer's philosophy (config/icp_rules.json, scripts/icp/
qualify.py):
- No AI/LLM calls, no paid APIs.
- No scoring, tiering, or final ICP decisions -- that is a later,
  not-yet-built layer that will consume this evidence.
- No email/phone/individual-name/LinkedIn/social-profile extraction.
- The master store is never mutated; output is written to a separate
  generated dataset (default: data/enrichment/).
- Deterministic: same input + config -> same output (aside from the
  crawl timestamps, which can be pinned via --crawled-at for byte-
  identical reruns in tests).

Usage:
    python3 scripts/enrichment/enrich_websites.py \
        --master-json data/master/master.json \
        --out-dir data/enrichment \
        [--config path/to/config.json] \
        [--max-pages 5] [--limit 50]
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from datetime import datetime, timezone

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from evidence import dedupe_evidence, extract_evidence, rollup_counts, sort_evidence
    from html_extract import extract_page
    from robots import SimpleRobots
    from scrapling_client import ScraplingFetcher
    from url_utils import domain_of, is_valid_url, normalize_url, resolve_link, same_domain
    from vocabulary import (
        LINK_PRIORITY_KEYWORDS,
        PRE_ENRICHMENT_HVAC_RESCUE_KEYWORDS,
        PRE_ENRICHMENT_NON_CONTRACTOR_KEYWORDS,
        PRE_ENRICHMENT_SINGLE_TRADE_KEYWORDS,
    )
else:
    from .evidence import dedupe_evidence, extract_evidence, rollup_counts, sort_evidence
    from .html_extract import extract_page
    from .robots import SimpleRobots
    from .scrapling_client import ScraplingFetcher
    from .url_utils import domain_of, is_valid_url, normalize_url, resolve_link, same_domain
    from .vocabulary import (
        LINK_PRIORITY_KEYWORDS,
        PRE_ENRICHMENT_HVAC_RESCUE_KEYWORDS,
        PRE_ENRICHMENT_NON_CONTRACTOR_KEYWORDS,
        PRE_ENRICHMENT_SINGLE_TRADE_KEYWORDS,
    )


DEFAULT_CONFIG = {
    "max_pages_per_domain": 5,
    "request_timeout": 10,
    "user_agent": "MetrivioWebsiteEnrichmentBot/1.0 (+https://github.com/leninkharat1996/metrivio-local-growth-engine)",
    "max_text_length": 20000,
    "max_snippet_length": 220,
    "allowed_schemes": ["http", "https"],
    "respect_robots_txt": True,
}

STATUS_SUCCESS = "success"
STATUS_PARTIAL = "partial"
STATUS_NO_WEBSITE = "no_website"
STATUS_INVALID_URL = "invalid_url"
STATUS_BLOCKED = "blocked"
STATUS_TIMEOUT = "timeout"
STATUS_HTTP_ERROR = "http_error"
STATUS_NON_HTML = "non_html"
STATUS_CONNECTION_ERROR = "connection_error"
STATUS_FAILED = "failed"
STATUS_SKIPPED_NON_HVAC_TRADE = "skipped_non_hvac_trade"


def is_obvious_non_hvac_trade(business_name: str | None, category: str | None) -> str | None:
    """Deterministic, keyword-only pre-enrichment filter (Google Maps
    name/category text ONLY -- no website evidence exists yet at this
    point). Returns a short reason string for an OBVIOUS non-HVAC business
    that should never be crawled, or None to proceed with crawling as
    normal. Mirrors config/icp_rules.json's hard_exclusions philosophy so
    excluded/skipped businesses stay consistent across the pipeline.

    Deliberately narrow and biased toward keeping ambiguous cases: a false
    negative (crawling a non-HVAC business) only wastes some crawl budget,
    while a false positive (skipping a real HVAC business) would silently
    drop a prospect -- so every single-trade match is rescued by any HVAC
    signal in the same text, and only the clearly-never-a-contractor
    manufacturer/distributor/supply/directory/association keywords skip
    unconditionally.
    """
    text = f"{business_name or ''} {category or ''}".lower()
    if not text.strip():
        return None

    non_contractor_hit = next(
        (kw for kw in PRE_ENRICHMENT_NON_CONTRACTOR_KEYWORDS if kw in text), None
    )
    if non_contractor_hit:
        return f"manufacturer/distributor/supply/directory keyword '{non_contractor_hit}' matched"

    has_hvac_signal = any(kw in text for kw in PRE_ENRICHMENT_HVAC_RESCUE_KEYWORDS)
    if has_hvac_signal:
        return None

    for trade_label, keywords in PRE_ENRICHMENT_SINGLE_TRADE_KEYWORDS.items():
        matched = next((kw for kw in keywords if kw in text), None)
        if matched:
            return f"{trade_label} keyword '{matched}' matched with no HVAC signal present"

    return None

OUTPUT_FIELDS = [
    "master_id",
    "business_name",
    "website",
    "website_status",
    "http_status",
    "final_url",
    "crawl_started_at",
    "crawl_completed_at",
    "pages_attempted",
    "pages_crawled",
    "pages_failed",
    "homepage_title",
    "homepage_description",
    "commercial_hvac_signals",
    "commercial_service_signals",
    "commercial_vertical_signals",
    "residential_signals",
    "evidence",
    "crawl_errors",
]


def load_config(path: str | None) -> dict:
    config = dict(DEFAULT_CONFIG)
    if path:
        with open(path, encoding="utf-8") as f:
            overrides = json.load(f)
        config.update({k: v for k, v in overrides.items() if k in DEFAULT_CONFIG})
    return config


def load_master(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, list) else data.get("results", [])


class Fetcher:
    """The crawler's single HTTP call site, backed by Scrapling.

    Wraps scripts/enrichment/scrapling_client.ScraplingFetcher (which calls
    Scrapling's plain `Fetcher.get`) so tests can substitute a fake fetch
    implementation and never touch the network. Only Scrapling's ordinary
    HTTP fetch is used -- never StealthyFetcher or any anti-bot/stealth/
    proxy feature.
    """

    def __init__(self, config: dict, client=None):
        self.config = config
        self.client = client or ScraplingFetcher(
            timeout=config["request_timeout"],
            user_agent=config["user_agent"],
        )

    def get(self, url: str):
        """Return Scrapling's Response (itself a parsed Selector), or raise.
        Callers are expected to catch every exception."""
        return self.client.get(url)

    def get_text_or_none(self, url: str) -> str | None:
        """Fetch a plain-text resource (robots.txt) or return None."""
        try:
            resp = self.get(url)
        except Exception:
            return None
        if getattr(resp, "status", 0) >= 400:
            return None
        return response_text(resp)


def response_text(resp) -> str:
    """Raw text of a Scrapling Response (its undecoded body, decoded once)."""
    body = getattr(resp, "body", None)
    if isinstance(body, bytes):
        return body.decode(getattr(resp, "encoding", None) or "utf-8", "replace")
    if isinstance(body, str):
        return body
    return str(getattr(resp, "html_content", "") or "")


def _is_timeout_error(exc: Exception) -> bool:
    name = type(exc).__name__.lower()
    return "timeout" in name or "timeout" in str(exc).lower()


def _is_connection_error(exc: Exception) -> bool:
    name = type(exc).__name__.lower()
    text = str(exc).lower()
    return (
        "connection" in name
        or "connect" in text
        or "dns" in text
        or "resolve" in text
        or "ssl" in name
    )


def classify_and_fetch(fetcher: Fetcher, url: str) -> dict:
    """Fetch one URL through Scrapling and classify the outcome.

    Never raises -- every fetch exception is caught and turned into a
    status + error string so a single broken page never crashes the batch.
    A blocked/forbidden response is recorded as `blocked` and crawling of
    that domain stops; we never retry it with a stealth/bypass technique.
    """
    try:
        resp = fetcher.get(url)
    except Exception as exc:
        if _is_timeout_error(exc):
            return {"status": STATUS_TIMEOUT, "error": f"timeout fetching {url}", "http_status": None}
        if _is_connection_error(exc):
            return {"status": STATUS_CONNECTION_ERROR, "error": f"connection error fetching {url}", "http_status": None}
        return {"status": STATUS_FAILED, "error": f"request error fetching {url}: {exc}", "http_status": None}

    http_status = getattr(resp, "status", None)
    if http_status is None:
        return {"status": STATUS_FAILED, "error": f"no HTTP status returned for {url}", "http_status": None}
    if http_status in (401, 403, 429):
        return {"status": STATUS_BLOCKED, "error": f"blocked (HTTP {http_status}) fetching {url}", "http_status": http_status}
    if http_status >= 400:
        return {"status": STATUS_HTTP_ERROR, "error": f"HTTP {http_status} fetching {url}", "http_status": http_status}

    headers = getattr(resp, "headers", None) or {}
    content_type = ""
    for key in ("Content-Type", "content-type"):
        value = headers.get(key) if hasattr(headers, "get") else None
        if value:
            content_type = str(value)
            break
    if content_type and "html" not in content_type.lower():
        return {
            "status": STATUS_NON_HTML,
            "error": f"non-HTML content-type '{content_type}' at {url}",
            "http_status": http_status,
            "content_type": content_type,
        }

    final_url = getattr(resp, "url", None)
    return {
        "status": STATUS_SUCCESS,
        "error": None,
        "http_status": http_status,
        "content_type": content_type,
        # Scrapling's Response IS its parsed Selector -- pass it straight to
        # the extractor instead of re-parsing the HTML.
        "page": resp,
        "final_url": normalize_url(final_url) if final_url else url,
    }


def _prioritize_links(links: list[dict], base_url: str, visited: set) -> list[str]:
    """Resolve, dedupe, and same-domain-filter discovered links, ordering
    ones whose href/text look commercially relevant first (deterministic
    tie-break: alphabetical URL)."""
    candidates: dict[str, str] = {}
    for link in links:
        resolved = resolve_link(base_url, link.get("href", ""))
        if not resolved:
            continue
        if not same_domain(resolved, base_url):
            continue
        if resolved in visited or resolved in candidates:
            continue
        candidates[resolved] = (link.get("text") or "").lower()

    def _priority(url: str) -> tuple:
        haystack = f"{url.lower()} {candidates[url]}"
        matched = any(kw in haystack for kw in LINK_PRIORITY_KEYWORDS)
        return (0 if matched else 1, url)

    return sorted(candidates.keys(), key=_priority)


def crawl_website(website: str, config: dict, fetcher: Fetcher, robots: SimpleRobots) -> dict:
    """Crawl homepage + up to max_pages_per_domain-1 same-domain internal
    pages. Returns a dict with status, pages (list of extracted page
    dicts), errors, and http_status of the homepage fetch."""
    max_pages = max(1, config["max_pages_per_domain"])
    start_url = normalize_url(website)

    to_visit = [start_url]
    visited: set = set()
    pages: list[dict] = []
    errors: list[str] = []
    homepage_result: dict | None = None

    while to_visit and len(pages) < max_pages:
        url = to_visit.pop(0)
        if url in visited:
            continue
        visited.add(url)

        if config.get("respect_robots_txt", True) and not robots.is_allowed(url):
            errors.append(f"robots.txt disallows {url}")
            continue

        result = classify_and_fetch(fetcher, url)
        if homepage_result is None:
            homepage_result = result

        if result["status"] != STATUS_SUCCESS:
            errors.append(result["error"])
            if result["status"] == STATUS_BLOCKED:
                # Blocked/forbidden/rate-limited: stop crawling this domain
                # entirely. We never escalate to a bypass/stealth technique.
                break
            continue

        extracted = extract_page(result["page"], config["max_text_length"], url)
        page_record = {
            "url": url,
            "final_url": result.get("final_url") or url,
            "http_status": result["http_status"],
            "content_type": result.get("content_type"),
            "title": extracted["title"],
            "meta_description": extracted["meta_description"],
            "body_text": extracted["body_text"],
            "headings": extracted["headings"],
        }
        pages.append(page_record)

        if len(pages) < max_pages:
            new_links = _prioritize_links(extracted["links"], url, visited | set(to_visit))
            to_visit.extend(new_links)

    if homepage_result is None:
        overall_status = STATUS_FAILED
        http_status = None
        final_url = None
    elif pages and homepage_result["status"] == STATUS_SUCCESS:
        overall_status = STATUS_SUCCESS if not errors else STATUS_PARTIAL
        http_status = homepage_result["http_status"]
        final_url = homepage_result.get("final_url")
    else:
        overall_status = homepage_result["status"]
        http_status = homepage_result.get("http_status")
        final_url = homepage_result.get("final_url")

    return {
        "status": overall_status,
        "http_status": http_status,
        "final_url": final_url,
        "pages": pages,
        "pages_attempted": len(visited),
        "errors": errors,
    }


def enrich_record(record: dict, config: dict, fetcher: Fetcher, robots: SimpleRobots, now_fn) -> dict:
    master_id = record.get("master_id")
    business_name = record.get("business_name")
    website = (record.get("website") or "").strip()

    started_at = now_fn()

    base = {
        "master_id": master_id,
        "business_name": business_name,
        "website": website or None,
        "crawl_started_at": started_at,
        "crawl_completed_at": None,
        "final_url": None,
        "pages_attempted": 0,
        "pages_crawled": 0,
        "pages_failed": 0,
        "homepage_title": None,
        "homepage_description": None,
        "commercial_hvac_signals": 0,
        "commercial_service_signals": 0,
        "commercial_vertical_signals": 0,
        "residential_signals": 0,
        "evidence": [],
        "crawl_errors": [],
    }

    if not website:
        base.update({
            "website_status": STATUS_NO_WEBSITE,
            "http_status": None,
            "crawl_completed_at": started_at,
        })
        return base

    skip_reason = is_obvious_non_hvac_trade(business_name, record.get("category"))
    if skip_reason:
        base.update({
            "website_status": STATUS_SKIPPED_NON_HVAC_TRADE,
            "http_status": None,
            "crawl_completed_at": started_at,
            "crawl_errors": [f"pre-enrichment filter: {skip_reason} -- website not crawled"],
        })
        return base

    if not is_valid_url(website, tuple(config["allowed_schemes"])):
        base.update({
            "website_status": STATUS_INVALID_URL,
            "http_status": None,
            "crawl_completed_at": started_at,
            "crawl_errors": [f"invalid or unsupported URL scheme: {website!r}"],
        })
        return base

    crawl = crawl_website(website, config, fetcher, robots)
    completed_at = now_fn()

    all_evidence: list[dict] = []
    for page in crawl["pages"]:
        page_text_sources = [page["title"] or "", page["meta_description"] or "", page["body_text"] or ""]
        page_text_sources.extend(page["headings"] or [])
        combined_text = " \n ".join(t for t in page_text_sources if t)
        all_evidence.extend(extract_evidence(page["url"], combined_text, config["max_snippet_length"]))

    all_evidence = sort_evidence(dedupe_evidence(all_evidence))
    counts = rollup_counts(all_evidence)

    homepage = crawl["pages"][0] if crawl["pages"] else None

    base.update({
        "website": normalize_url(website),
        "website_status": crawl["status"],
        "http_status": crawl["http_status"],
        "final_url": crawl.get("final_url"),
        "crawl_completed_at": completed_at,
        "pages_attempted": crawl.get("pages_attempted", 0),
        "pages_crawled": len(crawl["pages"]),
        "pages_failed": len(crawl["errors"]),
        "homepage_title": homepage["title"] if homepage else None,
        "homepage_description": homepage["meta_description"] if homepage else None,
        "commercial_hvac_signals": counts["commercial_hvac_signals"],
        "commercial_service_signals": counts["commercial_service_signals"],
        "commercial_vertical_signals": counts["commercial_vertical_signals"],
        "residential_signals": counts["residential_signals"],
        "evidence": all_evidence,
        "crawl_errors": crawl["errors"],
    })
    return base


def enrich_all(records: list[dict], config: dict, fetcher: Fetcher, now_fn=None, limit: int | None = None) -> list[dict]:
    now_fn = now_fn or (lambda: datetime.now(timezone.utc).isoformat())
    robots = SimpleRobots(lambda url: fetcher.get_text_or_none(url), config["user_agent"])

    to_process = records[:limit] if limit else records
    results = [enrich_record(record, config, fetcher, robots, now_fn) for record in to_process]
    results.sort(key=lambda r: (r.get("master_id") or ""))
    return results


def write_outputs(results: list[dict], out_dir: str, stats: dict | None = None) -> tuple[str, str]:
    os.makedirs(out_dir, exist_ok=True)
    json_path = os.path.join(out_dir, "website_enrichment.json")
    csv_path = os.path.join(out_dir, "website_enrichment.csv")

    payload = {"stats": stats or {}, "records": results}
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in results:
            flat = dict(row)
            flat["evidence"] = json.dumps(row.get("evidence") or [], ensure_ascii=False)
            flat["crawl_errors"] = " | ".join(row.get("crawl_errors") or [])
            writer.writerow({k: ("" if flat.get(k) is None else flat.get(k)) for k in OUTPUT_FIELDS})

    return json_path, csv_path


def compute_stats(records: list[dict], results: list[dict]) -> dict:
    records_with_websites = sum(1 for r in records if (r.get("website") or "").strip())
    status_counts: dict[str, int] = {}
    for row in results:
        status_counts[row["website_status"]] = status_counts.get(row["website_status"], 0) + 1

    pages_attempted = sum(row.get("pages_attempted", 0) for row in results)
    pages_successful = sum(row["pages_crawled"] for row in results)
    pages_failed = sum(row["pages_failed"] for row in results)

    return {
        "records_seen": len(records),
        "records_processed": len(results),
        "records_with_websites": records_with_websites,
        "successful": status_counts.get(STATUS_SUCCESS, 0),
        "partial": status_counts.get(STATUS_PARTIAL, 0),
        "failed": sum(
            v for k, v in status_counts.items()
            if k not in (
                STATUS_SUCCESS, STATUS_PARTIAL, STATUS_NO_WEBSITE, STATUS_INVALID_URL,
                STATUS_SKIPPED_NON_HVAC_TRADE,
            )
        ),
        "no_website": status_counts.get(STATUS_NO_WEBSITE, 0),
        "invalid_url": status_counts.get(STATUS_INVALID_URL, 0),
        "skipped_non_hvac_trade": status_counts.get(STATUS_SKIPPED_NON_HVAC_TRADE, 0),
        "pages_attempted": pages_attempted,
        "pages_successful": pages_successful,
        "pages_failed": pages_failed,
        "blocked": status_counts.get(STATUS_BLOCKED, 0),
        "status_counts": status_counts,
    }


def run(master_json_path: str, out_dir: str, config: dict, limit: int | None = None, now_fn=None, fetcher=None) -> dict:
    records = load_master(master_json_path)
    fetcher = fetcher or Fetcher(config)
    results = enrich_all(records, config, fetcher, now_fn=now_fn, limit=limit)
    stats = compute_stats(records, results)
    json_path, csv_path = write_outputs(results, out_dir, stats)
    stats["artifacts"] = {"enrichment_json": json_path, "enrichment_csv": csv_path}
    return stats


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--master-json", default="data/master/master.json", help="Path to the master prospect store JSON (read-only)")
    parser.add_argument("--out-dir", default="data/enrichment", help="Directory to write generated enrichment output (default: data/enrichment)")
    parser.add_argument("--config", default=None, help="Path to an optional JSON config overriding crawl defaults")
    parser.add_argument("--max-pages", type=int, default=None, help="Override max_pages_per_domain")
    parser.add_argument("--timeout", type=float, default=None, help="Override request_timeout (seconds)")
    parser.add_argument("--user-agent", default=None, help="Override the crawler User-Agent string")
    parser.add_argument("--limit", type=int, default=None, help="Only process the first N master records (useful for local testing)")
    parser.add_argument("--no-robots", action="store_true", help="Disable robots.txt checking (default: respected)")
    args = parser.parse_args(argv)

    config = load_config(args.config)
    if args.max_pages is not None:
        config["max_pages_per_domain"] = args.max_pages
    if args.timeout is not None:
        config["request_timeout"] = args.timeout
    if args.user_agent is not None:
        config["user_agent"] = args.user_agent
    if args.no_robots:
        config["respect_robots_txt"] = False

    stats = run(args.master_json, args.out_dir, config, limit=args.limit)
    print(json.dumps(stats, indent=2))
    return stats


if __name__ == "__main__":
    main(sys.argv[1:])
