# Refactor: Split `app.py` into `services/` + `web/`

**Status**: refreshed plan, superseding the original write-up in GitHub Issue #261.
**Source of truth**: this document — Issue #261 body predates recent churn.
**Scope**: no behavioural change. Pure relocation + blueprint registration.
**Line numbers pinned to**: `app.py` at commit `9c38c68` (3116 lines). If `app.py` has changed since, re-verify with `git blame` before starting a phase.

---

## Refresh delta (what changed since Issue #261 was written)

The original plan is structurally correct. The items below are net-new or changed:

1. **`app.py` grew by ~37 lines** (3079 → 3116). Net additions are in the security/CSRF area (PRs #259, #284, #286, #287) and are the *reason* Phase 1 is still worth doing — not a blocker.
2. **Templates contain exactly one `url_for()` call** across the entire `templates/` tree — `templates/index.html:170` referencing `url_for('static', ...)`. The original plan flagged Phase 5's `url_for` rewrite as highest-risk because blueprint-qualified endpoints (`feed.feed` vs `feed`) would break every template. **This risk is now effectively zero.** The real Phase 5 risk has shifted to hard-coded path strings in `hx-get` / `hx-post` / `action=` / `href=` attributes — there are ~80 of those across 8 templates, and they are **insensitive** to blueprint renaming (they reference URLs, not endpoint names). This is a net de-risk of Phase 5.
3. **One hard constraint needs to be re-stated explicitly**: the module-level names `app`, `_is_trusted_host`, `salary_fmt`, `timeago`, `_parse_ingest_summary`, `_stdout_reader`, `_build_llm_schemas`, `_config_warnings`, and the `_ingest_*` / `_pdf_*` globals are **all imported directly by the test suite** (`from app import app as flask_app`, `from app import _build_llm_schemas`, etc.). Any refactor that removes these from `app.py`'s public namespace will break tests unless `app.py` re-exports them. Call this the "test import contract" — it's not optional, and the original plan under-weighted it.
4. **Two new concerns** belong in Phase 1 that the original plan did not name: the prod-env `changeme_*` DATABASE_URL guard (lines 75–83) and the `SECRET_KEY` refusal-to-start guard (lines 52–59). Both were added in the CSRF/security hardening stream. They are startup-time guards, not request-time, and belong in `web/__init__.py`'s `create_app()` rather than `web/security.py`.
5. **Inter-phase coupling found**: `_load_providers_safe()` and `_build_llm_schemas()` are used by *both* the `/settings` routes (Phase 5 → `web/settings.py`) *and* the `/api/providers/reorder` route (Phase 5 → `web/ingest.py` or a new `web/api.py`). They must land in `services/provider_schemas.py` (Phase 4) **before** Phase 5 begins, otherwise Phase 5 would introduce a circular import between blueprints. The original plan got this ordering right — flagging it so the dependency is explicit.

---

## Target structure (unchanged from original)

```
services/                    # pure Python — zero Flask imports
  pdf_import.py
  profile_store.py
  provider_schemas.py
  ingest_control.py
web/                         # thin Flask glue
  __init__.py                # create_app() + startup guards
  filters.py                 # salary_fmt, parse_iso, timeago
  security.py                # _is_trusted_host, _is_localhost_request, csrf_localhost_guard, inject_demo_mode
  feed.py                    # feed_bp
  profile.py                 # profile_bp
  settings.py                # settings_bp
  ingest.py                  # ingest_bp
  admin.py                   # admin_bp
app.py                       # ~30-line entry point; re-exports test-import contract
```

---

## Hard constraints (do not relax)

- No endpoint URL changes.
- No session/CSRF behaviour change.
- No subprocess-architecture change (`_ingest_lock`, `_ingest_process`, `_ingest_log_file`, `_last_run`, `_ingest_just_completed`, `MAX_SSE_CONNECTIONS` remain module-level, just relocated into `services/ingest_control.py`).
- Zero `from flask import` in any `services/*.py`.
- `db.py` not touched.
- **Test import contract**: `app.py` must continue to re-export every name currently imported by `tests/` — see inventory below. Enforced by running `pytest` after every phase.
- One phase = one PR.

---

## Current `app.py` inventory (3116 lines)

### Module-level imports (lines 8–38)

Stdlib: `ipaddress`, `json`, `os`, `re`, `secrets`, `subprocess`, `sys`, `threading`, `time as _time`, `concurrent.futures.ThreadPoolExecutor`, `datetime`, `importlib.metadata`, `io.BytesIO`.
Third-party: `dotenv`, `flask` (Flask, render_template, make_response, request, jsonify, redirect, url_for, Response, session, stream_with_context, send_from_directory, abort), `pypdf`.
Internal: `db`, `credentials`, `paths`, `ingest_events`, `providers`, `providers.anthropic_provider.strip_fences`, `providers.base._sanitise_detail`, `job_sources.get_sources`, `ingest.validate_search_config`, `ingest.ValidationIssue`.
Late import (line 275): `job_sources.auto_register.ensure_plugins_registered`.

### Module-level globals (startup order-sensitive)

| Line | Name | Phase owner |
|---|---|---|
| 45 | `load_dotenv(override=False)` | `web/__init__.py` (create_app prologue) |
| 47 | `app = Flask(__name__)` | `web/__init__.py` |
| 52–59 | `SECRET_KEY` guard | `web/__init__.py` |
| 75–83 | prod `changeme_*` DATABASE_URL guard | `web/__init__.py` |
| 86–87 | `jinja_env.globals` for APP_ENV / APP_VERSION | `web/__init__.py` |
| 88 | `DEMO_MODE` | `web/__init__.py` (module-level; set by entry point) |
| 171–175 | `_CONFIG_DIR`, `_KEYS_PATH`, `_CONFIG_PATH`, `_PROFILE_PATH`, `_PROVIDERS_PATH` | `services/profile_store.py` |
| 178–185 | `_KEYS_DEFAULTS` | `services/profile_store.py` |
| 272 | `CONFIG = load_config()` | `web/__init__.py` (after services imported) |
| 273 | `db.init_db()` | `web/__init__.py` |
| 276 | `ensure_plugins_registered(...)` | `web/__init__.py` |
| 362 | `RUNTIME_VERSIONS` | `services/provider_schemas.py` or new `services/runtime_info.py` |
| 820–842 | `_ingest_lock`, `_ingest_process`, `_ingest_log_file`, `_last_run`, `_ingest_just_completed`, `MAX_SSE_CONNECTIONS` | `services/ingest_control.py` |
| 847–853 | `_INGEST_SUMMARY_RE` | `services/ingest_control.py` |
| 1586–1671 | `_IMPORT_PROMPT_FRESH`, `_IMPORT_PROMPT_PREFILTER_EXTENSION`, `_MAX_PATTERN_LEN`, `_MAX_PATTERNS_PER_LIST` | `services/pdf_import.py` |
| 1770–1777 | `_DEGREE_PREFIX_RE`, `_YEAR_RE` | `services/pdf_import.py` |
| 2017–2036 | `_PDF_ASYNC_THRESHOLD`, `_pdf_jobs`, `_pdf_jobs_lock`, `_pdf_executor`, `_MAX_CONCURRENT_PDF_JOBS`, `_PDF_JOB_TTL_SECONDS`, `_PDF_JOB_TIMEOUT_SECONDS`, `_last_prune_time`, `_PRUNE_INTERVAL_SECONDS` | `services/pdf_import.py` |
| 2576 | `_LOG_FILENAME_RE` | `web/admin.py` |
| 2579–2580 | `SCHEDULE_WARN_HOURS`, `SCHEDULE_CRITICAL_HOURS` | `web/admin.py` |
| 2786 | `_VALIDATE_TIMEOUT_SECONDS` | `services/provider_schemas.py` |

### Routes (28 total)

| Line | Method | Path | Handler | Phase |
|---|---|---|---|---|
| 538 | GET | `/` | `feed` | 5 (feed_bp) |
| 590 | GET | `/feed/fragment` | `feed_fragment` | 5 (feed_bp) |
| 639 | GET | `/bookmarks` | `bookmarks` | 5 (feed_bp) |
| 651 | POST | `/bookmark/<int:listing_id>` | `toggle_bookmark` | 5 (feed_bp) |
| 665 | POST | `/apply/<int:listing_id>` | `toggle_apply` | 5 (feed_bp) |
| 679 | GET | `/applied` | `applied` | 5 (feed_bp) |
| 691 | GET | `/snippets` | `snippets` | 5 (feed_bp) |
| 737 | GET | `/stats` | `stats` | 5 (feed_bp) |
| 749 | POST | `/dismiss/<int:listing_id>` | `dismiss` | 5 (feed_bp) |
| 761 | POST | `/listings/<int:listing_id>/open` | `mark_listing_opened` | 5 (feed_bp) |
| 981 | POST | `/ingest/trigger` | `ingest_trigger` | 5 (ingest_bp) |
| 1049 | GET | `/api/ingest/preflight` | `ingest_preflight` | 5 (ingest_bp) |
| 1082 | GET | `/ingest/status` | `ingest_status` | 5 (ingest_bp) |
| 1106 | GET | `/ingest/stream` | `ingest_stream` | 5 (ingest_bp) |
| 1250 | GET/POST | `/settings` | `settings` | 5 (settings_bp) |
| 2183 | POST | `/profile/import-pdf` | `profile_import_pdf` | 5 (profile_bp) |
| 2333 | GET | `/profile/import-pdf/status/<job_id>` | `profile_import_pdf_status` | 5 (profile_bp) |
| 2364 | GET/POST | `/profile` | `profile` | 5 (profile_bp) |
| 2567 | GET | `/settings/config` | `settings_config_redirect` | 5 (settings_bp) |
| 2583 | GET | `/admin` | `admin` | 5 (admin_bp) |
| 2597 | POST | `/admin/clear-db` | `admin_clear_db` | 5 (admin_bp) |
| 2667 | GET | `/admin/logs` | `admin_logs` | 5 (admin_bp) |
| 2699 | GET | `/admin/logs/<filename>/download` | `admin_log_download` | 5 (admin_bp) |
| 2727 | GET | `/admin/schedule-state` | `admin_schedule_state` | 5 (admin_bp) |
| 2824 | POST | `/api/apply-prefilter-suggestions` | `apply_prefilter_suggestions` | 5 (profile_bp) |
| 2925 | POST | `/api/validate-keys` | `validate_keys` | 5 (settings_bp) |
| 2967 | POST | `/api/providers/reorder` | `api_providers_reorder` | 5 (settings_bp) |
| 3013 | POST | `/api/job-sources/<source_key>/toggle` | `api_job_source_toggle` | 5 (settings_bp) |

### Flask lifecycle hooks & filters

| Line | Type | Name | Phase |
|---|---|---|---|
| 150 | `@app.context_processor` | `inject_demo_mode` | 1 (web/security.py) |
| 156 | `@app.before_request` | `csrf_localhost_guard` | 1 (web/security.py) |
| 444 | `@app.template_filter("salary_fmt")` | `salary_fmt` | 1 (web/filters.py) |
| 474 | `@app.template_filter("parse_iso")` | `parse_iso` | 1 (web/filters.py) |
| 489 | `@app.template_filter("timeago")` | `timeago` | 1 (web/filters.py) |

### Module-level helpers

| Line | Name | Phase |
|---|---|---|
| 96 | `_is_trusted_host` | 1 (web/security.py) — **imported by `tests/test_security.py`** |
| 115 | `_is_localhost_request` | 1 (web/security.py) |
| 192 | `load_config` | 2 (services/profile_store.py) |
| 216 | `_write_json_atomic` | 2 (services/profile_store.py) |
| 242 | `load_profile` | 2 (services/profile_store.py) |
| 284 | `_validate_profile_form` | 2 (services/profile_store.py) |
| 314 | `get_runtime_versions` | 4 (services/provider_schemas.py or runtime_info.py) |
| 369 | `_config_warnings` | 4 (services/provider_schemas.py) — **imported by `tests/test_credential_source_bugs.py`** |
| 409 | `_get_search_validation_issues` | 4 (services/provider_schemas.py) |
| 782 | `_mask_config_keys` | 4 (services/provider_schemas.py) |
| 856 | `_parse_ingest_summary` | 4 (services/ingest_control.py) — **imported by tests** |
| 882 | `_stdout_reader` | 4 (services/ingest_control.py) — **imported by tests** |
| 936 | `_ingest_running` | 4 (services/ingest_control.py) |
| 971 | `_render_ingest_idle` | 5 (web/ingest.py) — renders template |
| 976 | `_render_ingest_running` | 5 (web/ingest.py) — renders template |
| 1158 | `_build_llm_schemas` | 4 (services/provider_schemas.py) — **imported by tests** |
| 1223 | `_load_providers_safe` | 4 (services/provider_schemas.py) |
| 1502 | `_parse_education_rows` | 2 (services/profile_store.py) |
| 1544 | `_parse_repeating_rows` | 2 (services/profile_store.py) |
| 1567 | `_extract_pdf_text` | 3 (services/pdf_import.py) |
| 1626 | `_build_import_prompt` | 3 (services/pdf_import.py) |
| 1674 | `_parse_import_response` | 3 (services/pdf_import.py) |
| 1780 | `_normalise_education` | 3 (services/pdf_import.py) |
| 1855 | `_merge_import_result` | 3 (services/pdf_import.py) |
| 1957 | `_merge_prefilter_suggestions` | 3 (services/pdf_import.py) |
| 2039 | `_prune_pdf_jobs` | 3 (services/pdf_import.py) |
| 2075 | `_run_pdf_import_job` | 3 (services/pdf_import.py) |
| 2790 | `_validate_with_timeout` | 4 (services/provider_schemas.py) |

### Test import contract

The following `app`-module names are imported directly by `tests/` and must remain importable from `app.py` after the refactor (either by defining them there or by `from web.x import y as y`):

- `app` (the Flask instance) — imported by ~30 test files
- `_is_trusted_host`, `_config_warnings`, `_build_llm_schemas`, `_parse_ingest_summary`, `_stdout_reader`, `salary_fmt`, `timeago`

Every phase's PR must end with a green `pytest` run.

---

## Phase 0 — Scaffold packages (no behaviour change)

**Goal**: create `services/` + `web/` with empty modules; wire `web/__init__.py::create_app()` to simply return the existing `app` object.
**Files changed**: `services/__init__.py` (new, empty), `web/__init__.py` (new, re-exports `app`), `app.py` (unchanged — still contains everything).
**Exit criteria**:
- [ ] `pytest` fully green; `from web import create_app` returns the same Flask instance as `from app import app`.
- [ ] Verified line numbers in `docs/refactor-split-app-plan.md` match the `app.py` at the pinned SHA (or updated the plan).

## Phase 1 — Filters + security/CSRF + startup guards

**Goal**: move all Jinja filters and host/CSRF guards into `web/filters.py` + `web/security.py`; move `SECRET_KEY` + prod `changeme` guards into `web/__init__.py::create_app()`.

**Inventory (refreshed)**:
- `web/filters.py` ← `salary_fmt` (444), `parse_iso` (474), `timeago` (489)
- `web/security.py` ← `_is_trusted_host` (96), `_is_localhost_request` (115), `inject_demo_mode` (150), `csrf_localhost_guard` (156)
- `web/__init__.py` ← `SECRET_KEY` guard (52–59), prod `changeme_*` guard (75–83), `load_dotenv` (45), `app = Flask(...)` (47), jinja globals (86–87), `db.init_db()` (273), `ensure_plugins_registered(...)` (276)

**Re-export from `app.py`**: `app`, `_is_trusted_host`, `salary_fmt`, `timeago`.

**Test coverage**: `tests/test_security.py` directly imports `_is_trusted_host`. `tests/test_ingest.py` imports `salary_fmt` and `timeago`. Both must pass unchanged.

**Risk**: Low. Startup guards are invoked at import time — an incorrect move will surface the moment `pytest` imports `app`.

## Phase 2 — `services/profile_store.py`

**Goal**: move all JSON-on-disk config/profile I/O and form parsing helpers out of `app.py`.

**Inventory (refreshed)**:
- Path constants (171–175): `_CONFIG_DIR`, `_KEYS_PATH`, `_CONFIG_PATH`, `_PROFILE_PATH`, `_PROVIDERS_PATH`
- `_KEYS_DEFAULTS` (178–185)
- `load_config` (192)
- `_write_json_atomic` (216)
- `load_profile` (242)
- `_validate_profile_form` (284)
- `_parse_education_rows` (1502)
- `_parse_repeating_rows` (1544)

**Coupling**: `load_profile` and `_PROFILE_PATH` are used by `services/pdf_import.py` (Phase 3). Phase 2 must land first.

**Test coverage**: covered by `tests/test_profile.py`, `tests/test_settings_save.py`, `tests/test_validate_search_config.py` — all via the `/profile` and `/settings` routes, not direct imports. Low-risk relocation.

**Risk**: Low — pure functions, no Flask dependency.

## Phase 3 — `services/pdf_import.py`

**Goal**: extract all PDF import logic (sync + async worker).

**Inventory (refreshed)**:
- `_IMPORT_PROMPT_FRESH` (1586), `_IMPORT_PROMPT_PREFILTER_EXTENSION` (1607)
- `_MAX_PATTERN_LEN` (1667), `_MAX_PATTERNS_PER_LIST` (1671)
- `_DEGREE_PREFIX_RE` (1770), `_YEAR_RE` (1777)
- `_extract_pdf_text` (1567)
- `_build_import_prompt` (1626)
- `_parse_import_response` (1674)
- `_normalise_education` (1780)
- `_merge_import_result` (1855)
- `_merge_prefilter_suggestions` (1957)
- `_PDF_ASYNC_THRESHOLD` (2017), `_pdf_jobs` (2022), `_pdf_jobs_lock` (2023), `_pdf_executor` (2026), `_MAX_CONCURRENT_PDF_JOBS` (2027), `_PDF_JOB_TTL_SECONDS` (2030), `_PDF_JOB_TIMEOUT_SECONDS` (2032), `_last_prune_time` (2035), `_PRUNE_INTERVAL_SECONDS` (2036)
- `_prune_pdf_jobs` (2039)
- `_run_pdf_import_job` (2075)

**Coupling**: `_run_pdf_import_job` calls `load_profile` (Phase 2) and `build_provider_chain` (external). Depends on Phase 2.

**Test coverage**: `tests/test_pdf_async.py`, `tests/test_profile_import.py`. Both exercise via HTTP.

**Risk**: Medium. The `ThreadPoolExecutor` is a module-level singleton — moving it changes process-level resource ownership. Ensure the executor is created exactly once at import time and not per-request. Uses `app.logger` in four `warning()` / `error()` calls (1703, 1726, 1738, 1749) — replace with stdlib `logging.getLogger(__name__)` since services cannot import Flask.

## Phase 4 — `services/provider_schemas.py` + `services/ingest_control.py`

**Goal**: extract provider schema building, config warnings, runtime versions, key validation, and ingest subprocess control.

**Status**: Landed in PR #325, then fully consolidated in Issue #326 (PR on branch `refactor/split-app-phase-4-consolidation`).

**Inventory (refreshed)** — `services/provider_schemas.py`:
- `get_runtime_versions` (314) + `RUNTIME_VERSIONS` (362) — *or* put in a new `services/runtime_info.py` if we want to keep it single-responsibility
- `_config_warnings` (369)
- `_get_search_validation_issues` (409)
- `_mask_config_keys` (782)
- `_build_llm_schemas` (1158)
- `_load_providers_safe` (1223)
- `_validate_with_timeout` (2790) + `_VALIDATE_TIMEOUT_SECONDS` (2786)

**Inventory (refreshed)** — `services/ingest_control.py`:
- `_ingest_lock` (820), `_ingest_process` (823), `_ingest_log_file` (827), `_last_run` (830), `_ingest_just_completed` (836), `MAX_SSE_CONNECTIONS` (842)
- `_INGEST_SUMMARY_RE` (847)
- `_parse_ingest_summary` (856)
- `_stdout_reader` (882) — uses `app.logger`; switch to `logging.getLogger(__name__)`
- `_ingest_running` (936)

**Coupling**: `_build_llm_schemas` and `_load_providers_safe` are consumed by the Phase 5 settings blueprint and by `/api/providers/reorder`. They must be in a service module before Phase 5 to avoid a blueprint-to-blueprint import.

**Re-export from `app.py`**: `_build_llm_schemas`, `_parse_ingest_summary`, `_stdout_reader`, `_config_warnings` — all have direct test imports.

**Test coverage**: `tests/test_ingest_trigger.py`, `tests/test_ingest_stream.py`, `tests/test_ingest_integration.py`, `tests/test_settings_save.py`, `tests/test_credential_source_bugs.py`, `tests/test_reorder.py`. High coverage; this is the phase most likely to surface a regression early.

**Risk**: Medium. The `_ingest_*` globals are shared mutable state touched from (a) the request thread that spawns the subprocess, (b) the `_stdout_reader` daemon thread, and (c) the request thread that polls `/ingest/status`. Moving the globals into a new module changes their identity — any code that imports `from app import _ingest_process` will see `None` forever. The fix is to re-export via `app.py` *and* to have `web/ingest.py` access these via `from services import ingest_control; ingest_control._ingest_process` (never a bare `from services.ingest_control import _ingest_process`, which captures the value at import time). Verify with `tests/test_ingest_trigger.py` which monkeypatches `app._ingest_process`.

### Phase 4 deviation log (Issue #326 consolidation)

**PR #325 (original Phase 4)** landed with a dual-copy architecture instead of pure extraction. Three tiers of duplication were introduced:

- **Tier 3 (worst)**: Full function definitions in BOTH `app.py` AND `services/*` for `_validate_with_timeout`, `_stdout_reader`, `_ingest_running`. Tests exercised the `app.py` copies via monkeypatch; runtime would exercise the `services/*` copies — creating silent regression risk.
- **Tier 2**: `app.py` wrappers for `_config_warnings`, `_get_search_validation_issues`, `_load_providers_safe` that delegated to services with path arg injection.
- **Tier 1 (fine)**: Clean re-exports via `__all__` for `_parse_ingest_summary` and the path constants.

**Issue #326 (this consolidation)** closed the gap:

- Tier-3 function definitions removed from `app.py`. `app._validate_with_timeout`, `app._stdout_reader`, `app._ingest_running` are now aliases to the service implementations.
- Tier-2 wrappers removed. Route handlers call `_config_warnings(providers_path=_PROVIDERS_PATH)` etc. directly; the module-level path name is read at call time so test monkeypatches on `app._PROVIDERS_PATH` continue to work.
- Duplicate ingest globals removed from `app.py`. `app._ingest_process` etc. are now aliases to `ingest_control.*` at import time. Route handlers (`ingest_trigger`, `ingest_status`) access state exclusively via `ingest_control.*` to avoid the Python rebinding hazard.
- Tests updated: `test_ingest_trigger.py` patches `ingest_control.*` instead of `app_module.*` for ingest globals. `test_settings.py` patches `services.provider_schemas._VALIDATE_TIMEOUT_SECONDS`. `test_ingest_stream.py` patches `ingest_events.IngestEventParser` (the local import source in `ingest_control._stdout_reader`).

**`services/` is now the single source of truth** for all Phase 4 extractions. Phase 5 blueprints can safely import from `services.*` without the silent-regression risk that dual-copy created.

## Phase 5 — Split routes into blueprints

**Goal**: move 27 route handlers + 2 helper renderers into 5 blueprints.

**Inventory (refreshed)** — per-blueprint handler lists already tabulated in the "Routes" section above. Summary:
- `web/feed.py` (feed_bp) — 10 routes (`/`, `/feed/fragment`, `/bookmarks`, `/bookmark/<id>`, `/apply/<id>`, `/applied`, `/snippets`, `/stats`, `/dismiss/<id>`, `/listings/<id>/open`)
- `web/ingest.py` (ingest_bp) — 4 routes + `_render_ingest_idle` + `_render_ingest_running`
- `web/settings.py` (settings_bp) — 5 routes (`/settings`, `/settings/config`, `/api/validate-keys`, `/api/providers/reorder`, `/api/job-sources/<key>/toggle`)
- `web/profile.py` (profile_bp) — 4 routes (`/profile`, `/profile/import-pdf`, `/profile/import-pdf/status/<job_id>`, `/api/apply-prefilter-suggestions`)
- `web/admin.py` (admin_bp) — 5 routes (`/admin`, `/admin/clear-db`, `/admin/logs`, `/admin/logs/<file>/download`, `/admin/schedule-state`) + `_LOG_FILENAME_RE` + `SCHEDULE_*` constants

**Template sweep (refreshed)**:
- `url_for()` calls in templates: **1** (only `url_for('static', ...)` in `index.html:170`). Blueprint-qualified endpoint names are not needed anywhere. Original plan's biggest-flag risk is neutralised.
- Hard-coded paths in `hx-get` / `hx-post` / `action=` / `href=`: ~80 across `index.html`, `stats.html`, `snippets.html`, `admin.html`, `profile.html`, `settings.html`, `admin/_log_list.html`, `admin/_schedule_state.html`, `_ingest_trigger.html`, `_card.html`, `_actions.html`. These reference URLs, not endpoint names — **they remain valid unchanged** because no URL is being altered. No template edits required for the route split itself.
- Inline JS (`static/*.js`, `ingest-drawer.js`) referencing paths: also unaffected.

**Endpoint naming**: use `url_prefix=""` on all blueprints (no prefix change), and name blueprint functions identically to today so `url_for("feed")` / `url_for("settings")` / etc. still resolve. If we keep flat endpoint names via `endpoint=` overrides we avoid even the one-line change in `settings_config_redirect` (which calls `url_for("profile")`).

**Test coverage**: every route is hit by at least one test (see test file names). This phase has the highest test coverage of any phase and the lowest template risk.

**Risk**: Low-medium. The residual risk is **not** `url_for` but rather (a) `endpoint=` naming mismatches causing `settings_config_redirect`'s `url_for("profile")` to break, and (b) `session["csrf_token"]` being set in `/profile` (2554) and `/admin` (2586) — both must stay on the same session object when split across blueprints (Flask handles this correctly as long as `app.secret_key` is unchanged, which it will be).

## Phase 6 — Slim `app.py` + docs

**Goal**: reduce `app.py` to ~30 lines — re-exports for the test import contract, `create_app()` call, and the `if __name__ == "__main__"` block for demo mode + `app.run()`.

**Inventory (refreshed)**:
- `if __name__ == "__main__"` block (3092–3116) — argparse for `--demo`, `DEMO_MODE` assignment, `_PROFILE_PATH` / `_PROVIDERS_PATH` demo overrides, `app.run(debug=..., port=5000, threaded=True)`
- Re-exports for tests:
  ```python
  from web import create_app
  from web.security import _is_trusted_host
  from web.filters import salary_fmt, timeago
  from services.provider_schemas import _build_llm_schemas, _config_warnings
  from services.ingest_control import _parse_ingest_summary, _stdout_reader
  app = create_app()
  ```
- Update `README.md` + `CLAUDE.md` architecture section.

**Risk**: Low. Last phase; almost entirely documentation. The one gotcha is the `--demo` path — it mutates the module-level `_PROFILE_PATH` / `_PROVIDERS_PATH`, which after the split live in `services.profile_store`. The demo override must assign into `services.profile_store._PROFILE_PATH` (mutate the module attribute) rather than rebinding a local name.

```python
# WRONG — only rebinds the local name
from services.profile_store import _PROFILE_PATH
_PROFILE_PATH = demo_path  # does NOT affect services.profile_store._PROFILE_PATH

# CORRECT — mutates the module attribute
from services import profile_store
profile_store._PROFILE_PATH = demo_path  # actually changes the module-level value
```

---

## Under-weighted risk the original plan missed

**Module-level mutable state and Python rebinding semantics.** The biggest single hazard is the `_ingest_*` family of globals (Phase 4) and the `--demo` path mutation in `app.py`'s `__main__` block (Phase 6). In the current layout, code like `_ingest_process = proc` inside `ingest_trigger()` mutates a module-level name that other functions in the same module read directly. After the split, if `web/ingest.py` does `from services.ingest_control import _ingest_process, _ingest_lock` at the top, then mutates `_ingest_process = proc` inside a handler, it will only rebind `web.ingest._ingest_process` — `services.ingest_control._ingest_process` stays `None` forever, and `_ingest_running()` (living in `services/`) will never see a running process. Every touchpoint must go through the module object (`ingest_control._ingest_process = proc`), never through a direct name import. This applies identically to `_pdf_jobs`, `_last_prune_time`, and the `--demo` path-rebinding. **Flag this explicitly in the Phase 4 and Phase 6 PR descriptions and add a grep-based check in the PR author's self-review: `rg "from services\.(ingest_control|pdf_import|profile_store) import _"` must return zero mutable-state names.**
