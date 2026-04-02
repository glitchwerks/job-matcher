# Job Matcher — Design Document

> This document covers architecture, component design, data flow, key decisions,
> and edge-case handling. It reflects the current v2 codebase.

---

## 1. High-Level Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                         ingest.py (CLI)                          │
│                                                                  │
│  job_sources/        credentials.py   providers/                │
│  (9 pluggable    ──► load_providers() ──► build_provider_chain() │
│   sources)               │                       │               │
│       │                  ▼                       ▼               │
│  PreFilter ──► Scraper ──► score_listing_with_fallback() ──► DB  │
└──────────────────────────────────┬───────────────────────────────┘
                                   │ writes to
                             jobs.db (SQLite)
                                   │ reads from
┌──────────────────────────────────▼───────────────────────────────┐
│                        app.py (Flask server)                     │
│                                                                  │
│  GET /                  → main feed (scored, not dismissed)      │
│  GET /bookmarks         → bookmarked listings only               │
│  GET /applied           → applied listings only                  │
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
│  GET  /settings/config            → 301 redirect to /profile     │
└──────────────────────────────────────────────────────────────────┘
```

The ingestion pipeline and web server are **fully decoupled**. `ingest.py` is a CLI
script run manually or via Task Scheduler whether or not the Flask server is running.
They communicate only through the shared SQLite file.

### Deployment Model

The application runs natively on Windows using two independent processes:

```
┌──────────────────────────────────────────────────────────────────┐
│                    Windows native deployment                      │
│                                                                  │
│  NSSM service                  Task Scheduler job                │
│  (waitress-serve app:app)      (ingest.py --hours 25             │
│                                 daily at 6am)                    │
│          │                           │                           │
│          └──────────────┬────────────┘                           │
│                         │ shared SQLite file                     │
│                   C:\Apps\job_matcher\jobs.db                    │
└──────────────────────────────────────────────────────────────────┘
```

`DB_PATH` and Adzuna credentials can be set as machine-level Windows environment
variables so both processes pick them up automatically. LLM provider API keys and
job source credentials are stored in `config/providers.json` and managed through
the `/settings` UI — they are never set as environment variables.

---

## 2. Component Design

### 2.1 `db.py` — Database Layer

Owns all SQLite interactions. No other module opens the database file directly.

**Public functions:**

| Function | Purpose |
|---|---|
| `get_connection(db_path)` | Return an open connection with `row_factory = sqlite3.Row` |
| `init_db(db_path)` | Create or migrate the `listings` table; idempotent on every startup |
| `listing_exists(conn, source, source_id)` | Primary dedup check by `(source, source_id)` |
| `listing_exists_by_url(conn, redirect_url)` | Secondary cross-source dedup check by URL |
| `insert_listing(listing, db_path)` | Insert a new listing row; serialises JSON array columns |
| `update_score(source, source_id, score_data, db_path)` | Write scoring results back to an existing row |
| `get_feed(threshold, min_score, remote_only, search, job_type, sort, db_path)` | Listings scored ≥ threshold, not dismissed, not applied |
| `get_bookmarks(db_path)` | All bookmarked listings ordered by score DESC |
| `get_applied(db_path)` | All listings where `applied = 1`, ordered by `fetched_at DESC` |
| `get_all_scored(db_path)` | All listings with `seen = 1`, used by the rescorer |
| `get_listing_by_id(listing_id, db_path)` | Single listing by internal primary key |
| `get_job_types(db_path)` | Sorted list of distinct non-null `job_type` values |
| `get_last_fetch_time(db_path)` | Most recent `fetched_at` timestamp (for "last updated" display) |
| `get_usage_stats(db_path, input_cost_per_mtok, output_cost_per_mtok)` | Aggregated token usage and cost totals + per-day breakdown |
| `set_bookmarked(listing_id, value, db_path)` | Set `bookmarked` flag to 0 or 1 |
| `set_dismissed(listing_id, value, db_path)` | Set `dismissed` flag to 0 or 1 |
| `set_applied(listing_id, value, db_path)` | Set `applied` flag to 0 or 1 |
| `toggle_bookmarked(listing_id, db_path)` | Atomic flip of `bookmarked`; returns updated listing |
| `toggle_applied(listing_id, db_path)` | Atomic flip of `applied`; returns updated listing |

**Schema — `listings` table:**

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | Autoincrement |
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

**Constraints and indexes:**
- `UNIQUE(source, source_id)` — primary dedup constraint
- `CREATE INDEX idx_listings_redirect_url ON listings (redirect_url)` — secondary dedup

**Migration strategy:** `init_db()` inspects the existing schema and routes to one of
four migration paths (fresh, legacy `adzuna_id` column, partial migration, or
already-migrated). All paths are idempotent and safe to call on every startup.

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
       c. db.listing_exists() + db.listing_exists_by_url() — skip duplicates
       d. scrape_description(redirect_url) — full JD or fallback to snippet
       e. score_listing_with_fallback(listing, profile, chain, dead_providers)
       f. db.insert_listing(listing)
7. Print run summary (sources / fetched / filtered / dupes / scored / tokens / cost)
```

