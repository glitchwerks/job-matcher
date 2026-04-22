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
import subprocess
import sys
import threading
from datetime import datetime, timezone

from flask import render_template, make_response, request, jsonify, redirect, url_for, Response, session, stream_with_context, send_from_directory, abort

import db
from credentials import save_providers
from paths import LOG_DIR
from ingest_events import IngestEventParser, event_queue

from providers import _PROVIDER_CLASS_MAP, build_provider_chain, generate_with_fallback
from providers.base import _sanitise_detail
from job_sources import get_sources

# ---------------------------------------------------------------------------
# Phase 4 service imports
# ---------------------------------------------------------------------------
# Phase 4 service imports — provider_schemas
# ---------------------------------------------------------------------------
# Path-independent helpers are imported directly; functions that read path
# constants are wrapped below as thin delegators so that
# monkeypatch.setattr(app_module, "_PROVIDERS_PATH", ...) in the test suite
# affects the paths actually used at call time.
import services.provider_schemas as _provider_schemas  # noqa: E402

from services.provider_schemas import (  # noqa: E402
    _VALIDATE_TIMEOUT_SECONDS,
    RUNTIME_VERSIONS,
    _build_llm_schemas,
    _mask_config_keys,
    get_runtime_versions,
)

# ingest_control — subprocess lifecycle, SSE state, stdout reader.
# Imported as a module (not a bare `from`) so Phase 5 route handlers can
# write to ingest_control._ingest_process without the binding hazard.
from services import ingest_control  # noqa: E402

# _parse_ingest_summary is a pure function — it has no module-global reads
# so a simple alias works and tests that import it from ``app`` see the
# same object as ingest_control._parse_ingest_summary.
_parse_ingest_summary = ingest_control._parse_ingest_summary

# _stdout_reader and _validate_with_timeout are defined as real functions
# later in this file (after the profile_store imports) so that test patches
# on app.IngestEventParser / app._VALIDATE_TIMEOUT_SECONDS take effect at
# call time.  ingest_control.py holds the canonical Phase 5 copies.

