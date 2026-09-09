---
name: google-maps-scraper
description: Run the Google Maps prospecting scrape (gosom/google-maps-scraper) for one or many keyword+location searches via the production collection workflow, normalize/dedupe results deterministically, and report per-search + run-level counts. Use when the user asks to find/scrape businesses on Google Maps, gather local leads, or trigger a prospecting run across one or more keyword/location combinations.
---

# Google Maps Scraper Skill

Operational skill for running the Google Maps layer of Metrivio's free/open-source
prospecting engine. This layer ONLY scrapes and cleans Google Maps business
listings — no email finding, enrichment, CRM, or outreach happens here.

## What this skill does

Triggers the `Google Maps Scraper` GitHub Actions workflow
(`.github/workflows/google-maps-scraper.yml`) with a **search plan**: one or
more keyword+location searches run in a single, auditable run. Each search is
validated, queried, scraped, normalized, and deduped independently, and a
run manifest ties everything together.

**Do all parsing/normalization/dedup/manifest work with the Python scripts,
not by reasoning over rows.** That keeps token usage low and results
deterministic and reproducible.

## Search-plan format

One search per line:

```
keyword | location | depth
```

- `keyword` — business category, e.g. `commercial HVAC contractors`
- `location` — city/state or full location string, e.g. `Dallas, TX`
- `depth` — optional; a positive integer (scroll depth — more depth = more
  results per search, slower). Omit it to fall back to the workflow's
  `default_depth` input.

Blank lines and lines starting with `#` are ignored. Example plan:

```
commercial HVAC contractors | Dallas, TX | 3
commercial HVAC companies | Dallas, TX | 3
industrial HVAC contractors | Dallas, TX | 3
```

Never hard-code a specific city/keyword into the workflow or scripts — the
plan is always supplied by the caller at dispatch time.

### Choosing keywords

Use distinct, specific phrasings per line rather than one broad keyword —
Google Maps returns different result sets for "HVAC contractors" vs "HVAC
companies" vs "air conditioning repair", so a small set of close variants
covers more of the market than one search repeated.

### Choosing locations

One line per city/metro, using whatever specificity Google Maps understands
("Dallas, TX", "Dallas-Fort Worth, TX", or a ZIP). Don't combine multiple
cities into a single location string — put each on its own line so results
and provenance stay attributable to one place.

### Choosing depth

Depth controls how far the scraper scrolls per search, not how many searches
run. Start with a low depth (1-3) for a smoke test or exploratory pass;
raise it (5-10+) only for a search you already know is worth collecting
deeply, since higher depth means a longer-running, slower search line. Keep
production batches to a handful of lines at a controlled depth rather than
one enormous plan — see "Controlled batches" below.

## How to launch a collection

Dispatch the `Google Maps Scraper` workflow
(`.github/workflows/google-maps-scraper.yml`) via `workflow_dispatch` with:

- `search_plan` — the multiline plan described above (required)
- `default_depth` — depth used for any line that omits its own depth
  (optional, default `5`)

For each valid line the workflow:
1. Validates the line deterministically (`scripts/google_maps_scraper/search_plan.py`)
   and assigns a deterministic `search_id` (slug of location + keyword,
   deduplicated if two lines collide).
2. Builds the query file (`build_query.py`) and runs
   `gosom/google-maps-scraper` (MIT licensed, free, no paid API keys).
3. Normalizes + dedupes the raw output (`normalize.py`) into the canonical
   lead schema, stamping every record with `search_keyword`, `search_location`,
   `search_id`, and `run_id` for provenance.
4. Writes outputs to `data/google-maps/<run_id>/<search_id>/{raw,clean}/`
   so two searches in the same run can never mix their records.

A search line failing (scraper crash, non-zero exit, etc.) does **not** stop
the run — the workflow records the failure and continues to the next line.

If asked to "trigger a scrape" or "run a prospecting search", tell the user
to dispatch this workflow from the GitHub Actions tab (or use `gh workflow
run google-maps-scraper.yml -f search_plan="..." -f default_depth="5"` if
`gh` is available) rather than running anything locally.

## Where artifacts appear

On the workflow run page, under **Artifacts**:

- `google-maps-raw-<run_id>` — untouched scraper output per search
- `google-maps-clean-csv-<run_id>` — canonical-schema CSV per search
- `google-maps-clean-json-<run_id>` — canonical-schema JSON per search
- `google-maps-manifest-<run_id>` — `manifest.json`: the run's machine-readable
  record and run statistics (see below)

