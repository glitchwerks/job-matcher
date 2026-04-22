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

import json
import os
import re
import secrets
from datetime import datetime, timezone

from flask import (
    abort,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)

import db
from credentials import save_providers
from paths import LOG_DIR

from providers import _PROVIDER_CLASS_MAP, build_provider_chain, generate_with_fallback
from job_sources import get_sources

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
    "_KEYS_PATH",
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
    _parse_education_rows,
    _parse_repeating_rows,
    _validate_profile_form,
    _write_json_atomic,
    load_config,
    load_profile,
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

@app.route("/settings", methods=["GET", "POST"])
def settings():
    """Settings page — manage LLM provider credentials and job source credentials.

    GET:  Builds ``llm_schemas`` and ``source_schemas`` from the provider/source
          registries and passes only boolean ``has_values`` flags — never raw
          credential values — to the template.  Tab is selected via ``?tab=``
          query param (default: ``llm``).

    POST: Parses namespaced form fields (``<provider_key>__<field_name>``),
          deep-merges non-blank values into ``providers.json`` via
          :func:`credentials.save_providers`, then redirects to
          ``GET /settings?tab=<active_tab>``.
    """
    error = None

    if request.method == "POST":
        active_tab = request.form.get("tab", "llm").strip()

        # --- Build updates dict from namespaced form fields ---
        # Only populate the section that corresponds to the active tab.  Processing
        # the other section would send blank values for every field not present in
        # the submitted form, causing _deep_merge to overwrite previously-saved
        # credentials with empty strings (cross-tab wipe bug, issue #71).
        updates: dict = {}

        if active_tab == "llm":
            updates["llm"] = {}
            # LLM providers: iterate registry so new providers are handled automatically.
            # Only include fields that have a non-empty value so that providers the
            # user left blank are not merged into providers.json as empty strings
            # (within-tab wipe bug, issue #71).  A user who explicitly clears a field
            # will have submitted a blank for a provider that already had a value — we
            # distinguish "provider's form was on the page and submitted blank" from
            # "provider wasn't on the page at all" by limiting this block to
            # active_tab == "llm" above.
            #
            # Load the current stored state once so we can fill in missing
            # non-password field defaults when the JS dirty-tracker omits
            # unchanged fields from the POST body (fixes issue #231).
            _current_providers = _load_providers_safe(
                providers_path=_PROVIDERS_PATH, keys_path=_KEYS_PATH, config_path=_CONFIG_PATH
            )
            _current_llm = _current_providers.get("llm") or {}
            for provider_key, cls in _PROVIDER_CLASS_MAP.items():
                schema = cls.settings_schema()
                provider_updates: dict = {}
                for field in schema["fields"]:
                    field_name = field["name"]
                    form_key = f"{provider_key}__{field_name}"
                    raw = request.form.get(form_key)
                    if raw is None:
                        # Field not present in form at all — skip to preserve
                        # any existing stored value.
                        continue
                    stripped = raw.strip()
                    # No-JS guard: skip empty password fields unless the
                    # explicit __clear__ flag is present.  This prevents a
                    # native (no-JS) form submit from wiping an existing key
                    # just because the password placeholder was left blank.
                    if field.get("type") == "password" and stripped == "":
                        clear_key = f"__clear__{provider_key}__{field_name}"
                        if request.form.get(clear_key) != "1":
                            continue
                    provider_updates[field_name] = stripped
                # After processing normal fields, check for explicit __clear__
                # flags on password fields.  The flag writes "" regardless of
                # whether the password form field was also submitted.
                for field in schema["fields"]:
                    if field.get("type") != "password":
                        continue
                    clear_key = f"__clear__{provider_key}__{field['name']}"
                    if request.form.get(clear_key) == "1":
                        provider_updates[field["name"]] = ""
                # When the provider is being updated (at least one field was
                # submitted), ensure every non-password field that was NOT in
                # the POST body (because JS dirty-tracking only sends changed
                # fields) is written with its current stored value or its
                # schema default.  Without this, a user who only edits the
                # API key and never touches the model dropdown will end up with
                # no model in providers.json, causing has_values to return False
                # and the provider to show as "not configured" after every save.
                if provider_updates:
                    stored_cfg = _current_llm.get(provider_key) or {}
                    for field in schema["fields"]:
                        if field.get("type") == "password":
                            continue
                        field_name = field["name"]
                        if field_name in provider_updates:
                            continue
                        stored_val = stored_cfg.get(field_name, "")
                        if not stored_val:
                            default_val = field.get("default", "")
                            if default_val:
                                provider_updates[field_name] = default_val
                    updates["llm"][provider_key] = provider_updates

        elif active_tab == "sources":
            updates["job_sources"] = {}
            # Job sources: JS dirty-tracking sends only the fields the user
            # actually changed, so we must skip sources that have no form data
            # at all.  A source is "touched" when any of its namespaced fields
            # (credentials or the enabled checkbox) appears in the POST body.
            # This prevents the server from overwriting stored credentials or
            # toggling the enabled flag for sources the user never interacted
            # with (issue #89 — client-side dirty tracking companion fix).
            for source_key, cls in get_sources().items():
                schema_fields = cls.settings_schema()["fields"]
                cred_keys = [f"{source_key}__{f['name']}" for f in schema_fields]
                clear_keys = [f"__clear__{source_key}__{f['name']}" for f in schema_fields]
                enabled_key = f"{source_key}__enabled"
                # Skip this source entirely when none of its form keys are present.
                # Include __clear__ keys in this check: when the JS Clear button
                # is clicked, submitDirty() may send only the __clear__ flag
                # (plus the empty credential field after the client fix), but
                # this defense-in-depth ensures the server never skips a source
                # that has an explicit clear flag even if the credential field
                # is absent from the POST body.
                source_in_form = any(
                    request.form.get(k) is not None
                    for k in cred_keys + [enabled_key] + clear_keys
                )
                if not source_in_form:
                    continue

                source_updates: dict = {}

                # Checkbox: only update enabled when the field was explicitly
                # submitted.  JS dirty-tracking sends the checkbox only when
                # the user actually toggled it: 'on' = checked, '' = unchecked.
                # If the field is absent entirely (user only changed a
                # credential), leave the stored enabled state untouched.
                if enabled_key in request.form:
                    source_updates["enabled"] = request.form.get(enabled_key) == "on"

                for field in schema_fields:
                    field_name = field["name"]
                    form_key = f"{source_key}__{field_name}"
                    raw = request.form.get(form_key)
                    if raw is None:
                        continue
                    stripped = raw.strip()
                    # No-JS guard: skip empty password fields unless the
                    # explicit __clear__ flag is present.
                    if field.get("type") == "password" and stripped == "":
                        clear_key = f"__clear__{source_key}__{field_name}"
                        if request.form.get(clear_key) != "1":
                            continue
                    source_updates[field_name] = stripped
                # Explicit __clear__ flags for password fields.
                for field in schema_fields:
                    if field.get("type") != "password":
                        continue
                    clear_key = f"__clear__{source_key}__{field['name']}"
                    if request.form.get(clear_key) == "1":
                        source_updates[field["name"]] = ""

                updates["job_sources"][source_key] = source_updates

        try:
            save_providers(updates, providers_path=_PROVIDERS_PATH)
        except OSError:
            error = "Could not save settings — check file permissions."

        # Save search fields (country, what, where, results_per_page,
        # max_pages) to config.json.
        if error is None and active_tab == "search":
            existing_cfg = load_config(_CONFIG_PATH)
            existing_search = existing_cfg.get("search") or {}
            updated_search = dict(existing_search)

            # Free-text search fields — store as-is (stripped).
            for field_name in ("search_country", "search_what", "search_where"):
                raw = request.form.get(field_name, "").strip()
                config_key = field_name[len("search_"):]  # strip "search_" prefix
                if raw:
                    updated_search[config_key] = raw
                elif field_name in request.form:
                    # Explicit empty submission — allow clearing the field.
                    updated_search.pop(config_key, None)

            # Numeric search fields.
            rpp_str = request.form.get("search_results_per_page", "").strip()
            mp_str = request.form.get("search_max_pages", "").strip()
            if rpp_str:
                try:
                    updated_search["results_per_page"] = int(rpp_str)
                except ValueError:
                    pass
            if mp_str:
                try:
                    updated_search["max_pages"] = int(mp_str)
                except ValueError:
                    pass

            updated_cfg = dict(existing_cfg)
            updated_cfg["search"] = updated_search
            try:
                _write_json_atomic(_CONFIG_PATH, updated_cfg)
            except OSError:
                error = "Could not save config — check file permissions."

        if error is None:
            return redirect(url_for("settings", tab=active_tab))

    # --- GET (or POST with error) ---
    active_tab = request.args.get("tab", "llm").strip()
    providers_data = _load_providers_safe(
        providers_path=_PROVIDERS_PATH, keys_path=_KEYS_PATH, config_path=_CONFIG_PATH
    )
    llm_section: dict = providers_data.get("llm") or {}
    sources_section: dict = providers_data.get("job_sources") or {}

    # provider_order from providers.json determines display sequence.
    provider_order: list[str] = providers_data.get("provider_order") or []
    llm_schemas = _build_llm_schemas(llm_section, provider_order)

    source_schemas: list[tuple[str, dict, bool, bool, bool, set]] = []
    for key, cls in get_sources().items():
        schema = cls.settings_schema()
        cfg = sources_section.get(key) or {}
        required_fields = [f["name"] for f in schema["fields"] if f.get("required")]
        if required_fields:
            has_values = all(bool(cfg.get(fn, "").strip()) for fn in required_fields)
        else:
            has_values = False  # no-credential sources are never "configured"
        is_enabled = bool(cfg.get("enabled", False))
        credentials_required = bool(required_fields)
        populated_fields = {
            f["name"] for f in schema["fields"]
            if bool(cfg.get(f["name"], "").strip())
        }
        source_schemas.append((key, schema, has_values, is_enabled, credentials_required, populated_fields))

    # POST-with-error: re-render the form (not a redirect) so the error is shown.
    saved = False  # POST always redirects on success; reaching here means error or GET
    if request.method == "POST" and error:
        pass  # fall through to render with error

    # Pass search fields and validation issues to the Search Settings tab.
    search_cfg = load_config(_CONFIG_PATH).get("search") or {}
    search_issues = _get_search_validation_issues(
        providers_path=_PROVIDERS_PATH, config_path=_CONFIG_PATH
    )

    return render_template(
        "settings.html",
        view="settings",
        llm_schemas=llm_schemas,
        source_schemas=source_schemas,
        active_tab=active_tab,
        saved=saved,
        error=error,
        search_cfg=search_cfg,
        search_issues=search_issues,
    )


