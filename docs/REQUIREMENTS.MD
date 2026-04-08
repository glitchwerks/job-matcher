# Job Matcher — Requirements

A locally-run job search tool that ingests listings from the Adzuna API, scores them
against a personal skills profile using Claude Haiku, and surfaces ranked results
through a browser-based UI.

---

## Problem Statement

Job listing sites lack granular enough filtering for software engineers to efficiently
identify high-match roles. The goal is to automate ingestion, apply semantic skill
matching via LLM scoring, and present only relevant listings in a manageable UI.

---

## Functional Requirements

### Ingestion
- Fetch job listings from the Adzuna API on a scheduled or manual basis
- Apply a broad initial search by job category and location (configurable)
- Pre-filter listings before LLM scoring using cheap heuristics:
  - Title keyword inclusion/exclusion (regex)
  - Minimum salary threshold
  - Contract type (permanent, full-time)
  - Exclude already-seen listings (deduplication by Adzuna listing ID)
- Follow the listing `redirect_url` to scrape the full job description (Adzuna API
  only returns a snippet)
- Store all fetched and scored listings in a PostgreSQL database

### Scoring
- Score each unseen listing against a user-defined skills profile using Claude Haiku
- Scoring prompt returns structured JSON:
  - `score` (0–10)
  - `matched_skills` (array)
  - `missing_skills` (array)
  - `concerns` (array)
  - `verdict` (one-sentence summary)
- Persist score and scoring metadata to the database alongside the listing
- Only listings that pass pre-filtering are sent to the LLM

### User Actions
- Bookmark listings to save for later review
- Dismiss listings to hide them from the main feed permanently

### UI
- Browser-based web app served locally via Flask on `localhost:5000`
- Main feed: listings sorted by score descending, excluding dismissed entries
- Bookmarked view: saved listings only
- Per-listing panel:
  - Score, matched/missing skills, concerns, verdict (from Haiku)
  - Job title, company, location, salary range, contract type
  - Link to full listing (redirect URL)
- Bookmark and dismiss actions update the database without page reload (HTMX)
- No authentication required (local only)

---

## Non-Functional Requirements

- Deployable via Docker Compose (dev and prod stacks) or natively on a local machine
- Ingestion script and web server are decoupled — script can be run manually or via cron
  independent of whether the UI is open
- Near-zero running cost — Haiku API costs should remain negligible at typical job search
  volumes (~100–500 new listings per run)
- No frontend build tooling — UI uses plain HTML + HTMX, served directly by Flask

---

## Skills Profile

- Stored as a local `profile.json` file
- Injected into the Haiku scoring prompt at runtime
- Fields:
  - Primary skills with years of experience and recency
    (e.g. `"Python, 5yr, active"`)
  - Anti-preferences / exclusions
    (e.g. `"no .NET"`, `"no pure frontend"`)
  - Preferred seniority level
  - Preferred industries or company types (optional)
  - Location preferences / remote preference

---

## Data Model

### `listings` table

| Column | Type | Description |
|---|---|---|
| `id` | INTEGER PK | Internal ID |
| `adzuna_id` | TEXT UNIQUE | Adzuna listing ID (dedup key) |
| `title` | TEXT | Job title |
| `company` | TEXT | Company name |
| `location` | TEXT | Display location |
| `salary_min` | INTEGER | Minimum salary (may be null) |
| `salary_max` | INTEGER | Maximum salary (may be null) |
| `salary_is_predicted` | BOOLEAN | Whether salary is predicted by Adzuna |
| `contract_type` | TEXT | e.g. permanent, contract |
| `contract_time` | TEXT | e.g. full_time, part_time |
| `description` | TEXT | Full scraped job description |
| `redirect_url` | TEXT | Link to original listing |
| `created_at` | TEXT | Adzuna listing creation date |
| `fetched_at` | TEXT | Timestamp of local ingestion |
| `score` | REAL | Haiku match score (0–10) |
| `matched_skills` | TEXT | JSON array from Haiku |
| `missing_skills` | TEXT | JSON array from Haiku |
| `concerns` | TEXT | JSON array from Haiku |
| `verdict` | TEXT | One-sentence Haiku summary |
| `bookmarked` | BOOLEAN | User saved this listing |
| `dismissed` | BOOLEAN | User dismissed this listing |
| `seen` | BOOLEAN | Whether listing has been scored |

---

## Tech Stack

| Layer | Technology |
|---|---|
| Language | Python 3.11+ |
| Job data source | Adzuna REST API |
| Full description retrieval | `requests` + `BeautifulSoup4` |
| LLM scoring | Anthropic API — `claude-haiku-4-5-20251001` |
| Database | PostgreSQL via `psycopg2` |
| Web server | Flask |
| Frontend | HTML + HTMX |
| Scheduling | cron (manual setup by user) |

---

## Project Structure

```
job-matcher/
├── ingest.py           # Fetch, pre-filter, scrape, score, store
├── app.py              # Flask server and route handlers
├── db.py               # PostgreSQL schema and query helpers
├── profile.json        # User skills and preferences
├── config.json         # API keys, search parameters, thresholds
├── templates/
│   └── index.html      # Main UI template
├── static/             # CSS and any static assets
└── docker-compose.dev.yml  # Docker Compose dev stack (PostgreSQL + app)
```

---

## Configuration (`config.json`)

| Key | Description | Example |
|---|---|---|
| `adzuna_app_id` | Adzuna API app ID | `"abc123"` |
| `adzuna_app_key` | Adzuna API key | `"xyz789"` |
| `anthropic_api_key` | Anthropic API key | `"sk-ant-..."` |
| `search.country` | Adzuna country code | `"us"` |
| `search.what` | Broad keyword query | `"software engineer"` |
| `search.where` | Location | `"miami"` |
| `search.salary_min` | Minimum salary filter | `120000` |
| `search.results_per_page` | Listings per fetch | `50` |
| `scoring.threshold` | Minimum score to surface | `7.0` |
| `scoring.model` | Haiku model string | `"claude-haiku-4-5-20251001"` |
| `prefilter.title_exclude` | Regex patterns to exclude by title | `["junior", "intern", "lead"]` |
| `prefilter.title_include` | At least one of these must match title | `["engineer", "developer"]` |

---

## Out of Scope (v1)

- Application status tracking
- Notes per listing
- Email digest / notifications
- Multi-user support
- Cloud deployment
- LinkedIn or other data sources beyond Adzuna
- Resume parsing to auto-generate profile
