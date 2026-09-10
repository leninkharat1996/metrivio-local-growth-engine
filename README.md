# Metrivio Local Growth Engine

Free, open-source B2B prospecting engine for Metrivio. Built entirely from
free/open-source software — no paid APIs (no Apify, no Apollo, no Google
Places API).

## Google Maps Scraping Layer

The first layer of the prospecting engine: scrapes business listings from
Google Maps using [gosom/google-maps-scraper](https://github.com/gosom/google-maps-scraper)
(MIT licensed) and produces clean, deduplicated leads.

**What it does:** given a search plan of one or more keyword+location
searches, it validates each line, runs the scraper per search, normalizes
the raw results into a canonical lead schema, and deduplicates them
deterministically (no AI reasoning per-row) — all in one auditable,
repeatable run.

**How to trigger a collection run from GitHub Actions:**

1. Go to the repo's **Actions** tab → **Google Maps Scraper** workflow.
2. Click **Run workflow** and fill in:
   - `search_plan` — one search per line, `keyword | location | depth`:
     ```
     commercial HVAC contractors | Dallas, TX | 3
     commercial HVAC companies | Dallas, TX | 3
     industrial HVAC contractors | Dallas, TX | 3
     ```
     `depth` is optional per line (falls back to `default_depth`). Blank
     lines and `#` comments are ignored.
   - `default_depth` — optional, default `5` (higher = more results per
     search, slower)
3. Runs entirely on GitHub-hosted runners — nothing to install locally, no
   Docker Desktop required.

Each search line is validated, queried, scraped, and normalized
independently, and a failure on one line does not stop the others — the
workflow continues to the next search and records the failure.

**Where results appear:** as workflow run artifacts (Actions run page →
Artifacts section), named with the run's deterministic `run_id`:
- `google-maps-raw-<run_id>` — untouched scraper output, one file per search
- `google-maps-clean-csv-<run_id>` / `google-maps-clean-json-<run_id>` —
  deduplicated CSV/JSON in the canonical lead schema, one pair per search
- `google-maps-manifest-<run_id>` — `manifest.json`: run statistics and
  per-search success/failure detail (see the skill doc for the full shape)

Outputs are written to `data/google-maps/<run_id>/<search_id>/{raw,clean}/`
so two searches in the same run can never mix their records. Every clean
record also carries `search_keyword`, `search_location`, `search_id`, and
`run_id` fields for provenance.

The workflow job itself fails (non-zero) whenever the run's
`overall_status` isn't `success` — a run with any invalid line or failed
search is never reported as clean, even though the artifacts from
successful searches are still uploaded.

Datasets under `data/google-maps/` are gitignored by default and are not
committed to the repo automatically.

**How the Claude skill is invoked:** the skill at
`.claude/skills/google-maps-scraper/SKILL.md` teaches Claude how to trigger
this workflow (or run the scripts directly) and how to report result counts
without re-deriving them by reading the data itself. Reference it with
`/google-maps-scraper` in a Claude Code session, or Claude will pick it up
automatically when asked to run a Google Maps prospecting search.

This layer intentionally stops at clean Google Maps leads — no email
finding, enrichment, LinkedIn scraping, CRM sync, scoring, or outreach.

## Persistent Master Prospect Store

The same business can be scraped repeatedly across different cities,
keywords, and dates. `scripts/google_maps_scraper/update_master.py`
deterministically upserts every run's clean output into a single,
deduplicated master store so each business appears once, while a separate
discovery-history log records every run/search that surfaced it.

- `data/master/master.csv` / `master.json` — one row per unique business
  (canonical schema + `master_id`, `first_seen_at`, `last_seen_at`,
  `source_count`, `search_count`).
- `data/master/discovery_history.csv` — one row per run/search that
  discovered a business (`master_id`, `run_id`, `search_id`,
  `search_keyword`, `search_location`, `source`, `first_discovered_at`).
- `data/master/identity_conflicts.csv` — one row per detected identity
  collision (see below), for human review; normally empty.

Every incoming lead is resolved against a deterministic identity index
(`identity key -> master_id`) rebuilt from the existing master store —
normalized website domain, else normalized phone, else normalized
name+address, no fuzzy or AI matching — so a business keeps the same
`master_id` across runs, keywords, and cities even when its website or
phone is missing from a later scrape. Genuinely conflicting identifiers
(e.g. a lead whose website matches one master but whose phone matches a
different master) are never silently merged; they're resolved
deterministically and logged to `identity_conflicts.csv`. See the skill
doc's "Persistent master prospect store" section for the full
resolution/merge/conflict rules.

Run it manually with:
```
python3 scripts/google_maps_scraper/update_master.py \
  --clean-json "data/google-maps/<run_id>/*/clean/clean.json" \
  --master-dir data/master
```
Reprocessing the same clean dataset is idempotent (no duplicate records or
history events).

The `Google Maps Scraper` workflow runs this automatically after each
scrape and uploads `google-maps-master-store-<run_id>` (all four files) as
a workflow artifact. It also best-effort persists `data/master/` between
runs via `actions/cache` so the store can accumulate over time without ever
committing lead data to git — `data/master/*` is gitignored just like
`data/google-maps/`.

**Cache eviction fallback:** `actions/cache` is best-effort — GitHub can
evict a cache entry at any time (LRU eviction, the ~7-day-unused policy,
the ~10GB-per-repo cap, or a race with a concurrent run). The workflow
does not just start from an empty master store when the cache misses:
before running `update_master.py`, it checks whether `data/master/`
actually restored, and if not, looks up the most recent **successful**
`google-maps-scraper.yml` run that uploaded a `google-maps-master-store-*`
artifact and downloads *that* into `data/master/` first. Every run logs
which source it used:
- `MASTER_STORE_SOURCE=cache` — cache restore worked.
- `MASTER_STORE_SOURCE=artifact (run <id>, artifact <name>)` — cache
  missed, restored from that prior run's artifact instead.
- `MASTER_STORE_SOURCE=empty` — no cache hit *and* no previous successful
  run has ever uploaded a master-store artifact; this is treated as the
  first-ever run and is logged as such.

If the fallback lookup itself fails unexpectedly (GitHub API/network
error, or an artifact is found but fails to download/extract), the
workflow **fails outright** rather than silently falling back to an empty
store — an empty master store is only ever used when no prior artifact
genuinely exists.

## Preliminary ICP Qualification Layer

A deterministic, explainable filtering layer on top of the master prospect
store: `scripts/icp/qualify.py` reads `data/master/master.json` and scores
every record against the rules in `config/icp_rules.json`, producing a
primary `icp_status` decision (`qualified` / `review` / `excluded`) with an
`icp_confidence` (`high` / `medium` / `low`), a supporting `icp_evidence`
tag list, and a secondary transparent A+/A/B/C/D `icp_tier`/`icp_score`.

**This is explicitly a PRELIMINARY layer.** It reasons only over fields
that already exist today on a master record — business name,
category/type, website presence, rating, review_count, address/city/state/
country, and employee_count if present. It does **not** and cannot know a
business's true revenue, service capacity, or commercial specialization
with certainty from these fields alone. Final ICP qualification will
require later website/business enrichment — that enrichment step is **not**
built in this repo. Because of this, `icp_status: review` is an
**intentional, expected outcome** for a large share of records (e.g. a
generic "Joe's HVAC" with category "HVAC contractor" and no explicit
commercial keyword) — it is not a bug or a failure of the rules, it means
"plausible fit, insufficient current evidence to confidently qualify."

**What it is not:** this is pure rule-based Python — no AI/LLM calls, no
paid APIs, no web scraping, no enrichment (email finding, LinkedIn
scraping, CRM sync) and no outreach of any kind happen in this layer. It
only reads fields that already exist on a master record.

**ICP definition (who this engine looks for):** commercial and industrial
HVAC/mechanical service providers based in the US — businesses whose name
or category signals commercial-focused HVAC work (e.g. "commercial HVAC",
"industrial HVAC", "commercial mechanical", "commercial refrigeration",
"commercial air conditioning", "building automation", "HVAC controls",
"chiller", "rooftop unit"/RTU, "boiler service", or "mechanical
contractor" combined with a general HVAC signal) or that serve commercial
customer verticals (restaurants, retail, offices, multifamily/property
management, warehouses, hospitality, healthcare facilities, etc).
Residential-only HVAC businesses are excluded; a business that serves both
residential and commercial customers is **not** auto-rejected and is
scored normally, since mixed-book HVAC shops are still viable commercial
prospects.

**Hard exclusions (force `icp_status: excluded` and Tier D regardless of
any positive signal):** distributor / manufacturer / wholesaler / supply
store, training or trade school, staffing/recruiting agency, government
entity, a business that is plumbing-only or electrical-only or
handyman-only with no HVAC signal present, a business outside the US, and
residential-only HVAC (no commercial signal anywhere in its name/
category).

**`icp_confidence` (high / medium / low):** driven **only** by direct
keyword evidence, never by the generic secondary bonuses below —
website presence, a good rating, a high review count, or an employee
count can nudge the *score* within a confidence band but can never by
themselves manufacture `high` confidence or upgrade `low` to `medium`.
- `high` — a strong, direct commercial-HVAC keyword matched in name/
  category (e.g. "commercial HVAC", "commercial mechanical", "industrial
  HVAC", "commercial air conditioning", "commercial refrigeration",
  "building automation", "HVAC controls", "chiller", "rooftop unit"/RTU,
  "boiler service"), or "mechanical contractor" combined with a general
  HVAC signal elsewhere in the text.
- `medium` — a general HVAC/mechanical signal matched (e.g. plain "HVAC
  contractor") together with a commercial customer/vertical signal (e.g.
  "restaurant", "property management", "hospitality"), but no direct
  commercial-HVAC keyword.
- `low` — ambiguous or insufficient evidence (no signal, or only a
  general HVAC keyword with no commercial vertical signal).

**`icp_status` (qualified / review / excluded) — the PRIMARY downstream
decision; `icp_tier` remains a secondary, transparent score
classification:**
- `qualified` — no exclusion, and confidence is `high`, or `medium` with a
  strong score (≥ 65, the "A" tier threshold).
- `review` — no exclusion, but current evidence isn't strong enough to
  confidently qualify. Expected and common; not a failure.
- `excluded` — any hard exclusion matched, or explicit residential-only
  HVAC with no commercial evidence.

**`icp_evidence`:** a list of matched deterministic evidence tags actually
observed on the record, drawn from a fixed vocabulary: `commercial_hvac_
signal`, `industrial_signal`, `commercial_service_signal`, `commercial_
vertical_signal`, `general_hvac_signal`, `website_present`, `strong_
rating`, `strong_review_count`, `employee_count_signal`, `hard_exclusion`,
`residential_only_signal`. Only literal keyword/field matches are tagged —
never invented conclusions like "high revenue" or "has capacity".

**How scoring works:** every record starts from a base score and gains
points additively for positive signals — a general HVAC/mechanical
keyword match, strong commercial-service-provider keywords (capped),
"mechanical contractor" (capped, direct-but-ambiguous), commercial
customer/vertical keywords (capped), having a website on file, a Google
rating ≥ 4.0, and review-count thresholds. Employee/company-size data is
used **only** as a bonus when a record happens to carry it — it is never
required and never fabricated, since the current collection pipeline does
not produce it. A record with no positive signal at all (no HVAC-relevant
text, no website, no rating/review data) is capped so it cannot reach tier
A/A+ on the base score alone. The full list of matched signals is
included in `icp_reasons` on every record; any hard exclusion that
applied is listed in `icp_exclusions`. All rules, keyword lists, and
point values live in `config/icp_rules.json`, separate from the scoring
logic in `scripts/icp/qualify.py`, so the ICP definition can be tuned
without touching code.

**Tiers (secondary, transparent score classification):** A+ (85+) and A
(65+) are strong commercial-fit prospects by score; B (40+) is a
plausible fit worth a manual look; C (15+) is weak/uncertain evidence; D
is either a low score or a hard exclusion. Downstream logic should treat
`icp_status` as primary and `icp_tier` as supporting detail.

**Determinism:** the same master record always produces the same score,
tier, status, confidence, evidence, reasons, and exclusions — matching
normalize.py/update_master.py's existing no-AI, no-fuzzy-matching
philosophy. The only non-deterministic field is `icp_evaluated_at`
(current UTC time by default); pass `--evaluated-at` to pin it for
reproducible output.

**How to run it:**
```
python3 scripts/icp/qualify.py \
  --master-json data/master/master.json \
  --rules config/icp_rules.json \
  --out-dir data/icp
```
Output is written to `data/icp/icp_qualified.json` and `.csv` (`master_id`,
`business_name`, `website`, `phone`, `address`, `city`, `state`,
`country`, `rating`, `review_count`, `icp_tier`, `icp_score`,
`icp_status`, `icp_confidence`, `icp_evidence`, `icp_reasons`,
`icp_exclusions`, `icp_evaluated_at`). The filenames are unchanged from
the original layer; only the schema gained the three new fields. This
never mutates `data/master/` — it is a read-only pass over the master
store — and `data/icp/` is gitignored the same way `data/master/` and
`data/google-maps/` are, since it is fully regenerable from the master
store at any time.

This layer intentionally stops at a preliminary qualification status,
confidence, tier, and score — no enrichment, no contact discovery, no CRM
sync, and no outreach happen here or anywhere else in this repo yet. A
future enrichment step (website/business-level lookups) would be needed
to move `review` records to a final, confident qualification.

## Step 9 -- Website Enrichment

A free/open-source, GitHub-Actions-compatible **evidence-collection**
layer on top of the master prospect store: `scripts/enrichment/
enrich_websites.py` reads the `website` field already present on each
master record (read-only, never mutates `data/master/`) and crawls the
homepage plus a small number of same-domain internal pages to collect
factual, publicly-visible website evidence for a later, not-yet-built ICP
qualification step.

**Dependency -- Scrapling:** website fetching and HTML parsing both go
through [Scrapling](https://github.com/D4Vinci/Scrapling), a free/open-
source Python fetching + parsing framework. It is pinned in the repo's
`requirements.txt` (the first third-party dependency in this repo -- the
Google Maps and ICP layers remain stdlib-only):

```
python3 -m pip install -r requirements.txt   # scrapling[fetchers]==0.4.15
```

Only Scrapling's **plain HTTP `Fetcher`** and its parser/selector API are
used, wrapped in a single adapter (`scripts/enrichment/scrapling_client.py`)
that is also the only place tests substitute a fake. `StealthyFetcher`,
`DynamicFetcher`, browser-fingerprint impersonation, stealth headers,
proxies/proxy rotation and CAPTCHA solving are **never used** -- if a site
blocks us we record the failure and stop crawling that domain.

**What it is not:** this layer makes **no final ICP decisions**. It does
not score, tier, qualify, or exclude a business -- it only records what a
website literally says, with a page URL and a text snippet for every
match, so a future qualification layer can judge strength on its own.
There is **no AI/LLM call anywhere in this layer** -- evidence extraction
is pure deterministic keyword matching (`scripts/enrichment/
vocabulary.py`), matching the existing preliminary ICP layer's no-AI
philosophy. No paid APIs, no email/phone/individual-name/LinkedIn/social-
profile extraction, no CRM sync, no outreach.

**Inputs:** `data/master/master.json` (read-only) and an optional JSON
config overriding crawl defaults (`max_pages_per_domain`,
`request_timeout`, `user_agent`, `max_text_length`, `max_snippet_length`,
`allowed_schemes`, `respect_robots_txt`).

**Crawl scope and page limit:** homepage + same-domain internal pages
only -- external domains are never followed. Discovered links are
normalized (fragment stripped, tracking params like `utm_*`/`gclid`/
`fbclid` stripped, deduped) and links whose URL/anchor text look
commercially relevant (services, commercial, industries, maintenance,
about, contact, or any evidence keyword) are prioritized. Default cap:
**5 pages per domain** (`--max-pages` / `max_pages_per_domain`). A simple,
best-effort `robots.txt` check is applied per domain (matching user-agent
or `*` group, longest-prefix Allow/Disallow) and fails open (treated as
allowed) if `robots.txt` cannot be fetched or parsed.

**Evidence philosophy -- factual extraction, not classification:** every
match against the fixed vocabulary (commercial HVAC/mechanical terms,
commercial service-delivery terms like preventive maintenance/service
contracts, commercial customer-vertical terms like warehouse/healthcare/
property management, and residential terms) is recorded as a
`{category, keyword, page_url, snippet, evidence_type}` evidence item -- literal keyword
matches only, never an inferred conclusion like "this is a commercial
HVAC company." `evidence_type` is itself decided by fixed literal marker
phrases, never by inference: `direct_service` (a service-verb phrase such
as "we provide/offer/install/service/repair/maintain" appears in the same
sentence), `customer_vertical` (a customer/building-type term),
`residential`, or `incidental` -- a product/brand/parts context such as
"we install HVAC products from leading manufacturers", which does **not**
prove HVAC contracting. Incidental deliberately beats direct_service when
both marker kinds are present, so the layer under-claims rather than
over-claims. Evidence lists are sorted deterministically (category,
then page_url, then keyword), and the same input + config always produces
the same output (aside from crawl timestamps, which `--crawled-at`-style
determinism in tests pins via an injectable clock).

**Failure handling:** every record is processed independently and a
broken page or site never stops the batch. `website_status` per record is
one of: `success`, `partial` (some pages crawled, some failed),
`no_website` (no website field -- no HTTP request attempted), `invalid_url`
(unparseable or disallowed scheme -- no HTTP request attempted), `blocked`
(HTTP 403/429), `timeout`, `http_error` (other 4xx/5xx), `non_html`
(non-HTML content-type), `connection_error`, or `failed` (any other
request error). Per-page failures are collected in `crawl_errors`.

**Output schema** (`data/enrichment/website_enrichment.json` and `.csv`,
one row per master record): `master_id`, `business_name`, `website`,
`website_status`, `http_status`, `final_url`, `crawl_started_at`,
`crawl_completed_at`, `pages_attempted`, `pages_crawled`, `pages_failed`, `homepage_title`,
`homepage_description`, `commercial_hvac_signals`,
`commercial_service_signals`, `commercial_vertical_signals`,
`residential_signals` (rollup counts), `evidence` (the full structured
evidence list), `crawl_errors`. The JSON file wraps these as
`{"stats": {...}, "records": [...]}`, where the stats block carries
`records_seen`, `records_with_websites`, `no_website`, `successful`,
`partial`, `blocked`, `failed`, `pages_attempted`, `pages_successful` and
`pages_failed`; the CSV is the flattened one-row-per-record summary. No raw HTML is stored; body text is
whitespace-normalized and length-capped (`max_text_length`), and evidence
snippets are separately length-capped (`max_snippet_length`), so a single
site cannot blow up output size.

**How to run it locally:**
```
python3 scripts/enrichment/enrich_websites.py \
  --master-json data/master/master.json \
  --out-dir data/enrichment \
  [--config path/to/config.json] [--max-pages 5] [--limit 50]
```
This never mutates `data/master/` -- it is a read-only pass over the
master store -- and `data/enrichment/` is gitignored the same way
`data/master/`, `data/google-maps/`, and `data/icp/` are, since it is
fully regenerable from the master store at any time.

**GitHub Actions:** this layer is written to be GitHub-Actions-compatible
(pure Python + Scrapling's plain HTTP fetcher, no browser/headless-Chrome
use, no paid API keys required) but **no production workflow has been added
for it yet** -- it currently runs locally/on-demand only via the CLI
above -- **production crawling is not enabled yet**. Its test suite
(`tests/test_enrich_websites.py`, with HTML fixtures under
`tests/fixtures/website_enrichment/`) mocks the network fetch only: fixture
HTML is fed through Scrapling's real parser, so no test makes a real
network call.

**This layer does not make final ICP decisions.** It stops at collecting
and structuring factual website evidence. A future, separate qualification
layer is expected to consume `data/enrichment/website_enrichment.json`
(alongside `data/icp/icp_qualified.json`) to produce a final,
confidence-upgraded ICP result -- that layer is not built in this repo yet.

**This is not a transactional database.** GitHub Actions cache and
artifacts provide no locking: two workflow runs updating the master store
at the same time can still race each other. The workflow sets
`concurrency: {group: google-maps-master-store, cancel-in-progress: false}`
at the workflow level, which makes GitHub queue overlapping runs of this
workflow one at a time instead of letting two writers touch
`data/master/` concurrently — this is GitHub-native queuing, not a
guarantee against every possible race (e.g. a run started outside this
workflow's queue, or a manual artifact download/upload done by hand).
Artifacts also have a finite retention window (`retention-days: 90` here,
and GitHub enforces its own account/plan-level caps) — they are durable
relative to the cache, not permanent.

## Step 10 -- Final ICP Qualification

The pipeline now runs in three stages: **Google Maps preliminary
qualification** (name/category text signals only) → **website enrichment**
(Step 9 -- factual, keyword-matched evidence from a business's own site)
→ **Final ICP qualification** (`scripts/final_icp/qualify_final.py`),
which is the last, decision-making layer. It reads `data/icp/
icp_qualified.json` and `data/enrichment/website_enrichment.json`
read-only, left-joined by `master_id` (a record with no website/no
enrichment row is still evaluated, via the fallback path below), and
never mutates the master store, the preliminary ICP output, or the
enrichment output.

**Evidence-gated, not score-gated.** `final_icp_status`
(`qualified`/`review`/`excluded`) is decided entirely by a fixed rule
hierarchy -- geography, hard exclusions, residential-only, then direct
commercial-service website evidence -- *before* any score is computed.
Qualifying on website evidence requires at least one `direct_service`
match against an **HVAC-specific** category (commercial/industrial HVAC,
commercial mechanical, chillers, RTUs/rooftop units, boilers, HVAC
controls, building automation, energy management, commercial
refrigeration/air-conditioning/heating/cooling) -- generic commercial
service signals alone (preventive maintenance, maintenance/service
contracts, mechanical services, design-build, facility services) or
commercial-vertical mentions can never qualify a record on their own, no
matter how many of them are present. A business whose website is
unavailable or unusable can still qualify through a single fallback path
(high preliminary confidence, no preliminary exclusion, US-confirmed
geography), but is always capped at `medium` confidence and tier `B`.

`final_icp_score` (0-100, fixed point table) and `final_icp_tier`
(`A+`/`A`/`B`/`C`/`D`) are computed strictly *after* status is finalized
and are **prioritization signals only** -- they can never promote a
record past the status the gate rules already decided. A record with a
high rating, many reviews, broad commercial-vertical coverage, or several
generic service-category matches but zero HVAC-specific evidence remains
`review` (or `excluded`), regardless of score. Tier bands are additionally
gated (e.g. `A+` requires a fully successful website crawl and at least
two distinct HVAC-specific categories) so a fallback-qualified or
partially-crawled record cannot outrank a fully evidence-confirmed one.

**Output:** `data/final_icp/final_icp_qualified.json` and `.csv`, one row
per preliminary ICP record, sorted by `master_id`. Configuration lives in
`config/final_icp_rules.json` (category sets, score-bucket point tables,
tier thresholds -- no field is hardcoded in the script). Same as the
other generated datasets, `data/final_icp/` is gitignored and fully
regenerable:

```
python3 scripts/final_icp/qualify_final.py \
  --master-json data/master/master.json \
  --preliminary-json data/icp/icp_qualified.json \
  --enrichment-json data/enrichment/website_enrichment.json \
  --out-dir data/final_icp
```

No AI/LLM calls, no paid APIs, no network requests, no IP geolocation --
geography uses only the `country`/`state` fields already present on the
master/preliminary record. Deterministic: the same three input files plus
a fixed `--evaluated-at` produce byte-identical output.
