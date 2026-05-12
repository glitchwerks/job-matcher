# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

@C:\Users\chris\.claude\standards\software-standards.md

## Commands

```powershell
# Install dependencies
uv pip install -r requirements.txt

# Run ingestion pipeline (fetch → filter → scrape → score → store)
python ingest.py
python ingest.py --hours 25        # Only process listings from the last 25 hours
python ingest.py --rescore         # Re-score all stored listings against updated config/profile.json
python ingest.py --verbose         # Log full scoring breakdown (verdict, matched/missing skills, concerns) per listing
python ingest.py -v                # Short form of --verbose

# Run web UI (http://localhost:5000)
python app.py

# Phase A feature flag: route specific sources through JobAggregatorProvider
# JOB_AGGREGATOR_SOURCES=arbeitnow python ingest.py --hours 24
#   → arbeitnow fetched via job_aggregator; all other sources via legacy loader
# Unset (default): all sources use the legacy in-tree loader
# Phase B removal criterion: grep -rn JOB_AGGREGATOR_SOURCES returns empty

# Run tests (requires PostgreSQL pointed at a TEST database — set DATABASE_URL)
# Option A: use jobmatcher_test (created automatically on fresh docker volume;
#           for existing setups run once: docker exec job-matcher-pr-dev-db-1
#           psql -U jobmatcher -d postgres -c "CREATE DATABASE jobmatcher_test;")
# PowerShell
#   $env:DATABASE_URL = "postgresql://jobmatcher:<password>@localhost:5432/jobmatcher_test"; pytest
# Bash/zsh
#   export DATABASE_URL="postgresql://jobmatcher:<password>@localhost:5432/jobmatcher_test" && pytest
# (<password> is in .env.dev or docker-compose.dev.yml)
# Option B: DATABASE_URL already exported in your shell pointing at a test DB
pytest
pytest tests/test_prefilter.py     # Single file
pytest -k "test_title_include"     # By name pattern
# TEST ISOLATION: each test uses scoped DELETE with a test-specific source_id
# prefix (e.g. "test_114_", "cdb-") — only rows inserted by that test are
# removed. No blanket TRUNCATE is used anywhere in the suite.
# SAFETY GUARD: conftest.py refuses to run against a DB whose name does not
# contain "test" (e.g. jobmatcher_dev). Set ALLOW_NON_TEST_DB=1 to override
# (emits a warning). This prevents accidental data loss on dev/prod databases.
```

## Architecture

The app is two decoupled processes sharing a PostgreSQL database (connection via `DATABASE_URL`):

- **`app.py`** — Thin entry point: `from web import create_app; app = create_app()` plus a `__main__` runner for the dev server. WSGI servers (waitress in Docker, gunicorn) import `app` directly. Contains no routes, helpers, or business logic.
- **`web/`** — Flask layer. `web/__init__.py::create_app()` constructs the app, registers Jinja filters, security hooks, context processors, blueprints, `db.init_db()`, and plugins. Blueprints: `feed_bp` (`web/feed.py`), `ingest_bp` (`web/ingest.py`), `settings_bp` (`web/settings.py`), `profile_bp` (`web/profile.py`), `admin_bp` (`web/admin.py`) — all registered with `url_prefix=""` so every URL path is unchanged. Template filters live in `web/filters.py`; CSRF/host guards in `web/security.py`.
- **`services/`** — Pure Python, zero Flask imports. Unit-testable in isolation. Modules: `profile_store.py` (config/profile path constants and load/save helpers), `pdf_import.py` (async PDF-to-profile extraction), `provider_schemas.py` (LLM and job-source schema builders, config warnings, runtime versions), `ingest_control.py` (subprocess lifecycle, SSE state, stdout reader).
- **`ingest.py`** — CLI pipeline: multiple job source APIs → pre-filter → scrape full JD → score with configured LLM provider → insert into DB. Runs on a schedule or manually.
- **`db.py`** — All PostgreSQL access via `psycopg2`. JSON array columns (`matched_skills`, `missing_skills`, `concerns`) are serialized/deserialized here.

### Ingestion pipeline (per listing)

```
source pages → [1] hours filter → [2] prefilter() → [3] geo filter → [4] dedup check → [5] scrape_description() → [6] score_listing() → db.insert_listing()
```

