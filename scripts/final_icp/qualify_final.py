#!/usr/bin/env python3
"""
Step 10 -- Final ICP (Ideal Customer Profile) Qualification.

Deterministic, evidence-gated qualification layer that sits on top of the
two existing generated datasets:

    data/icp/icp_qualified.json          (preliminary ICP -- Google Maps
                                           name/category text signals only)
    data/enrichment/website_enrichment.json (Step 9 -- factual website
                                           evidence, keyword-matched, never
                                           scored/classified)

Final ICP consumes both (read-only, left-joined by master_id) and decides
a final qualification STATUS using an evidence-gate hierarchy: geography,
hard exclusions, residential-only, then direct commercial-service website
evidence. A numeric 0-100 score and A+/A/B/C/D tier are computed strictly
AFTER status is finalized, purely for prioritization -- score/tier can
NEVER promote a record past the status the gate rules already decided. In
particular, a record with zero direct-service evidence in the HVAC-
specific "Bucket 1" category set can never become "qualified" through
website-confirmed evidence, no matter how high its score would otherwise
be (large vertical breadth, high rating, many reviews, multiple generic
Bucket-2 service categories).

This is NOT enrichment and makes NO network calls:
- No AI/LLM calls, no paid APIs, no web crawling, no IP geolocation.
- Reads data/master/master.json, data/icp/icp_qualified.json and
  data/enrichment/website_enrichment.json read-only; none of the three
  are ever mutated. Output is written to a separate generated dataset
  (default: data/final_icp/).
- Deterministic: same three inputs + the same --evaluated-at -> byte-
  identical output (aside from the evaluated_at field itself).

Usage:
    python3 scripts/final_icp/qualify_final.py \
        --master-json data/master/master.json \
        --preliminary-json data/icp/icp_qualified.json \
        --enrichment-json data/enrichment/website_enrichment.json \
        --rules config/final_icp_rules.json \
        --out-dir data/final_icp
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from datetime import datetime, timezone

OUTPUT_FIELDS = [
    "master_id",
    "business_name",
    "website",
    "city",
    "state",
    "country",
    "final_icp_status",
    "final_icp_tier",
    "final_icp_score",
    "final_icp_confidence",
    "qualification_reasons",
    "exclusion_reasons",
    "commercial_service_signals",
    "hvac_direct_signals",
    "commercial_vertical_signals",
    "high_value_service_signals",
    "residential_signals",
    "incidental_signals",
    "evidence_source",
    "website_status",
    "preliminary_icp_status",
    "preliminary_icp_confidence",
    "preliminary_icp_score",
    "score_breakdown",
    "final_icp_evaluated_at",
]

EVIDENCE_SOURCE_WEBSITE = "website_enrichment"
EVIDENCE_SOURCE_PRELIMINARY = "preliminary_icp_only"
EVIDENCE_SOURCE_NONE = "none"

STATUS_QUALIFIED = "qualified"
STATUS_REVIEW = "review"
STATUS_EXCLUDED = "excluded"

GEO_US = "US_CONFIRMED"
GEO_NON_US = "NON_US_CONFIRMED"
GEO_AMBIGUOUS = "AMBIGUOUS"

SINGLE_TRADE_LABELS = ("plumbing_only", "electrical_only", "handyman_only")


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def load_rules(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_json_list(path: str) -> list[dict]:
    if not path or not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, list) else data.get("results", [])


def load_enrichment(path: str) -> dict[str, dict]:
    """Returns master_id -> enrichment record. Never mutates the file."""
    if not path or not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        payload = json.load(f)
    records = payload.get("records", []) if isinstance(payload, dict) else payload
    return {r.get("master_id"): r for r in records if r.get("master_id")}


def index_master(path: str) -> dict[str, dict]:
    return {r.get("master_id"): r for r in load_json_list(path) if r.get("master_id")}


# ---------------------------------------------------------------------------
# STEP 1 -- Geography
# ---------------------------------------------------------------------------

def resolve_geography(record: dict, rules: dict) -> str:
    """record is the preliminary ICP row (carries country/state verbatim
    from the master store). No external geo source is used -- only these
    existing fields."""
    country = (record.get("country") or "").strip()
    state = (record.get("state") or "").strip().upper()
    allowlist = {c.lower() for c in rules["non_us_country_allowlist"]}
    us_states = set(rules["us_state_codes"])

    if country and country.lower() not in allowlist:
        return GEO_NON_US
    if country.lower() in allowlist and country:
        return GEO_US
    if state in us_states:
        return GEO_US
    if not country and not state:
        return GEO_AMBIGUOUS
    # Blank country but a non-US-looking state code -- ambiguous, never
    # guessed as qualified.
    return GEO_AMBIGUOUS


# ---------------------------------------------------------------------------
# Evidence extraction helpers (read-only consumption of Step 9 output)
# ---------------------------------------------------------------------------

def _website_usable(enrichment: dict | None, rules: dict) -> bool:
    if not enrichment:
        return False
    return enrichment.get("website_status") in rules["usable_website_statuses"]


def _direct_service_categories(evidence: list[dict], categories: set) -> set:
    return {
        e["category"]
        for e in evidence
        if e.get("evidence_type") == "direct_service" and e.get("category") in categories
    }


def _any_commercial_evidence(
    evidence: list[dict], hvac_categories: set, bucket2_categories: set, vertical_category: str
) -> bool:
    """True only when there is genuine (not merely incidental/standalone)
    commercial evidence on the site -- used only to decide whether a
    residential mention is truly "residential-only" or merely "residential
    + genuine commercial evidence". A direct-service hit in a HVAC/Bucket-2
    category counts; a commercial_vertical hit counts only when
    evidence.py already confirmed it co-occurred with HVAC/service context
    (evidence_type == "customer_vertical") -- a bare, standalone building-
    type word ("office", "warehouse", ...) recorded as "incidental" does
    NOT count here."""
    for e in evidence:
        category = e.get("category")
        evidence_type = e.get("evidence_type")
        if category in hvac_categories and evidence_type == "direct_service":
            return True
        if category in bucket2_categories and evidence_type == "direct_service":
            return True
        if category == vertical_category and evidence_type == "customer_vertical":
            return True
    return False


def _vertical_keywords(evidence: list[dict], vertical_category: str) -> list[str]:
    """Only keywords whose occurrence was confirmed (by evidence.py) to
    co-occur with HVAC/service context in the same sentence -- a bare,
    standalone building-type mention never counts as vertical evidence."""
    return sorted({
        e["keyword"]
        for e in evidence
        if e.get("category") == vertical_category and e.get("evidence_type") == "customer_vertical"
    })


def _residential_keywords(evidence: list[dict], residential_categories: set) -> list[str]:
    return sorted({e["keyword"] for e in evidence if e.get("category") in residential_categories})


def _incidental_items(evidence: list[dict]) -> list[dict]:
    items = [
        {"category": e["category"], "keyword": e["keyword"]}
        for e in evidence
        if e.get("evidence_type") == "incidental"
    ]
    seen = set()
    deduped = []
    for item in sorted(items, key=lambda i: (i["category"], i["keyword"])):
        key = (item["category"], item["keyword"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def _homepage_text(enrichment: dict | None) -> str:
    if not enrichment:
        return ""
    return " ".join(
        filter(None, [enrichment.get("homepage_title"), enrichment.get("homepage_description")])
    ).lower()


# ---------------------------------------------------------------------------
# STEP 2 -- Hard exclusions (preliminary carry-through + mixed-trade rescue
# + new website-evidence-based exclusions)
# ---------------------------------------------------------------------------

def evaluate_hard_exclusions(
    prelim: dict, evidence: list[dict], enrichment: dict | None, rules: dict, n_hvac: int
) -> list[str]:
    reasons: list[str] = []
    prelim_exclusions = prelim.get("icp_exclusions") or []
    # Residential-only exclusions are NOT handled here -- they have their
    # own rescue rule (evaluate_residential_only, STEP 3) and must never be
    # carried through as a non-rescuable hard exclusion at this step.
    hard_reasons = [
        r for r in prelim_exclusions
        if r.startswith("hard exclusion:") and "residential-only" not in r
    ]

    single_trade_reasons = [r for r in hard_reasons if any(lbl in r for lbl in SINGLE_TRADE_LABELS)]
    other_hard_reasons = [r for r in hard_reasons if r not in single_trade_reasons]

    # Non-rescuable preliminary hard exclusions (manufacturer, distributor,
    # wholesaler, supply, training school, recruiter, government, country)
    # carry straight through -- unchanged behavior.
    reasons.extend(other_hard_reasons)

    # Mixed-trade rescue: a plumbing/electrical/handyman-only preliminary
    # exclusion is overridden ONLY when the website itself shows genuine
    # direct-service commercial HVAC evidence (n_hvac >= 1). Incidental
    # HVAC mentions never rescue.
    if single_trade_reasons and n_hvac < 1:
        reasons.extend(single_trade_reasons)

    # New website-evidence-based hard exclusion: homepage title/description
    # reads as a manufacturer/distributor/supply-house/parts-counter or
    # association/directory/media-only business AND there is zero direct-
    # service commercial evidence anywhere on the site (all matches, if
    # any, are incidental -- e.g. "we install commercial HVAC products
    # from leading manufacturers").
    homepage_text = _homepage_text(enrichment)
    sig = rules["website_hard_exclusion_signals"]
    supply_hit = any(kw in homepage_text for kw in sig["supply_manufacturer_keywords"])
    assoc_hit = any(kw in homepage_text for kw in sig["association_directory_media_keywords"])
    n_all_direct = n_hvac + len(
        _direct_service_categories(evidence, set(rules["bucket2_service_categories"]))
    )
    if (supply_hit or assoc_hit) and n_all_direct == 0:
        pattern = "supply/manufacturer/distributor" if supply_hit else "association/directory/media-only"
        reasons.append(
            f"hard exclusion: website homepage matches {pattern} pattern with no direct commercial-service evidence"
        )

    return reasons


# ---------------------------------------------------------------------------
# STEP 3 -- Residential-only
# ---------------------------------------------------------------------------

def evaluate_residential_only(
    prelim: dict, evidence: list[dict], rules: dict, n_hvac: int, n_all_direct: int
) -> list[str]:
    prelim_evidence_tags = set(prelim.get("icp_evidence") or [])
    prelim_residential_flag = "residential_only_signal" in prelim_evidence_tags

    residential_categories = set(rules["residential_only_categories"])
    hvac_categories = set(rules["hvac_direct_categories"])
    bucket2_categories = set(rules["bucket2_service_categories"])
    vertical_category = rules["vertical_category"]
    residential_on_site = any(e.get("category") in residential_categories for e in evidence)
    any_commercial_on_site = _any_commercial_evidence(
        evidence, hvac_categories, bucket2_categories, vertical_category
    )

    if prelim_residential_flag:
        if n_hvac >= 1:
            return []  # rescued: real commercial HVAC service evidence on the site
        return [
            "hard exclusion: residential-only at both preliminary (name/category) "
            "and website evidence layers, with no direct commercial HVAC service evidence"
        ]

    if residential_on_site and not any_commercial_on_site:
        return [
            "hard exclusion: website evidence is residential-only "
            "with no commercial evidence of any kind present"
        ]

    return []


# ---------------------------------------------------------------------------
# STEP 4/5 -- Direct evidence + website qualification / fallback
# ---------------------------------------------------------------------------

def evaluate_status(
    prelim: dict, enrichment: dict | None, evidence: list[dict], geography: str, rules: dict
) -> tuple[str, str, str, list[str]]:
    """Returns (status, confidence, evidence_source, qualification_reasons)."""
    hvac_categories = set(rules["hvac_direct_categories"])
    bucket2_categories = set(rules["bucket2_service_categories"])

    hvac_direct = _direct_service_categories(evidence, hvac_categories)
    bucket2_direct = _direct_service_categories(evidence, bucket2_categories)
    all_direct = hvac_direct | bucket2_direct
    n_hvac = len(hvac_direct)
    n_all = len(all_direct)

    prelim_confidence = prelim.get("icp_confidence")
    prelim_exclusions = prelim.get("icp_exclusions") or []

    if _website_usable(enrichment, rules):
        if n_hvac >= 1 and n_all >= 2:
            return (
                STATUS_QUALIFIED,
                "high",
                EVIDENCE_SOURCE_WEBSITE,
                [
                    f"Path A: {n_hvac} HVAC-specific direct-service categor(y/ies) "
                    f"and {n_all} total direct-service categories on website evidence"
                ],
            )
        if n_hvac >= 1 and prelim_confidence == "high":
            return (
                STATUS_QUALIFIED,
                "high",
                EVIDENCE_SOURCE_WEBSITE,
                [
                    f"Path B: {n_hvac} HVAC-specific direct-service categor(y/ies) "
                    "on website evidence, corroborated by high preliminary ICP confidence"
                ],
            )
        if n_hvac == 1:
            return (
                STATUS_REVIEW,
                "medium",
                EVIDENCE_SOURCE_WEBSITE,
                [
                    "1 HVAC-specific direct-service category found, but neither Path A "
                    "(>=2 total direct-service categories) nor Path B "
                    "(high preliminary confidence) is satisfied"
                ],
            )
        return (
            STATUS_REVIEW,
            "low",
            EVIDENCE_SOURCE_WEBSITE,
            [
                "no HVAC-specific (Bucket 1) direct-service evidence found on website "
                "-- generic/vertical/incidental evidence alone cannot qualify"
            ],
        )

    # Website unavailable/unusable -- the only fallback path, and the only
    # path that does not require website HVAC evidence.
    if prelim_confidence == "high" and not prelim_exclusions and geography == GEO_US:
        return (
            STATUS_QUALIFIED,
            "medium",
            EVIDENCE_SOURCE_PRELIMINARY,
            [
                "website unavailable/unusable -- qualified on high preliminary "
                "(Google Maps name/category) confidence alone; tier capped at B"
            ],
        )
    evidence_source = EVIDENCE_SOURCE_PRELIMINARY if enrichment is not None or prelim else EVIDENCE_SOURCE_NONE
    return (
        STATUS_REVIEW,
        "low",
        evidence_source,
        ["website unavailable/unusable and preliminary confidence insufficient for the fallback path"],
    )


# ---------------------------------------------------------------------------
# Score (0-100, fixed table, computed strictly after status)
# ---------------------------------------------------------------------------

def _bucket1_score(n_hvac: int, cfg: dict) -> int:
    return min(n_hvac * cfg["points_per_category"], cfg["max_points"])


def _bucket2_score(n_bucket2: int, cfg: dict) -> int:
    if n_bucket2 <= 0:
        return 0
    if n_bucket2 == 1:
        return 8
    if n_bucket2 == 2:
        return 17
    return cfg["max_points"]


def _bucket3_score(n_vertical: int, prelim_confidence: str | None, rating, review_count, cfg: dict) -> int:
    vertical_points = min(n_vertical * cfg["vertical_breadth"]["points_per_vertical"], cfg["vertical_breadth"]["max_points"])
    corrob_cfg = cfg["preliminary_corroboration"]
    corrob_points = min(corrob_cfg.get(prelim_confidence or "low", 0), corrob_cfg["max_points"])

    rr_cfg = cfg["rating_reviews"]
    rr_points = 0
    if isinstance(rating, (int, float)) and rating >= 4.0:
        rr_points += rr_cfg["rating_at_least_4"]
    if isinstance(review_count, (int, float)):
        if review_count >= 50:
            rr_points += rr_cfg["review_count_at_least_50"]
        elif review_count >= 10:
            rr_points += rr_cfg["review_count_at_least_10"]
    rr_points = min(rr_points, rr_cfg["max_points"])

    return min(vertical_points + corrob_points + rr_points, cfg["max_points"])


def _bucket4_score(
    prelim: dict,
    residential_present: bool,
    n_all_direct: int,
    website_status: str | None,
    evidence_source: str,
    cfg: dict,
) -> int:
    total = 0
    emp_cfg = cfg["employee_count"]
    employee_count = None
    for field_name in emp_cfg["field_names"]:
        value = prelim.get(field_name)
        if isinstance(value, (int, float)):
            employee_count = value
            break
    if employee_count is not None:
        if 10 <= employee_count <= 75:
            total += emp_cfg["band_10_75"]
        elif 76 <= employee_count <= 250:
            total += emp_cfg["band_76_250"]

    if residential_present and n_all_direct >= 1:
        total += cfg["residential_alongside_commercial"]

    penalty_cfg = cfg["website_status_penalty"]
    if evidence_source == EVIDENCE_SOURCE_PRELIMINARY:
        total += penalty_cfg["fallback_preliminary_only"]
    elif website_status == "partial":
        total += penalty_cfg["partial"]
    else:
        total += penalty_cfg.get("success", 0)

    return max(0, total)


def compute_score(
    prelim: dict,
    n_hvac: int,
    n_bucket2: int,
    n_vertical: int,
    residential_present: bool,
    website_status: str | None,
    evidence_source: str,
    rules: dict,
) -> tuple[int, dict]:
    score_cfg = rules["score"]
    bucket1 = _bucket1_score(n_hvac, score_cfg["bucket1_hvac_direct"])
    bucket2 = _bucket2_score(n_bucket2, score_cfg["bucket2_service"])
    bucket3 = _bucket3_score(
        n_vertical, prelim.get("icp_confidence"), prelim.get("rating"), prelim.get("review_count"),
        score_cfg["bucket3_supporting"],
    )
    bucket4 = _bucket4_score(
        prelim, residential_present, n_hvac + n_bucket2, website_status, evidence_source,
        score_cfg["bucket4_modifiers"],
    )
    total = max(0, min(100, bucket1 + bucket2 + bucket3 + bucket4))
    breakdown = {"bucket1_hvac_direct": bucket1, "bucket2_service": bucket2, "bucket3_supporting": bucket3, "bucket4_modifiers": bucket4}
    return total, breakdown


# ---------------------------------------------------------------------------
# Tier (score-derived, gated by status -- never the other way around)
# ---------------------------------------------------------------------------

def _tier_gate_satisfied(
    band: str, status: str, n_hvac: int, n_bucket2: int, evidence_source: str, website_status: str | None
) -> bool:
    if band == "A+":
        return (
            status == STATUS_QUALIFIED
            and n_hvac >= 2
            and n_bucket2 >= 1
            and evidence_source == EVIDENCE_SOURCE_WEBSITE
            and website_status == "success"
        )
    if band == "A":
        return status == STATUS_QUALIFIED and n_hvac >= 1 and evidence_source == EVIDENCE_SOURCE_WEBSITE
    if band == "B":
        return status == STATUS_QUALIFIED
    if band == "C":
        return status in (STATUS_QUALIFIED, STATUS_REVIEW)
    return False


def score_to_tier(
    score: int, status: str, n_hvac: int, n_bucket2: int, evidence_source: str, website_status: str | None, rules: dict
) -> str:
    if status == STATUS_EXCLUDED:
        return "D"
    band_order = [
        ("A+", rules["tiers"]["A_PLUS"]["min_score"]),
        ("A", rules["tiers"]["A"]["min_score"]),
        ("B", rules["tiers"]["B"]["min_score"]),
        ("C", rules["tiers"]["C"]["min_score"]),
    ]
    for band, min_score in band_order:
        if score >= min_score and _tier_gate_satisfied(band, status, n_hvac, n_bucket2, evidence_source, website_status):
            return band
    # Only an excluded record may reach D (handled above). A qualified or
    # review record always floors at C, even when its score falls below
    # the C band's own min_score threshold.
    return "C"


# ---------------------------------------------------------------------------
# Per-record orchestration
# ---------------------------------------------------------------------------

def qualify_final_record(prelim: dict, enrichment: dict | None, rules: dict, evaluated_at: str) -> dict:
    hvac_categories = set(rules["hvac_direct_categories"])
    bucket2_categories = set(rules["bucket2_service_categories"])
    residential_categories = set(rules["residential_only_categories"])
    vertical_category = rules["vertical_category"]

    evidence = (enrichment or {}).get("evidence") or []
    website_status = (enrichment or {}).get("website_status")

    hvac_direct = _direct_service_categories(evidence, hvac_categories)
    bucket2_direct = _direct_service_categories(evidence, bucket2_categories)
    all_direct = hvac_direct | bucket2_direct
    n_hvac = len(hvac_direct)
    n_all_direct = len(all_direct)

    geography = resolve_geography(prelim, rules)

    exclusion_reasons: list[str] = []

    if geography == GEO_NON_US:
        exclusion_reasons.append(f"hard exclusion: country '{prelim.get('country')}' is not confirmed US")

    if not exclusion_reasons:
        exclusion_reasons.extend(evaluate_hard_exclusions(prelim, evidence, enrichment, rules, n_hvac))

    if not exclusion_reasons:
        exclusion_reasons.extend(
            evaluate_residential_only(prelim, evidence, rules, n_hvac, n_all_direct)
        )

    if exclusion_reasons:
        status = STATUS_EXCLUDED
        confidence = "low"
        evidence_source = EVIDENCE_SOURCE_WEBSITE if enrichment else EVIDENCE_SOURCE_PRELIMINARY
        qualification_reasons: list[str] = []
    else:
        status, confidence, evidence_source, qualification_reasons = evaluate_status(
            prelim, enrichment, evidence, geography, rules
        )
        if geography == GEO_AMBIGUOUS and status == STATUS_QUALIFIED:
            status = STATUS_REVIEW
            confidence = "low"
            qualification_reasons = ["geography is ambiguous -- cannot resolve to qualified"]

    residential_keywords = _residential_keywords(evidence, residential_categories)
    residential_present = bool(residential_keywords)

    score, breakdown = compute_score(
        prelim, n_hvac, len(bucket2_direct),
        len(_vertical_keywords(evidence, vertical_category)),
        residential_present, website_status, evidence_source, rules,
    )
    tier = score_to_tier(score, status, n_hvac, len(bucket2_direct), evidence_source, website_status, rules)

    return {
        "master_id": prelim.get("master_id"),
        "business_name": prelim.get("business_name"),
        "website": prelim.get("website"),
        "city": prelim.get("city"),
        "state": prelim.get("state"),
        "country": prelim.get("country"),
        "final_icp_status": status,
        "final_icp_tier": tier,
        "final_icp_score": score,
        "final_icp_confidence": confidence,
        "qualification_reasons": qualification_reasons,
        "exclusion_reasons": exclusion_reasons,
        "commercial_service_signals": sorted(all_direct),
        "hvac_direct_signals": sorted(hvac_direct),
        "commercial_vertical_signals": _vertical_keywords(evidence, vertical_category),
        "high_value_service_signals": sorted(hvac_direct),
        "residential_signals": {"present": residential_present, "keywords": residential_keywords},
        "incidental_signals": _incidental_items(evidence),
        "evidence_source": evidence_source,
        "website_status": website_status,
        "preliminary_icp_status": prelim.get("icp_status"),
        "preliminary_icp_confidence": prelim.get("icp_confidence"),
        "preliminary_icp_score": prelim.get("icp_score"),
        "score_breakdown": breakdown,
        "final_icp_evaluated_at": evaluated_at,
    }


def qualify_all(
    preliminary_records: list[dict], enrichment_by_id: dict[str, dict], rules: dict, evaluated_at: str
) -> list[dict]:
    results = [
        qualify_final_record(prelim, enrichment_by_id.get(prelim.get("master_id")), rules, evaluated_at)
        for prelim in preliminary_records
    ]
    results.sort(key=lambda r: (r.get("master_id") or ""))
    return results


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def write_outputs(results: list[dict], out_dir: str) -> tuple[str, str]:
    os.makedirs(out_dir, exist_ok=True)
    json_path = os.path.join(out_dir, "final_icp_qualified.json")
    csv_path = os.path.join(out_dir, "final_icp_qualified.csv")

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False, sort_keys=False)

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in results:
            flat = dict(row)
            for list_field in (
                "qualification_reasons", "exclusion_reasons", "commercial_service_signals",
                "hvac_direct_signals", "commercial_vertical_signals", "high_value_service_signals",
                "incidental_signals",
            ):
                flat[list_field] = json.dumps(row.get(list_field) or [], ensure_ascii=False)
            flat["residential_signals"] = json.dumps(row.get("residential_signals") or {}, ensure_ascii=False)
            flat["score_breakdown"] = json.dumps(row.get("score_breakdown") or {}, ensure_ascii=False)
            writer.writerow({k: ("" if flat.get(k) is None else flat.get(k)) for k in OUTPUT_FIELDS})

    return json_path, csv_path


def run(
    master_json_path: str,
    preliminary_json_path: str,
    enrichment_json_path: str,
    rules_path: str,
    out_dir: str,
    evaluated_at: str | None = None,
) -> dict:
    rules = load_rules(rules_path)
    # master.json is read for API/pipeline-boundary compatibility (Final
    # ICP's inputs are documented as master + preliminary + enrichment)
    # but every field it could supply is already carried unchanged onto
    # the preliminary ICP row, so it is not re-consulted per record.
    index_master(master_json_path)
    preliminary_records = load_json_list(preliminary_json_path)
    enrichment_by_id = load_enrichment(enrichment_json_path)
    evaluated_at = evaluated_at or datetime.now(timezone.utc).isoformat()

    results = qualify_all(preliminary_records, enrichment_by_id, rules, evaluated_at)
    json_path, csv_path = write_outputs(results, out_dir)

    tier_counts: dict[str, int] = {}
    status_counts: dict[str, int] = {}
    confidence_counts: dict[str, int] = {}
    for row in results:
        tier_counts[row["final_icp_tier"]] = tier_counts.get(row["final_icp_tier"], 0) + 1
        status_counts[row["final_icp_status"]] = status_counts.get(row["final_icp_status"], 0) + 1
        confidence_counts[row["final_icp_confidence"]] = confidence_counts.get(row["final_icp_confidence"], 0) + 1

    return {
        "records_evaluated": len(results),
        "tier_counts": tier_counts,
        "status_counts": status_counts,
        "confidence_counts": confidence_counts,
        "artifacts": {"final_icp_json": json_path, "final_icp_csv": csv_path},
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--master-json", default="data/master/master.json", help="Path to the master prospect store JSON (read-only)")
    parser.add_argument("--preliminary-json", default="data/icp/icp_qualified.json", help="Path to the preliminary ICP output JSON (read-only)")
    parser.add_argument("--enrichment-json", default="data/enrichment/website_enrichment.json", help="Path to the website enrichment output JSON (read-only)")
    parser.add_argument("--rules", default="config/final_icp_rules.json", help="Path to the Final ICP rules config JSON")
    parser.add_argument("--out-dir", default="data/final_icp", help="Directory to write generated Final ICP output (default: data/final_icp)")
    parser.add_argument(
        "--evaluated-at",
        default=None,
        help="Override final_icp_evaluated_at (RFC3339). Defaults to current UTC time. Pass a fixed value for byte-identical reruns.",
    )
    args = parser.parse_args(argv)

    stats = run(
        args.master_json, args.preliminary_json, args.enrichment_json, args.rules, args.out_dir, args.evaluated_at
    )
    print(json.dumps(stats, indent=2))
    return stats


if __name__ == "__main__":
    main(sys.argv[1:])
