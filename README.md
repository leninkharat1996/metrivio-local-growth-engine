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

## ICP Qualification Layer

A deterministic, explainable filtering layer on top of the master prospect
store: `scripts/icp/qualify.py` reads `data/master/master.json` and scores
every record against the rules in `config/icp_rules.json`, producing a
tiered A+/A/B/C/D qualification with a transparent, human-readable score
breakdown.

**What it is not:** this is pure rule-based Python — no AI/LLM calls, no
paid APIs, no web scraping, no enrichment (email finding, LinkedIn
scraping, CRM sync) and no outreach of any kind happen in this layer. It
only reads fields that already exist on a master record.

**ICP definition (who this engine looks for):** commercial and industrial
HVAC/mechanical service providers based in the US — businesses whose name
or category signals commercial-focused HVAC work (e.g. "commercial HVAC",
"industrial HVAC", "mechanical contractor", "commercial refrigeration") or
that serve commercial customer verticals (restaurants, retail, offices,
multifamily/property management, warehouses, hospitality, healthcare
facilities, etc). Residential-only HVAC businesses are excluded; a
business that serves both residential and commercial customers is **not**
auto-rejected and is scored normally, since mixed-book HVAC shops are
still viable commercial prospects.

**Hard exclusions (force Tier D regardless of any positive signal):**
distributor / manufacturer / wholesaler / supply store, training or trade
school, staffing/recruiting agency, government entity, a business that is
plumbing-only or electrical-only or handyman-only with no HVAC signal
present, a business outside the US, and residential-only HVAC (no
commercial signal anywhere in its name/category).

**How scoring works:** every record starts from a base score and gains
points additively for positive signals — a general HVAC/mechanical
keyword match, strong commercial-service-provider keywords (capped),
commercial customer/vertical keywords (capped), having a website on file,
a Google rating ≥ 4.0, and review-count thresholds. Employee/company-size
data is used **only** as a bonus when a record happens to carry it — it is
never required and never fabricated, since the current collection
pipeline does not produce it. A record with no positive signal at all
(no HVAC-relevant text, no website, no rating/review data) is capped so it
cannot reach tier A/A+ on the base score alone. The full list of matched
signals is included in `icp_reasons` on every record; any hard exclusion
that applied is listed in `icp_exclusions`. All rules, keyword lists, and
point values live in `config/icp_rules.json`, separate from the scoring
logic in `scripts/icp/qualify.py`, so the ICP definition can be tuned
without touching code.

**Tiers:** A+ (85+) and A (65+) are strong commercial-fit prospects; B
(40+) is a plausible fit worth a manual look; C (15+) is weak/uncertain
evidence; D is either a low score or a hard exclusion.

**Determinism:** the same master record always produces the same score,
tier, reasons, and exclusions — matching normalize.py/update_master.py's
existing no-AI, no-fuzzy-matching philosophy. The only non-deterministic
field is `icp_evaluated_at` (current UTC time by default); pass
`--evaluated-at` to pin it for reproducible output.

**How to run it:**
```
python3 scripts/icp/qualify.py \
  --master-json data/master/master.json \
  --rules config/icp_rules.json \
  --out-dir data/icp
```
Output is written to `data/icp/icp_qualified.json` and `.csv`
(`master_id`, `business_name`, `website`, `phone`, `address`, `city`,
`state`, `country`, `rating`, `review_count`, `icp_tier`, `icp_score`,
`icp_reasons`, `icp_exclusions`, `icp_evaluated_at`). This never mutates
`data/master/` — it is a read-only pass over the master store — and
`data/icp/` is gitignored the same way `data/master/` and
`data/google-maps/` are, since it is fully regenerable from the master
store at any time.

This layer intentionally stops at a qualification tier and score — no
enrichment, no contact discovery, no CRM sync, and no outreach happen
here or anywhere else in this repo yet.

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
