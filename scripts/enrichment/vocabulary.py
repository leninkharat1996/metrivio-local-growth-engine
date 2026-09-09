"""
Fixed, deterministic keyword vocabulary for Step 9 website enrichment.

This is pure evidence collection: every keyword match is recorded as a
structured {category, keyword, page_url, snippet} evidence item. Nothing
here scores, classifies, or decides ICP fit -- that is left entirely to a
later, not-yet-built qualification layer, matching the existing
preliminary ICP layer's philosophy (config/icp_rules.json,
scripts/icp/qualify.py) of separating fixed vocabulary/config from logic.

Categories are grouped for the OUTPUT_FIELDS rollup counts
(commercial_hvac_signals / commercial_service_signals /
commercial_vertical_signals / residential_signals) but every individual
match is still recorded with its own specific category name in the
evidence list -- no information is lost by the rollup.
"""
from __future__ import annotations

# Commercial / industrial HVAC business-type signals.
COMMERCIAL_HVAC_CATEGORIES: dict[str, list[str]] = {
    "commercial_hvac": ["commercial hvac"],
    "commercial_mechanical": ["commercial mechanical"],
    "industrial_hvac": ["industrial hvac"],
    "commercial_refrigeration": ["commercial refrigeration"],
    "commercial_air_conditioning": ["commercial air conditioning"],
    "commercial_heating": ["commercial heating"],
    "commercial_cooling": ["commercial cooling"],
    "hvac_controls": ["hvac controls"],
    "building_automation": ["building automation"],
    "energy_management": ["energy management"],
    "chiller": ["chiller", "chillers"],
    "rtu_rooftop": ["rooftop unit", "rooftop units", "rtu"],
    "boiler": ["boiler", "boilers"],
}

# Commercial service-delivery signals.
COMMERCIAL_SERVICE_CATEGORIES: dict[str, list[str]] = {
    "preventive_maintenance": ["preventive maintenance", "preventative maintenance"],
    "maintenance_contract": ["maintenance contract", "maintenance agreement"],
    "service_contract": ["service contract", "service agreement"],
    "mechanical_services": ["mechanical services", "mechanical contractor"],
    "design_build": ["design build", "design-build"],
    "facility_services": ["facility services", "facilities services"],
}

# Commercial customer vertical / building-type signals.
COMMERCIAL_VERTICAL_CATEGORIES: dict[str, list[str]] = {
    "commercial_vertical": [
        "office",
        "warehouse",
        "industrial",
        "manufacturing",
        "retail",
        "restaurant",
        "hotel",
        "hospitality",
        "healthcare",
        "medical",
        "school",
        "education",
        "multifamily",
        "multi-family",
        "property management",
        "facilities management",
        "data center",
        "government",
    ]
}

# Residential signals (recorded as evidence only -- no exclusion logic here).
RESIDENTIAL_CATEGORIES: dict[str, list[str]] = {
    "residential": ["residential"],
    "residential_only_indicator": [
        "residential only",
        "residential heating and air conditioning",
        "home comfort",
        "residential service",
        "we do not service commercial",
        "homeowners",
    ],
}

# Combined lookup used by the extractor: category -> keyword list.
ALL_CATEGORIES: dict[str, list[str]] = {
    **COMMERCIAL_HVAC_CATEGORIES,
    **COMMERCIAL_SERVICE_CATEGORIES,
    **COMMERCIAL_VERTICAL_CATEGORIES,
    **RESIDENTIAL_CATEGORIES,
}

# Rollup group name -> set of category keys, for the per-record summary
# counts (commercial_hvac_signals / commercial_service_signals /
# commercial_vertical_signals / residential_signals).
ROLLUP_GROUPS: dict[str, set] = {
    "commercial_hvac_signals": set(COMMERCIAL_HVAC_CATEGORIES),
    "commercial_service_signals": set(COMMERCIAL_SERVICE_CATEGORIES),
    "commercial_vertical_signals": set(COMMERCIAL_VERTICAL_CATEGORIES),
    "residential_signals": set(RESIDENTIAL_CATEGORIES),
}

# Keywords used only to prioritize which internal links get crawled first
# (a superset covering all evidence categories plus common nav labels).
LINK_PRIORITY_KEYWORDS: list[str] = sorted(
    {kw for kws in ALL_CATEGORIES.values() for kw in kws}
    | {
        "service",
        "services",
        "about",
        "commercial",
        "industries",
        "industries-served",
        "solutions",
        "maintenance",
        "contact",
    }
)
