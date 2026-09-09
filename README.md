# Metrivio Local Growth Engine

Free, open-source B2B prospecting engine for Metrivio. Built entirely from
free/open-source software — no paid APIs (no Apify, no Apollo, no Google
Places API).

## Google Maps Scraping Layer

The first layer of the prospecting engine: scrapes business listings from
Google Maps using [gosom/google-maps-scraper](https://github.com/gosom/google-maps-scraper)
(MIT licensed) and produces clean, deduplicated leads.

**What it does:** given a business category/keyword and a location, it runs
the scraper, normalizes the raw results into a canonical lead schema, and
deduplicates them deterministically (no AI reasoning per-row).

**How to trigger a scrape from GitHub Actions:**

1. Go to the repo's **Actions** tab → **Google Maps Scraper** workflow.
2. Click **Run workflow** and fill in:
   - `keyword` — e.g. `commercial HVAC contractors`
   - `location` — e.g. `Dallas, TX`
   - `depth` — optional, default `5` (higher = more results, slower)
3. Runs entirely on GitHub-hosted runners — nothing to install locally, no
   Docker Desktop required.

**Where results appear:** as workflow run artifacts (Actions run page →
Artifacts section):
- `google-maps-raw-<slug>-<timestamp>` — untouched scraper output
- `google-maps-clean-<slug>-<timestamp>` — deduplicated CSV + JSON in the
  canonical lead schema

Raw/clean datasets are gitignored by default and are not committed to the
repo automatically.

**How the Claude skill is invoked:** the skill at
`.claude/skills/google-maps-scraper/SKILL.md` teaches Claude how to trigger
this workflow (or run the scripts directly) and how to report result counts
without re-deriving them by reading the data itself. Reference it with
`/google-maps-scraper` in a Claude Code session, or Claude will pick it up
automatically when asked to run a Google Maps prospecting search.

This layer intentionally stops at clean Google Maps leads — no email
finding, enrichment, LinkedIn scraping, CRM sync, scoring, or outreach.
