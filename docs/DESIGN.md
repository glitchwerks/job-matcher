# Job Matcher — Design Document

> This document covers architecture, component design, data flow, key decisions,
> and edge-case handling. It reflects the current codebase with PostgreSQL,
> Docker Compose, and the plugin-based multi-source architecture.

---

## 1. High-Level Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                         ingest.py (CLI)                          │
│                                                                  │
│  job_sources/        credentials.py   providers/                │
│  (plugin-based   ──► load_providers() ──► build_provider_chain() │
│   sources)               │                       │               │
│       │                  ▼                       ▼               │
│  PreFilter ──► GeoFilter ──► Dedup ──► Scraper ──► score() ──► DB │
└──────────────────────────────────┬───────────────────────────────┘
                                   │ writes to
                           PostgreSQL (DATABASE_URL)
                                   │ reads from
┌──────────────────────────────────▼───────────────────────────────┐
│                        app.py (Flask server)                     │
│                                                                  │
│  GET /                  → main feed (scored, not dismissed)      │
│  GET /bookmarks         → bookmarked listings only               │
│  GET /applied           → applied listings only                  │
│  GET /snippets          → snippet-scored listings                │
│  GET /stats             → usage and cost dashboard               │
│  GET/POST /settings     → LLM provider + job source credentials  │
│  GET/POST /profile      → config.json editor                     │
│  POST /bookmark/<id>    → HTMX toggle bookmark                   │
│  POST /dismiss/<id>     → HTMX dismiss listing                   │
│  POST /apply/<id>       → HTMX toggle applied                    │
│  POST /ingest/trigger   → spawn ingest.py subprocess             │
│  GET  /ingest/status    → poll ingest subprocess state           │
│  POST /api/validate-keys          → test LLM credentials         │
│  POST /api/providers/reorder      → save provider fallback order │
│  POST /api/job-sources/<key>/toggle → enable/disable a source    │
└──────────────────────────────────────────────────────────────────┘
```

The ingestion pipeline and web server are **fully decoupled**. `ingest.py` is a CLI
script run manually or on a schedule — independent of whether the Flask server is
running. They communicate only through the shared PostgreSQL database.

### Deployment Model

The application runs in Docker Compose using three containers per stack:

```
┌─────────────────────────────────────────────────────────────────────┐
│                     Docker Compose stack                             │
│                                                                      │
│  web container                   scheduler container                 │
│  (Flask/Waitress on :5000)        (Ofelia cron scheduler)           │
│        │                               │                             │
│        │  execs into web:              │                             │
│        │  python ingest.py --hours 25  │                             │
│        │  @daily                  ─────┘                             │
│        │                                                             │
│        └─────────────────┬────────────────────────────────────────  │
│                           │ DATABASE_URL                             │
│                     db container                                     │
│                     (postgres:16-alpine)                             │
│                     pgdata_dev / pgdata_prod volume                  │
└─────────────────────────────────────────────────────────────────────┘
```

Two independent stacks run side-by-side on the same host:
- **Dev stack** (`job-matcher-pr-dev`) — port 5000, database `jobmatcher_dev`, config from `./config-dev`
- **Prod stack** (`job-matcher-pr-prod`) — port 5001, database `jobmatcher_prod`, config from `./config`

`DATABASE_URL` and `LOG_DIR` are injected as Docker environment variables; LLM
provider and job source credentials are stored in `config/providers.json` and
managed through the `/settings` UI — they are never set as environment variables.

---

## 2. Component Design

### 2.1 `db.py` — Database Layer

Owns all PostgreSQL interactions. No other module imports `psycopg2` or opens
database connections directly.

**Connection pooling:**

A module-level `ThreadedConnectionPool` (minconn=1, maxconn=10) is initialised at
import time using the `DATABASE_URL` environment variable. Every call to
`get_connection()` checks out a connection from the pool; the `_Conn` wrapper's
`close()` returns it. `DATABASE_URL` is required — the module raises `RuntimeError`
at import time if absent.

**Public functions:**

| Function | Purpose |
|---|---|
| `get_connection()` | Check out a pooled connection; returns a `_Conn` wrapper |
| `init_db()` | Create or migrate the `listings` and `location_geocache` tables; idempotent |
| `listing_exists(conn, source, source_id)` | Primary dedup check by `(source, source_id)` |
| `listing_exists_by_url(conn, redirect_url)` | Secondary cross-source dedup check by URL |
| `insert_listing(listing)` | Insert a new listing row; serialises JSON array columns |
| `update_score(source, source_id, score_data)` | Write scoring results back to an existing row |
| `get_feed(threshold, min_score, remote_only, search, job_type, sort)` | Full-JD listings scored ≥ threshold, not dismissed, not applied |
| `get_snippet_feed(sort)` | Snippet-scored listings, not dismissed, score not NULL |
| `get_bookmarks()` | All bookmarked listings ordered by score DESC |
| `get_applied()` | All listings where `applied = 1`, ordered by `fetched_at DESC` |
| `get_all_scored()` | All listings with `seen = 1`, used by the rescorer |
| `get_listing_by_id(listing_id)` | Single listing by internal primary key |
| `get_job_types()` | Sorted list of distinct non-null `job_type` values |
| `get_last_fetch_time()` | Most recent `fetched_at` timestamp |
| `get_usage_stats(input_cost_per_mtok, output_cost_per_mtok)` | Aggregated token usage and cost totals + per-day breakdown |
| `set_bookmarked(listing_id, value)` | Set `bookmarked` flag to 0 or 1 |
| `set_dismissed(listing_id, value)` | Set `dismissed` flag to 0 or 1 |
| `set_applied(listing_id, value)` | Set `applied` flag to 0 or 1 |
| `toggle_bookmarked(listing_id)` | Atomic flip of `bookmarked`; returns updated listing |
| `toggle_applied(listing_id)` | Atomic flip of `applied`; returns updated listing |
| `geocache_get_many(conn, location_texts)` | Batch geocache lookup by location string |
| `geocache_put(conn, location_text, lat, lon)` | Insert or update a geocache entry |

**Schema — `listings` table:**

| Column | Type | Notes |
|---|---|---|
| `id` | SERIAL PRIMARY KEY | Auto-increment |
| `source` | TEXT NOT NULL | Source identifier, e.g. `"adzuna"` |
| `source_id` | TEXT NOT NULL | Source-specific listing ID |
| `title` | TEXT | |
| `company` | TEXT | |
| `location` | TEXT | |
| `salary_min` | REAL | |
| `salary_max` | REAL | |
| `salary_is_predicted` | INTEGER | 1 = salary is an estimate |
| `contract_type` | TEXT | e.g. `"permanent"` |
| `contract_time` | TEXT | e.g. `"full_time"` |
| `description` | TEXT | Full scraped JD or API snippet fallback |
| `redirect_url` | TEXT | Canonical job URL |
| `created_at` | TEXT | ISO 8601 — when the listing was posted |
| `fetched_at` | TEXT | ISO 8601 — when ingest.py processed it |
| `score` | REAL | LLM score 0–10; NULL if scoring failed |
| `matched_skills` | TEXT | JSON array (deserialised on read) |
| `missing_skills` | TEXT | JSON array (deserialised on read) |
| `concerns` | TEXT | JSON array (deserialised on read) |
| `verdict` | TEXT | One-sentence LLM summary |
| `bookmarked` | INTEGER DEFAULT 0 | |
| `dismissed` | INTEGER DEFAULT 0 | |
| `seen` | INTEGER DEFAULT 0 | 1 = scored; 0 = score failed, retry eligible |
| `model_used` | TEXT | `"provider/model"` string, e.g. `"anthropic/claude-haiku-4-5-20251001"` |
| `tokens_input` | INTEGER | |
| `tokens_output` | INTEGER | |
| `applied` | INTEGER DEFAULT 0 | |
| `job_type` | TEXT | Search query used during ingest (e.g. `"software engineer"`) |
| `posted_at` | TEXT | ISO 8601 — populated from `created_at` when not set by source |
| `opened_at` | TEXT DEFAULT NULL | ISO 8601 — when the listing was first opened in the UI |
| `description_source` | TEXT NOT NULL DEFAULT `'full'` | `'full'` = scored from scraped JD; `'snippet'` = scored from short API description |

**Schema — `location_geocache` table:**

| Column | Type | Notes |
|---|---|---|
| `location_text` | TEXT PRIMARY KEY | Raw location string from listing |
| `lat` | REAL NOT NULL | |
| `lon` | REAL NOT NULL | |
| `cached_at` | TIMESTAMP | Auto-set on insert |

Stores resolved lat/lon for location strings so repeated ingest runs do not
re-call the Nominatim geocoding API for the same location.

**Constraints and indexes:**
- `UNIQUE(source, source_id)` — primary dedup constraint; `INSERT ... ON CONFLICT DO NOTHING` skips duplicates
- `CREATE INDEX idx_listings_redirect_url ON listings (redirect_url)` — secondary dedup by URL

**Migration strategy:** `init_db()` uses `CREATE TABLE IF NOT EXISTS` and
`ALTER TABLE ... ADD COLUMN IF NOT EXISTS` (PostgreSQL 9.6+). Both are idempotent
and safe to call on every startup.

**PostgreSQL-specific notes:**
- `SERIAL PRIMARY KEY` replaces SQLite's `INTEGER PRIMARY KEY AUTOINCREMENT`
- `%s` placeholders replace SQLite's `?`
- `ILIKE` replaces `LOWER(col) LIKE LOWER(?)`
- Sort keys are validated against an explicit allowlist (`_ALLOWED_SORT_COLUMNS`) before interpolation into SQL to prevent injection

---

### 2.2 `ingest.py` — Ingestion Pipeline

Runs as a standalone script. Orchestrates the full pipeline in sequence:

```
1. Load config/config.json and config/profile.json
2. db.init_db()
3. credentials.load_providers() → providers dict
4. job_sources.make_enabled_sources(providers, config) → list of source clients
5. providers.build_provider_chain(providers) → ordered LLM provider list
6. For each enabled source client:
   For each page from client.pages():
     For each listing in page:
       a. Hours filter (--hours flag) — skip if listing is older than cutoff
       b. prefilter(listing, config) — skip if fails title/salary/contract checks
       c. geo filter — skip if outside configured radius_km
       d. db.listing_exists() + db.listing_exists_by_url() — skip duplicates
       e. scrape_description(redirect_url) — full JD or fallback to snippet
       f. score_listing_with_fallback(listing, profile, chain, dead_providers)
       g. db.insert_listing(listing)
