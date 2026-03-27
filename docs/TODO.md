# Job Matcher — Implementation Plan

## Phase 1: Foundation

- [x] Create `requirements.txt` with `flask`, `requests`, `beautifulsoup4`, `anthropic`
- [x] Create `config.example.json` with all keys, placeholder values, and comments
- [x] Create `profile.json` with example skills profile structure
- [x] Create `.gitignore` (exclude `config.json`, `jobs.db`, `__pycache__`, `.env`)
- [x] Implement `db.py` — schema init, all query helpers (`init_db`, `listing_exists`, `insert_listing`, `update_score`, `get_feed`, `get_bookmarks`, `set_bookmarked`, `set_dismissed`)

## Phase 2: Ingestion Pipeline

- [x] Implement `AdzunaClient` in `ingest.py` — paginated fetch, respects `max_pages` config
- [x] Implement `prefilter()` — title include/exclude regex, salary floor, contract type/time
- [x] Implement `scrape_description()` — GET redirect_url, extract visible text via BS4, fallback to API snippet on failure
- [x] Implement `score_listing()` — call Claude Haiku, parse structured JSON response, retry once on failure
- [x] Wire up `run()` orchestrator in `ingest.py` — full pipeline with summary output
- [x] Add startup validation — raise clearly if config keys are missing

## Phase 3: Flask UI

- [x] Implement `app.py` — routes for `/`, `/bookmarks`, `/bookmark/<id>`, `/dismiss/<id>`
- [x] Create `templates/index.html` — header/nav, listing cards, score badge, skill tags
- [x] Create `templates/_card.html` — reusable card partial for HTMX swaps
- [x] Wire up HTMX bookmark toggle — `hx-post`, `hx-swap="outerHTML"` on action buttons
- [x] Wire up HTMX dismiss — `hx-post`, removes card from DOM on success
- [x] Create `static/style.css` — score badge colours, card layout, minimal polish
- [x] Add `get_listing_by_id()` to `db.py` for bookmark toggle read-modify-write
- [x] Create `templates/_actions.html` — action partial returned by POST /bookmark/<id>

## Phase 4: Polish & Documentation

- [x] Add logging throughout `ingest.py` (counts: fetched / pre-filtered / deduped / scraped / scored)
- [x] Handle `score = NULL` listings in UI gracefully (show "pending score" state)
- [x] Write `README.md` — setup steps, config instructions, how to run ingest + server, cron example
- [ ] Manual end-to-end test with real Adzuna API credentials

## Release Readiness

### Security
- [x] Verify `config.json` was never committed (`git log --all --full-history -- config.json`)
- [ ] Rotate Anthropic and Adzuna API keys before making repo public

### Missing files
- [x] Create `profile.example.json` with sanitised generic values (mirrors `config.example.json` pattern)
- [x] Add `profile.json` to `.gitignore` (contains personal career data)
- [x] Add `.vscode/` to `.gitignore` (machine-specific paths in `settings.json`)

### README updates
- [x] Add venv creation + activation step before `pip install`
- [x] Document `--config` and `--profile` CLI flags alongside `--rescore`
- [x] Add brief description of all four UI pages (`/`, `/bookmarks`, `/applied`, `/stats`)

### requirements.txt
- [x] Pin `pytest` to an exact version (currently unpinned while all other deps are pinned)

### Design doc updates
- [x] `DESIGN.md`: update config example block to include `distance` and `max_days_old`
- [x] `DESIGN.md`: update file map (section 8) to reflect actual template/static files
- [x] `DESIGN.md`: update route table (section 2.3) to include `/applied`, `/stats`, `POST /apply/<id>`

## Portfolio Hardening

### Critical fixes
- [x] `app.py`: wrap `float(min_score_raw)` in try/except — currently crashes 500 on bad input
- [x] `ingest.py`: remove duplicate pricing constants — import from `db.py` instead
- [x] `stats.html`: add missing "applied" nav tab (regression vs index.html)
- [x] `app.py`: make `debug=True` conditional on an env var, not hardcoded