Any step can short-circuit the listing with a logged reason (`FILTERED`, `DUPE`, `SCRAPE FALLBACK`, `SCORE FAILED`). A summary is printed at the end of each run.

### Adding New Job Sources

New job sources are plugins — see `docs/PLUGIN_DEVELOPMENT.md` for the step-by-step guide. The template lives at `plugins/sources/_template/`. Folders starting with `_` are skipped by the loader.

### LLM provider integration

`credentials.load_providers()` reads `config/providers.json` (falling back to legacy `config/keys.json` migration, then env vars `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GOOGLE_API_KEY` if the file is absent). `build_provider_chain()` returns an ordered list of `LLMProvider` instances based on `provider_order` and the `llm` sub-dict in `providers.json`. `score_listing_with_fallback()` tries providers in sequence: auth failures (401/403) permanently remove a provider for the run; transient failures skip only the current listing. The scoring prompt expects a JSON response with exactly: `score` (0–10), `matched_skills`, `missing_skills`, `concerns`, `verdict`. Markdown code fences are stripped before parsing.

Results include a `model_used` field stored as `"provider/model"` per listing. Scoring threshold is set in `config/config.json` under `scoring`. Token counts and estimated cost are stored per listing and aggregated in the `/stats` view.

### Config & profile

- **`config/config.json`** — Search params (`country`, `what`, `where`, `distance`, `max_days_old`, `salary_min`, `results_per_page`, `max_pages`), scoring threshold, and optional `prefilter` block (title include/exclude patterns, contract type/time). Adzuna credentials have moved to `config/providers.json`.
- **`config/keys.json`** — Legacy LLM credential file. Superseded by `config/providers.json`. `credentials.load_providers()` will auto-migrate it to `providers.json` on first run if `providers.json` is absent.
- **`config/profile.json`** — Candidate skills and preferences injected verbatim into the scoring prompt. Fields: `primary_skills` (array of objects with `description` (string), `years_active` (integer), `active` (boolean) — active skills are weighted more heavily; `format_skills_for_prompt()` in `ingest.py` converts these to LLM-readable strings before sending), `anti_preferences`, `seniority`, `education` (array of structured objects with `degree_type` (e.g. `"B.S."`, `"M.S."`), `degree_field` (area of study), `school` (institution name), `graduation_year` (four-digit year string) — `format_education_for_prompt()` in `ingest.py` converts each object to a human-readable string before injection into the LLM scoring prompt so the model does not flag degree requirements as concerns when the candidate already satisfies them), `preferred_industries`, `scoring_notes`. Location is configured via a single nested `location` block: `location.center` (geocodable string, e.g. `"Miami, FL"`), `location.radius_km` (number — hard filter radius before LLM scoring), `location.geocode_fallback` (`"pass"` or `"discard"` — controls what happens when a listing location cannot be geocoded; default `"pass"`), `location.notes` (free-text injected into the LLM prompt; auto-generated from `center` + `radius_km` when absent). **Migration note:** the flat fields `location_preference`, `location_center`, `location_radius_km`, and `location_geocode_fallback` are no longer read; update any existing `profile.json` to use the nested `location` block.
- **`config/providers.json`** — Unified credential store for all sources, including Adzuna (`job_sources.adzuna.app_id` / `app_key`), Jooble, and USAJobs, as well as LLM providers (replaces `config/keys.json`). Managed via the `/settings` UI. Gitignored — copy from `config/providers.example.json` to get started.
- All files are gitignored. Copy from `*.example.json` to get started.
- Database connection is configured via the `DATABASE_URL` environment variable (PostgreSQL). Adzuna credentials can be overridden via env vars `ADZUNA_APP_ID` / `ADZUNA_APP_KEY`; at runtime these are injected into the providers dict so they flow to `AdzunaClient` via the same `credentials=` path as providers.json.

### Database schema notes

- Unique constraint is on `(source, source_id)` — one row per source/ID pair. The legacy `adzuna_id` column has been migrated to `source_id`; `db.init_db()` handles this migration on startup.
- `seen=1` means the listing has been scored; `seen=0` means score failed and it should be retried.
- Schema migration uses `ALTER TABLE ... ADD COLUMN` wrapped in try/except to handle existing databases gracefully.