7. Print run summary (sources / fetched / filtered / dupes / scored / tokens / cost)
```

**Key functions:**

| Name | Purpose |
|---|---|
| `load_config(path)` | Load and validate `config/config.json` |
| `load_profile(path)` | Load `config/profile.json`; raises `SystemExit` on missing/invalid |
| `prefilter(listing, config)` | Returns `None` (pass) or a reason string (fail) |
| `scrape_description(url, fallback)` | HTTP GET + BS4 parse; returns `(text, scraped_ok)` |
| `score_listing(description, profile, provider)` | Single-provider scoring call |
| `score_listing_with_fallback(listing, profile, chain, dead_providers)` | Tries providers in order; auth errors permanently disable a provider for the run |
| `run(...)` | Full ingest orchestrator |
| `rescore(...)` | Re-score all `seen=1` listings against current profile (no new fetches) |
| `format_skills_for_prompt(primary_skills)` | Convert `profile.json` skill objects to LLM-readable strings |

---

### 2.3 `app.py` — Flask Server

Thin routing layer. All data access goes through `db.py`; no business logic lives here.

| Route | Method | Template / Response |
|---|---|---|
| `/` | GET | `index.html` — feed; accepts `min_score`, `remote_only`, `search`, `job_type`, `sort` query params |
| `/bookmarks` | GET | `index.html` — bookmarked listings |
| `/applied` | GET | `index.html` — applied listings |
| `/snippets` | GET | `index.html` — snippet-scored listings (separate tab) |
| `/stats` | GET | `stats.html` — token usage and cost dashboard + runtime versions |
| `/bookmark/<id>` | POST | HTMX partial — updated action buttons (`_actions.html`) |
| `/dismiss/<id>` | POST | HTMX — empty 200; card removed from DOM |
| `/apply/<id>` | POST | HTMX partial — updated action buttons (`_actions.html`) |
| `/listings/<id>/open` | POST | HTMX — marks listing opened, returns updated state |
| `/ingest/trigger` | POST | Spawns `ingest.py` subprocess; returns 202 with `_ingest_trigger.html` or 409 if already running |
| `/ingest/status` | GET | Polls subprocess state; returns `_ingest_trigger.html` partial + `HX-Trigger: ingestComplete` on completion |
| `/settings` | GET | `settings.html` — LLM credentials and job source settings; `?tab=llm` or `?tab=sources` |
| `/settings` | POST | Save credentials via `credentials.save_providers()`; redirect to GET |
| `/profile` | GET | `profile.html` — `config.json` editor; sensitive fields masked as `"***"` |
| `/profile` | POST | Validate JSON, restore masked fields, write `config.json`; returns 400 on parse error |
| `/profile/import-pdf` | POST | Async PDF import — spawns background job to extract profile from uploaded PDF |
| `/profile/import-pdf/status/<job_id>` | GET | Poll status of PDF import background job |
| `/settings/config` | GET | 301 redirect to `/profile` |
| `/admin/clear-db` | POST | Truncate all listings (admin-only action) |
| `/api/validate-keys` | POST | Test each configured LLM provider; returns `_validation_results.html` partial |
| `/api/providers/reorder` | POST | Persist `provider_order` list from drag-to-reorder; returns `_provider_order.html` partial |
| `/api/job-sources/<key>/toggle` | POST | Enable or disable a job source; returns updated source card |

HTMX actions swap only the affected element — no full page reload.

---

### 2.4 Templates and Static Files

**Templates** (`templates/`):

| File | Purpose |
|---|---|
| `index.html` | Main page — feed, bookmarks, applied, and snippets views |
| `stats.html` | API usage and cost dashboard + runtime component versions |
| `settings.html` | LLM provider credentials and job source settings (tabbed) |
| `profile.html` | `config.json` editor textarea |
| `_card.html` | Listing card partial — reused across all list views |
| `_actions.html` | Action buttons partial — returned by HTMX write routes |
| `_ingest_trigger.html` | Ingest trigger button / in-progress indicator |
| `_provider_order.html` | Provider drag-to-reorder list fragment |
| `_validation_results.html` | Credential validation results fragment |

**Static files** (`static/`):

| File | Purpose |
|---|---|
| `style.css` | All application styles — see `docs/STYLE_GUIDE.md` for token/component reference |
| `favicon.svg` | JM monogram favicon |
| `js/sortable.min.js` | Drag-to-reorder library used by the provider order UI on the settings page |

---

### 2.5 `config/profile.json` — Skills Profile

Human-editable. Loaded at scoring time and injected into the LLM prompt.
Gitignored — copy from `config/profile.example.json`.

```json
{
  "primary_skills": [
    {
      "description": "Python",
      "years_active": 5,
      "active": true
    }
  ],
  "anti_preferences": [
    "no .NET",
    "no pure frontend"
  ],
  "seniority": "Senior / Staff",
  "education": [
    "B.S. Computer Science"
  ],
  "preferred_industries": ["fintech", "developer tooling", "infrastructure"],
  "location": {
    "center": "Miami, FL",
    "radius_km": 80,
    "geocode_fallback": "pass",
    "notes": "Open to remote or on-site / hybrid in South Florida"
  },
  "scoring_notes": ""
}
```

`primary_skills` is an array of objects with `description` (string),
`years_active` (integer), and `active` (boolean). Active skills are weighted more
heavily. `format_skills_for_prompt()` in `ingest.py` converts these objects to
LLM-readable strings before sending.

`education` entries are injected into the scoring prompt so the LLM does not flag
degree requirements as concerns when the candidate already satisfies them.

`location.geocode_fallback` controls what happens when a listing location cannot
be geocoded: `"pass"` (default) allows the listing through; `"discard"` drops it.

---

### 2.6 `config/config.json` — Runtime Configuration

Gitignored — copy from `config/config.example.json`. Edited via the `/profile` UI
or directly.

```json
{
  "search": {
    "country": "us",
    "what": "software engineer",
    "where": "miami",
    "distance": 32,
    "max_days_old": 14,
    "salary_min": 120000,
    "results_per_page": 50,
    "max_pages": 5
  },
  "scoring": {
    "threshold": 7.0
  },
  "prefilter": {
    "title_exclude": ["junior", "intern", "manager"],
    "title_include": ["engineer", "developer", "architect"],
    "require_contract_time": null,
    "require_contract_type": null
  }
}
```

---

### 2.7 `config/providers.json` — Unified Credential Store

Unified credential store for all LLM providers and job source API credentials.
Managed via the `/settings` UI. Gitignored — copy from `config/providers.example.json`.

```json
{
  "provider_order": ["anthropic", "openai"],
  "llm": {
    "anthropic": { "api_key": "...", "model": "claude-haiku-4-5-20251001" },
    "openai":    { "api_key": "...", "model": "gpt-4o-mini" },
    "gemini":    { "api_key": "...", "model": "gemini-1.5-flash" }
  },
  "job_sources": {
    "adzuna": { "app_id": "...", "app_key": "...", "enabled": true },
    "jooble": { "api_key": "...", "enabled": false }
  }
}
```

The `enabled` field on each job source is managed by the `/settings` UI toggle.
The `provider_order` array controls the LLM fallback sequence and is managed by
the drag-to-reorder interface.

---

### 2.8 `job_sources/` — Pluggable Job Source Module

Provides an abstract base class and a dynamic plugin loader so the ingestion
pipeline can fetch from multiple sources without source-specific branching in
`ingest.py`.

**Abstract base class** (`job_sources/base.py` → `JobSource`):

| Method | Signature | Purpose |
|---|---|---|
| `fetch_page(page)` | `(int) → list[dict]` | Fetch one page of raw listings |
| `total_pages()` | `() → int` | Return the number of pages to iterate |
| `normalise(raw)` | `(dict) → dict` | Convert a raw listing to the canonical schema |
| `settings_schema()` | `(cls) → dict` | Return `{display_name, fields}` for the Settings UI |
| `pages()` | `() → Iterator[list[dict]]` | Default pagination iterator |

All `normalise()` implementations must return a dict with these keys:
`source`, `source_id`, `title`, `company`, `location`, `salary_min`,
`salary_max`, `salary_period`, `contract_type`, `contract_time`, `description`,
`redirect_url`, `created_at`. An optional `skip_scrape` boolean flag tells the
pipeline to skip the HTTP scrape step and use the API description directly (useful
when the source URL is known to block scrapers).

**Plugin system** (`job_sources/loader.py`):

Sources are discovered from `plugins/sources/` at import time. Each plugin is a
subdirectory containing:

- `plugin.py` — must define exactly one `JobSource` subclass
- `source.json` — metadata manifest with required keys: `source_key`,
  `display_name`, `description`, `home_url`, `fields`

The loader skips folders whose names start with `_` (used for templates/drafts).
`source_key` must match the folder name exactly. Duplicate `source_key` values
are rejected. Failed plugins are skipped with a warning — they never crash the
loader.

**Factory functions:**

- `get_sources()` — lazy registry accessor; scans `plugins/sources/` on first
  call and caches the result
- `make_source(config)` — instantiate a single source from `config["job_source"]`
  (default: `"adzuna"`)
- `make_enabled_sources(providers_data, config)` — return all sources where
  `providers_data["job_sources"][key]["enabled"] == True` and required credentials
  are present

---

### 2.9 `providers/` — LLM Provider Module

Provides an abstract base class and three concrete LLM backends with a shared
fallback chain mechanism.

**Abstract base** (`providers/base.py` → `LLMProvider`):

| Member | Purpose |
|---|---|
| `complete(prompt)` | Send a completion request; return scored result dict with token counts |
| `input_cost_per_mtok` | USD per million input tokens |
| `output_cost_per_mtok` | USD per million output tokens |
| `settings_schema()` | Return `{display_name, fields}` for the Settings UI |

**Concrete providers:**

| Key | Class | Default model |
|---|---|---|
| `anthropic` | `AnthropicProvider` | `claude-haiku-4-5-20251001` |
| `openai` | `OpenAIProvider` | `gpt-4o-mini` |
| `gemini` | `GeminiProvider` | `gemini-1.5-flash` |

**`build_provider_chain(providers_data)`** reads `providers_data["provider_order"]`
and the `providers_data["llm"]` sub-dict to return an ordered list of instantiated
`LLMProvider` objects. Providers with an empty `api_key` are excluded.

**`score_listing_with_fallback(listing, profile, chain, dead_providers)`** iterates
the chain in order:
- **Auth error (401/403):** permanently adds the provider to `dead_providers` for the rest of the run
- **Transient error (rate limit, 5xx, network):** logs a warning and moves to the next provider; provider remains available for subsequent listings
- **Success:** injects `"model_used": "provider/model"` into the result dict and returns it

**Scoring prompt** — the LLM is expected to return a JSON object with exactly:

| Field | Type | Description |
|---|---|---|
| `score` | number 0–10 | Overall fit score |
| `matched_skills` | array of strings | Skills from the JD the candidate has |
| `missing_skills` | array of strings | Skills required but absent from the profile |
| `concerns` | array of strings | Other issues (culture fit, location, seniority) |
| `verdict` | string | One-sentence summary of the match |

Markdown code fences are stripped before JSON parsing.

---

### 2.10 `credentials.py` — Unified Credential Loading

Single shared module imported by both `ingest.py` and `app.py`.

**Public API:**
- `CredentialError` — raised when no usable credentials can be found
- `load_providers(providers_path, keys_path, config_path)` — load `providers.json`
  with fallback migration from legacy `keys.json`, then env vars
- `migrate_from_legacy(...)` — atomic one-time migration; original files are never modified
- `save_providers(updates, providers_path)` — deep-merge updates into `providers.json`;
  write is atomic via `.tmp` rename

**Credential precedence:**
1. `providers.json` present and parseable → use it; env vars are NOT consulted
2. `providers.json` absent → attempt migration from legacy `keys.json`
3. Migration succeeds → return migrated data (also writes `providers.json`)
4. Migration returns `None` → build from env vars (`ANTHROPIC_API_KEY`,
   `OPENAI_API_KEY`, `GOOGLE_API_KEY`, `ADZUNA_APP_ID`, `ADZUNA_APP_KEY`)
5. No env vars → raise `CredentialError`

---

## 3. Data Flow — Ingestion Run

```
ingest.py
  │
  ├─ load_config(), load_profile()
  ├─ db.init_db()
  ├─ credentials.load_providers() → providers dict
  ├─ job_sources.make_enabled_sources(providers, config) → [source1, source2, ...]
  ├─ providers.build_provider_chain(providers) → [llm1, llm2, ...]
  │
  └─ for source in sources:
       for page in source.pages():
         for listing in page:
           │
           ├─ hours filter     → skip if too old
           ├─ prefilter()      → skip if title/salary/contract fails
           ├─ geo filter       → skip if outside radius_km
           ├─ dedup check      → skip if (source, source_id) or URL already exists
           ├─ scrape_description(redirect_url) → full text or snippet fallback
           ├─ score_listing_with_fallback(...) → {score, skills, verdict, model_used}
           └─ db.insert_listing({...listing + score data})
