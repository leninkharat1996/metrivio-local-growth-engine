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
