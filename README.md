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
scrape and uploads `google-maps-master-store-<run_id>` (all three files) as
a workflow artifact. It also best-effort persists `data/master/` between
runs via `actions/cache` so the store can accumulate over time without ever
committing lead data to git — `data/master/*` is gitignored just like
`data/google-maps/`. The cache is not a durability guarantee (GitHub evicts
unused caches); the uploaded artifact from each run is the authoritative
snapshot.
