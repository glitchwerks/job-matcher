"""
app.py — Flask web server for Job Matcher.

Thin routing layer only. All data access goes through db.py.
Business logic lives in ingest.py; none of it belongs here.

Flask app construction, startup guards, filter registration, and
CSRF wiring have moved to ``web/__init__.py::create_app()``.  This
module now calls ``create_app()`` at import time and re-exports the
names that the test suite imports directly from ``app``.

Phase 4 extraction note
-----------------------
``services/provider_schemas.py`` owns: ``get_runtime_versions``,
``RUNTIME_VERSIONS``, ``_config_warnings``, ``_get_search_validation_issues``,
``_mask_config_keys``, ``_build_llm_schemas``, ``_load_providers_safe``,
``_validate_with_timeout`` + ``_VALIDATE_TIMEOUT_SECONDS``.

``services/ingest_control.py`` owns: the canonical copies of the ingest
subprocess state globals and helpers for Phase 5 blueprint use.

This module keeps its own module-level copies of the ingest globals
(``_ingest_process``, ``_ingest_lock``, etc.) and the ``_ingest_running``
function so that the test suite's ``monkeypatch.setattr(app_module, ...)``
patches continue to work without modification.  Phase 5 will migrate the
route handlers to read from ``services.ingest_control`` directly.

Re-exports in this module (test suite imports these from ``app``):
    ``_build_llm_schemas``, ``_parse_ingest_summary``, ``_stdout_reader``,
    ``_config_warnings``.
"""

import os

import db
from providers import build_provider_chain, generate_with_fallback  # noqa: F401

# ---------------------------------------------------------------------------
# Phase 4 service imports (Issue #326 consolidation)
# ---------------------------------------------------------------------------
# All provider-schema helpers live in services/provider_schemas.py and are
# imported here so that existing test call-sites importing from ``app`` continue
# to work.  Tier-2 wrapper functions (_config_warnings, _get_search_validation_issues,
# _load_providers_safe) have been collapsed: the service functions accept explicit
# path arguments and the route handlers pass the current module-level path globals
# at call time, so monkeypatch.setattr(app_module, "_PROVIDERS_PATH", ...) in the
# test suite still takes effect.
from services.provider_schemas import (  # noqa: E402
    _VALIDATE_TIMEOUT_SECONDS,
    _build_llm_schemas,
    _config_warnings,
    _get_search_validation_issues,
    _load_providers_safe,
    _mask_config_keys,
    _validate_with_timeout,
    get_runtime_versions,
    RUNTIME_VERSIONS,
)

# Re-export pdf_import helpers so test_profile_import.py can access them
# via ``app_module._extract_pdf_text``, etc., and monkeypatch them via
# ``patch("app._extract_pdf_text", ...)``.  The routes themselves now live
# in web/profile.py which reads these from services.pdf_import directly;
# tests patching "app.X" do NOT affect web/profile.py and must be migrated
# to "web.profile.X" targets (see Phase 5b test migration notes).
from services.pdf_import import (  # noqa: E402, F401
    _DEGREE_PREFIX_RE,
    _IMPORT_PROMPT_FRESH,
    _IMPORT_PROMPT_PREFILTER_EXTENSION,
    _MAX_CONCURRENT_PDF_JOBS,
    _MAX_PATTERN_LEN,
    _MAX_PATTERNS_PER_LIST,
    _PDF_ASYNC_THRESHOLD,
    _PDF_JOB_TIMEOUT_SECONDS,
    _PDF_JOB_TTL_SECONDS,
    _PRUNE_INTERVAL_SECONDS,
    _YEAR_RE,
    _build_import_prompt,
    _extract_pdf_text,
    _last_prune_time,
    _merge_import_result,
    _merge_prefilter_suggestions,
    _normalise_education,
    _parse_import_response,
    _pdf_executor,
    _pdf_jobs,
    _pdf_jobs_lock,
    _prune_pdf_jobs,
    _run_pdf_import_job,
)

# ingest_control — subprocess lifecycle, SSE state, stdout reader.
# Imported as a module so route handlers can mutate ingest_control._ingest_process
# without the Python rebinding hazard (``ingest_control._ingest_process = proc``
# mutates the module attribute; a bare ``from … import _ingest_process`` would
# only rebind a local name).
from services import ingest_control  # noqa: E402

# Pure-function and constant re-exports — aliased here so the test import
# contract (``from app import _parse_ingest_summary``, etc.) is preserved.
_parse_ingest_summary = ingest_control._parse_ingest_summary
_stdout_reader = ingest_control._stdout_reader