```

**Scraping fallback:** If the scraper fails (timeout, bot block, text too short),
the source API snippet is used as the description and `description_source` is set
to `'snippet'`. Scoring still proceeds — the result may be lower quality but the
listing is not lost.

**Provider fallback:** Auth failures permanently remove a provider from the chain
for that run. Transient failures skip only the current listing. If all providers
fail for a listing, it is stored with `score = NULL` and `seen = 0` for retry on
the next run.

---

## 4. Data Flow — UI Interaction

```
Browser
  │
  ├─ GET / → Flask → db.get_feed() → render index.html
  │
  ├─ POST /bookmark/42
  │    hx-swap="outerHTML" on the button group
  │    → Flask → db.toggle_bookmarked(42) → render _actions.html partial
  │
  ├─ POST /dismiss/42
  │    hx-swap="outerHTML" hx-target="#card-42"
  │    → Flask → db.set_dismissed(42, 1) → return "" (removes card)
  │
  └─ POST /ingest/trigger
       → Flask → subprocess.Popen([sys.executable, "ingest.py", ...])
       → returns 202 with polling partial
       → GET /ingest/status polls until done
       → on completion: HX-Trigger: ingestComplete → feed reloads
```

---

## 5. Deployment

### Docker Compose

Each stack is defined by a Compose file and a corresponding `.env` file:

| | Dev | Prod |
|---|---|---|
| Compose file | `docker-compose.dev.yml` | `docker-compose.prod.yml` |
| Env file | `.env.dev` | `.env.prod` |
| Host port | 5000 | 5001 |
| Database name | `jobmatcher_dev` | `jobmatcher_prod` |
| Config volume | `./config-dev` | `./config` |
| Logs volume | `./logs-dev` | `./logs` |
| Project name | `job-matcher-pr-dev` | `job-matcher-pr-prod` |

**Starting stacks:**
```bash
# Dev
docker compose -p job-matcher-pr-dev -f docker-compose.dev.yml up -d