# _parse_education_rows and _parse_repeating_rows are imported from
# services/profile_store (see top-of-file imports block, Phase 2).

# ---------------------------------------------------------------------------
# PDF resume import — moved to services/pdf_import.py (Phase 3)
# ---------------------------------------------------------------------------
# All helpers, prompt constants, regexes, async job state, and the background
# worker now live in services/pdf_import.py.  The names below are re-exported
# so that existing call sites in this module and the test-import contract
# (tests/test_pdf_async.py, tests/test_profile_import.py) continue to work
# without any changes to call sites.
#
# Re-export hazard — _last_prune_time:
#   _prune_pdf_jobs() reads and writes services.pdf_import._last_prune_time
#   via a ``global`` declaration inside that module.  Rebinding
#   ``app_module._last_prune_time`` in tests does NOT propagate to the service
#   module; tests must rebind ``pdf_import_module._last_prune_time`` directly
#   (see tests/test_pdf_async.py).  The re-export below is read-only and is
#   kept only so that ``app_module._last_prune_time`` remains a valid attribute
#   for test setup that hasn't migrated yet.
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


# ---------------------------------------------------------------------------
# PDF resume import — endpoint
# ---------------------------------------------------------------------------


@app.route("/profile/import-pdf", methods=["POST"])
def profile_import_pdf():
    """Import profile data from an uploaded PDF resume via LLM extraction.

    Accepts a multipart/form-data POST with:
    - ``file``: PDF file upload (required, max 10 MB).
    - ``mode``: ``"fresh"`` (default) or ``"merge"``.

    **Small PDFs** (extracted text ≤ ``_PDF_ASYNC_THRESHOLD`` chars) are
    processed synchronously and return the result directly.

    **Large PDFs** (extracted text > ``_PDF_ASYNC_THRESHOLD`` chars) are
    dispatched to a daemon thread; the response is HTTP 202 with a ``job_id``
    that the client must poll via ``GET /profile/import-pdf/status/<job_id>``.

    Returns JSON — does NOT write profile.json.  The response payload is
    intended for client-side form pre-fill so the user can review before saving.

    .. note::
        **CSRF protection**: the endpoint is guarded by the app's
        localhost/private-network origin check, which rejects cross-origin
        requests from outside the trusted network.

    Returns:
        200 ``{"success": True, "profile": {...}, "model_used": "provider/model"}``
        202 ``{"async": True, "job_id": "<uuid>"}`` (large PDF, poll for result)
        400 invalid input (no file, non-PDF, unreadable PDF)
        413 file or extracted text exceeds size limits
        422 extracted text too short to be useful
        502 LLM failure (all providers failed or unparseable response)
        503 no LLM provider configured
    """
    import uuid as _uuid

    # Validate file
    uploaded = request.files.get("file")
    if not uploaded or not uploaded.filename:
        return jsonify({"success": False, "error": "No file uploaded."}), 400
    if not uploaded.filename.lower().endswith(".pdf"):
        return jsonify({"success": False, "error": "Only PDF files are accepted."}), 400

    mode = request.form.get("mode", "fresh")
    if mode not in ("fresh", "merge"):
        mode = "fresh"

    # Optional prefilter title-filter suggestions (off by default).
    suggest_filters = request.form.get("suggest_filters") == "1"

    # Extract text
    pdf_bytes = uploaded.read()
    if len(pdf_bytes) > 10 * 1024 * 1024:
        return jsonify({"success": False, "error": "PDF exceeds the 10 MB size limit."}), 413
    try:
        resume_text = _extract_pdf_text(pdf_bytes)
    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400

    if len(resume_text.strip()) < 50:
        return jsonify({"success": False, "error": "Could not extract meaningful text from this PDF."}), 422

    # Prompt injection mitigation: enforce length cap and strip control characters
    if len(resume_text) > 50_000:
        return jsonify({"success": False, "error": "Extracted PDF text exceeds the 50,000 character limit."}), 413
    resume_text = "".join(ch for ch in resume_text if ch.isprintable() or ch in "\n\r\t")

    # Dispatch large PDFs asynchronously to avoid blocking the Flask thread.
    if len(resume_text) > _PDF_ASYNC_THRESHOLD:
        job_id = str(_uuid.uuid4())
        with _pdf_jobs_lock:
            active = sum(
                1 for j in _pdf_jobs.values()
                if j["status"] in ("pending", "running")
            )
            if active >= _MAX_CONCURRENT_PDF_JOBS:
                return jsonify({
                    "success": False,
                    "error": "Too many concurrent imports. Please wait and try again.",
                }), 429
            _pdf_jobs[job_id] = {
                "id": job_id,
                "status": "pending",
                "result": None,
                "error": None,
                "created_at": datetime.now(timezone.utc).timestamp(),
            }
        providers_dict = _load_providers_safe(
            providers_path=_PROVIDERS_PATH, keys_path=_KEYS_PATH, config_path=_CONFIG_PATH
        )
        _pdf_executor.submit(
            _run_pdf_import_job,
            job_id,
            resume_text,
            mode,
            providers_dict,
            _PROFILE_PATH,
            suggest_filters,
        )
        return jsonify({"async": True, "job_id": job_id}), 202

    # Small PDF — synchronous path
    providers_dict = _load_providers_safe(
        providers_path=_PROVIDERS_PATH, keys_path=_KEYS_PATH, config_path=_CONFIG_PATH
    )
    chain = build_provider_chain(providers_dict)
    if not chain:
        return jsonify({"success": False, "error": "No LLM provider is configured. Add one in Settings first."}), 503

    # Build prompt and call LLM
    current_profile = load_profile(_PROFILE_PATH) if mode == "merge" else None
    prompt = _build_import_prompt(resume_text, suggest_filters=suggest_filters)
    result = generate_with_fallback(prompt, chain, set())
    if result is None:
        return jsonify({"success": False, "error": "All LLM providers failed. Check your API keys in Settings."}), 502

    raw_text, model_used = result

    # Parse response
    parsed = _parse_import_response(raw_text)
    if parsed is None:
        return jsonify({"success": False, "error": "LLM returned an unparseable response. Try again."}), 502

    # Apply merge or format for fresh
    if mode == "merge":
        profile_result = _merge_import_result(current_profile, parsed)
    else:
        structured_skills = []
        for s in parsed.get("primary_skills", []):
            name = s.get("skill", "")
            years = s.get("years", 0)
            status = s.get("status", "active")
            structured_skills.append({
                "description": name,
                "years_active": int(years) if years else 0,
                "active": status != "dormant",
            })
        profile_result = {
            "primary_skills": structured_skills,
            "education": _normalise_education(parsed.get("education", [])),
            "seniority": parsed.get("seniority", ""),
            "preferred_industries": parsed.get("preferred_industries", []),
            "location_center": parsed.get("location_center"),
        }

    response_payload: dict = {
        "success": True,
        "profile": profile_result,
        "model_used": model_used,
    }
    if suggest_filters and "prefilter_suggestions" in parsed:
        response_payload["prefilter_suggestions"] = parsed["prefilter_suggestions"]

    return jsonify(response_payload), 200


