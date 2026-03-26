# Job Matcher — Implementation Plan

This document breaks the build into four sequential phases. Each phase produces a
testable, coherent slice of the system. Complete phases in order — later phases depend
on earlier ones being stable.

---

## Phase 1: Foundation

**Goal:** Project skeleton is in place. The database layer is fully implemented and
testable in isolation. Configuration and profile files exist with documented structure.
No ingestion or UI yet.

| # | Task | Files touched |
|---|---|---|
| 1.1 | Create `requirements.txt` with `flask`, `requests`, `beautifulsoup4`, `anthropic` | `requirements.txt` |
| 1.2 | Create `config.example.json` — all keys present, placeholder values, inline comments explaining each field | `config.example.json` |
| 1.3 | Create `profile.json` — example skills profile with all supported fields populated | `profile.json` |
| 1.4 | Implement `db.py` — `init_db()`, `listing_exists()`, `insert_listing()`, `update_score()` | `db.py` |
| 1.5 | Implement `db.py` read helpers — `get_feed()`, `get_bookmarks()`, `set_bookmarked()`, `set_dismissed()` | `db.py` |

**Exit criteria:** Running `python db.py` (or a quick REPL test) initialises `jobs.db`,
inserts a fake listing, and retrieves it. No external API keys required.

---

## Phase 2: Ingestion Pipeline

**Goal:** `ingest.py` can be run from the command line and will fetch listings from
Adzuna, pre-filter them, scrape full descriptions, score them with Claude Haiku, and
persist everything to `jobs.db`. The web UI does not need to exist for this to work.

| # | Task | Files touched |
|---|---|---|
| 2.1 | Implement config loader with startup validation — raise a clear error if required keys are absent | `ingest.py` |
| 2.2 | Implement `AdzunaClient` — wraps the Adzuna REST API, handles pagination up to `max_pages`, returns normalised listing dicts | `ingest.py` |
| 2.3 | Implement `prefilter()` — title include/exclude regex, minimum salary check, contract type and time checks | `ingest.py` |
| 2.4 | Implement `scrape_description()` — GET `redirect_url`, extract visible text with BeautifulSoup; fall back to API snippet on timeout, bot-block, or empty result | `ingest.py` |
| 2.5 | Implement `score_listing()` — build prompt from description + profile, call Claude Haiku, parse JSON response (`score`, `matched_skills`, `missing_skills`, `concerns`, `verdict`); retry once on failure; return `None` on persistent failure | `ingest.py` |
| 2.6 | Implement `run()` orchestrator — ties 2.1–2.5 together, deduplicates via `db.listing_exists()`, prints run summary (fetched / pre-filtered / deduped / scraped / scored / failed) | `ingest.py` |

**Exit criteria:** `python ingest.py` runs against real Adzuna credentials, populates
`jobs.db` with scored listings, and prints a coherent summary. Scrape failures and
Haiku errors are handled without crashing the run.

---

## Phase 3: Flask UI

**Goal:** The web interface is live at `localhost:5000`. Users can browse scored
listings, bookmark ones of interest, and dismiss ones they want to hide — all without
page reloads.

| # | Task | Files touched |
|---|---|---|
| 3.1 | Implement `app.py` — `GET /` (main feed), `GET /bookmarks`, `POST /bookmark/<id>`, `POST /dismiss/<id>` | `app.py` |
| 3.2 | Create `templates/_card.html` — single listing card partial: title, company, location, salary, score badge, matched/missing skills, concerns, verdict, action buttons | `templates/_card.html` |
| 3.3 | Create `templates/index.html` — page shell with header, nav tabs (Feed / Bookmarks), renders card loop using `_card.html`, includes HTMX CDN | `templates/index.html` |
| 3.4 | Wire HTMX bookmark toggle — `hx-post="/bookmark/<id>"`, response swaps the action button group in place (card stays, star icon toggles) | `templates/_card.html`, `app.py` |
| 3.5 | Wire HTMX dismiss — `hx-post="/dismiss/<id>"`, response is empty 200, `hx-target="#card-<id>"` with `hx-swap="outerHTML"` removes the card from the DOM | `templates/_card.html`, `app.py` |
| 3.6 | Create `static/style.css` — card layout, score badge colour tiers (green ≥ 8, yellow ≥ 6, red < 6), skill tag chips, responsive single-column layout | `static/style.css` |

**Exit criteria:** Flask server starts, feed loads with listings from `jobs.db` sorted
by score, bookmarking and dismissing work without page reload, bookmarks view shows
only saved listings. Listings with `score = NULL` render a "pending" badge rather than
breaking the page.

---

## Phase 4: Polish & Documentation

**Goal:** The tool is ready for daily use. Errors are visible without inspecting code,
edge cases are handled gracefully, and a new user can get set up from the README alone.

| # | Task | Files touched |
|---|---|---|
| 4.1 | Add structured logging throughout `ingest.py` — per-listing status (filtered / deduped / scraped / scored / failed) plus end-of-run counts | `ingest.py` |
| 4.2 | Handle NULL-score listings in the UI — show a "not yet scored" state on the card instead of a blank or broken score section | `templates/_card.html`, `static/style.css` |
| 4.3 | Write `README.md` — prerequisites, install steps, how to copy and fill `config.example.json`, how to run `ingest.py`, how to start the Flask server, optional cron setup example | `README.md` |
| 4.4 | Manual end-to-end test — full run with real credentials, verify listings appear in UI, bookmark/dismiss persist across page reload, cron-style re-run deduplicates correctly | — |

**Exit criteria:** A developer who has never seen this project can follow the README
from a fresh clone to a working local instance. A re-run of `ingest.py` adds only new
listings and does not duplicate existing ones.

---

## Dependency Map

```
Phase 1 (db.py, config files)
  └── Phase 2 (ingest.py) — needs db.py to persist results
        └── Phase 3 (app.py + UI) — needs db.py populated with data to display
              └── Phase 4 (polish) — needs all prior phases stable
```

---

## What Is Not in This Plan (v1 Scope)

- Application status tracking or notes per listing
- Multiple job data sources (LinkedIn, Indeed, etc.)
- Resume parsing to auto-generate `profile.json`
- Email digest or push notifications
- Any cloud deployment path
- Multi-user support or authentication
