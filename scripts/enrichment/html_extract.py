"""
HTML extraction for Step 9 website enrichment, built on Scrapling's parser
(https://github.com/D4Vinci/Scrapling).

Everything here goes through Scrapling's Selector API -- the very same
parsed object Scrapling's plain HTTP `Fetcher` returns -- so a local HTML
fixture parsed offline and a real fetched page take an identical code path.

Extracts only: title, meta description, visible body text (capped),
headings, and internal-page links. Raw HTML is never stored, and no PII is
intentionally collected. Malformed markup degrades to empty fields rather
than crashing the batch (lxml, which Scrapling parses with, is tolerant).
"""
from __future__ import annotations

import re

if __package__ in (None, ""):
    from scrapling_client import parse_html
else:
    from .scrapling_client import parse_html

# Tags whose text is boilerplate/noise rather than page content. Scrapling's
# get_all_text() takes this straight as `ignore_tags`.
IGNORED_TEXT_TAGS = (
    "script",
    "style",
    "noscript",
    "template",
    "svg",
    "nav",
    "header",
    "footer",
    "form",
)

MAX_HEADINGS = 50


def _normalize_whitespace(text) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _first_text(selector, css_query) -> str:
    matches = selector.css(css_query)
    if not matches:
        return ""
    return _normalize_whitespace(matches[0])


def as_selector(page, url=None):
    """Accept either raw HTML or an already-parsed Scrapling Selector.

    A Scrapling `Response` is itself a `Selector`, so a fetched page is
    passed through untouched instead of being re-parsed.
    """
    if isinstance(page, str) or isinstance(page, bytes):
        return parse_html(
            page.decode("utf-8", "replace") if isinstance(page, bytes) else page,
            url,
        )
    return page


def extract_page(page, max_text_length: int, url: str | None = None) -> dict:
    """Parse a page with Scrapling and return extracted, size-capped fields.

    `page` is raw HTML or a Scrapling Selector/Response. Never raises: any
    parse problem degrades to empty fields so one broken page can never
    abort the batch.
    """
    try:
        selector = as_selector(page, url)
    except Exception:
        return {
            "title": "",
            "meta_description": None,
            "body_text": "",
            "headings": [],
            "links": [],
        }

    title = _first_text(selector, "title::text")

    meta_description = None
    for query in (
        'meta[name="description"]::attr(content)',
        'meta[name="Description"]::attr(content)',
        'meta[property="og:description"]::attr(content)',
    ):
        try:
            value = _first_text(selector, query)
        except Exception:
            value = ""
        if value:
            meta_description = value
            break

    # Headings in document order across h1/h2/h3 (one XPath union keeps the
    # document's own ordering, which per-tag CSS queries would not).
    headings = []
    try:
        for node in selector.xpath("//h1|//h2|//h3"):
            heading_text = _normalize_whitespace(node.get_all_text(ignore_tags=("script", "style")))
            if heading_text:
                headings.append(heading_text)
    except Exception:
        headings = []

    body_nodes = selector.css("body")
    text_root = body_nodes[0] if body_nodes else selector
    try:
        body_text = _normalize_whitespace(
            text_root.get_all_text(ignore_tags=IGNORED_TEXT_TAGS, strip=True)
        )
    except Exception:
        body_text = ""
    if max_text_length and len(body_text) > max_text_length:
        body_text = body_text[:max_text_length]

    links = []
    try:
        for anchor in selector.css("a"):
            href = anchor.attrib.get("href")
            if not href:
                continue
            links.append(
                {
                    "href": str(href),
                    "text": _normalize_whitespace(anchor.get_all_text()),
                }
            )
    except Exception:
        links = []

    return {
        "title": title,
        "meta_description": meta_description,
        "body_text": body_text,
        "headings": headings[:MAX_HEADINGS],
        "links": links,
    }
