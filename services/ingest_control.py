"""Ingest subprocess lifecycle management and SSE-related module state.

This module owns all mutable globals that track a running ``ingest.py``
subprocess plus the helper functions that operate on them.  It is Flask-free:
all logging uses :func:`logging.getLogger`, not ``app.logger``.

These symbols are the canonical source of truth for Phase 5 (blueprints).
During Phase 4 the route handlers in ``app.py`` still maintain their own
parallel copies of the mutable globals for backward-compatibility with the
test suite; Phase 5 will migrate the handlers to use this module directly.

Thread safety
-------------
Three threads touch the mutable globals in this module:

1. The request thread that spawns the ingest subprocess
   (:func:`~app.ingest_trigger`).
2. The :func:`_stdout_reader` daemon thread.
3. The request thread that polls ``/ingest/status`` via
   :func:`_ingest_running`.

All read-modify-write sequences on the shared globals must hold
:data:`_ingest_lock`.

Module-attribute access pattern
--------------------------------
Callers that need to rebind the mutable globals (``_ingest_process``,
``_last_run``, etc.) must import this module as a whole and qualify the name::

    from services import ingest_control
    ...
    ingest_control._ingest_process = proc   # correct

A bare ``from services.ingest_control import _ingest_process`` captures the
value at import time (``None``) and **never sees rebindings**.  Do not use it.

Public API
----------
Mutable subprocess-state globals
    :data:`_ingest_lock`, :data:`_ingest_process`, :data:`_ingest_log_file`,
    :data:`_last_run`, :data:`_ingest_just_completed`

SSE connection cap
    :data:`MAX_SSE_CONNECTIONS`

Summary parsing
    :data:`_INGEST_SUMMARY_RE`, :func:`_parse_ingest_summary`

Subprocess I/O
    :func:`_stdout_reader`

Lifecycle query
    :func:`_ingest_running`
"""

from __future__ import annotations

import logging
import re
import subprocess
import threading
from datetime import datetime, timezone
from typing import Optional

from ingest_events import event_queue

_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Mutable subprocess-state globals
# ---------------------------------------------------------------------------

_ingest_lock: threading.Lock = threading.Lock()
"""Mutex protecting all read-modify-write sequences on the globals below.

Any code that reads *and then writes* ``_ingest_process``, ``_last_run``,
``_ingest_just_completed``, or ``_ingest_log_file`` must hold this lock.
"""

_ingest_process: Optional[subprocess.Popen] = None
"""Running ``Popen`` handle while ``ingest.py`` is active; ``None`` when idle."""

_ingest_log_file: Optional[object] = None
"""Legacy handle — no longer written.

Kept so existing tests and monkeypatches that set ``_ingest_log_file`` work
without :exc:`AttributeError`.
"""

_last_run: Optional[dict] = None
"""Parsed result dict from the most recently completed ingest run.

Shape: ``{"new": int, "filtered": int, "errors": int,
"completed_at": datetime}``.  ``None`` before the first run completes.
"""

_ingest_just_completed: bool = False
"""One-shot flag: set when :func:`_ingest_running` first sees the subprocess
exit.  Consumed by the first ``/ingest/status`` idle response that sends
``HX-Trigger: ingestComplete``, then reset to ``False``.
"""

# ---------------------------------------------------------------------------
# SSE connection cap
# ---------------------------------------------------------------------------

MAX_SSE_CONNECTIONS: int = 2
"""Maximum concurrent SSE connections to ``/ingest/stream``.

Limited to 2 to prevent resource exhaustion — each connection holds an open
HTTP connection plus an event-queue subscription.  Typical use is 1 browser
tab; 2 allows for tab duplication or a background monitoring process.
"""

# ---------------------------------------------------------------------------
# Summary parsing
# ---------------------------------------------------------------------------

_INGEST_SUMMARY_RE = re.compile(
    r"Run complete:\s*\d+\s*source\(s\)\s*\|"   # source count prefix
    r"\s*(\d+)\s*fetched\s*\|"                   # group 1 = fetched
    r".*?(\d+)\s*pre-filtered\s*\|"              # group 2 = pre-filtered
    r".*?scored\s*\((\d+)\s*failed\)",            # group 3 = score-failed
    re.IGNORECASE,
)
"""Compiled regex for the ``Run complete:`` summary line emitted by
``ingest.py`` at the end of each run.
"""