**Key functions:**

| Name | Purpose |
|---|---|
| `load_config(path)` | Load and validate `config/config.json`; env vars override Adzuna credentials |
| `load_profile(path)` | Load `config/profile.json`; raises `SystemExit` on missing/invalid |
| `prefilter(listing, config)` | Returns `None` (pass) or a reason string (fail) |
| `scrape_description(url, fallback)` | GET + BS4 parse; returns `(text, scraped_ok)` |
| `score_listing(description, profile, provider)` | Single-provider scoring call |
| `score_listing_with_fallback(listing, profile, chain, dead_providers)` | Tries providers in order; auth errors permanently disable a provider for the run |
| `run(...)` | Full ingest orchestrator |
| `rescore(...)` | Re-score all `seen=1` listings against current profile (no new fetches) |

**Note:** `AdzunaClient` has moved to `job_sources/adzuna.py`. It is re-exported from
`ingest.py` for backward compatibility but should be imported from `job_sources` in
new code.

---

### 2.3 `app.py` — Flask Server

Thin routing layer. All data access goes through `db.py`; no business logic lives here.

| Route | Method | Template / Response |
|---|---|---|
| `/` | GET | `index.html` — feed; accepts `min_score`, `remote_only`, `search`, `job_type`, `sort` query params |
| `/bookmarks` | GET | `index.html` — bookmarked listings |
| `/applied` | GET | `index.html` — applied listings |
| `/stats` | GET | `stats.html` — token usage and cost dashboard + runtime versions |
| `/bookmark/<id>` | POST | HTMX partial — updated action buttons (`_actions.html`) |
| `/dismiss/<id>` | POST | HTMX — empty 200; card removed from DOM |
| `/apply/<id>` | POST | HTMX partial — updated action buttons (`_actions.html`) |
| `/ingest/trigger` | POST | Spawns `ingest.py` subprocess; returns 202 with `_ingest_trigger.html` or 409 if already running |
| `/ingest/status` | GET | Polls subprocess state; returns `_ingest_trigger.html` partial + `HX-Trigger: ingestComplete` on completion |
| `/settings` | GET | `settings.html` — LLM credentials and job source settings; `?tab=llm` or `?tab=sources` |
| `/settings` | POST | Save credentials via `credentials.save_providers()`; redirect to GET |
| `/profile` | GET | `profile.html` — `config.json` editor; sensitive fields masked as `"***"` |
| `/profile` | POST | Validate JSON, restore masked fields, write `config.json`; returns 400 on parse error |
| `/settings/config` | GET | 301 redirect to `/profile` |
| `/api/validate-keys` | POST | Test each configured LLM provider; returns `_validation_results.html` partial |
| `/api/providers/reorder` | POST | Persist `provider_order` list from drag-to-reorder; returns `_provider_order.html` partial |

