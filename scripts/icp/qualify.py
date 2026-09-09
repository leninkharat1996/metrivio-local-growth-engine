#!/usr/bin/env python3
"""
Deterministic, explainable PRELIMINARY ICP (Ideal Customer Profile)
qualification layer.

This layer reasons ONLY over fields that already exist on a master record
today (business name, category/type, website presence, rating,
review_count, address/city/state/country, and employee_count if present).
It does NOT know revenue, capacity, or true commercial specialization with
certainty -- those require later website/business enrichment that is not
built here. Every result is therefore "preliminary": icp_status 'review'
is an expected, intentional outcome for ambiguous records, not a failure.

Reads the persistent master prospect store (data/master/master.json,
produced by scripts/google_maps_scraper/update_master.py) and scores each
record against the rules in config/icp_rules.json, producing a tiered
(A+/A/B/C/D) qualification result with a transparent additive/subtractive
numeric score and human-readable reasons.

This is NOT enrichment and NOT AI/LLM scoring:
- No paid APIs, no LLM calls, no web scraping happen here.
- Only fields that already exist on a master record are read: business_name,
  category, website, address, city, state, country, phone, rating,
  review_count (and, if present -- never required or fabricated -- optional
  employee_count/employees/company_size).
- No email/LinkedIn/CRM/outreach functionality of any kind.
- The original master store is never mutated; output is written to a
  separate generated dataset (default: data/icp/).

Scoring is pure, rule-based Python matching normalize.py/update_master.py's
existing style: same input -> byte-identical output (the icp_evaluated_at
timestamp is the one exception -- see --evaluated-at below).

Usage:
    python3 qualify.py \
        --master-json data/master/master.json \
        --rules config/icp_rules.json \
        --out-dir data/icp
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from datetime import datetime, timezone

EVIDENCE_VOCABULARY = [
    "commercial_hvac_signal",
    "industrial_signal",
    "commercial_service_signal",
    "commercial_vertical_signal",
    "general_hvac_signal",
    "website_present",
    "strong_rating",
    "strong_review_count",
    "employee_count_signal",
    "hard_exclusion",
    "residential_only_signal",
]

OUTPUT_FIELDS = [
    "master_id",
    "business_name",
    "website",
    "phone",
    "address",
    "city",
    "state",
    "country",
    "rating",
    "review_count",
    "icp_tier",
    "icp_score",
    "icp_status",
    "icp_confidence",
    "icp_evidence",
    "icp_reasons",
    "icp_exclusions",
    "icp_evaluated_at",
]


def _text(value) -> str:
    return (value or "").strip()


def _lower(*values: str) -> str:
    return " ".join(_text(v) for v in values).lower()


def _contains_any(haystack: str, keywords: list[str]) -> list[str]:
    return [kw for kw in keywords if kw and kw.lower() in haystack]


def load_rules(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_master(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, list) else data.get("results", [])


def evaluate_hard_exclusions(record: dict, rules: dict, text: str) -> list[str]:
    """Returns a list of human-readable exclusion reasons; empty if none."""
    exclusions: list[str] = []
    hard = rules["hard_exclusions"]

    matched_business_type = _contains_any(text, hard["business_type_keywords"])
    for kw in matched_business_type:
        exclusions.append(f"hard exclusion: business type keyword '{kw}' matched")

    single_trade = hard["single_trade_only_keywords"]
    rescue = hard["hvac_rescue_keywords"]
    has_hvac_signal = bool(_contains_any(text, rescue))
    for trade_label, keywords in single_trade.items():
        matched = _contains_any(text, keywords)
        if matched and not has_hvac_signal:
            exclusions.append(
                f"hard exclusion: {trade_label} keyword(s) {matched} matched with no HVAC signal present"
            )

    country = _text(record.get("country"))
    allowlist = {c.lower() for c in hard["non_us_country_allowlist"]}
    if country and country.lower() not in allowlist:
        exclusions.append(f"hard exclusion: country '{country}' is not in the US allowlist")

    return exclusions


def evaluate_residential_only(record: dict, rules: dict, text: str) -> list[str]:
    cfg = rules["residential_only_exclusion"]
    residential_matched = _contains_any(text, cfg["residential_keywords"])
    if not residential_matched:
        return []
    commercial_rescue = _contains_any(text, cfg["commercial_rescue_keywords"])
    if commercial_rescue:
        return []
    return [
        f"hard exclusion: residential-only keyword(s) {residential_matched} matched with no commercial signal present"
    ]


def evaluate_positive_signals(record: dict, rules: dict, text: str) -> tuple[int, list[str], set[str]]:
    score = rules["base_score"]
    reasons = [f"base score: {rules['base_score']}"]
    evidence: set[str] = set()

    general = rules["general_hvac_signal"]
    general_matched = _contains_any(text, general["keywords"])
    if general_matched:
        score += general["points"]
        reasons.append(f"+{general['points']}: HVAC/mechanical signal ({general_matched[0]})")
        evidence.add("general_hvac_signal")

    strong = rules["strong_commercial_service_signals"]
    strong_matched = _contains_any(text, strong["keywords"])
    strong_points = min(len(strong_matched) * strong["points_each"], strong["max_points"])
    if strong_matched:
        score += strong_points
        reasons.append(
            f"+{strong_points}: strong commercial service signal(s) {strong_matched}"
        )
        evidence.add("commercial_hvac_signal")
        if _contains_any(text, strong.get("industrial_keywords", [])):
            evidence.add("industrial_signal")

    # 'mechanical contractor' is direct-but-ambiguous evidence: it only
    # counts as strong DIRECT commercial evidence when paired with a
    # general HVAC signal elsewhere in the text (otherwise it could be a
    # plumbing/other-trade mechanical contractor).
    mech = rules["mechanical_contractor_signal"]
    mech_matched = _contains_any(text, mech["keywords"])
    mech_points = min(len(mech_matched) * mech["points_each"], mech["max_points"])
    if mech_matched:
        score += mech_points
        reasons.append(f"+{mech_points}: mechanical contractor signal {mech_matched}")
        if general_matched:
            evidence.add("commercial_service_signal")

    vertical = rules["commercial_customer_vertical_signals"]
    vertical_matched = _contains_any(text, vertical["keywords"])
    vertical_points = min(len(vertical_matched) * vertical["points_each"], vertical["max_points"])
    if vertical_matched:
        score += vertical_points
        reasons.append(
            f"+{vertical_points}: commercial customer/vertical signal(s) {vertical_matched}"
        )
        evidence.add("commercial_vertical_signal")

    bonus = rules["bonus_signals"]

    website = _text(record.get("website"))
    if website:
        score += bonus["has_website"]["points"]
        reasons.append(f"+{bonus['has_website']['points']}: has a website on file")
        evidence.add("website_present")

    rating = record.get("rating")
    if isinstance(rating, (int, float)) and rating >= bonus["rating_at_least"]["threshold"]:
        score += bonus["rating_at_least"]["points"]
        reasons.append(
            f"+{bonus['rating_at_least']['points']}: rating {rating} >= {bonus['rating_at_least']['threshold']}"
        )
        evidence.add("strong_rating")

    review_count = record.get("review_count")
    if isinstance(review_count, (int, float)):
        low_cfg = bonus["review_count_at_least"]
        high_cfg = bonus["review_count_at_least_high"]
        if review_count >= low_cfg["threshold"]:
            score += low_cfg["points"]
            reasons.append(f"+{low_cfg['points']}: review_count {review_count} >= {low_cfg['threshold']}")
            evidence.add("strong_review_count")
        if review_count >= high_cfg["threshold"]:
            score += high_cfg["points"]
            reasons.append(f"+{high_cfg['points']}: review_count {review_count} >= {high_cfg['threshold']}")
            evidence.add("strong_review_count")

    emp_cfg = bonus["employee_count_present"]
    employee_count = None
    for field_name in emp_cfg["field_names"]:
        value = record.get(field_name)
        if isinstance(value, (int, float)):
            employee_count = value
            break
    if employee_count is not None:
        if employee_count >= emp_cfg["mid_min"]:
            score += emp_cfg["points_mid"]
            reasons.append(
                f"+{emp_cfg['points_mid']}: employee count {employee_count} >= {emp_cfg['mid_min']} (bonus only, never required)"
            )
            evidence.add("employee_count_signal")
        elif employee_count >= emp_cfg["small_min"]:
            score += emp_cfg["points_small"]
            reasons.append(
                f"+{emp_cfg['points_small']}: employee count {employee_count} >= {emp_cfg['small_min']} (bonus only, never required)"
            )
            evidence.add("employee_count_signal")

    has_any_positive_signal = bool(
        general_matched or strong_matched or mech_matched or vertical_matched or website
        or isinstance(rating, (int, float)) or isinstance(review_count, (int, float))
    )
    if not has_any_positive_signal:
        cap = rules["insufficient_evidence_penalty"]["cap_score_when_no_positive_signals"]
        if score > cap:
            reasons.append(
                f"capped at {cap}: no positive signal present on this record (insufficient evidence)"
            )
            score = cap

    return score, reasons, evidence


def evaluate_confidence(evidence: set[str]) -> str:
    """icp_confidence is driven ONLY by direct keyword evidence. Generic
    secondary bonuses (website/rating/review_count/employee_count) can
    never by themselves produce 'high' or upgrade 'low' to 'medium'."""
    if "commercial_hvac_signal" in evidence or "commercial_service_signal" in evidence:
        return "high"
    if "general_hvac_signal" in evidence and "commercial_vertical_signal" in evidence:
        return "medium"
    return "low"


def evaluate_status(exclusions: list[str], confidence: str, score: int, rules: dict) -> str:
    if exclusions:
        return "excluded"
    min_score_medium = rules["status_rules"]["qualified_min_score_for_medium_confidence"]
    if confidence == "high":
        return "qualified"
    if confidence == "medium" and score >= min_score_medium:
        return "qualified"
    return "review"


def score_to_tier(score: int, rules: dict) -> str:
    tiers = rules["tiers"]
    ordered = sorted(tiers.items(), key=lambda kv: kv[1]["min_score"], reverse=True)
    for _, cfg in ordered:
        if score >= cfg["min_score"]:
            return cfg["label"]
    return "D"


def qualify_record(record: dict, rules: dict, evaluated_at: str) -> dict:
    text = _lower(record.get("business_name"), record.get("category"))

    hard_exclusions = evaluate_hard_exclusions(record, rules, text)
    residential_exclusions = evaluate_residential_only(record, rules, text)
    exclusions = hard_exclusions + residential_exclusions

    score, reasons, evidence = evaluate_positive_signals(record, rules, text)

    if hard_exclusions:
        evidence.add("hard_exclusion")
    if residential_exclusions:
        evidence.add("residential_only_signal")

    if exclusions:
        tier = "D"
    else:
        tier = score_to_tier(score, rules)

    confidence = evaluate_confidence(evidence)
    status = evaluate_status(exclusions, confidence, score, rules)

    return {
        "master_id": record.get("master_id"),
        "business_name": record.get("business_name"),
        "website": record.get("website"),
        "phone": record.get("phone"),
        "address": record.get("address"),
        "city": record.get("city"),
        "state": record.get("state"),
        "country": record.get("country"),
        "rating": record.get("rating"),
        "review_count": record.get("review_count"),
        "icp_tier": tier,
        "icp_score": score,
        "icp_status": status,
        "icp_confidence": confidence,
        "icp_evidence": sorted(evidence),
        "icp_reasons": reasons,
        "icp_exclusions": exclusions,
        "icp_evaluated_at": evaluated_at,
    }


def qualify_all(records: list[dict], rules: dict, evaluated_at: str) -> list[dict]:
    results = [qualify_record(record, rules, evaluated_at) for record in records]
    # Deterministic ordering regardless of input order/master.json write order.
    results.sort(key=lambda r: (r.get("master_id") or ""))
    return results


def write_outputs(results: list[dict], out_dir: str) -> tuple[str, str]:
    os.makedirs(out_dir, exist_ok=True)
    json_path = os.path.join(out_dir, "icp_qualified.json")
    csv_path = os.path.join(out_dir, "icp_qualified.csv")

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in results:
            flat = dict(row)
            flat["icp_reasons"] = " | ".join(row.get("icp_reasons") or [])
            flat["icp_exclusions"] = " | ".join(row.get("icp_exclusions") or [])
            flat["icp_evidence"] = " | ".join(row.get("icp_evidence") or [])
            writer.writerow({k: ("" if flat.get(k) is None else flat.get(k)) for k in OUTPUT_FIELDS})

    return json_path, csv_path


def run(master_json_path: str, rules_path: str, out_dir: str, evaluated_at: str | None = None) -> dict:
    rules = load_rules(rules_path)
    records = load_master(master_json_path)
    evaluated_at = evaluated_at or datetime.now(timezone.utc).isoformat()

    results = qualify_all(records, rules, evaluated_at)
    json_path, csv_path = write_outputs(results, out_dir)

    tier_counts: dict[str, int] = {}
    status_counts: dict[str, int] = {}
    confidence_counts: dict[str, int] = {}
    for row in results:
        tier_counts[row["icp_tier"]] = tier_counts.get(row["icp_tier"], 0) + 1
        status_counts[row["icp_status"]] = status_counts.get(row["icp_status"], 0) + 1
        confidence_counts[row["icp_confidence"]] = confidence_counts.get(row["icp_confidence"], 0) + 1

    return {
        "records_evaluated": len(results),
        "tier_counts": tier_counts,
        "status_counts": status_counts,
        "confidence_counts": confidence_counts,
        "artifacts": {"icp_json": json_path, "icp_csv": csv_path},
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--master-json", default="data/master/master.json", help="Path to the master prospect store JSON")
    parser.add_argument("--rules", default="config/icp_rules.json", help="Path to the ICP rules config JSON")
    parser.add_argument("--out-dir", default="data/icp", help="Directory to write generated ICP output (default: data/icp)")
    parser.add_argument(
        "--evaluated-at",
        default=None,
        help="Override icp_evaluated_at (RFC3339). Defaults to current UTC time. Pass a fixed value for byte-identical reruns.",
    )
    args = parser.parse_args(argv)

    stats = run(args.master_json, args.rules, args.out_dir, args.evaluated_at)
    print(json.dumps(stats, indent=2))
    return stats


if __name__ == "__main__":
    main(sys.argv[1:])