@app.route("/profile/import-pdf/status/<job_id>", methods=["GET"])
def profile_import_pdf_status(job_id: str):
    """Poll the status of an async PDF import job.

    Args:
        job_id: UUID returned by ``POST /profile/import-pdf`` when a large PDF
                was submitted (response contained ``"async": True``).

    Returns:
        200 ``{"status": "pending"}`` or ``{"status": "running"}``
        200 ``{"status": "complete", "result": {...}}`` — same shape as sync 200
        200 ``{"status": "failed", "error": "..."}``
        404 if ``job_id`` is unknown or has already been pruned
    """
    _prune_pdf_jobs()

    with _pdf_jobs_lock:
        job = _pdf_jobs.get(job_id)

    if job is None:
        return jsonify({"error": "Job not found."}), 404

    status = job["status"]
    if status in ("pending", "running"):
        return jsonify({"status": status}), 200
    if status == "complete":
        return jsonify({"status": "complete", "result": job["result"]}), 200
    # status == "failed"
    return jsonify({"status": "failed", "error": job["error"]}), 200


@app.route("/profile", methods=["GET", "POST"])
def profile():
    """Profile page — structured form for candidate preferences.

    GET:  Loads both ``profile.json`` and the candidate-facing subset of
          ``config.json``, and passes structured dicts to the template.  No
          raw JSON is exposed; no sensitive fields are present.

    POST: Parses individual form fields, writes ``profile.json`` from the
          profile fields, and deep-merges only the candidate-facing config
          fields (``search.*`` candidate keys, ``scoring.threshold``,
          ``prefilter.*``) back into ``config.json`` — leaving technical keys
          (``results_per_page``, ``max_pages``, ``model``, etc.) untouched.
          Returns 422 on validation errors without touching either file.
    """
    saved = False
    error = None
    status_code = 200

    if request.method == "POST":
        # --- Validate before touching disk ---
        threshold_str = request.form.get("scoring_threshold", "")
        validation_errors = _validate_profile_form(threshold_str)
        if validation_errors:
            error = "; ".join(validation_errors)
            status_code = 422
        else:
            # Collect any additional field-level validation errors.
            field_errors: list[str] = []

            # Build profile.json dict from profile fields.
            location_block: dict = {}
            loc_center = request.form.get("location_center", "").strip()
            loc_radius = request.form.get("location_radius_km", "").strip()
            loc_fallback = request.form.get("location_geocode_fallback", "pass").strip()
            loc_notes = request.form.get("location_notes", "").strip()
            if loc_center:
                location_block["center"] = loc_center
            if loc_radius:
                try:
                    radius = float(loc_radius)
                    if radius > 0:
                        location_block["radius_km"] = radius
                    else:
                        field_errors.append("location.radius_km must be greater than 0")
                except ValueError:
                    field_errors.append("location.radius_km must be a number")
            location_block["geocode_fallback"] = loc_fallback or "pass"
            if loc_notes:
                location_block["notes"] = loc_notes

            # Parse structured primary_skills fields.
            # Each skill is submitted as parallel arrays:
            #   skill_description[]   — the skill name
            #   skill_years_active[]  — years of experience (integer)
            #   skill_active_idx[]    — indices (0-based) of rows where active=true
            # We use an index list for active because unchecked checkboxes are not
            # submitted by browsers; the hidden-input trick captures which rows
            # the user toggled ON.
            descriptions = request.form.getlist("skill_description[]")
            years_raw = request.form.getlist("skill_years_active[]")
            active_indices_raw = request.form.getlist("skill_active_idx[]")
            try:
                active_indices = {int(x) for x in active_indices_raw if x.strip()}
            except ValueError:
                active_indices = set()

            primary_skills: list[dict] = []
            for i, desc in enumerate(descriptions):
                desc = desc.strip()
                if not desc:
                    continue  # skip empty rows
                years_str = years_raw[i] if i < len(years_raw) else "0"
                try:
                    years = int(years_str)
                except (ValueError, TypeError):
                    field_errors.append(
                        f"Primary skill '{desc}': years must be a whole number, got '{years_str}'"
                    )
                    continue
                if years < 0:
                    field_errors.append(
                        f"Primary skill '{desc}': years_active cannot be negative"
                    )
                primary_skills.append({
                    "description": desc,
                    "years_active": years,
                    "active": i in active_indices,
                })

            new_profile: dict = {
                "primary_skills": primary_skills,
                "anti_preferences": _parse_repeating_rows(request.form, "anti_preferences"),
                "education": _parse_education_rows(request.form),
                "seniority": request.form.get("seniority", "").strip(),
                "preferred_industries": _parse_repeating_rows(request.form, "preferred_industries"),
                "location": location_block,
                "scoring_notes": _parse_repeating_rows(request.form, "scoring_notes"),
            }

            # Build the candidate-facing config.json subset.
            # Read existing config first so we can merge (preserving technical keys).
            existing_cfg = load_config(_CONFIG_PATH)
            existing_search = existing_cfg.get("search") or {}
            existing_scoring = existing_cfg.get("scoring") or {}
            existing_prefilter = existing_cfg.get("prefilter") or {}

            # Candidate search fields — only update these; leave results_per_page
            # and max_pages (technical fields managed on the Settings page) alone.
            salary_min_str = request.form.get("search_salary_min", "").strip()
            distance_str = request.form.get("search_distance", "").strip()
            max_days_str = request.form.get("search_max_days_old", "").strip()

            updated_search = dict(existing_search)
            updated_search["country"] = request.form.get("search_country", "").strip()
            updated_search["what"] = request.form.get("search_what", "").strip()
            updated_search["where"] = request.form.get("search_where", "").strip()
            if distance_str:
                try:
                    dist = int(distance_str)
                    if dist >= 0:
                        updated_search["distance"] = dist
                    else:
                        field_errors.append("search.distance must be 0 or greater")
                except ValueError:
                    field_errors.append("search.distance must be a whole number")
            else:
                updated_search.pop("distance", None)
            if salary_min_str:
                try:
                    sal = int(salary_min_str)
                    if sal >= 0:
                        updated_search["salary_min"] = sal
                    else:
                        field_errors.append("search.salary_min must be 0 or greater")
                except ValueError:
                    field_errors.append("search.salary_min must be a whole number")
            else:
                updated_search.pop("salary_min", None)
            if max_days_str:
                try:
                    days = int(max_days_str)
                    if days > 0:
                        updated_search["max_days_old"] = days
                    else:
                        field_errors.append("search.max_days_old must be greater than 0")
                except ValueError:
                    field_errors.append("search.max_days_old must be a whole number")
            else:
                updated_search.pop("max_days_old", None)

            # Bail out before touching disk if any field-level errors were found.
            if field_errors:
                error = "; ".join(field_errors)
                status_code = 422

            if not field_errors:
                # scoring.threshold — parse is already validated above.
                updated_scoring = dict(existing_scoring)
                updated_scoring["threshold"] = float(threshold_str.strip())

                # prefilter fields.
                require_contract_time_raw = request.form.get("prefilter_require_contract_time", "").strip()
                require_contract_type_raw = request.form.get("prefilter_require_contract_type", "").strip()
                updated_prefilter = dict(existing_prefilter)
                updated_prefilter["title_include"] = _parse_repeating_rows(request.form, "prefilter_title_include")
                updated_prefilter["title_exclude"] = _parse_repeating_rows(request.form, "prefilter_title_exclude")
                updated_prefilter["require_contract_time"] = require_contract_time_raw or None
                updated_prefilter["require_contract_type"] = require_contract_type_raw or None

                new_cfg = dict(existing_cfg)
                new_cfg["search"] = updated_search
                new_cfg["scoring"] = updated_scoring
                new_cfg["prefilter"] = updated_prefilter

                # Write profile.json atomically.
                try:
                    _write_json_atomic(_PROFILE_PATH, new_profile)
                    _write_json_atomic(_CONFIG_PATH, new_cfg)
                    saved = True
                except OSError:
                    error = "Could not save — check file permissions."
                    status_code = 500

    # Load current values for the form (GET, or POST after error).
    cfg = load_config(_CONFIG_PATH)
    prof = load_profile(_PROFILE_PATH)

    # Establish the session CSRF token so the import drawer can include it on
    # the POST /api/apply-prefilter-suggestions request.
    session.setdefault("csrf_token", secrets.token_urlsafe(32))

    return render_template(
        "profile.html",
        view="profile",
        prof=prof,
        cfg=cfg,
        saved=saved,
        error=error,
        csrf_token=session["csrf_token"],
    ), status_code