# Mutable globals — re-exported as module-level names so test fixtures using
# ``monkeypatch.setattr(app_module, "_ingest_process", ...)`` still find the
# attributes.  Route handlers must access these via ``ingest_control.*`` so
# mutations actually reach the module that owns the state.
_ingest_lock = ingest_control._ingest_lock
_ingest_process = ingest_control._ingest_process          # None at import
_ingest_log_file = ingest_control._ingest_log_file        # None at import
_last_run = ingest_control._last_run                      # None at import
_ingest_just_completed = ingest_control._ingest_just_completed  # False at import
MAX_SSE_CONNECTIONS = ingest_control.MAX_SSE_CONNECTIONS  # 2 at import

# _ingest_running alias — calling app_module._ingest_running() delegates to
# ingest_control._ingest_running() which reads the authoritative
# ingest_control.* globals.
_ingest_running = ingest_control._ingest_running

# ---------------------------------------------------------------------------
# Public re-export declarations
# ---------------------------------------------------------------------------
# Names listed here are intentionally imported and re-exported from this
# module so that existing call sites and test fixtures that import or patch
# them on ``app_module`` continue to work without modification.  Listing
# them in ``__all__`` suppresses ruff F401 "imported but unused" warnings
# while making the re-export contract explicit.
__all__ = [
    # Provider schemas re-exports (Phase 4 consolidation)
    "_VALIDATE_TIMEOUT_SECONDS",
    "_validate_with_timeout",
    "_build_llm_schemas",
    "_config_warnings",
    "_get_search_validation_issues",
    "_load_providers_safe",
    "_mask_config_keys",
    "get_runtime_versions",
    "RUNTIME_VERSIONS",
    # Ingest control re-exports (Phase 4 consolidation)
    "_parse_ingest_summary",
    "_stdout_reader",
    "_ingest_running",
    "_ingest_lock",
    "_ingest_process",
    "_ingest_log_file",
    "_last_run",
    "_ingest_just_completed",
    "MAX_SSE_CONNECTIONS",
    "_INGEST_SUMMARY_RE",
    # Profile store path constants used by test fixtures via monkeypatch
    "_CONFIG_PATH",
    "_KEYS_PATH",
    # PDF import re-exports — test_profile_import.py accesses these via
    # app_module.<name> and patches them via patch("app.<name>", ...).
    # NOTE: patch("app._extract_pdf_text", ...) does NOT affect web/profile.py
    # because web/profile imports _extract_pdf_text from services.pdf_import
    # directly.  Tests that exercise the /profile/import-pdf HTTP endpoint
    # must be migrated to patch("web.profile._extract_pdf_text", ...) etc.
    "_DEGREE_PREFIX_RE",
    "_IMPORT_PROMPT_FRESH",
    "_IMPORT_PROMPT_PREFILTER_EXTENSION",
    "_MAX_CONCURRENT_PDF_JOBS",
    "_MAX_PATTERN_LEN",
    "_MAX_PATTERNS_PER_LIST",
    "_PDF_ASYNC_THRESHOLD",
    "_PDF_JOB_TIMEOUT_SECONDS",
    "_PDF_JOB_TTL_SECONDS",
    "_PRUNE_INTERVAL_SECONDS",
    "_YEAR_RE",
    "_build_import_prompt",
    "_extract_pdf_text",
    "_last_prune_time",
    "_merge_import_result",
    "_merge_prefilter_suggestions",
    "_normalise_education",
    "_parse_import_response",
    "_pdf_executor",
    "_pdf_jobs",
    "_pdf_jobs_lock",
    "_prune_pdf_jobs",
    "_run_pdf_import_job",
]

# DEMO_MODE is read by web/__init__.py's context-processor closure at
# request time.  The __main__ block may set it to True before the
# server starts; keep it at module scope so the closure always sees
# the current value.
DEMO_MODE: bool = False

# Build (or retrieve the already-built) Flask application.  All
# startup guards, filter registration, db.init_db(), and plugin
# registration happen inside create_app().
from web import create_app  # noqa: E402
app = create_app()


# ---------------------------------------------------------------------------
# CSRF guard — moved to web/security.py; registered by create_app()
# ---------------------------------------------------------------------------
# The guard functions (_is_trusted_host, _is_localhost_request,
# inject_demo_mode, csrf_localhost_guard) now live in web/security.py.
# create_app() registers csrf_localhost_guard via app.before_request()
# and inject_demo_mode via app.context_processor().

# ---------------------------------------------------------------------------
# Config / profile store — moved to services/profile_store.py (Phase 2)
# ---------------------------------------------------------------------------
# Path constants, _KEYS_DEFAULTS, load_config, _write_json_atomic,
# load_profile, _validate_profile_form, _parse_education_rows, and
# _parse_repeating_rows now live in services/profile_store.py.
# _KEYS_DEFAULTS is only used internally in services/profile_store.py
# and is not re-exported here (no call sites in this module).
# They are imported here so all existing call-sites in this module and
# in the test suite resolve to the same objects without any change.
from services.profile_store import (  # noqa: E402
    _CONFIG_DIR,
    _CONFIG_PATH,
    _KEYS_PATH,
    _PROFILE_PATH,
    _PROVIDERS_PATH,
    load_config,
)