## Deployment

**Docker (active deployment path):**
- Dev stack (port 5000): `docker compose -p job-matcher-pr-dev --env-file .env.dev -f docker-compose.dev.yml up -d --build` (use `--build` on first run if you lack GHCR access; subsequent runs can drop it)
- Prod stack (port 5001): `docker compose -p job-matcher-pr-prod --env-file .env.prod -f docker-compose.prod.yml up -d`
- Credentials: copy `.env.dev.example` → `.env.dev` and `.env.prod.example` → `.env.prod`
- Config/logs: dev uses `./config-dev` and `./logs-dev`; prod uses `./config` and `./logs`
- `scripts/docker-setup.sh` — one-time VM provisioning
- `scripts/docker-status.sh` / `scripts/docker-teardown.sh` — ops helpers
- `scripts/deploy-remote-linux.sh` — workstation-driven remote update. Pushes compose files, scripts, config examples, **and live `.env.prod` / `.env.dev`** (with overwrite confirmation + chmod 600). Run this after editing any `.env.*.example` schema to get the new required fields onto the server.
- **As of issue #373**, `deploy.yml` (all four deploy jobs) also syncs compose files, scripts, and `.env.*.example` to `/opt/job-matcher-pr/` on every deploy via a `Sync deploy files` step that runs before the Preflight check. `deploy-remote-linux.sh` is now only needed for (a) first-time server provisioning and (b) pushing updated live `.env.prod` / `.env.dev` secret values to the server.

**Log rotation:** all services use the `json-file` driver with `max-size: 10m` and `max-file: 3` (≤ 30 MB total per service), configured via a shared YAML anchor in each compose file.

**Env-file migration rule:** when `.env.prod.example` or `.env.dev.example` gains a new required field (a new `SECRET_KEY`-style variable, a renamed DB, etc.), the running server's live `.env.*` does **not** automatically pick it up. The `deploy-prod` GHA job now runs a preflight `docker compose config` + `changeme_*` grep against `/opt/job-matcher-pr/.env.prod` and will fail the run with `::error::` if the live file is missing, unedited, or still has unresolved compose variables — catch the drift at CI time instead of during a partial `up -d`.

**Password encoding:** if `POSTGRES_PASSWORD` contains URI-reserved characters (`@`, `:`, `/`, `#`, `?`), percent-encode them — e.g. `p@ss` → `p%40ss`. Docker Compose interpolates `POSTGRES_PASSWORD` directly into `DATABASE_URL`; `db.py` auto-encodes the password at startup as a safety net, but encoding it in the env file is the authoritative fix and ensures other tools (e.g. `psql`, `pg_dump`) also work correctly.

## UI Development

All UI work must follow `docs/STYLE_GUIDE.md`. Read it before touching any HTML or CSS.

- **Consult first** — the guide documents every CSS token, component class, typography rule, and state convention. Do not introduce new patterns without checking whether an existing one already covers the case.
- **Keep it current** — if a change introduces a new component, token, or convention, update `docs/STYLE_GUIDE.md` in the same PR. The guide is the source of truth, not `static/style.css`.
- **Never hard-code hex values** — always use a CSS custom property from `:root`.
- **Tier colors are semantic** — green (`--score-high-*`) = success/configured/matched; amber (`--score-mid-*`) = warning; red (`--score-low-*`) = error. Do not use tier colors for decorative purposes.

## Key design decisions

| Decision | Why |
|---|---|
| Pre-filter before LLM | Each filtered listing saves a Haiku API call (~$0.001); meaningful at 500 listings/run |
| Scrape full JD | Adzuna snippets (200–300 chars) are too short for accurate skill matching |
| PostgreSQL, no ORM | Schema is small and stable; `psycopg2` is the only driver dependency |
| HTMX, no JS framework | Zero build tooling for a read-mostly UI with two write actions |
| Decouple ingest from serve | Ingest takes minutes (scraping + LLM); it cannot run inside a web request |
| `config/profile.json` flat file | Edited manually as a whole unit; easier to version-control than a DB record |
| `config/providers.json` separate from `config/config.json` | API keys and source credentials change more often and are more sensitive than search params; separation allows tighter file ACLs on `config/providers.json` |