# Prod
docker compose -p job-matcher-pr-prod -f docker-compose.prod.yml up -d
```

**Containers per stack:**

- **`db`** — `postgres:16-alpine`. Data persists in a named Docker volume
  (`pgdata_dev` / `pgdata_prod`). Exposes a healthcheck so the `web` container
  waits for readiness before starting.
- **`web`** — Application image pulled from GHCR
  (`ghcr.io/cbeaulieu-gt/job-matcher-pr`). Mounts config and logs directories
  from the host. Receives `DATABASE_URL` as an environment variable.
- **`scheduler`** — `mcuadros/ofelia`. Reads cron schedules from Docker labels on
  the `web` container and executes `python ingest.py --hours 25` daily via
  `docker exec`. Requires Docker socket access (mounted read-only).

### Ofelia Scheduling

Ofelia is configured via Docker labels on the `web` container:

```yaml
labels:
  ofelia.enabled: "true"
  ofelia.job-exec.daily-ingest-dev.schedule: "@daily"
  ofelia.job-exec.daily-ingest-dev.command: "python ingest.py --hours 25"
```

This triggers a `docker exec` into the running `web` container daily. The
`--hours 25` flag limits ingestion to listings posted in the last 25 hours,
ensuring there is slight overlap with the previous day's run.

### Image Publishing

The application image is built from the `Dockerfile` and published to
`ghcr.io/cbeaulieu-gt/job-matcher-pr` via GitHub Actions on merge to `main`.
The `web` service in each Compose file pulls from GHCR rather than building locally.

### Helper Scripts

```
scripts/
├── docker-setup.sh      — one-time VM provisioning (Docker install, user groups)
├── docker-status.sh     — show running containers, ports, and recent logs
└── docker-teardown.sh   — stop and remove containers (data volumes preserved)
```

---

## 6. Key Design Decisions

### PostgreSQL, no ORM
Schema is small and stable. `psycopg2` is the only driver dependency; an ORM adds
complexity without benefit for a well-defined query surface. PostgreSQL over SQLite
was chosen for Docker deployment correctness — concurrent access from the `web`
container and Ofelia-triggered ingest would cause SQLite lock contention.

### Connection pooling in `db.py`
A module-level `ThreadedConnectionPool` avoids per-request TCP handshake overhead.
Flask's multi-threaded request handling means multiple connections may be live
simultaneously; the pool handles this safely.

### Why HTMX (not React/Vue)
Zero build tooling. The UI is a read-mostly display layer with a small number of
write actions. HTMX handles all of them with a CDN script tag and a few HTML
attributes.

### Why pre-filter before LLM
Each LLM call costs ~$0.001. At 500 listings/run with a 60% filter rate, this saves
~300 calls per run. Over weeks of daily runs this compounds meaningfully.

### Why scrape the full description
Source API snippets are typically 200–300 characters — not enough for reliable skill
matching. The full JD gives the LLM the context it needs for accurate scoring.

### Why decouple ingest from serve
The ingestion run can take minutes (scraping + LLM calls). Running it inside the
web request would block the UI. Decoupling means the UI is always responsive and
ingest can be scheduled independently via Ofelia.

### Why `config/profile.json` rather than a DB table
The profile is edited as a whole unit, infrequently, and is straightforward to
version-control as a flat file.

### Why `config/providers.json` separate from `config/config.json`
Credentials change more often and are more sensitive than search parameters.
Separation allows tighter file-level access controls (the config volume mounts
can apply different permissions per file if needed).

### Why a pluggable job_sources module
Multiple sources run without any branching in the orchestrator. Adding a new source
requires only a `plugin.py` and `source.json` in `plugins/sources/` — no changes
to `ingest.py`.

### Why a provider chain with fallback
A single LLM provider is a single point of failure. The fallback chain means a
temporary rate limit or quota exhaustion on the primary provider does not stop a run.

### Ofelia vs. Task Scheduler / cron
Ofelia runs entirely within Docker, eliminating host-level cron configuration. It
reads its schedule from container labels, keeping scheduling config colocated with
the service definition in the Compose file.

---

## 7. Error Handling Strategy

| Failure | Handling |
|---|---|
| Source API error (4xx/5xx) | Log warning; source client returns empty list for that page |
| Scrape timeout / bot block | Use API snippet as fallback; log `SCRAPE FALLBACK` |
| Scrape produces short text | Use API snippet as fallback |
| Geocoding failure | Honour `geocode_fallback` setting (`"pass"` or `"discard"`) |
| LLM returns non-JSON | Provider retries once; if still broken, returns `None` |
| LLM auth error (401/403) | Provider permanently disabled for the rest of the run |
| LLM transient error | Skip current listing; provider stays available |
| All providers fail for listing | Store with `score = NULL`, `seen = 0` for retry |
| DB write failure | Log and skip listing; run continues |
| Missing required config keys | `SystemExit` at startup with a descriptive message |
| `DATABASE_URL` absent | `RuntimeError` at `db.py` import time |

---

## 8. Dependencies

**Core** (`requirements.txt`):
- `flask` — web framework
- `requests` — HTTP client for scraping and source API calls
- `beautifulsoup4` — HTML parsing for the scraper
- `psycopg2-binary` — PostgreSQL driver
- `geopy` — geocoding for geo filter
- `waitress` — production WSGI server

**LLM clients** (`requirements.txt`):
- `anthropic` — Anthropic (Claude) API client
- `openai` — OpenAI API client
- `google-genai` — Google Gemini API client

**Dev** (`requirements-dev.txt`):
- `pytest` — test runner
- `ruff` — linter

---

## 9. File Map

```
job_matcher/
├── app.py                       # Flask server + route handlers
├── db.py                        # PostgreSQL schema, pooling, and all query helpers
├── ingest.py                    # CLI pipeline: fetch → filter → scrape → score → store
├── credentials.py               # Unified credential loading (shared by ingest + app)
├── Dockerfile                   # Application image build
├── docker-compose.dev.yml       # Dev stack (port 5000, jobmatcher_dev)
├── docker-compose.prod.yml      # Prod stack (port 5001, jobmatcher_prod)
├── requirements.txt             # Python dependencies (pinned)
├── requirements-dev.txt         # Dev-only dependencies (pytest, ruff)
├── README.md
├── CLAUDE.md
├── config/
│   ├── config.json              # Search params + scoring threshold (gitignored)
│   ├── config.example.json
│   ├── profile.json             # Candidate skills profile (gitignored)
│   ├── profile.example.json
│   ├── providers.json           # LLM + job source credentials (gitignored)
│   └── providers.example.json
├── plugins/                     # External job source plugins (auto-discovered)
│   └── sources/
│       └── <source_key>/        # One folder per plugin; _ prefix = skipped
│           ├── plugin.py        # JobSource subclass implementation
│           └── source.json      # Manifest: source_key, display_name, fields, …
├── job_sources/                 # Plugin framework
│   ├── __init__.py              # get_sources(), make_source(), make_enabled_sources()
│   ├── base.py                  # JobSource abstract base class
│   └── loader.py                # Dynamic plugin discovery from plugins/sources/
├── providers/                   # LLM provider backends
│   ├── __init__.py              # build_provider_chain(), _PROVIDER_CLASS_MAP
│   ├── base.py                  # LLMProvider abstract base class
│   ├── anthropic_provider.py
│   ├── openai_provider.py
│   └── gemini_provider.py
├── scripts/                     # Docker deployment helpers
│   ├── docker-setup.sh          # One-time VM provisioning
│   ├── docker-status.sh         # Show running containers and logs
│   └── docker-teardown.sh       # Stop and remove containers
├── templates/                   # Jinja2 templates (see §2.4)
├── static/
│   ├── style.css                # All styles — see docs/STYLE_GUIDE.md
│   ├── favicon.svg
│   └── js/
│       └── sortable.min.js      # Drag-to-reorder for provider order UI
├── tests/
└── docs/
    ├── DESIGN.md                # This document
    ├── STYLE_GUIDE.md           # CSS token and component reference
    └── PLUGIN_DEVELOPMENT.md    # Step-by-step guide for adding new job sources
```

---

## 10. Out of Scope

- Application status notes / tracking beyond the `applied` flag
- Resume parsing to auto-generate `config/profile.json` (beyond the PDF import feature)
- Email digest or push notifications
- Multi-user support