Nothing under `data/google-maps/` is committed to the repo — it's
`.gitignore`d by default (only `data/google-maps/.gitkeep` is tracked).
Commit a dataset only when the user explicitly asks to keep it.

## How to interpret the manifest

`manifest.json` (built by `scripts/google_maps_scraper/build_manifest.py`)
contains:

- `run_id`, `generated_at` — run identity/timestamp
- `overall_status` — `success`, `partial_failure`, or `failed` (see below)
- `requested_searches` — every valid line that was attempted
- `invalid_search_lines` — lines that failed validation before any scraping
  (with the line number and a human-readable error)
- `successful_searches` — per-search stats: `raw_record_count`,
  `clean_record_count`, `malformed_count`, `duplicate_count`,
  `partial_count`, plus the artifact paths for that search
- `failed_searches` — per-search failure reason
- `totals` — requested/valid/invalid/successful/failed counts for the whole run

Report these numbers back to the user directly from the manifest — do not
re-derive counts by reading CSV/JSON output yourself.

## How to handle partial failures

- `overall_status: "success"` — every requested line was valid and every
  search succeeded. The workflow job itself is green.
- `overall_status: "partial_failure"` — at least one line was invalid or one
  search failed, but at least one search succeeded. The workflow job is
  marked **failed** (so a partial run is never mistaken for a clean one),
  but all artifacts from the searches that did succeed are still uploaded.
- `overall_status: "failed"` — no search succeeded (or the plan had zero
  valid lines).

When a run comes back `partial_failure` or `failed`, read `invalid_search_lines`
and `failed_searches` in the manifest, tell the user exactly which lines/
searches failed and why, and only re-dispatch the workflow for the failed
lines (not the whole original plan) unless asked otherwise.

## Controlled batches

Production collection should be run in controlled batches — a handful of
search lines (e.g. 3-10) per dispatch, at a depth appropriate to that batch —
rather than one very large plan. This keeps each run's blast radius small,
keeps run time under the workflow's timeout, and keeps the manifest easy to
audit line-by-line before triggering the next batch.

## Canonical lead schema

Every row in a search's `clean/*.csv` and `*.json` has exactly these fields
(see `scripts/google_maps_scraper/normalize.py::CANONICAL_FIELDS`):

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
| `search_keyword` | The keyword line used for this search (provenance) |
| `search_location` | The location line used for this search (provenance) |
| `search_id` | Deterministic id for the search that produced this record |
| `run_id` | Deterministic id for the overall collection run |

Raw scraper output (with any extra upstream fields such as opening hours,
price range, review text) is preserved unmodified in
`data/google-maps/<run_id>/<search_id>/raw/` — the clean schema above is
intentionally narrow.

## Deduplication rules (deterministic, no AI)

Deduplication happens **within each search**, not across the whole run. A
record is a duplicate of an earlier one in the same search if, in priority
order:
1. Website domains match (`example.com` == `www.example.com`).
2. Phone numbers match after stripping formatting/country code.
3. Normalized business name AND normalized address match (punctuation,
   legal suffixes like "LLC"/"Inc", and common street-type abbreviations
   stripped).

When two records collide, the one with more complete contact info (has a
phone or website) is kept.

## Persistent master prospect store (cross-run deduplication)

The same business often appears in multiple runs (different cities,
keywords, depths, dates). `scripts/google_maps_scraper/update_master.py`
upserts every run's clean output into a persistent, deduplicated master
store so each business is represented once, while keeping full discovery
history.

**What it is:** two files under `data/master/` (not committed to git,
see below):
- `master.csv` / `master.json` — one row per unique business, in the
  canonical `normalize.py` schema plus `master_id`, `first_seen_at`,
  `last_seen_at`, `source_count`, `search_count`.
- `discovery_history.csv` — one row per `(master_id, run_id, search_id)`
  triple: `master_id, run_id, search_id, search_keyword, search_location,
  source, first_discovered_at`. This is where you look up every run/search
  that surfaced a given business; the master record itself stays compact.
- `identity_conflicts.csv` — one row per detected identity collision (see
  below): `master_id, resolved_via_key, conflicting_master_ids,
  business_name, run_id, search_id, detected_at`. Empty in the normal case;
  non-empty rows need a human look, since they are never auto-merged.

