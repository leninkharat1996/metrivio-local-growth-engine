"""
Deterministic, keyword-based evidence extraction over already-extracted
page text. NO AI/LLM, NO scoring/classification -- this only records
literal keyword matches with traceable page_url + snippet context, exactly
mirroring the preliminary ICP layer's "only literal keyword/field matches,
never invented conclusions" philosophy (see config/icp_rules.json).

Every evidence item carries an `evidence_type` describing how the keyword
was literally used on the page. That label is itself decided by fixed,
literal marker phrases -- never by inference:

    direct_service    the sentence also contains a service-verb phrase
                      ("we provide/offer/install/service/repair/maintain")
    customer_vertical the keyword is a customer/building-type term
    residential       the keyword is a residential term
    incidental        the keyword appears in a product/brand/parts context
                      ("we install HVAC products from leading manufacturers"),
                      or with nothing that literally states service delivery

Incidental deliberately beats direct_service when both marker kinds are
present: we would rather under-claim than assert a service the page does
not actually claim.
"""
from __future__ import annotations

import re

try:
    from .vocabulary import (
        ALL_CATEGORIES,
        COMMERCIAL_VERTICAL_CATEGORIES,
        RESIDENTIAL_CATEGORIES,
        ROLLUP_GROUPS,
    )
except ImportError:
    from vocabulary import (
        ALL_CATEGORIES,
        COMMERCIAL_VERTICAL_CATEGORIES,
        RESIDENTIAL_CATEGORIES,
        ROLLUP_GROUPS,
    )

EVIDENCE_TYPES = ("direct_service", "customer_vertical", "residential", "incidental")

# Literal service-verb phrases: the business states it performs the work.
DIRECT_SERVICE_MARKERS = (
    "our crews",
    "our service",
    "our services include",
    "our team installs",
    "our technicians",
    "provides",
    "providing",
    "service and repair",
    "services include",
    "specializes in",
    "specializing in",
    "we design",
    "we handle",
    "we install",
    "we maintain",
    "we offer",
    "we perform",
    "we provide",
    "we repair",
    "we replace",
    "we service",
    "we specialize",
)

# Literal product/brand/parts phrases: a mention, not proof of contracting.
INCIDENTAL_MARKERS = (
    "authorized dealer",
    "brands we carry",
    "dealer of",
    "distributor of",
    "equipment from",
    "leading brands",
    "leading manufacturers",
    "manufacturer of",
    "parts and supplies",
    "parts department",
    "products from",
    "supplier of",
    "top brands",
    "we carry",
    "we sell",
    "wholesale",
)

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?;:])\s+|\n+")
_WHITESPACE_RE = re.compile(r"\s+")


def split_sentences(text: str) -> list[str]:
    """Split visible text into deterministic sentence-sized chunks."""
    if not text:
        return []
    chunks = []
    for raw in _SENTENCE_SPLIT_RE.split(text):
        cleaned = _WHITESPACE_RE.sub(" ", raw).strip()
        if cleaned:
            chunks.append(cleaned)
    return chunks


def classify_evidence_type(category: str, sentence_lower: str) -> str:
    """Label one keyword occurrence using fixed literal markers only."""
    if category in RESIDENTIAL_CATEGORIES:
        return "residential"
    if category in COMMERCIAL_VERTICAL_CATEGORIES:
        return "customer_vertical"
    if any(marker in sentence_lower for marker in INCIDENTAL_MARKERS):
        return "incidental"
    if any(marker in sentence_lower for marker in DIRECT_SERVICE_MARKERS):
        return "direct_service"
    return "incidental"


def _make_snippet(sentence: str, max_snippet_length: int) -> str:
    cleaned = _WHITESPACE_RE.sub(" ", sentence or "").strip()
    if max_snippet_length and len(cleaned) > max_snippet_length:
        return cleaned[:max_snippet_length].rstrip()
    return cleaned


def extract_evidence(page_url: str, text: str, max_snippet_length: int) -> list[dict]:
    """Return {category, keyword, page_url, snippet, evidence_type} items.

    Matching is case-insensitive literal substring matching over
    sentence-sized chunks, same style as scripts/icp/qualify.py. The first
    occurrence of a keyword on a page wins (one item per
    category+keyword+page).
    """
    if not text:
        return []
    sentences = [(s, s.lower()) for s in split_sentences(text)]
    evidence: list[dict] = []
    for category, keywords in ALL_CATEGORIES.items():
        for keyword in keywords:
            keyword_lower = keyword.lower()
            for sentence, sentence_lower in sentences:
                if keyword_lower not in sentence_lower:
                    continue
                evidence.append(
                    {
                        "category": category,
                        "keyword": keyword,
                        "page_url": page_url,
                        "snippet": _make_snippet(sentence, max_snippet_length),
                        "evidence_type": classify_evidence_type(
                            category, sentence_lower
                        ),
                    }
                )
                break
    return evidence


def sort_evidence(evidence: list[dict]) -> list[dict]:
    return sorted(
        evidence,
        key=lambda e: (e["category"], e["page_url"], e["keyword"], e.get("snippet", "")),
    )


def dedupe_evidence(evidence: list[dict]) -> list[dict]:
    """Dedupe exact (category, keyword, page_url) repeats -- e.g. the same
    keyword found in both body text and a heading on the same page --
    keeping the first snippet seen."""
    seen = set()
    result = []
    for item in evidence:
        key = (item["category"], item["keyword"], item["page_url"])
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def rollup_counts(evidence: list[dict]) -> dict[str, int]:
    counts = {group: 0 for group in ROLLUP_GROUPS}
    for item in evidence:
        for group, categories in ROLLUP_GROUPS.items():
            if item["category"] in categories:
                counts[group] += 1
    return counts