@app.route("/settings/config")
def settings_config_redirect():
    return redirect(url_for("profile"), code=301)


# ---------------------------------------------------------------------------
# Admin actions
# ---------------------------------------------------------------------------

_LOG_FILENAME_RE = re.compile(r"^ingest_(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})\.log$")

# Scheduler health thresholds (hours since last scheduled run).
SCHEDULE_WARN_HOURS = 25
SCHEDULE_CRITICAL_HOURS = 49


@app.route("/admin")
def admin():
    """Administration page — runtime info, log downloads, ingest schedule, and database ops."""
    session.setdefault("csrf_token", secrets.token_urlsafe(32))
    listing_count = db.get_listing_count()
    return render_template(
        "admin.html",
        view="admin",
        listing_count=listing_count,
        csrf_token=session["csrf_token"],
        runtime_versions=RUNTIME_VERSIONS,
    )


@app.route("/admin/clear-db", methods=["POST"])
def admin_clear_db():
    """Delete all rows from the listings table.

    Requires the ``confirmation`` form field to equal exactly ``"DELETE"``
    (case-sensitive).  Any other value is rejected with 400 so that a
    misconfigured HTMX request or stray form submit cannot wipe data silently.

    On success the deleted row count is logged with a UTC timestamp and an
    HTMX-compatible HTML fragment is returned so the caller can swap it into
    the confirmation panel target.  The fragment includes the success notice
    and resets the danger-zone panel to its collapsed initial state so the
    user sees clear feedback without a full page reload.

    Returns:
        200 HTML fragment on success.
        400 HTML fragment when the confirmation phrase is wrong.
        500 HTML fragment on database error.
    """
    # CSRF check — token must match the session value established on GET /admin.
    csrf_token = request.form.get("csrf_token", "")
    if not csrf_token or csrf_token != session.get("csrf_token"):
        html = (
            '<p class="save-error" id="clear-db-result">'
            "Invalid or missing CSRF token — request rejected."
            "</p>"
        )
        return make_response(html, 400)

    confirmation = request.form.get("confirmation", "").strip()

    if confirmation != "DELETE":
        html = (
            '<p class="save-error" id="clear-db-result">'
            "Confirmation phrase did not match — database was not cleared."
            "</p>"
        )
        return make_response(html, 400)

    try:
        conn = db.get_connection()
        try:
            deleted = db.clear_all_listings(conn)
        finally:
            conn.close()
    except Exception as exc:  # pragma: no cover — DB errors are rare in tests
        app.logger.error("clear_all_listings failed: %s", exc)
        html = (
            '<p class="save-error" id="clear-db-result">'
            f"Database error — listings were not cleared: {exc}"
            "</p>"
        )
        return make_response(html, 500)

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    app.logger.info("[%s] admin/clear-db: deleted %d listing(s).", ts, deleted)

    # Return an HTML fragment that:
    # 1. Replaces the confirmation panel with a success notice.
    # 2. Hides the danger-zone panel (collapsed back to just the trigger button).
    noun = "listing" if deleted == 1 else "listings"
    html = (
        f'<p class="save-notice" id="clear-db-result">'
        f"{deleted} {noun} deleted successfully."
        f"</p>"
        f'<div id="clear-db-panel" style="display:none"></div>'
    )
    return make_response(html, 200)


