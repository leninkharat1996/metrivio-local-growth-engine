---
name: google-maps-scraper
description: Run the Google Maps prospecting scrape (gosom/google-maps-scraper) for a business category + location, normalize/dedupe results deterministically, and report counts. Use when the user asks to find/scrape businesses on Google Maps, gather local leads, or trigger a prospecting run for a keyword and city/state.
---

# Google Maps Scraper Skill

Operational skill for running the Google Maps layer of Metrivio's free/open-source
prospecting engine. This layer ONLY scrapes and cleans Google Maps business
listings — no email finding, enrichment, CRM, or outreach happens here.

## What this skill does

1. Accepts a `keyword` (business category, e.g. "commercial HVAC contractors")
   and a `location` (city/state, e.g. "Dallas, TX").
2. Triggers the `google-maps-scraper.yml` GitHub Actions workflow (or runs the
   equivalent commands locally in CI) using `gosom/google-maps-scraper`
   (MIT licensed, free, no paid API keys).
3. The workflow writes raw scraper output to `data/google-maps/raw/`.
4. `scripts/google_maps_scraper/normalize.py` deterministically normalizes and
   deduplicates the raw output into the canonical lead schema (see below) and
   writes CSV + JSON to `data/google-maps/clean/`.
5. Reports: total businesses found, clean records after dedup, duplicates
   merged, and partial/incomplete records (missing both phone and website).

**Do all normalization/dedup with the Python script, not by reasoning over
rows.** That keeps token usage low and results deterministic and reproducible.

## How to run it

### Preferred: GitHub Actions (no local install required)

Trigger the `Google Maps Scraper` workflow (`.github/workflows/google-maps-scraper.yml`)
via `workflow_dispatch` with inputs:

- `keyword` — e.g. `commercial HVAC contractors`
- `location` — e.g. `Dallas, TX`
- `depth` — optional, default `5` (higher = more results, slower)

The workflow:
1. Checks out this repo and `gosom/google-maps-scraper` side by side.
2. Builds the scraper with Go (Playwright/Chromium is fetched automatically
   on first run — nothing to install manually).
3. Builds the query file via `scripts/google_maps_scraper/build_query.py`.
4. Runs the scraper, writing raw CSV to `data/google-maps/raw/`.
5. Runs `scripts/google_maps_scraper/normalize.py` to produce clean
   CSV + JSON in `data/google-maps/clean/`.
6. Uploads both raw and clean outputs as workflow artifacts (raw kept 30
   days, clean kept 90 days) — nothing is committed to the repo automatically.

If asked to "trigger a scrape" or "run a prospecting search", tell the user to
dispatch this workflow from the GitHub Actions tab (or use `gh workflow run
google-maps-scraper.yml -f keyword="..." -f location="..." -f depth="5"` if
`gh` is available) rather than running anything locally.

### Direct script usage (e.g. inside an existing CI job or sandbox)

```bash
# 1. Build the query line
python3 scripts/google_maps_scraper/build_query.py \
  --keyword "commercial HVAC contractors" --location "Dallas, TX" \
  --out queries.txt

# 2. Run the upstream scraper (already built at ./google-maps-scraper/google-maps-scraper)
./google-maps-scraper/google-maps-scraper \
  -input queries.txt -results data/google-maps/raw/dallas_hvac.csv -depth 5

# 3. Normalize + dedupe deterministically
python3 scripts/google_maps_scraper/normalize.py \
  --raw-csv data/google-maps/raw/dallas_hvac.csv \
  --category "commercial HVAC contractors" \
  --location "Dallas, TX" \
  --out-prefix data/google-maps/clean/dallas_hvac
```

The normalize script prints a JSON stats block to stdout:

```json
{
  "raw_records": 42,
  "malformed_skipped": 1,
  "duplicates_merged": 3,
  "clean_records": 38,
  "partial_records_missing_phone_and_website": 2,
  "csv_path": "data/google-maps/clean/dallas_hvac.csv",
  "json_path": "data/google-maps/clean/dallas_hvac.json"
}
```

Report these numbers back to the user directly — do not re-derive counts by
reading the CSV yourself.

## Canonical lead schema

Every row in `data/google-maps/clean/*.csv` and `*.json` has exactly these
fields (see `scripts/google_maps_scraper/normalize.py::CANONICAL_FIELDS`):

| Field | Notes |
|---|---|
| `business_name` | Required; records without one are dropped as malformed |
| `category` | The keyword searched, not scraped per-row |
| `address` | Raw address string as scraped |
| `city` | Best-effort parsed from `address` |
| `state` | Best-effort parsed from `address` |
| `country` | Best-effort parsed; defaults to `USA` when a US state is detected |
| `phone` | As scraped, unformatted |
| `website` | As scraped |
| `google_maps_url` | Link to the listing |
| `rating` | Float, may be null |
| `review_count` | Int, may be null |
| `latitude` | Float, may be null |
| `longitude` | Float, may be null |
| `source` | Always `google_maps_scraper` |
| `scraped_at` | UTC ISO-8601 timestamp of the normalize run |

Raw scraper output (with any extra upstream fields such as opening hours,
price range, review text) is preserved unmodified in `data/google-maps/raw/`
— the clean schema above is intentionally narrow.

## Deduplication rules (deterministic, no AI)

A record is a duplicate of an earlier one if, in priority order:
1. Website domains match (`example.com` == `www.example.com`).
2. Phone numbers match after stripping formatting/country code.
3. Normalized business name AND normalized address match (punctuation,
   legal suffixes like "LLC"/"Inc", and common street-type abbreviations
   stripped).

When two records collide, the one with more complete contact info (has a
phone or website) is kept.

## Failed / partial records

- **Malformed**: raw rows with no business name are dropped and counted in
  `malformed_skipped`.
- **Partial**: clean records missing both `phone` and `website` are counted
  in `partial_records_missing_phone_and_website` — still included in output,
  just flagged in the stats so downstream layers know which leads need more
  work.

## Output locations

```
data/google-maps/raw/<slug>_<timestamp>.csv     # untouched scraper output
data/google-maps/clean/<slug>_<timestamp>.csv   # canonical schema, deduped
data/google-maps/clean/<slug>_<timestamp>.json  # same data, machine-readable
```

These directories are gitignored by default (see repo `.gitignore`) so scrape
runs don't silently accumulate into the git history — commit a dataset only
when the user explicitly asks to keep it.

## Explicitly out of scope for this skill

Do not perform email finding/verification, LinkedIn scraping, enrichment,
scoring, CRM sync, personalization, or outreach when this skill is invoked.
Those are later, separate layers of the prospecting engine.
