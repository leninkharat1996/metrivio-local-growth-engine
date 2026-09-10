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
    "commercial_hvac": [
        "commercial hvac",
        "commercial hvac service",
        "commercial hvac contractor",
        "commercial hvac repair",
        "commercial hvac installation",
        "commercial hvac maintenance",
        "commercial hvac replacement",
        "hvac for businesses",
        "hvac services for businesses",
        "business hvac",
    ],
    "commercial_mechanical": ["commercial mechanical"],
    "industrial_hvac": ["industrial hvac"],
    "commercial_refrigeration": ["commercial refrigeration"],
    "commercial_air_conditioning": [
        "commercial air conditioning",
        "commercial air conditioning service",
    ],
    "commercial_heating": [
        "commercial heating",
        "commercial heating and cooling",
        "commercial heating and air",
    ],
    "commercial_cooling": ["commercial cooling"],
    "hvac_controls": ["hvac controls"],
    "building_automation": ["building automation"],
    "energy_management": ["energy management"],
    "chiller": ["chiller", "chillers"],
    "rtu_rooftop": ["rooftop unit", "rooftop units", "packaged rooftop unit", "packaged rooftop units", "rtu"],
    "boiler": ["boiler", "boilers"],
    "vrf_vrv": ["vrf system", "vrv system", "variable refrigerant flow", "vrf", "vrv"],
    # Building/facility-scoped HVAC language -- each phrase already carries
    # its own hvac/heating/cooling/air-conditioning context baked in, so no
    # separate proximity check is needed for these (unlike the generic
    # commercial-vertical building-type words below).
    "building_facility_hvac": [
        "building hvac",
        "facility hvac",
        "facilities hvac",
        "building heating and cooling",
        "facility heating and cooling",
        "building air conditioning",
        "facility air conditioning",
        "property hvac",
        "commercial property hvac",
    ],
}

# Commercial service-delivery signals.
COMMERCIAL_SERVICE_CATEGORIES: dict[str, list[str]] = {
    "preventive_maintenance": ["preventive maintenance", "preventative maintenance"],
    "maintenance_contract": ["maintenance contract", "maintenance agreement"],
    "service_contract": ["service contract", "service agreement"],
    "mechanical_services": ["mechanical services", "mechanical contractor"],
    "design_build": ["design build", "design-build"],
    "facility_services": ["facility services", "facilities services"],
    "hvac_maintenance_language": [
        "preventive hvac maintenance",
        "planned hvac maintenance",
        "hvac maintenance contract",
        "hvac maintenance contracts",
        "mechanical hvac service",
    ],
}

# Commercial customer vertical / building-type signals. NOTE: these are
# generic building/customer-type words that are also common in everyday,
# non-HVAC contexts ("office", "warehouse", "retail", ...). evidence.py
# requires one of these keywords to co-occur, in the SAME sentence, with an
# HVAC/heating/cooling/air-conditioning/service context word before it is
# recorded as evidence_type "customer_vertical" -- a bare, standalone
# mention is recorded as "incidental" instead and therefore cannot rescue a
# residential-only exclusion or otherwise stand in for real HVAC evidence.
COMMERCIAL_VERTICAL_CATEGORIES: dict[str, list[str]] = {
    "commercial_vertical": [
        "office",
        "offices",
        "office building",
        "office buildings",
        "warehouse",
        "warehouses",
        "industrial",
        "industrial facility",
        "industrial facilities",
        "manufacturing",
        "manufacturing facility",
        "manufacturing facilities",
        "retail",
        "restaurant",
        "restaurants",
        "hotel",
        "hotels",
        "hospitality",
        "healthcare",
        "hospital",
        "hospitals",
        "medical",
        "school",
        "schools",
        "education",
        "multifamily",
        "multi-family",
        "property management",
        "facilities management",
        "commercial building",
        "commercial buildings",
        "commercial property",
        "data center",
        "government",
    ]
}

# HVAC/heating/cooling/air-conditioning/service context words required, in
# the same sentence, for a COMMERCIAL_VERTICAL_CATEGORIES keyword to count
# as real "customer_vertical" evidence (see evidence.py:classify_evidence_type).
VERTICAL_CONTEXT_MARKERS: tuple[str, ...] = (
    "hvac",
    "heating",
    "cooling",
    "air conditioning",
    "air-conditioning",
    "climate control",
    "refrigeration",
    "mechanical contractor",
    "mechanical services",
    "service",
    "services",
    "maintenance",
    "repair",
    "install",
)

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

# ---------------------------------------------------------------------------
# Pre-enrichment (pre-crawl) trade filter -- Google Maps name/category text
# only, deterministic keyword matching, mirroring config/icp_rules.json's
# hard_exclusions philosophy. Used to skip website crawling entirely for
# OBVIOUS non-HVAC businesses before spending any crawl budget on them.
# Bias toward keeping ambiguous cases: a false negative (crawling a business
# that turns out not to be HVAC) is far cheaper than a false positive
# (skipping a real HVAC business), so this list is deliberately narrow and
# every skip below (except manufacturer/distributor/supply/directory, which
# are never HVAC contractors regardless of wording) is rescued by the
# presence of any HVAC-signal keyword in the same name/category text.
# ---------------------------------------------------------------------------

# Single-trade keywords with no HVAC signal alongside them -- not an HVAC
# contractor at all. Rescued by PRE_ENRICHMENT_HVAC_RESCUE_KEYWORDS.
PRE_ENRICHMENT_SINGLE_TRADE_KEYWORDS: dict[str, list[str]] = {
    "plumbing_only": ["plumbing", "plumber"],
    "electrical_only": ["electrician", "electrical contractor", "electric company"],
    "handyman_only": ["handyman", "handy man", "handy-man"],
}

# Presence of any of these in the name/category text means "this business
# does claim HVAC work" -- it rescues a single-trade keyword match above.
PRE_ENRICHMENT_HVAC_RESCUE_KEYWORDS: list[str] = [
    "hvac",
    "heating and air",
    "heating & air",
    "heating cooling",
    "heating and cooling",
    "air conditioning",
    "mechanical contractor",
    "climate control",
    "refrigeration",
]

# Manufacturer/distributor/supply-house/directory/association businesses:
# never HVAC contractors regardless of any HVAC wording alongside them, so
# no rescue keyword applies to these.
PRE_ENRICHMENT_NON_CONTRACTOR_KEYWORDS: list[str] = [
    "manufacturer",
    "manufacturing",
    "distributor",
    "distribution",
    "wholesale",
    "wholesaler",
    "supply co",
    "supply company",
    "supply store",
    "hvac supply",
    "parts store",
    "parts supply",
    "parts counter",
    "parts department",
    "trade association",
    "industry association",
    "business directory",
]

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