@app.route("/admin/logs")
def admin_logs():
    """Return an HTML fragment listing available ingest log files."""
    logs = []
    try:
        for entry in os.scandir(LOG_DIR):
            if not entry.is_file():
                continue
            m = _LOG_FILENAME_RE.match(entry.name)
            if not m:
                continue
            # Check readability
            try:
                size = entry.stat().st_size
            except OSError:
                continue
            timestamp = f"{m.group(1)}-{m.group(2)}-{m.group(3)} {m.group(4)}:{m.group(5)}:{m.group(6)}"
            # Human-readable size
            if size >= 1_048_576:
                size_str = f"{size / 1_048_576:.1f} MB"
            elif size >= 1024:
                size_str = f"{size / 1024:.1f} KB"
            else:
                size_str = f"{size} B"
            logs.append({"filename": entry.name, "timestamp": timestamp, "size": size_str})
    except FileNotFoundError:
        pass  # LOG_DIR doesn't exist yet — empty list

    logs.sort(key=lambda x: x["filename"], reverse=True)  # newest first
    return render_template("admin/_log_list.html", logs=logs)


@app.route("/admin/logs/<filename>/download")
def admin_log_download(filename):
    """Download an ingest log file."""
    # Validate filename against strict regex
    if not _LOG_FILENAME_RE.match(filename):
        abort(404)

    target = (LOG_DIR / filename).resolve()

    # Symlink escape check — resolved path must be inside LOG_DIR.
    # Use relative_to() rather than startswith() so the check is
    # case-insensitive-safe and handles path separators correctly on Windows.
    try:
        target.relative_to(LOG_DIR.resolve())
    except ValueError:
        abort(404)

    if not target.is_file():
        abort(404)

    return send_from_directory(
        LOG_DIR,
        filename,
        as_attachment=True,
        mimetype="text/plain; charset=utf-8",
    )