def _parse_ingest_summary(output: str) -> dict:
    """Parse the summary line from ``ingest.py`` stdout and return a result dict.

    Expected format (single line emitted by ``ingest.py run()``)::

        Run complete: 25 fetched | 10 pre-filtered | 5 dupes skipped |
                      7 scored (3 failed) | 0 scrape skipped |
                      0 scrape fallbacks | ~1,234 tok | ~$0.0012

    Extracted fields:

    * ``new``      — listings fetched from the job source this run
    * ``filtered`` — listings dropped by the pre-filter
    * ``errors``   — listings that failed scoring

    If the pattern is not found (e.g. the process was killed or produced no
    output), all counts default to zero so the template always has a safe
    value.

    Args:
        output: Full stdout of the completed ingest subprocess.

    Returns:
        Dict with keys ``new``, ``filtered``, ``errors``, and
        ``completed_at`` (a UTC :class:`datetime`).
    """
    m = _INGEST_SUMMARY_RE.search(output)
    if m:
        return {
            "new": int(m.group(1)),
            "filtered": int(m.group(2)),
            "errors": int(m.group(3)),
            "completed_at": datetime.now(timezone.utc),
        }
    return {
        "new": 0,
        "filtered": 0,
        "errors": 0,
        "completed_at": datetime.now(timezone.utc),
    }


# ---------------------------------------------------------------------------
# Subprocess I/O
# ---------------------------------------------------------------------------


def _stdout_reader(proc: subprocess.Popen) -> None:
    """Daemon thread: read subprocess stdout and push structured events to the queue.

    Reads ``ingest.py`` stdout line-by-line, passes each line through
    :class:`ingest_events.IngestEventParser`, and pushes resulting events to
    the global :data:`ingest_events.event_queue`.

    Error handling:

    * Per-line parse errors are logged and skipped — the reader does not die
      on a single malformed line.
    * An unhandled exception in the outer loop kills the subprocess and pushes
      an ``"aborted"`` event so SSE clients disconnect cleanly.
    * EOF without a ``"complete"`` event pushes an ``"aborted"`` event with
      the process exit code.

    Args:
        proc: Running :class:`subprocess.Popen` handle whose ``stdout`` is
              opened in text mode with ``bufsize=1`` (line-buffered).
    """
    from ingest_events import IngestEventParser  # local import avoids cycle

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
                _logger.exception(
                    "IngestEventParser failed on line: %r", line
                )
                continue
            if event is not None:
                if event["type"] == "complete":
                    saw_complete = True
                event_queue.push(event)
    except Exception:
        _logger.exception("StdoutReader crashed")
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


# ---------------------------------------------------------------------------
# Lifecycle query
# ---------------------------------------------------------------------------


def _ingest_running() -> bool:
    """Return ``True`` if an ingest subprocess is currently active.

    Acquires :data:`_ingest_lock` before touching shared state so concurrent
    calls from waitress worker threads are serialised.

    Polls the process exit code: if ``poll()`` returns ``None`` the process
    is still running.  If it has exited, reads the summary from the event
    queue, parses it into :data:`_last_run`, resets the handle to ``None``
    so a new run can start, and sets :data:`_ingest_just_completed` so the
    next ``/ingest/status`` response fires ``HX-Trigger: ingestComplete``
    exactly once.

    Returns:
        ``True`` while the subprocess is alive; ``False`` otherwise.
    """
    global _ingest_process, _ingest_log_file, _last_run, _ingest_just_completed
    with _ingest_lock:
        if _ingest_process is None:
            return False
        if _ingest_process.poll() is not None:
            # Process has exited — extract summary from event queue for
            # backward compat.  Clean up legacy log file handle if present.
            if _ingest_log_file is not None:
                try:
                    _ingest_log_file.close()
                except (OSError, ValueError):
                    pass
                _ingest_log_file = None
            _last_run = _parse_ingest_summary(
                event_queue.get_latest_summary()
            )
            _ingest_process = None
            # Mark the running→idle transition so /ingest/status sends
            # HX-Trigger: ingestComplete exactly once.
            _ingest_just_completed = True
            return False
        return True