CONFIG = load_config()
# db.init_db() and ensure_plugins_registered() have moved to
# web/__init__.py::create_app(), called above at module scope.


# ---------------------------------------------------------------------------
# Runtime version capture — moved to services/provider_schemas.py (Phase 4)
# ---------------------------------------------------------------------------
# get_runtime_versions, RUNTIME_VERSIONS imported above from
# services.provider_schemas.

# ---------------------------------------------------------------------------
# Config warnings, search validation, and provider loader are now imported
# directly from services.provider_schemas (see top-of-file import block).
# Route handlers pass the current module-level path globals as explicit
# arguments so that monkeypatch.setattr(app_module, "_PROVIDERS_PATH", ...)
# still takes effect at call time (Issue #326 consolidation).
# ---------------------------------------------------------------------------
# Template filters — moved to web/filters.py; registered by create_app()
# ---------------------------------------------------------------------------
# salary_fmt, parse_iso, and timeago now live in web/filters.py.
# create_app() registers them via app.add_template_filter().
# They are re-exported below for backward-compat with the test suite.


# ---------------------------------------------------------------------------
# Routes — feed and ingest routes extracted to web/ blueprints (Phase 5a)
# ---------------------------------------------------------------------------
# feed_bp (web/feed.py): /, /feed/fragment, /bookmarks, /bookmark/<id>,
#   /apply/<id>, /applied, /snippets, /stats, /dismiss/<id>,
#   /listings/<id>/open
# ingest_bp (web/ingest.py): /ingest/trigger, /api/ingest/preflight,
#   /ingest/status, /ingest/stream
# Both blueprints are registered by web/__init__.py::create_app() with
# url_prefix="" so all URL paths remain unchanged.

# ---------------------------------------------------------------------------
# Credential masking — moved to services/provider_schemas.py (Phase 4)
# ---------------------------------------------------------------------------
# _mask_config_keys imported above from services.provider_schemas.

# ---------------------------------------------------------------------------
# Ingestion globals re-exported for test-import contract (Phase 4)
# ---------------------------------------------------------------------------
# _INGEST_SUMMARY_RE alias — kept for call-sites that import it from app.
_INGEST_SUMMARY_RE = ingest_control._INGEST_SUMMARY_RE

# ingest_trigger, ingest_preflight, ingest_status, ingest_stream, and the
# two helper renderers (_render_ingest_idle, _render_ingest_running) have
# moved to web/ingest.py (ingest_bp).  Registered by create_app() above.

# ---------------------------------------------------------------------------
# Settings UI helpers — moved to services/provider_schemas.py (Phase 4)
# ---------------------------------------------------------------------------
# _build_llm_schemas, _load_providers_safe imported above from
# services.provider_schemas.

# ---------------------------------------------------------------------------
# Routes — settings, profile, and admin routes extracted to web/ blueprints
# (Phase 5b).
# ---------------------------------------------------------------------------
# settings_bp (web/settings.py): /settings, /settings/config,
#   /api/validate-keys, /api/providers/reorder,
#   /api/job-sources/<source_key>/toggle
# profile_bp (web/profile.py): /profile, /profile/import-pdf,
#   /profile/import-pdf/status/<job_id>, /api/apply-prefilter-suggestions
# admin_bp (web/admin.py): /admin, /admin/clear-db, /admin/logs,
#   /admin/logs/<filename>/download, /admin/schedule-state
# All three blueprints are registered by web/__init__.py::create_app()
# with url_prefix="" so all URL paths remain unchanged.


# ---------------------------------------------------------------------------
# Backward-compat re-exports — keep test-import contract intact
# ---------------------------------------------------------------------------
# Tests import these names directly from `app`.  Once all call sites
# migrate to the canonical web/* paths these re-exports can be removed.
from web.filters import salary_fmt, timeago  # noqa: E402, F401
from web.security import (  # noqa: E402, F401
    _is_trusted_host,
    _is_localhost_request,
)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Job Matcher web server")
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Run in demo mode: uses jobs.demo.db and demo config/profile files",
    )
    args = parser.parse_args()

    if args.demo:
        DEMO_MODE = True
        # TODO: demo mode is not supported in the PostgreSQL deployment
        _PROFILE_PATH = os.path.join(_CONFIG_DIR, "profile.demo.json")
        _PROVIDERS_PATH = os.path.join(_CONFIG_DIR, "providers.demo.json")
        print("Demo mode enabled — using demo config files.")

    db.init_db()
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    # threaded=True is required for SSE (/ingest/stream) — without it Flask's
    # dev server is single-threaded and an open SSE connection blocks all other
    # requests, causing 429 errors.  Docker deployments use waitress (multi-
    # threaded) via the Dockerfile CMD and never execute this code path.
    app.run(debug=debug, port=5000, threaded=True)