@app.route("/admin/schedule-state")
def admin_schedule_state():
    """Return an HTML fragment showing ingest run history and scheduler health."""
    try:
        runs = db.get_recent_ingest_runs(10)
    # Catch-all: schedule state is best-effort; a DB error must never break the admin page.
    except Exception:  # noqa: BLE001
        runs = []

    # Compute health badge
    badge = "none"  # no data
    badge_text = "No runs recorded yet"

    if runs:
        # Find most recent scheduled run
        scheduled_runs = [r for r in runs if r.get("trigger_source") == "scheduled"]

        if scheduled_runs:
            last_scheduled = scheduled_runs[0]
            age_hours = None
            if last_scheduled.get("started_at"):
                started = last_scheduled["started_at"]
                if hasattr(started, "tzinfo") and started.tzinfo:
                    now = datetime.now(timezone.utc)
                else:
                    now = datetime.utcnow()
                age_hours = (now - started).total_seconds() / 3600

            if last_scheduled.get("status") == "failed":
                badge = "red"
                badge_text = "Last scheduled run failed"
            elif age_hours is not None and age_hours > SCHEDULE_CRITICAL_HOURS:
                badge = "red"
                badge_text = f"Scheduler may be down — no scheduled run in {SCHEDULE_CRITICAL_HOURS}+ hours"
            elif age_hours is not None and age_hours > SCHEDULE_WARN_HOURS:
                badge = "amber"
                badge_text = f"Last scheduled run was {SCHEDULE_WARN_HOURS}+ hours ago"
            elif last_scheduled.get("status") == "running":
                badge = "amber"
                badge_text = "Scheduled run in progress"
            else:
                badge = "green"
                badge_text = "Scheduler healthy"
        else:
            badge = "none"
            badge_text = "No scheduled runs recorded"

    return render_template(
        "admin/_schedule_state.html",
        runs=runs,
        badge=badge,
        badge_text=badge_text,
    )


