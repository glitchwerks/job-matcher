"""Ingest blueprint — subprocess control, status polling, and SSE stream.

Owns the 4 routes that manage and observe the background ingest process:
  POST  /ingest/trigger            spawn ingest.py subprocess
  GET   /api/ingest/preflight      validate config before triggering
  GET   /ingest/status             HTMX poll endpoint (idle / running)
  GET   /ingest/stream             SSE stream of ingest events

Also owns two private helpers used only by these routes:
  _render_ingest_idle()    — idle-state HTML partial
  _render_ingest_running() — running-state HTML partial

Mutable subprocess globals live in ``services/ingest_control``.  Every
access to them goes through the module object (``ingest_control.*``) to
avoid the Python rebinding hazard: a bare ``from … import _ingest_process``
would capture the value at import time, so subsequent mutations in
``ingest_control`` would not be visible here.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading

from flask import (
    Blueprint,
    Response,
    jsonify,
    make_response,
    render_template,
    request,
    stream_with_context,
)

from ingest_events import event_queue
from services import ingest_control
from services.provider_schemas import _get_search_validation_issues

ingest_bp = Blueprint("ingest", __name__)


def _get_providers_path() -> str:
    """Return the current _PROVIDERS_PATH from ``services.profile_store``.

    Reads the attribute from the canonical module at call time so that
    ``monkeypatch.setattr(profile_store, "_PROVIDERS_PATH", ...)`` in
    the test suite takes effect for ingest routes without any coupling
    to ``app.py``.

    Returns:
        The providers.json file path string.
    """
    import services.profile_store as _ps
    return _ps._PROVIDERS_PATH


def _get_config_path() -> str:
    """Return the current _CONFIG_PATH from ``services.profile_store``.

    Reads the attribute from the canonical module at call time so that
    ``monkeypatch.setattr(profile_store, "_CONFIG_PATH", ...)`` in the
    test suite takes effect for ingest routes without any coupling to
    ``app.py``.

    Returns:
        The config.json file path string.
    """
    import services.profile_store as _ps
    return _ps._CONFIG_PATH


def _render_ingest_idle() -> str:
    """Return the HTML partial for the idle 'Run Ingestion' button.

    Returns:
        Rendered ``_ingest_trigger.html`` template with running=False.
    """
    return render_template(
        "_ingest_trigger.html",
        running=False,
        last_run=ingest_control._last_run,
    )


def _render_ingest_running() -> str:
    """Return the HTML partial for the in-progress status element.

    Returns:
        Rendered ``_ingest_trigger.html`` template with running=True.
    """
    return render_template("_ingest_trigger.html", running=True)


@ingest_bp.route("/ingest/trigger", methods=["POST"])
def ingest_trigger():
    """Spawn ingest.py as a background subprocess.

    Returns 202 with the 'Running...' HTML partial when the process
    starts. Returns 409 with a JSON error body if a run is already in
    progress — the caller can check Content-Type to distinguish the two
    response shapes.

    Uses sys.executable so the subprocess runs in the same virtualenv as
    the app server, picking up all installed dependencies automatically.

    stdout and stderr are merged and piped via subprocess.PIPE to a
    StdoutReader daemon thread. The reader parses each line into a
    structured event and pushes it to the global event queue for
    real-time SSE consumption by /ingest/stream subscribers.

    Returns:
        202 HTML partial when a new process is started.
        409 JSON when a process is already running.
        500 JSON when subprocess.Popen fails.
    """
    # Build command from optional UI parameters before taking the lock
    # so the critical section stays as short as possible.
    hours_raw = request.form.get("hours", "25").strip()
    rescore = request.form.get("rescore") == "1"

    try:
        hours = int(hours_raw)
    except (ValueError, TypeError):
        hours = 25

    cmd = [sys.executable, "ingest.py", "--hours", str(hours)]
    if rescore:
        cmd.append("--rescore")

    with ingest_control._ingest_lock:
        # Re-check inside the lock: another thread may have started a
        # process between our pre-lock poll and now.
        if (
            ingest_control._ingest_process is not None
            and ingest_control._ingest_process.poll() is None
        ):
            return jsonify({"error": "already running"}), 409

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,  # line-buffered
                # Force unbuffered output from the child so log lines
                # reach the parent pipe immediately even when stderr is
                # not a tty.
                env={
                    **os.environ,
                    "PYTHONUNBUFFERED": "1",
                    "INGEST_TRIGGER": "manual_ui",
                },
            )
        except (OSError, PermissionError) as e:
            return jsonify({"error": f"Failed to start ingestion: {e}"}), 500
        event_queue.clear()

        ingest_control._ingest_process = proc
        # _ingest_log_file no longer used; kept for backward compat.
        ingest_control._ingest_log_file = None

        reader = threading.Thread(
            target=ingest_control._stdout_reader,
            args=(proc,),
            daemon=True,
        )
        reader.start()

    resp = make_response(_render_ingest_running(), 202)
    resp.headers["Content-Type"] = "text/html"
    return resp


@ingest_bp.route("/api/ingest/preflight", methods=["GET"])
def ingest_preflight():
    """Pre-flight validation endpoint for the ingest drawer.

    Returns a JSON object describing whether the current configuration
    is valid enough to start an ingest run.  The ingest drawer calls
    this before enabling the "Run Ingestion" button so users learn about
    configuration gaps before submitting the form.

    Returns:
        200 with ``{"ok": true}`` when all enabled sources are fully
        configured.
        422 with ``{"ok": false, "issues": [...]}`` when one or more
        enabled sources have missing or empty required search fields.
        Each issue in the list has the shape
        ``{"source": "<key>", "missing_fields": ["country", ...]}``.
    """
    issues = _get_search_validation_issues(
        providers_path=_get_providers_path(),
        config_path=_get_config_path(),
    )
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


@ingest_bp.route("/ingest/status")
def ingest_status():
    """Poll endpoint — returns an HTML partial reflecting current ingest state.

    While the process is running, returns the polling div so HTMX keeps
    refreshing. Once it stops, returns the idle button.

    ``HX-Trigger: ingestComplete`` is sent only on the running→idle
    transition (i.e. the first idle response after a run finishes), not
    on every subsequent idle poll. This prevents the ``ingestComplete``
    listener from firing repeatedly and causing an infinite refresh loop.

    Returns:
        200 HTML partial (running or idle depending on state).
    """
    running = ingest_control._ingest_running()
    html = _render_ingest_running() if running else _render_ingest_idle()
    resp = make_response(html, 200)
    resp.headers["Content-Type"] = "text/html"
    if not running and ingest_control._ingest_just_completed:
        # Consume the flag — subsequent idle polls will NOT carry this
        # header.
        ingest_control._ingest_just_completed = False
        resp.headers["HX-Trigger"] = "ingestComplete"
    return resp


@ingest_bp.route("/ingest/stream")
def ingest_stream():
    """SSE endpoint streaming real-time ingest events.

    Yields events from the EventQueue in SSE wire format. Supports
    replay via Last-Event-ID header (format: "{run_id}:{event_id}").
    Returns 429 if the max-connections limit is reached.

    Returns:
        200 text/event-stream response (streaming).
        429 JSON when MAX_SSE_CONNECTIONS is exceeded.
    """
    if event_queue.connection_count >= ingest_control.MAX_SSE_CONNECTIONS:
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
            # Stale run_id → replay from beginning.
            if req_run_id == event_queue.run_id:
                last_id = candidate_id
        else:
            try:
                last_id = int(parts[0])
            except ValueError:
                last_id = 0

    def generate():
        """Yield SSE-formatted events from the event queue."""
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