**How `master_id` is assigned and kept stable (deterministic, no
fuzzy/AI matching):** `master_id` is assigned once per business and never
recomputed from an incoming lead's own fields alone. Every incoming lead is
resolved against a **deterministic identity index** (`identity key ->
master_id`) rebuilt from the existing master store, using the same
identifiers as `normalize.dedup_key()`/`normalize.match_keys()`, in
priority order: normalized website domain > normalized phone > normalized
business name + address.

- If a key the lead carries already resolves to an existing master, the
  lead attaches to that master — even if the *other* fields that could
  have identified it are blank on this run. This is what keeps a business's
  `master_id` stable when its website or phone disappears from a later
  Google Maps scrape (previously this recomputed the id from whichever
  fields were present on that run alone, which changed the id and created
  a duplicate record).
- If none of the lead's keys match anything in the index, it's a brand-new
  business: its `master_id` is minted via
  `sha256(":".join(dedup_key(lead))).hexdigest()[:16]`, using the same
  priority order (domain > phone > name+address) so a business with no
  website/phone still gets a stable id from name+address.
- If a later scrape reveals a website or phone for a business that was
  first seen with only a name+address key, that new key is added to the
  index under the *existing* master_id (matched via name+address) rather
  than minting a new record.
- A present, higher-priority key (domain, or phone when domain is absent)
  that is brand-new to the index is trusted on its own: a lower-priority
  key incidentally matching a different, unrelated master (e.g. a shared
  phone number) does not attach this lead to that master. name+address is
  always checked as a last-resort corroboration.
- If a lead's own keys point at two *different* existing masters (e.g. its
  website matches master A but its phone matches master B), this is an
  identity conflict: it is never silently merged. Resolution picks the
  higher-priority key's master deterministically and the collision is
  logged to `identity_conflicts.csv` for a human to review.

Because resolution is always against the persisted store, not against
`run_id`/`search_id`/`scraped_at` or the shape of a single incoming
record, the same business gets the same `master_id` no matter which run,
keyword, city, or date surfaced it, and no matter which of its identifying
fields happened to be populated on that particular scrape.

**Cross-run merge rule when a business reappears:**
- Blank-fill only: a populated existing field is never overwritten by an
  incoming blank value; a blank existing field is filled by an incoming
  populated value.
- For two differing non-empty values (e.g. rating changed), the
  **existing (first-seen) value wins** — deterministic, not guessed.
- `scraped_at`, `search_keyword`, `search_location`, `search_id`, `run_id`
  are treated as "latest snapshot" provenance and are always refreshed to
  the most recent occurrence; the full history of every occurrence lives in
  `discovery_history.csv`, not in the master record.
- `first_seen_at`/`last_seen_at` track the earliest/latest `scraped_at`
  seen for that business. `source_count`/`search_count` count distinct
  `source`/`search_id` values from history.

**How to update the master store:**
```
python3 scripts/google_maps_scraper/update_master.py \
  --clean-json "data/google-maps/<run_id>/*/clean/clean.json" \
  --master-dir data/master
```
`--clean-json` accepts a glob and may be repeated. Reprocessing the exact
same clean dataset is idempotent: master record count and history event
count do not grow.

**In the workflow:** after normalize/manifest, the workflow runs
`update_master.py` on the run's clean JSON files, best-effort restores/saves
`data/master/` via `actions/cache` between runs (so the store accumulates
run-over-run without committing anything), and always uploads
`google-maps-master-store-<run_id>` (`master.csv`, `master.json`,
`discovery_history.csv`) as a workflow artifact. `update_master.py` also
writes `data/master/identity_conflicts.csv`, but the workflow's artifact
upload step is unchanged as part of this fix, so check that file locally
(or extend the upload step separately) if you need to review conflicts
from a CI run.

**Why lead data is never committed:** `data/master/*` is gitignored (only
`data/master/.gitkeep` is tracked), matching `data/google-maps/`. The
GitHub Actions cache used for cross-run persistence is best-effort (subject
to GitHub's cache eviction policy, ~7 days unused / 10GB per repo) — the
authoritative snapshot for any given run is always the uploaded
`google-maps-master-store-<run_id>` artifact. If you need guaranteed
long-term continuity, download the latest `master.csv`/`master.json`/
`discovery_history.csv` from that artifact into `data/master/` before the
next run (so `update_master.py` resumes from it), or maintain the store
outside CI.

## Explicitly out of scope for this skill

Do not perform email finding/verification, LinkedIn scraping, enrichment,
scoring, CRM sync, personalization, or outreach when this skill is invoked.
Those are later, separate layers of the prospecting engine.