### Code quality
- [x] `ingest.py`: move `anthropic.Anthropic()` client construction out of per-call loop — instantiate once in `run()` and `rescore()`, pass into `score_listing()`
- [x] `db.py`: add comment on ALTER TABLE migration approach acknowledging the limitation
- [x] `ingest.py`: add brief comment on scrape rate-limiting absence and why it's acceptable

### Tests
- [x] Add `tests/test_prefilter.py` — unit tests for `prefilter()` include/exclude/salary/contract logic
- [x] Add `tests/test_db.py` — tests for `init_db`, `insert_listing`, `get_feed` filters, `set_bookmarked`, `set_dismissed` using an in-memory or temp DB
- [x] Add `tests/test_ingest.py` — tests for `scrape_description` fallback logic, `score_listing` JSON fence stripping, `salary_fmt` filter
- [x] Add `pytest` to `requirements.txt`

### Documentation
- [x] `README.md`: add `--rescore` usage to the Running section
- [x] `README.md`: update summary line example to include token/cost fields

## Feature: Rescore Existing Listings

- [x] Add `get_all_scored(db_path)` to `db.py` — fetch all listings with `seen = 1`
- [x] Add `--rescore` flag to `ingest.py` entry point
- [x] Implement `rescore()` function — re-runs all scored listings through Haiku, updates scores in place
- [x] Print per-listing rescore log and final summary with token cost

## Feature: Job Type Tagging & Filter

- [x] Add `job_type` TEXT column to `listings` table via migration
- [x] Store `search.what` value as `job_type` on each listing at ingestion
- [x] Add `job_type` filter param to `db.get_feed()`
- [x] Add `db.get_job_types()` helper returning distinct job_type values present in DB
- [x] Add job type dropdown to feed filter bar (populated from DB)
- [x] Wire `job_type` query param through `app.py` feed route
- [x] Show job type badge on each card summary row

## Feature: Applied Tracking

- [x] Add `applied` column to `listings` table via `init_db()` migration
- [x] Add `set_applied()` and `get_applied()` helpers to `db.py`
- [x] Exclude applied listings from `get_feed()` by default
- [x] Add `POST /apply/<id>` route to `app.py`
- [x] Add `GET /applied` route and nav tab
- [x] Add apply button to `_actions.html` (distinct from bookmark/dismiss)
- [x] Create `templates/applied.html` (or reuse index.html with applied view)
- [x] CSS for applied button state and applied badge on cards

## Feature: Collapsible Cards + UI Filters

- [x] Make listing cards collapsible — collapsed state shows title, company, location, salary, remote/onsite badge, score
- [x] Add filter bar to feed: min score selector, remote-only toggle, title/company text search
- [x] Update `db.get_feed()` to accept optional filter params (min_score, remote_only, search query)
- [x] Wire filters through `app.py` feed route via query params
- [x] CSS for filter bar, collapsible details/summary, remote/onsite badge

## Feature: max_days_old Filter

- [x] Add `search.max_days_old` to `config.example.json`
- [x] Wire `max_days_old` param through `AdzunaClient.fetch_page()`
- [x] Update `config.json` with `max_days_old: 14`

## Feature: Search Distance Parameter

- [x] Add `search.distance` (km) to `config.example.json`
- [x] Wire `distance` param through `AdzunaClient.fetch_page()` when present
- [x] Update `config.json` to Coconut Creek, 32km (~20 miles)

## Feature: Pre-filter Rejection Reasons

- [x] Change `prefilter()` to return the rejection reason string instead of bare `False`
- [x] Log the specific reason for each filtered listing (title_exclude match, title_include miss, salary, contract type/time)

## Feature: Usage & Cost Tracking

- [x] Add `tokens_input` and `tokens_output` columns to `listings` table (migrate existing DB)
- [x] Capture token usage from Anthropic API response in `score_listing()`, return alongside score data
- [x] Store token counts per listing in DB via `insert_listing()` / `update_score()`
- [x] Add `get_usage_stats()` to `db.py` — total tokens, estimated cost, per-run breakdown
- [x] Print per-run cost estimate in ingest summary line
- [x] Add `/stats` route to `app.py` and `stats` nav tab showing cumulative usage and cost

## Native Deployment (#9)