HTMX actions swap only the affected element — no full page reload.

---

### 2.4 Templates and Static Files

**Templates** (`templates/`):

| File | Purpose |
|---|---|
| `index.html` | Main page — feed, bookmarks, and applied views (mode set by `view` context var) |
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

Human-editable. Loaded at scoring time and injected into the LLM prompt verbatim.
Gitignored — copy from `config/profile.example.json`.

```json
{
  "primary_skills": [
    "Python, 5yr, active",
    "Go, 2yr, active",
    "SQL, 6yr, active"
  ],
  "anti_preferences": [
    "no .NET",
    "no pure frontend",
    "no QA/testing roles"
  ],
  "seniority": "Senior / Staff",
  "preferred_industries": ["fintech", "developer tooling", "infrastructure"],
  "location_preference": "remote or Miami, FL",
  "scoring_notes": ""
}
```

---

### 2.6 `config/config.json` — Runtime Configuration

Gitignored — copy from `config/config.example.json`. Edited via the `/profile` UI or
directly. Adzuna credentials and `DB_PATH` can also be set as environment variables
(`ADZUNA_APP_ID`, `ADZUNA_APP_KEY`).

```json
{
  "adzuna_app_id": "",
  "adzuna_app_key": "",
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

**`config/providers.json`** supersedes the legacy `keys.json`. It is the unified
credential store for LLM provider keys and job source API credentials. Managed via the
`/settings` UI and written atomically via `credentials.save_providers()`. Gitignored —
copy from `config/providers.example.json`.

---

### 2.7 `job_sources/` — Pluggable Job Source Module

Provides an abstract base class and seven concrete backends so the ingestion pipeline
can fetch from multiple sources without source-specific branching in `ingest.py`.

**Abstract base class** (`job_sources/base.py` → `JobSource`):

| Method | Signature | Purpose |
|---|---|---|
| `fetch_page(page)` | `(int) → list[dict]` | Fetch one page of raw listings |
| `total_pages()` | `() → int` | Return the number of pages to iterate |
| `normalise(raw)` | `(dict) → dict` | Convert a raw listing to the canonical schema |
| `settings_schema()` | `(cls) → dict` | Return `{display_name, fields}` for the Settings UI |
| `pages()` | `() → Iterator[list[dict]]` | Default pagination iterator (calls `fetch_page` + `normalise`) |

All `normalise()` implementations must return a dict with these keys: `source`,
`source_id`, `title`, `company`, `location`, `salary_min`, `salary_max`,
`salary_period`, `contract_type`, `contract_time`, `description`, `redirect_url`,
`created_at`.

**Registered sources** (`SOURCES` dict in `job_sources/__init__.py`):

| Key | Class | Notes |
|---|---|---|
| `adzuna` | `AdzunaClient` | Requires `adzuna_app_id` and `adzuna_app_key` |
| `arbeitnow` | `ArbeitnowClient` | No credentials required |
| `himalayas` | `HimalayasClient` | No credentials required |
| `remoteok` | `RemoteOKClient` | No credentials required |
| `usajobs` | `USAJobsClient` | Requires `user_agent` header value |
| `the_muse` | `TheMuseClient` | No credentials required |
| `remotive` | `RemotiveClient` | No credentials required |
| `jobicy` | `JobicyClient` | No credentials required |
| `jooble` | `JoobleClient` | Requires `api_key` |

**Factory functions:**

- `make_source(config)` — instantiate a single source from `config["job_source"]`
  (default: `"adzuna"`).
- `make_enabled_sources(providers_data, config)` — return all sources where
  `providers_data["job_sources"][key]["enabled"] == True` and required credentials
  are present.

---

### 2.8 `providers/` — LLM Provider Module

Provides an abstract base class and three concrete LLM backends with a shared fallback
chain mechanism.

**Abstract base** (`providers/base.py` → `LLMProvider`):

| Member | Purpose |
|---|---|
| `complete(prompt)` | Send a completion request; return scored result dict with token counts |
| `input_cost_per_mtok` | USD per million input tokens (used for cost tracking) |
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
`LLMProvider` objects. Providers with an empty `api_key` are excluded from the chain.

**`score_listing_with_fallback(listing, profile, chain, dead_providers)`** iterates the
chain in order:
- **Auth error (401/403):** permanently adds the provider to `dead_providers` for the
  rest of the run — the API key is bad.
- **Transient error (rate limit, 5xx, network):** logs a warning and moves to the next
  provider; this provider remains available for subsequent listings.
- **Success:** injects `"model_used": "provider/model"` into the result dict and
  returns it.

---

### 2.9 `credentials.py` — Unified Credential Loading

Single shared module imported by both `ingest.py` and `app.py`.

**Public API:**
- `CredentialError` — raised when no usable credentials can be found
- `load_providers(providers_path, keys_path, config_path)` — load `providers.json`
  with fallback migration from legacy `keys.json`/`config.json`, then env vars
- `migrate_from_legacy(...)` — atomic one-time migration from `keys.json` + `config.json`
  to `providers.json`; original files are never modified
- `save_providers(updates, providers_path)` — deep-merge updates into `providers.json`;
  write is atomic via `.tmp` rename

**Credential precedence:**
1. `providers.json` present and parseable → use it; env vars are NOT consulted
2. `providers.json` absent → attempt migration from legacy files
3. Migration succeeds → return migrated data (also writes `providers.json`)
4. Migration returns `None` → build from env vars (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`,
   `GOOGLE_API_KEY`, `ADZUNA_APP_ID`, `ADZUNA_APP_KEY`)
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
           ├─ hours filter → skip if too old
           ├─ prefilter(listing, config) → skip if fails
           ├─ db.listing_exists() + db.listing_exists_by_url() → skip if dupe
           ├─ scrape_description(redirect_url) → full text or snippet fallback
           ├─ score_listing_with_fallback(...) → {score, skills, verdict, model_used}
           └─ db.insert_listing({...listing + score data})
