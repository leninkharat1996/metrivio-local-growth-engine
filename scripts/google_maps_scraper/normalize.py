#!/usr/bin/env python3
"""
Deterministic normalization + deduplication for gosom/google-maps-scraper output.

No AI calls are made here on purpose: normalization/dedup is pure, rule-based
Python so Claude usage stays limited to orchestration, not per-row reasoning.

Usage:
    python3 normalize.py \
        --raw-csv data/google-maps/raw/raw_20260909_120000.csv \
        --category "commercial HVAC contractors" \
        --location "Dallas, TX" \
        --out-prefix data/google-maps/clean/dallas_tx_hvac_20260909_120000
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from datetime import datetime, timezone
from urllib.parse import urlparse

CANONICAL_FIELDS = [
    "business_name",
    "category",
    "address",
    "city",
    "state",
    "country",
    "phone",
    "website",
    "google_maps_url",
    "rating",
    "review_count",
    "latitude",
    "longitude",
    "source",
    "scraped_at",
]

# gosom/google-maps-scraper CSV/JSON header names we might see, mapped to our
# canonical fields. Kept permissive because upstream column names have
# changed across versions.
RAW_FIELD_ALIASES = {
    "business_name": ["title", "name", "business_name"],
    "address": ["address", "complete_address", "full_address"],
    "phone": ["phone", "phone_number"],
    "website": ["website", "web_site"],
    "google_maps_url": ["link", "url", "google_maps_url", "place_url"],
    "rating": ["review_rating", "rating"],
    "review_count": ["review_count", "reviews_count", "user_ratings_total"],
    "latitude": ["latitude", "lat"],
    "longitude": ["longitude", "lng", "long"],
}


def _first_present(row: dict, keys: list[str]) -> str:
    for key in keys:
        if key in row and row[key] not in (None, ""):
            return str(row[key]).strip()
    return ""


def normalize_whitespace(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def normalize_name_key(name: str) -> str:
    """Lowercase, strip punctuation/legal suffixes for dedup matching only."""
    name = name.lower()
    name = re.sub(r"[^a-z0-9 ]", "", name)
    name = re.sub(
        r"\b(llc|inc|corp|co|ltd|company|the)\b", "", name
    )
    return normalize_whitespace(name)


def normalize_address_key(address: str) -> str:
    address = address.lower()
    address = re.sub(r"[^a-z0-9 ]", "", address)
    address = re.sub(
        r"\b(street|st|avenue|ave|road|rd|boulevard|blvd|suite|ste|drive|dr|floor|fl)\b",
        "",
        address,
    )
    return normalize_whitespace(address)


def normalize_phone_key(phone: str) -> str:
    digits = re.sub(r"\D", "", phone or "")
    # Drop a leading US country code so "+1 214..." matches "214..."
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits


def domain_key(website: str) -> str:
    if not website:
        return ""
    parsed = urlparse(website if "://" in website else f"//{website}")
    host = (parsed.netloc or parsed.path or "").lower()
    host = host.split("/")[0]
    if host.startswith("www."):
        host = host[4:]
    return host


def parse_city_state_country(address: str) -> tuple[str, str, str]:
    """Best-effort split of a US-style address: '..., City, ST ZIP, Country'.

    This is deliberately simple/deterministic. Malformed or non-US addresses
    fall back to empty city/state and country left blank rather than guessed.
    """
    if not address:
        return "", "", ""
    parts = [normalize_whitespace(p) for p in address.split(",") if normalize_whitespace(p)]
    city, state, country = "", "", ""
    if not parts:
        return city, state, country

    # Country is often the last segment if it's a known non-US-state token.
    if parts and re.search(r"[A-Za-z]", parts[-1]) and not re.match(r"^[A-Z]{2}\s*\d{0,5}$", parts[-1]):
        maybe_country = parts[-1]
        if len(maybe_country) > 2 and not re.search(r"\d", maybe_country):
            country = maybe_country
            parts = parts[:-1]

    if parts:
        state_zip = parts[-1]
        m = re.match(r"^([A-Za-z]{2})\s*(\d{5}(-\d{4})?)?$", state_zip)
        if m:
            state = m.group(1).upper()
            parts = parts[:-1]

    if parts:
        city = parts[-1]

    if not country and state:
        country = "USA"

    return city, state, country


def to_float(value: str):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def to_int(value: str):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def load_raw_rows(path: str) -> list[dict]:
    if path.endswith(".json"):
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else data.get("results", [])
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def build_lead(raw_row: dict, category: str, location: str, scraped_at: str) -> dict | None:
    business_name = normalize_whitespace(_first_present(raw_row, RAW_FIELD_ALIASES["business_name"]))
    if not business_name:
        # Malformed record with no name is not a usable lead.
        return None

    address = normalize_whitespace(_first_present(raw_row, RAW_FIELD_ALIASES["address"]))
    city, state, country = parse_city_state_country(address)

    phone = normalize_whitespace(_first_present(raw_row, RAW_FIELD_ALIASES["phone"]))
    website = normalize_whitespace(_first_present(raw_row, RAW_FIELD_ALIASES["website"]))
    google_maps_url = normalize_whitespace(_first_present(raw_row, RAW_FIELD_ALIASES["google_maps_url"]))

    lead = {
        "business_name": business_name,
        "category": category,
        "address": address,
        "city": city,
        "state": state,
        "country": country,
        "phone": phone,
        "website": website,
        "google_maps_url": google_maps_url,
        "rating": to_float(_first_present(raw_row, RAW_FIELD_ALIASES["rating"])),
        "review_count": to_int(_first_present(raw_row, RAW_FIELD_ALIASES["review_count"])),
        "latitude": to_float(_first_present(raw_row, RAW_FIELD_ALIASES["latitude"])),
        "longitude": to_float(_first_present(raw_row, RAW_FIELD_ALIASES["longitude"])),
        "source": "google_maps_scraper",
        "scraped_at": scraped_at,
        "_search_location": location,
    }
    return lead


def dedup_key(lead: dict) -> tuple:
    """Primary key used only for display/back-compat; see match_keys() for
    the actual matching logic, which considers all available identifiers."""
    domain = domain_key(lead.get("website", ""))
    phone = normalize_phone_key(lead.get("phone", ""))
    if domain:
        return ("domain", domain)
    if phone:
        return ("phone", phone)
    return (
        "name_address",
        normalize_name_key(lead.get("business_name", "")),
        normalize_address_key(lead.get("address", "")),
    )


def match_keys(lead: dict) -> list[tuple]:
    """All identifiers this lead could be matched on. Two leads are the same
    business if they share ANY of these keys."""
    keys = []
    domain = domain_key(lead.get("website", ""))
    if domain:
        keys.append(("domain", domain))
    phone = normalize_phone_key(lead.get("phone", ""))
    if phone:
        keys.append(("phone", phone))
    name_key = normalize_name_key(lead.get("business_name", ""))
    address_key = normalize_address_key(lead.get("address", ""))
    if name_key and address_key:
        keys.append(("name_address", name_key, address_key))
    return keys


def is_complete(lead: dict) -> bool:
    return bool(lead.get("phone") or lead.get("website"))


def normalize_and_dedup(raw_rows: list[dict], category: str, location: str, scraped_at: str):
    leads = []
    malformed = 0
    for raw_row in raw_rows:
        lead = build_lead(raw_row, category, location, scraped_at)
        if lead is None:
            malformed += 1
            continue
        leads.append(lead)

    # Union-find over all match keys so two leads merge if they share ANY
    # identifier (domain, phone, or name+address), even if each lead only
    # has partial contact info populated.
    parent: dict[tuple, tuple] = {}

    def find(x: tuple) -> tuple:
        while parent.get(x, x) != x:
            parent[x] = parent.get(parent[x], parent[x])
            x = parent[x]
        return x

    def union(a: tuple, b: tuple) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    key_to_lead_indices: dict[tuple, list[int]] = {}
    for idx, lead in enumerate(leads):
        for key in match_keys(lead):
            parent.setdefault(key, key)
            key_to_lead_indices.setdefault(key, []).append(idx)

    lead_group: dict[int, tuple] = {}
    for idx, lead in enumerate(leads):
        keys = match_keys(lead)
        if not keys:
            lead_group[idx] = ("_unmatched", idx)
            continue
        first = keys[0]
        for other in keys[1:]:
            union(first, other)
        lead_group[idx] = first

    groups: dict[tuple, list[int]] = {}
    for idx, key in lead_group.items():
        root = find(key) if key[0] != "_unmatched" else key
        groups.setdefault(root, []).append(idx)

    duplicate_count = 0
    clean_leads = []
    for indices in groups.values():
        duplicate_count += len(indices) - 1
        best = leads[indices[0]]
        for idx in indices[1:]:
            candidate = leads[idx]
            if is_complete(candidate) and not is_complete(best):
                best = candidate
        clean_leads.append(best)
    partial_leads = [lead for lead in clean_leads if not is_complete(lead)]

    stats = {
        "raw_records": len(raw_rows),
        "malformed_skipped": malformed,
        "duplicates_merged": duplicate_count,
        "clean_records": len(clean_leads),
        "partial_records_missing_phone_and_website": len(partial_leads),
    }
    return clean_leads, stats


def write_outputs(leads: list[dict], out_prefix: str):
    csv_path = f"{out_prefix}.csv"
    json_path = f"{out_prefix}.json"

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CANONICAL_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for lead in leads:
            writer.writerow({k: ("" if lead.get(k) is None else lead.get(k)) for k in CANONICAL_FIELDS})

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(
            [{k: lead.get(k) for k in CANONICAL_FIELDS} for lead in leads],
            f,
            indent=2,
            ensure_ascii=False,
        )

    return csv_path, json_path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-csv", required=True, help="Path to raw scraper output (.csv or .json)")
    parser.add_argument("--category", required=True, help="Business category/keyword searched")
    parser.add_argument("--location", required=True, help="City/state location searched")
    parser.add_argument("--out-prefix", required=True, help="Output path prefix (no extension)")
    args = parser.parse_args(argv)

    scraped_at = datetime.now(timezone.utc).isoformat()
    raw_rows = load_raw_rows(args.raw_csv)
    clean_leads, stats = normalize_and_dedup(raw_rows, args.category, args.location, scraped_at)
    csv_path, json_path = write_outputs(clean_leads, args.out_prefix)

    stats["csv_path"] = csv_path
    stats["json_path"] = json_path
    print(json.dumps(stats, indent=2))
    return stats


if __name__ == "__main__":
    main(sys.argv[1:])