### Web service (gunicorn via NSSM)
- [ ] Download and install NSSM on target server
- [x] Register `JobMatcher` service pointing to `gunicorn app:app`
- [x] Set `AppDirectory`, `AppEnvironmentExtra` (`DB_PATH`, `FLASK_DEBUG`)
- [ ] Verify service starts and survives reboot

### Scheduled ingest (Windows Task Scheduler)
- [x] Create `JobMatcherIngest` scheduled task running `python ingest.py --hours 25` daily
- [x] Set `DB_PATH`, `ANTHROPIC_API_KEY`, `ADZUNA_APP_ID`, `ADZUNA_APP_KEY` as system env vars
- [ ] Verify task runs and writes to the correct DB path

### Scripts
- [x] Create `scripts/setup.ps1` — interactive setup: prompts for API keys, sets system env vars, creates data dir, registers NSSM service, registers scheduled task
- [x] Create `scripts/teardown.ps1` — removes NSSM service and scheduled task cleanly
- [x] Create `scripts/status.ps1` — shows service status, last scheduled task run, DB row count, and last fetch time
- [x] Create `scripts/deploy-remote.ps1` — copies project files to a remote server and invokes setup.ps1 via PowerShell Remoting (WinRM)

### Documentation
- [x] Add "Native Deployment" section to `README.md` (NSSM setup, Task Scheduler, env vars, ops commands)

## Feature: Component Version Display (#6)

- [ ] Capture versions of key runtime components at startup (Python, Flask, anthropic, beautifulsoup4, gunicorn)
- [ ] Expose version data via stats page or footer
- [ ] Decide display location: stats page sidebar, footer, or dedicated info endpoint

## Feature: Last Fetch Time in UI (#7)

- [ ] Add `get_last_fetch_time()` helper to `db.py` — queries `MAX(fetched_at)` from listings
- [ ] Pass last fetch time to feed template context in `app.py`
- [ ] Display in feed header/filter bar (e.g. "Last updated 3 hours ago")

## Feature: Pluggable Model Provider (#8)

- [x] Define a common scorer interface/adapter shape `{score, matched_skills, missing_skills, concerns, verdict, tokens_input, tokens_output}`
- [x] Add `scoring.provider` key to `config.json` / `config.example.json` (e.g. `"provider": "anthropic"`)
- [x] Implement Anthropic adapter (refactor existing `score_listing()`)
- [x] Implement OpenAI adapter (GPT-4o-mini / GPT-4o)
- [x] Implement Gemini adapter (gemini-1.5-flash / gemini-1.5-pro)
- [x] Instantiate correct client in `run()` / `rescore()` based on config provider value
- [x] Add provider-specific API key fields to `config.example.json`

## Feature: Claude CI Auto-Diagnosis (#25)

- [ ] Add `ANTHROPIC_API_KEY` and `GH_PAT` to GitHub Actions secrets (manual step)
- [x] Create `.github/workflows/ci-failure.yml` — triggers on CI workflow_run failure, fetches logs, calls Claude, posts PR comment
- [x] Create `.github/workflows/apply-fix.yml` — manual workflow_dispatch trigger to apply suggested fix to PR branch
- [x] Document secrets and new workflows in `README.md`

## Milestone: Dynamic Provider Key Management (#28–#36)

- [x] **#28** — Create `keys.example.json`; strip API key fields from `config.example.json`; add `keys.json` to `.gitignore`
- [x] **#29** — Add `build_provider_chain()` to `providers/__init__.py`; add `TestBuildProviderChain` tests
- [x] **#30** — Add `model_used TEXT` column to `listings` table via migration in `db.py`
- [x] **#31** — Add `load_keys()` to `ingest.py` with env-var fallback; update `load_config()` to use it
- [x] **#32** — Wire provider chain into `run()`/`rescore()`; per-listing fallback loop; cost breakdown by provider
- [x] **#33** — Flask `/settings` GET+POST routes; `settings.html` template (keys-only, masked display)
- [x] **#34** — `model_used` badge on listing cards; add settings nav tab to all templates
- [x] **#35** — Update `setup.ps1` (key file ACLs, remove LLM key prompts); deployment docs
- [x] **#36** — Final `CLAUDE.md` and `README.md` documentation pass