# ---------------------------------------------------------------------------
# Key validation — moved to services/provider_schemas.py (Phase 4)
# ---------------------------------------------------------------------------
# _VALIDATE_TIMEOUT_SECONDS, _validate_with_timeout imported above from
# services.provider_schemas.

@app.route("/api/apply-prefilter-suggestions", methods=["POST"])
def apply_prefilter_suggestions():
    """Merge LLM-suggested title filters into config.json prefilter block.

    Accepts a form-encoded POST with fields:

    * ``csrf_token`` — session-scoped CSRF token (required; 403 on mismatch)
    * ``title_include`` — JSON-encoded array of include patterns
    * ``title_exclude`` — JSON-encoded array of exclude patterns

    The suggestions are merged (union-then-dedup, case-insensitive) into the
    existing ``config.json`` ``prefilter`` block via
    ``_merge_prefilter_suggestions()``.  All other prefilter keys
    (``require_contract_time``, ``require_contract_type``) are preserved.

    The disjoint-set invariant is enforced here too: if the POST body itself
    contains overlapping include/exclude terms the request is rejected with
    400 so malformed client payloads cannot corrupt config.

    Returns:
        200 ``{"success": True}`` on success.
        400 on missing/invalid input or overlapping include/exclude terms.
        403 on CSRF token mismatch.
        500 on config read/write failure.
    """
    # CSRF check — token must match the session value established on GET /profile.
    csrf_token = request.form.get("csrf_token", "")
    if not csrf_token or csrf_token != session.get("csrf_token"):
        return jsonify({
            "success": False,
            "error": "Invalid or missing CSRF token — request rejected.",
        }), 403

    inc_json = request.form.get("title_include", "")
    exc_json = request.form.get("title_exclude", "")

    try:
        inc_raw = json.loads(inc_json) if inc_json else None
        exc_raw = json.loads(exc_json) if exc_json else None
    except (json.JSONDecodeError, ValueError):
        inc_raw = None
        exc_raw = None

    if not isinstance(inc_raw, list) or not isinstance(exc_raw, list):
        return jsonify({
            "success": False,
            "error": (
                "title_include and title_exclude must be JSON-encoded arrays."
            ),
        }), 400

    inc = [str(s).lower() for s in inc_raw]
    exc = [str(s).lower() for s in exc_raw]

    # Intentional double-check: _parse_import_response validates the LLM response,
    # but the form submission could be tampered between the preview render and the
    # Apply click.  Re-validate at the HTTP boundary.
    overlap = set(inc) & set(exc)
    if overlap:
        return jsonify({
            "success": False,
            "error": (
                "title_include and title_exclude must be disjoint. "
                f"Overlapping terms: {sorted(overlap)}"
            ),
        }), 400

    try:
        with open(_CONFIG_PATH, "r", encoding="utf-8") as fh:
            cfg = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc_io:
        app.logger.error(
            "[apply-prefilter-suggestions] failed to read config: %s", exc_io
        )
        return jsonify({
            "success": False,
            "error": "Could not read config.json.",
        }), 500

    existing_prefilter = cfg.get("prefilter") or {}
    cfg["prefilter"] = _merge_prefilter_suggestions(
        existing_prefilter,
        {"title_include": inc, "title_exclude": exc},
    )

    try:
        with open(_CONFIG_PATH, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2)
            fh.write("\n")
    except OSError as exc_io:
        app.logger.error(
            "[apply-prefilter-suggestions] failed to write config: %s", exc_io
        )
        return jsonify({
            "success": False,
            "error": "Could not write config.json.",
        }), 500

    return jsonify({"success": True}), 200