# ---------------------------------------------------------------------------
# Public re-export declarations
# ---------------------------------------------------------------------------
# Names listed here are intentionally imported and re-exported from this
# module so that existing call sites and test fixtures that import or patch
# them on ``app_module`` continue to work without modification.  Listing
# them in ``__all__`` suppresses ruff F401 "imported but unused" warnings
# while making the re-export contract explicit.
__all__ = [
    # Provider schemas re-exports (Phase 4)
    "_VALIDATE_TIMEOUT_SECONDS",
    "_build_llm_schemas",
    "_config_warnings",
    "_get_search_validation_issues",
    "_load_providers_safe",
    "_mask_config_keys",
    "get_runtime_versions",
    "RUNTIME_VERSIONS",
    # Ingest control re-exports (Phase 4)
    "_parse_ingest_summary",
    "_stdout_reader",
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
# Config warnings + search validation + provider loader (Phase 4)
# ---------------------------------------------------------------------------
# Implementation lives in services/provider_schemas.py.
# Thin wrappers below pass the current module-level path globals so that
# test fixtures using monkeypatch.setattr(app_module, "_PROVIDERS_PATH", ...)
# affect the actual paths used at call time — without requiring any changes
# to the test suite.


def _config_warnings() -> list[str]:
    """Return human-readable warnings for missing/empty Adzuna config.

    Delegates to :func:`services.provider_schemas._config_warnings`, passing
    the current ``_PROVIDERS_PATH`` module global so that test monkeypatches
    on ``app._PROVIDERS_PATH`` are honoured at call time.

    Returns:
        List of human-readable warning strings (may contain HTML).
        Empty list when there are no warnings.
    """
    return _provider_schemas._config_warnings(providers_path=_PROVIDERS_PATH)


def _get_search_validation_issues():
    """Return search-config validation issues for enabled sources.

    Delegates to :func:`services.provider_schemas._get_search_validation_issues`,
    passing the current path globals so that test monkeypatches on
    ``app._PROVIDERS_PATH`` / ``app._CONFIG_PATH`` are honoured at call time.

    Returns:
        List of :class:`ingest.ValidationIssue` objects.  Empty when all
        enabled sources have complete search configuration.
    """
    return _provider_schemas._get_search_validation_issues(
        providers_path=_PROVIDERS_PATH,
        config_path=_CONFIG_PATH,
    )


def _load_providers_safe() -> dict:
    """Load providers.json and return a parsed dict with safe defaults.

    Delegates to :func:`services.provider_schemas._load_providers_safe`,
    passing the current path globals so that test monkeypatches on
    ``app._PROVIDERS_PATH`` / ``app._KEYS_PATH`` / ``app._CONFIG_PATH``
    are honoured at call time.

    Returns:
        ``providers.json``-shaped dict with ``provider_order``, ``llm``, and
        ``job_sources`` keys guaranteed to be present.
    """
    return _provider_schemas._load_providers_safe(
        providers_path=_PROVIDERS_PATH,
        keys_path=_KEYS_PATH,
        config_path=_CONFIG_PATH,
    )


# ---------------------------------------------------------------------------
# Template filters — moved to web/filters.py; registered by create_app()
# ---------------------------------------------------------------------------
# salary_fmt, parse_iso, and timeago now live in web/filters.py.
# create_app() registers them via app.add_template_filter().
# They are re-exported below for backward-compat with the test suite.


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def feed():
    """Main feed: listings scored at or above the configured threshold.

    Accepts optional query params for filtering:
      - min_score: float override for the score floor
      - remote_only: "1" to restrict to remote listings
      - search: text matched against title and company
    """
    threshold = CONFIG["scoring"]["threshold"]
    if not isinstance(threshold, (int, float)) or threshold < 0:
        threshold = 7.0

    min_score_raw = request.args.get("min_score")
    try:
        min_score = float(min_score_raw) if min_score_raw else None
    except ValueError:
        min_score = None
    remote_only = request.args.get("remote_only") == "1"
    search = request.args.get("search", "").strip() or None
    job_type = request.args.get("job_type", "").strip() or None
    sort = request.args.get("sort", "").strip() or None

    listings = db.get_feed(
        threshold=threshold,
        min_score=min_score,
        remote_only=remote_only,
        search=search,
        job_type=job_type,
        sort=sort,
    )
    job_types = db.get_job_types()
    last_fetch_time = db.get_last_fetch_time()
    new_count = sum(1 for listing in listings if listing["opened_at"] is None)
    return render_template(
        "index.html",
        listings=listings,
        view="feed",
        threshold=threshold,
        min_score=min_score,
        remote_only=remote_only,
        search=search,
        job_type=job_type,
        job_types=job_types,
        sort=sort,
        last_fetch_time=last_fetch_time,
        new_count=new_count,
        config_warnings=_config_warnings(),
        running=_ingest_running(),
    )


@app.route("/feed/fragment")
def feed_fragment():
    """Feed content fragment — returns only the listing cards (or empty state).

    Used by the ``ingestComplete`` HTMX listener to refresh just the
    ``#feed-content`` container after an ingest run completes, without
    reloading the full page (which would destroy the ingest drawer).

    Accepts the same filter query params as ``/``:
      - min_score, remote_only, search, job_type, sort
    """
    threshold = CONFIG["scoring"]["threshold"]
    if not isinstance(threshold, (int, float)) or threshold < 0:
        threshold = 7.0

    min_score_raw = request.args.get("min_score")
    try:
        min_score = float(min_score_raw) if min_score_raw else None
    except ValueError:
        min_score = None
    remote_only = request.args.get("remote_only") == "1"
    search = request.args.get("search", "").strip() or None
    job_type = request.args.get("job_type", "").strip() or None
    sort = request.args.get("sort", "").strip() or None

    listings = db.get_feed(
        threshold=threshold,
        min_score=min_score,
        remote_only=remote_only,
        search=search,
        job_type=job_type,
        sort=sort,
    )
    last_fetch_time = db.get_last_fetch_time()
    new_count = sum(1 for listing in listings if listing["opened_at"] is None)
    resp = make_response(
        render_template(
            "_feed_fragment.html",
            listings=listings,
            threshold=threshold,
            new_count=new_count,
            last_fetch_time=last_fetch_time,
        ),
        200,
    )
    resp.headers["Content-Type"] = "text/html"
    return resp


@app.route("/bookmarks")
def bookmarks():
    """Bookmarked listings only."""
    listings = db.get_bookmarks()
    return render_template(
        "index.html",
        listings=listings,
        view="bookmarks",
        config_warnings=_config_warnings(),
    )


@app.route("/bookmark/<int:listing_id>", methods=["POST"])
def toggle_bookmark(listing_id: int):
    """Toggle the bookmarked state for a listing.

    Delegates to db.toggle_bookmarked(), which performs the flip atomically
    in a single SQL statement so rapid double-clicks cannot produce a net
    no-op. Returns the re-rendered action button group as an HTMX partial.
    """
    listing = db.toggle_bookmarked(listing_id)
    if listing is None:
        return make_response("", 404)
    return render_template("_actions.html", listing=listing)


@app.route("/apply/<int:listing_id>", methods=["POST"])
def toggle_apply(listing_id: int):
    """Toggle the applied state for a listing.

    Delegates to db.toggle_applied(), which performs the flip atomically
    in a single SQL statement so rapid double-clicks cannot produce a net
    no-op. Returns the re-rendered action button group as an HTMX partial.
    """
    listing = db.toggle_applied(listing_id)
    if listing is None:
        return make_response("", 404)
    return render_template("_actions.html", listing=listing)


@app.route("/applied")
def applied():
    """Applied listings — all listings marked as applied, most recent first."""
    listings = db.get_applied()
    return render_template(
        "index.html",
        listings=listings,
        view="applied",
        config_warnings=_config_warnings(),
    )


@app.route("/snippets")
def snippets():
    """Snippet-scored listings — roles scored from short API descriptions rather than full JDs.

    Accepts the same filter query params as the main feed: ``sort``, ``search``,
    ``remote_only``, ``job_type``, and ``min_score``.
    """
    sort = request.args.get("sort", "").strip() or None
    search = request.args.get("search", "").strip() or None
    remote_only = request.args.get("remote_only") == "1"
    job_type = request.args.get("job_type", "").strip() or None
    raw_min_score = request.args.get("min_score", "").strip()
    min_score: float | None = None
    if raw_min_score:
        try:
            min_score = float(raw_min_score)
        except ValueError:
            min_score = None

    threshold = CONFIG["scoring"]["threshold"]
    if not isinstance(threshold, (int, float)) or threshold < 0:
        threshold = 7.0
    job_types = db.get_job_types()
    listings = db.get_snippet_feed(
        threshold=threshold,
        min_score=min_score,
        remote_only=remote_only,
        search=search,
        job_type=job_type,
        sort=sort,
    )
    return render_template(
        "snippets.html",
        listings=listings,
        view="snippets",
        sort=sort,
        search=search,
        remote_only=remote_only,
        job_type=job_type,
        job_types=job_types,
        threshold=threshold,
        min_score=min_score,
        config_warnings=_config_warnings(),
    )


@app.route("/stats")
def stats():
    """API usage and cost statistics, plus runtime version information."""
    data = db.get_usage_stats()
    return render_template(
        "stats.html",
        stats=data,
        view="stats",
        config_warnings=_config_warnings(),
    )


@app.route("/dismiss/<int:listing_id>", methods=["POST"])
def dismiss(listing_id: int):
    """Dismiss a listing.

    Returns an empty 200 response. HTMX is configured to swap `outerHTML`
    on the card element, replacing it with the empty string — this removes
    the card from the DOM without a page reload.
    """
    db.set_dismissed(listing_id, 1)
    return make_response("", 200)


@app.route("/listings/<int:listing_id>/open", methods=["POST"])
def mark_listing_opened(listing_id: int):
    """Mark a listing as opened (first-time expand) and clear its New badge.

    Called fire-and-forget by HTMX when the user expands a card for the first
    time.  The operation is idempotent — if the listing is already marked
    opened, the DB write is a no-op.

    Returns an HTMX out-of-band swap fragment that removes the badge-new element
    from the DOM immediately.  The CSS rule `.card-details[open] .badge-new` is
    kept as a belt-and-suspenders fallback, but some browsers do not trigger a
    style recalculation for <summary> descendants when <details> gains [open],
    so relying solely on CSS is not reliable across all browsers.
    """
    db.mark_opened(listing_id)
    # hx-swap-oob="outerHTML" replaces the target element entirely with the new
    # element.  An empty <span> with the same id effectively removes the badge.
    oob_fragment = f'<span id="badge-new-{listing_id}" hx-swap-oob="outerHTML"></span>'
    return oob_fragment, 200


# ---------------------------------------------------------------------------
# Credential masking — moved to services/provider_schemas.py (Phase 4)
# ---------------------------------------------------------------------------
# _mask_config_keys imported above from services.provider_schemas.

# ---------------------------------------------------------------------------
# Ingestion trigger — module-level handle prevents concurrent runs
# ---------------------------------------------------------------------------
# The canonical copies of these globals live in services/ingest_control.py
# for Phase 5 blueprint use.  This module keeps its own copies so that
# monkeypatch.setattr(app_module, "_ingest_process", ...) in the test suite
# continues to affect the globals that _ingest_running() and ingest_trigger()
# actually read, without requiring any changes to tests/test_ingest_trigger.py.

# Protects concurrent access to _ingest_process and _last_run from waitress
# thread-pool workers.  Any read-modify-write on these globals must hold this
# lock so two simultaneous POST /ingest/trigger requests cannot both pass the
# "not running" check and spawn duplicate subprocesses.
_ingest_lock: threading.Lock = threading.Lock()

# Holds the running Popen handle while ingest.py is active. None when idle.
_ingest_process: subprocess.Popen | None = None

# Legacy handle — no longer written; kept so existing tests/monkeypatches that
# set _ingest_log_file still work without AttributeError.
_ingest_log_file: "object | None" = None

# Stores the result of the most recently completed ingest run.
_last_run: dict | None = None

# Set to True when _ingest_running() first observes that the subprocess has
# exited.  Consumed (cleared back to False) by the first /ingest/status
# response that sends HX-Trigger: ingestComplete, so the event fires exactly
# once per run — not on every subsequent idle poll.
_ingest_just_completed: bool = False

# Maximum number of concurrent SSE connections to /ingest/stream.
# Limited to 2 to prevent resource exhaustion — each connection holds an open
# HTTP connection plus an event queue subscription. Typical use case is 1
# browser tab; 2 allows for tab duplication or a background monitoring process.
MAX_SSE_CONNECTIONS: int = 2

# ---------------------------------------------------------------------------
# Summary parsing + stdout reader — moved to services/ingest_control.py (Phase 4)
# ---------------------------------------------------------------------------
# _INGEST_SUMMARY_RE, _parse_ingest_summary, _stdout_reader are re-exported
# above via:
#   _parse_ingest_summary = ingest_control._parse_ingest_summary
#   _stdout_reader = ingest_control._stdout_reader
# The _INGEST_SUMMARY_RE constant is internal to ingest_control and not
# re-exported (no test or call site imports it directly from app).

_INGEST_SUMMARY_RE = ingest_control._INGEST_SUMMARY_RE


def _ingest_running() -> bool:
    """Return True if an ingest subprocess is currently active.

    Acquires ``_ingest_lock`` before touching shared state so concurrent calls
    from waitress worker threads are serialised.

    Polls the process exit code: if poll() returns None the process is still
    running. If it has exited, read the temp log file to capture stdout, parse
    the summary into ``_last_run``, reset the handle to None so a new run can
    start, and set ``_ingest_just_completed`` so the next /ingest/status
    response fires ``HX-Trigger: ingestComplete`` exactly once.

    Note: this function reads from ``app.py``'s own module-level globals so
    that ``monkeypatch.setattr(app_module, "_ingest_process", mock)`` in the
    test suite reaches the variables this function actually reads.  Phase 5
    will migrate to ``ingest_control._ingest_running()`` when the route
    handlers move to blueprints.
    """
    global _ingest_process, _ingest_log_file, _last_run, _ingest_just_completed
    with _ingest_lock:
        if _ingest_process is None:
            return False
        if _ingest_process.poll() is not None:
            # Process has exited — extract summary from event queue for
            # backward compat.  Clean up legacy log file handle if present
            # (no-op for new PIPE-based runs).
            if _ingest_log_file is not None:
                try:
                    _ingest_log_file.close()
                except (OSError, ValueError):
                    pass
                _ingest_log_file = None
            _last_run = _parse_ingest_summary(event_queue.get_latest_summary())
            _ingest_process = None
            # Mark the running→idle transition so /ingest/status sends
            # HX-Trigger: ingestComplete exactly once (not on every idle poll).
            _ingest_just_completed = True
            return False
        return True


def _stdout_reader(proc: subprocess.Popen) -> None:
    """Daemon thread: read ingest subprocess stdout and push events to the queue.

    Implementation note — kept in ``app.py`` (not moved to a re-export from
    ``services/ingest_control.py``) so that ``monkeypatch.setattr("app.IngestEventParser",
    FakeParser)`` in the test suite patches the parser class that this function
    instantiates.  :mod:`services.ingest_control` holds the canonical Phase 5
    copy; this copy is kept here for backward-compatible test patching only.

    Args:
        proc: Running :class:`subprocess.Popen` handle whose ``stdout`` is
              opened in text mode with ``bufsize=1`` (line-buffered).
    """
    parser = IngestEventParser()
    saw_complete = False
    try:
        # readline() returns '' at EOF — iter sentinel stops on that.
        for raw_line in iter(proc.stdout.readline, ""):
            line = raw_line.rstrip("\n")
            if not line:
                continue
            try:
                event = parser.parse(line)
            except Exception:
                app.logger.exception(
                    "IngestEventParser failed on line: %r", line
                )
                continue
            if event is not None:
                if event["type"] == "complete":
                    saw_complete = True
                event_queue.push(event)
    except Exception:
        app.logger.exception("StdoutReader crashed")
        try:
            proc.kill()
        except OSError:
            pass
        event_queue.push({
            "type": "aborted",
            "source": None,
            "title": None,
            "url": None,
            "detail": {"error": "reader thread crashed"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        return

    # EOF — subprocess exited
    if not saw_complete:
        exit_code = proc.wait()
        event_queue.push({
            "type": "aborted",
            "source": None,
            "title": None,
            "url": None,
            "detail": {"error": f"process exited with code {exit_code}"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })


def _validate_with_timeout(
    validator,
    api_key: str,
    model: str,
) -> tuple:
    """Run *validator(api_key, model)* in a daemon thread with a fixed timeout.

    Implementation note — defined here (not re-exported from
    ``services/provider_schemas.py``) so that
    ``monkeypatch.setattr(app_module, "_VALIDATE_TIMEOUT_SECONDS", N)`` in the
    test suite takes effect at call time.  :mod:`services.provider_schemas`
    holds the canonical Phase 5 copy; this copy is kept for backward-compatible
    test patching only.

    Returns the validator's ``(state, detail)`` tuple, or a synthetic
    ``('unreachable', ...)`` tuple if the call does not complete within
    :data:`_VALIDATE_TIMEOUT_SECONDS`.

    Args:
        validator: Callable ``(api_key, model) -> tuple[str, str | None]``.
        api_key:   Provider API key string.
        model:     Provider model name string.

    Returns:
        ``(state, detail)`` where *state* is one of: ``'valid'``,
        ``'invalid_key'``, ``'unknown_model'``, ``'unreachable'``.
        *detail* is ``None`` on success or a short error string on failure.
    """
    global _VALIDATE_TIMEOUT_SECONDS
    result_holder: list[tuple] = []

    def _target() -> None:
        try:
            result_holder.append(validator(api_key, model))
        except Exception as exc:
            result_holder.append(
                ("unreachable", _sanitise_detail(str(exc), api_key))
            )

    t = threading.Thread(target=_target, daemon=True)
    t.start()
    t.join(timeout=_VALIDATE_TIMEOUT_SECONDS)
    if t.is_alive():
        return (
            "unreachable",
            f"Timed out after {_VALIDATE_TIMEOUT_SECONDS}s",
        )
    return result_holder[0] if result_holder else ("unreachable", None)


def _render_ingest_idle() -> str:
    """Return the HTML partial for the idle 'Run Ingestion' button."""
    return render_template("_ingest_trigger.html", running=False, last_run=_last_run)


def _render_ingest_running() -> str:
    """Return the HTML partial for the in-progress status element."""
    return render_template("_ingest_trigger.html", running=True)


@app.route("/ingest/trigger", methods=["POST"])
def ingest_trigger():
    """Spawn ingest.py as a background subprocess.

    Returns 202 with the 'Running...' HTML partial when the process starts.
    Returns 409 with a JSON error body if a run is already in progress — the
    caller can check Content-Type to distinguish the two response shapes.

    Uses sys.executable so the subprocess runs in the same virtualenv as the
    app server, picking up all installed dependencies automatically.

    stdout and stderr are merged and piped via subprocess.PIPE to a
    StdoutReader daemon thread. The reader parses each line into a structured
    event and pushes it to the global event queue for real-time SSE
    consumption by /ingest/stream subscribers.
    """
    global _ingest_process, _ingest_log_file

    # Build command from optional UI parameters before taking the lock so the
    # critical section stays as short as possible.
    hours_raw = request.form.get("hours", "25").strip()
    rescore = request.form.get("rescore") == "1"

    try:
        hours = int(hours_raw)
    except (ValueError, TypeError):
        hours = 25

    cmd = [sys.executable, "ingest.py", "--hours", str(hours)]
    if rescore:
        cmd.append("--rescore")

    with _ingest_lock:
        # Re-check inside the lock: another thread may have started a process
        # between our pre-lock poll and now.
        if _ingest_process is not None and _ingest_process.poll() is None:
            return jsonify({"error": "already running"}), 409

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,  # line-buffered
                # Force unbuffered output from the child so log lines reach
                # the parent pipe immediately even when stderr is not a tty.
                env={**os.environ, "PYTHONUNBUFFERED": "1", "INGEST_TRIGGER": "manual_ui"},
            )
        except (OSError, PermissionError) as e:
            return jsonify({"error": f"Failed to start ingestion: {e}"}), 500
        event_queue.clear()

        _ingest_process = proc
        _ingest_log_file = None  # no longer used; kept for backward compat

        reader = threading.Thread(
            target=_stdout_reader,
            args=(proc,),
            daemon=True,
        )
        reader.start()

    resp = make_response(_render_ingest_running(), 202)
    resp.headers["Content-Type"] = "text/html"
    return resp


@app.route("/api/ingest/preflight", methods=["GET"])
def ingest_preflight():
    """Pre-flight validation endpoint for the ingest drawer.

    Returns a JSON object describing whether the current configuration is
    valid enough to start an ingest run.  The ingest drawer calls this
    before enabling the "Run Ingestion" button so users learn about
    configuration gaps before submitting the form.

    Returns:
        200 with ``{"ok": true}`` when all enabled sources are fully
        configured.
        422 with ``{"ok": false, "issues": [...]}`` when one or more enabled
        sources have missing or empty required search fields.  Each issue in
        the list has the shape
        ``{"source": "<key>", "missing_fields": ["country", ...]}``.
    """
    issues = _get_search_validation_issues()
    if not issues:
        return jsonify({"ok": True})

    return jsonify({
        "ok": False,
        "issues": [
            {
                "source": issue.source_key,
                "missing_fields": issue.missing_fields,
            }
            for issue in issues
        ],
    }), 422


@app.route("/ingest/status")
def ingest_status():
    """Poll endpoint — returns an HTML partial reflecting current ingest state.

    While the process is running, returns the polling div so HTMX keeps
    refreshing. Once it stops, returns the idle button.

    ``HX-Trigger: ingestComplete`` is sent only on the running→idle transition
    (i.e. the first idle response after a run finishes), not on every
    subsequent idle poll. This prevents the ``ingestComplete`` listener from
    firing repeatedly and causing an infinite refresh loop.
    """
    global _ingest_just_completed
    running = _ingest_running()
    html = _render_ingest_running() if running else _render_ingest_idle()
    resp = make_response(html, 200)
    resp.headers["Content-Type"] = "text/html"
    if not running and _ingest_just_completed:
        # Consume the flag — subsequent idle polls will NOT carry this header.
        _ingest_just_completed = False
        resp.headers["HX-Trigger"] = "ingestComplete"
    return resp


@app.route("/ingest/stream")
def ingest_stream():
    """SSE endpoint streaming real-time ingest events.

    Yields events from the EventQueue in SSE wire format. Supports replay
    via Last-Event-ID header (format: "{run_id}:{event_id}"). Returns 429
    if max connections exceeded.
    """
    if event_queue.connection_count >= MAX_SSE_CONNECTIONS:
        return jsonify({"error": "too many connections"}), 429

    # Parse Last-Event-ID: "{run_id}:{event_id}" or just "{event_id}"
    last_event_id_raw = request.headers.get("Last-Event-ID", "")
    last_id = 0
    if last_event_id_raw:
        parts = last_event_id_raw.rsplit(":", 1)
        if len(parts) == 2:
            req_run_id, id_str = parts
            try:
                candidate_id = int(id_str)
            except ValueError:
                candidate_id = 0
            # Stale run_id → replay from beginning
            if req_run_id == event_queue.run_id:
                last_id = candidate_id
        else:
            try:
                last_id = int(parts[0])
            except ValueError:
                last_id = 0

    def generate():
        event_queue.connect()
        try:
            for event in event_queue.subscribe(last_id=last_id):
                eid = event.get("id", 0)
                run_id = event.get("run_id", event_queue.run_id)
                data = json.dumps(event, separators=(",", ":"))
                yield f"id: {run_id}:{eid}\ndata: {data}\n\n"
        finally:
            event_queue.disconnect()

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


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
            _current_providers = _load_providers_safe()
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
    providers_data = _load_providers_safe()
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
    search_issues = _get_search_validation_issues()

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
        providers_dict = _load_providers_safe()
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
    providers_dict = _load_providers_safe()
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
    providers_data = _load_providers_safe()
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
    providers_data = _load_providers_safe()
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
            providers_data = _load_providers_safe()
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