```

**Scraping fallback:** If the scraper fails (timeout, bot block, text too short), the
source API snippet is used as the description. Scoring still proceeds — the result may
be lower quality but the listing is not lost.

**Provider fallback:** Auth failures permanently remove a provider from the chain for
that run. Transient failures skip only the current listing. If all providers fail for a
listing, it is stored with `score = NULL` and `seen = 0` for retry on the next run.

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

## 5. Key Design Decisions

### Why SQLite stdlib (not SQLAlchemy)
Schema is small and stable. An ORM adds complexity without benefit for a single-user
local tool with a well-defined query surface.

### Why HTMX (not React/Vue)
Zero build tooling. The UI is a read-mostly display layer with a small number of write
actions. HTMX handles all of them with a CDN script tag and a few HTML attributes.

### Why pre-filter before LLM
Each LLM call costs ~$0.001. At 500 listings/run with a 60% filter rate, this saves
~300 calls per run. Over weeks of daily runs this compounds meaningfully.

### Why scrape the full description
Adzuna's API snippet is typically 200–300 characters — not enough for reliable skill
matching. The full JD gives the LLM the context it needs for accurate scoring.

### Why decouple ingest from serve
The ingestion run can take minutes (scraping + LLM calls). Running it inside the web
request would block the UI. Decoupling means the UI is always responsive and ingest
can be scheduled independently. The trigger UI in the browser spawns a subprocess and
polls for completion.

### Why `config/profile.json` rather than a DB table
The profile is edited as a whole unit, infrequently, and is straightforward to
version-control as a flat file.

### Why `config/providers.json` separate from `config/config.json`
Credentials change more often and are more sensitive than search parameters.
Separation allows tighter file-level access controls.

### Why a pluggable job_sources module
Multiple sources (9 at present) run without any branching in the orchestrator.
Adding a new source requires only a new file implementing `JobSource` plus a registry
entry — no changes to `ingest.py`.

### Why a provider chain with fallback
A single LLM provider is a single point of failure. The fallback chain means a
temporary rate limit or quota exhaustion on the primary provider does not stop a run.

---

## 6. Error Handling Strategy

| Failure | Handling |
|---|---|
| Source API error (4xx/5xx) | Log warning; source client returns empty list for that page |
| Scrape timeout / bot block | Use API snippet as fallback; log `SCRAPE FALLBACK` |
| Scrape produces short text | Use API snippet as fallback |
| LLM returns non-JSON | Provider retries once; if still broken, returns `None` |
| LLM auth error (401/403) | Provider permanently disabled for the rest of the run |
| LLM transient error | Skip current listing; provider stays available |
| All providers fail for listing | Store with `score = NULL`, `seen = 0` for retry |
| DB write failure | Log and skip listing; run continues |
| Missing required config keys | `SystemExit` at startup with a descriptive message |

---

## 7. Dependencies

**Core** (`requirements.txt`):
- `flask` — web framework
- `requests` — HTTP client for scraping and source API calls
- `beautifulsoup4` — HTML parsing for the scraper
- `waitress` — production WSGI server (used by the NSSM service)
- `pydantic` — data validation (used internally by LLM SDK clients)

**LLM clients** (`requirements.txt`):
- `anthropic` — Anthropic (Claude) API client
- `openai` — OpenAI API client
- `google-genai` — Google Gemini API client

**Dev** (`requirements-dev.txt`):
- `pytest` — test runner
- `ruff` — linter

Note: `pytest` also appears in `requirements.txt` for convenience in the Windows
native deployment; `requirements-dev.txt` is the canonical dev dependency file.

---

## 8. File Map

```
job_matcher/
├── app.py                       # Flask server + route handlers
├── db.py                        # SQLite schema, migrations, and all query helpers
├── ingest.py                    # CLI pipeline: fetch → filter → scrape → score → store
├── credentials.py               # Unified credential loading (shared by ingest + app)
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
├── job_sources/                 # Pluggable job source backends
│   ├── __init__.py              # Registry (SOURCES), make_source(), make_enabled_sources()
│   ├── base.py                  # JobSource abstract base class
│   ├── adzuna.py
│   ├── arbeitnow.py
│   ├── himalayas.py
│   ├── remoteok.py
│   ├── usajobs.py
│   ├── the_muse.py
│   ├── remotive.py
│   ├── jobicy.py
│   └── jooble.py
├── providers/                   # LLM provider backends
│   ├── __init__.py              # build_provider_chain(), _PROVIDER_CLASS_MAP
│   ├── base.py                  # LLMProvider abstract base class
│   ├── anthropic_provider.py
│   ├── openai_provider.py
│   └── gemini_provider.py
├── scripts/                     # Windows deployment helpers
│   ├── setup.ps1                # Register NSSM service + Task Scheduler job
│   ├── status.ps1               # Show service and scheduler status
│   ├── teardown.ps1             # Remove service and scheduled task
│   └── deploy-remote.ps1        # Remote deployment helper
├── templates/                   # 9 Jinja2 templates (see §2.4)
├── static/
│   ├── style.css                # All styles — see docs/STYLE_GUIDE.md
│   ├── favicon.svg
│   └── js/
│       └── sortable.min.js      # Drag-to-reorder for provider order UI
├── tests/
└── docs/
    ├── DESIGN.md                # This document
    └── STYLE_GUIDE.md           # CSS token and component reference
```

---

## 9. Out of Scope

- Application status notes / tracking beyond the `applied` flag
- Resume parsing to auto-generate `config/profile.json`
- Email digest or push notifications
- Cloud / hosted deployment (designed for self-hosted, single-user use)
- Multi-user support