@app.route("/api/validate-keys", methods=["POST"])
def validate_keys():
    """Validate each configured LLM provider by making a minimal 1-token test call.

    Loops ``_PROVIDER_CLASS_MAP`` so new providers are included automatically
    without any template or route changes.

    Returns an HTML partial (not JSON) intended for HTMX to swap into the page.
    Each provider gets one of five states: valid, invalid_key, unknown_model,
    unreachable, not_configured.  Each provider call is bounded to
    ``_VALIDATE_TIMEOUT_SECONDS`` seconds; a timeout maps to ``unreachable``.

    No API key values are logged or returned in the response.
    """
    providers_data = _load_providers_safe(
        providers_path=_PROVIDERS_PATH, keys_path=_KEYS_PATH, config_path=_CONFIG_PATH
    )
    llm_section: dict = providers_data.get("llm") or {}

    providers_list = []
    for provider_key, cls in _PROVIDER_CLASS_MAP.items():
        schema = cls.settings_schema()
        display_name: str = schema.get("display_name", provider_key.title())

        cfg = llm_section.get(provider_key, {})
        api_key = cfg.get("api_key", "").strip()
        model   = cfg.get("model", "").strip()

        if not api_key:
            state = "not_configured"
            detail = None
        else:
            state, detail = _validate_with_timeout(cls.validate_credentials, api_key, model)

        providers_list.append({
            "key":          provider_key,
            "display_name": display_name,
            "state":        state,
            "detail":       detail,
        })

    return render_template("_validation_results.html", providers=providers_list)


@app.route("/api/providers/reorder", methods=["POST"])
def api_providers_reorder():
    """Persist a new LLM provider fallback order.

    Expects JSON body: ``{"order": ["anthropic", "gemini", "openai"]}``

    * All entries must be known keys in ``_PROVIDER_CLASS_MAP``; unknown keys → 400.
    * ``order`` may be a subset of the registry (omitted providers are appended at
      runtime by ``build_provider_chain()``).
    * Writes only ``provider_order`` at the top level of ``providers.json``.
    * Returns the rendered ``_provider_order.html`` fragment on success (200).
    * Returns a plain-text error message on failure (400/500).
    """
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return "Invalid request body — expected JSON object.", 400

    order = body.get("order")
    if not isinstance(order, list):
        return "Missing or invalid 'order' field — expected a JSON array.", 400

    if not all(isinstance(k, str) for k in order):
        return "All entries in 'order' must be strings.", 400

    unknown = [k for k in order if k not in _PROVIDER_CLASS_MAP]
    if unknown:
        return f"Unknown provider key(s): {', '.join(unknown)}", 400

    if len(order) != len(set(order)):
        return "Duplicate provider key(s) in order list.", 400

    try:
        save_providers({"provider_order": order}, providers_path=_PROVIDERS_PATH)
    except OSError:
        return "Could not save order — check file permissions.", 500

    # Re-build llm_schemas in the new order for the response fragment.
    # We re-read providers.json here (rather than using the in-memory `order`
    # list alone) to pick up the has_values flags from the just-written file.
    providers_data = _load_providers_safe(
        providers_path=_PROVIDERS_PATH, keys_path=_KEYS_PATH, config_path=_CONFIG_PATH
    )
    llm_section: dict = providers_data.get("llm") or {}
    llm_schemas = _build_llm_schemas(llm_section, order)

    return render_template("_provider_order.html", llm_schemas=llm_schemas)


@app.route("/api/job-sources/<source_key>/toggle", methods=["POST"])
def api_job_source_toggle(source_key: str):
    """Persist the enabled/disabled state for a single job source.

    Designed for HTMX ``hx-trigger="change"`` on the source toggle checkbox so
    the change is saved immediately without requiring a full form submit.

    Request body (JSON)::

        {"enabled": true}   # or false

    Validation rules:

    * ``source_key`` must exist in the ``SOURCES`` registry → 404 if unknown.
    * When ``enabled=true``, all ``required`` credential fields for the source
      must have non-empty values already stored in ``providers.json`` → 422 if
      any are missing.
    * When ``enabled=false``, no credential check is performed.

    Returns:
        200 JSON ``{"ok": true}`` on success.
        404 JSON ``{"error": "..."}`` for unknown source keys.
        422 JSON ``{"error": "..."}`` when required credentials are missing.
        400 plain text for a malformed request body.
        500 plain text if the file cannot be written.
    """
    if source_key not in get_sources():
        return jsonify({"error": f"Unknown job source: {source_key!r}"}), 404

    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return "Invalid request body — expected JSON object.", 400

    if "enabled" not in body:
        return "Missing 'enabled' field in request body.", 400

    enabled = body["enabled"]
    if not isinstance(enabled, bool):
        return "The 'enabled' field must be a boolean (true or false).", 400

    # When enabling, verify required credentials are already stored.
    if enabled:
        cls = get_sources()[source_key]
        schema = cls.settings_schema()
        required_fields = [f for f in schema.get("fields", []) if f.get("required")]

        if required_fields:
            providers_data = _load_providers_safe(
                providers_path=_PROVIDERS_PATH,
                keys_path=_KEYS_PATH,
                config_path=_CONFIG_PATH,
            )
            src_cfg: dict = (providers_data.get("job_sources") or {}).get(source_key) or {}
            missing = [
                f["label"]
                for f in required_fields
                if not str(src_cfg.get(f["name"], "")).strip()
            ]
            if missing:
                display_name = schema.get("display_name", source_key)
                fields_str = " and ".join(missing)
                return jsonify({
                    "error": (
                        f"{display_name} requires {fields_str} before it can be enabled. "
                        "Add credentials in the Settings form and save, then try again."
                    )
                }), 422

    try:
        save_providers(
            {"job_sources": {source_key: {"enabled": enabled}}},
            providers_path=_PROVIDERS_PATH,
        )
    except OSError:
        return "Could not save — check file permissions.", 500

    return jsonify({"ok": True}), 200


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
