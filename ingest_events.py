"""Ingest event parsing and queue infrastructure for the live log stream.

This module is the bridge between raw ingest.py stdout and the SSE endpoint.
The parser converts log lines into structured event dicts; the queue stores
them thread-safely for consumption by SSE subscribers.
"""

from __future__ import annotations

import re
import threading
import uuid
from datetime import datetime, timezone
from typing import Generator

# ---------------------------------------------------------------------------
# Event parser
# ---------------------------------------------------------------------------

# Logging prefix: "INFO ingest: " or "WARNING ingest: "
_LOG_PREFIX_RE = re.compile(r"^(?:INFO|WARNING|ERROR|DEBUG)\s+\w+:\s*")

# Message patterns (matched AFTER prefix is stripped)
_SCORED_RE = re.compile(r"SCORED\s+(\d+)/10\s+\[(.+?)\]\s+(.+)")
_FILTERED_RE = re.compile(r"FILTERED\s+\[(.+?)\]\s+(.+?)\s+\u2014\s+(.+)")
_DUPE_RE = re.compile(r"DUPE\s+\[(.+?)\]\s+(.+)")
_SCORE_FAILED_RE = re.compile(r"SCORE FAILED\s+\[(.+?)\]\s+(.+)")
_SCRAPE_FALLBACK_RE = re.compile(r"SCRAPE FALLBACK\s+\[(.+?)\]\s+(.+)")
_SCRAPE_SKIP_RE = re.compile(r"SCRAPE SKIP\s+\((\w+)\)\s+\[(.+?)\]\s+(.+)")
_FETCHED_RE = re.compile(r"Fetched\s+(\d+)\s+listing\(s\)\s+from\s+(.+)")
_RUN_COMPLETE_RE = re.compile(r"Run complete:\s+.+")
_RESCORED_RE = re.compile(r"RESCORED\s+(\d+)/10\s+(.+)")
_RESCORE_FAILED_RE = re.compile(r"RESCORE FAILED\s+(.+)")
_RESCORE_COMPLETE_RE = re.compile(r"Rescore complete:\s+.+")


class IngestEventParser:
    """Stateful parser that converts raw log lines into structured events.

    Tracks a scrape-fallback flag that propagates to the next scored event so
    the scored event can be annotated with ``scraped=False``.  The
    ``scrape_fallback`` log line also emits its own event so the drawer can
    display it in the live stream.
    """

    def __init__(self) -> None:
        self._scrape_fallback = False

    def parse(self, line: str) -> dict | None:
        """Parse a single log line into a structured event dict.

        Returns None for unrecognised or irrelevant lines.
        """
        stripped = _LOG_PREFIX_RE.sub("", line).strip()
        if not stripped:
            return None

        now = datetime.now(timezone.utc).isoformat()

        # -- SCRAPE FALLBACK (sets flag, emits event) --
        m = _SCRAPE_FALLBACK_RE.match(stripped)
        if m:
            self._scrape_fallback = True
            return {
                "type": "scrape_fallback",
                "source": m.group(1),
                "title": m.group(2),
                "url": None,
                "detail": {},
                "timestamp": now,
            }

        # -- SCORED --
        m = _SCORED_RE.match(stripped)
        if m:
            scraped = not self._scrape_fallback
            self._scrape_fallback = False
            return {
                "type": "scored",
                "source": m.group(2),
                "title": m.group(3),
                "url": None,
                "detail": {
                    "score": int(m.group(1)),
                    "scraped": scraped,
                },
                "timestamp": now,
            }

        # -- FILTERED --
        m = _FILTERED_RE.match(stripped)
        if m:
            return {
                "type": "filtered",
                "source": m.group(1),
                "title": m.group(2),
                "url": None,
                "detail": {"reason": m.group(3)},
                "timestamp": now,
            }

        # -- DUPE --
        m = _DUPE_RE.match(stripped)
        if m:
            return {
                "type": "dupe",
                "source": m.group(1),
                "title": m.group(2),
                "url": None,
                "detail": {},
                "timestamp": now,
            }

        # -- SCORE FAILED --
        m = _SCORE_FAILED_RE.match(stripped)
        if m:
            return {
                "type": "score_failed",
                "source": m.group(1),
                "title": m.group(2),
                "url": None,
                "detail": {},
                "timestamp": now,
            }

        # -- SCRAPE SKIP --
        m = _SCRAPE_SKIP_RE.match(stripped)
        if m:
            return {
                "type": "scrape_skip",
                "source": m.group(2),
                "title": m.group(3),
                "url": None,
                "detail": {"reason": m.group(1)},
                "timestamp": now,
            }

        # -- FETCHED --
        m = _FETCHED_RE.match(stripped)
        if m:
            return {
                "type": "fetched",
                "source": m.group(2),
                "title": None,
                "url": None,
                "detail": {"fetched_count": int(m.group(1))},
                "timestamp": now,
            }

        # -- RUN COMPLETE --
        m = _RUN_COMPLETE_RE.match(stripped)
        if m:
            return {
                "type": "complete",
                "source": None,
                "title": None,
                "url": None,
                "detail": {"summary": stripped},
                "timestamp": now,
            }

        # -- RESCORED --
        m = _RESCORED_RE.match(stripped)
        if m:
            scraped = not self._scrape_fallback
            self._scrape_fallback = False
            return {
                "type": "rescored",
                "source": None,
                "title": m.group(2),
                "url": None,
                "detail": {
                    "score": int(m.group(1)),
                    "scraped": scraped,
                },
                "timestamp": now,
            }

        # -- RESCORE FAILED --
        m = _RESCORE_FAILED_RE.match(stripped)
        if m:
            return {
                "type": "rescore_failed",
                "source": None,
                "title": m.group(1),
                "url": None,
                "detail": {},
                "timestamp": now,
            }

        # -- RESCORE COMPLETE --
        m = _RESCORE_COMPLETE_RE.match(stripped)
        if m:
            return {
                "type": "complete",
                "source": None,
                "title": None,
                "url": None,
                "detail": {"summary": stripped},
                "timestamp": now,
            }

        return None


# ---------------------------------------------------------------------------
# Event queue
# ---------------------------------------------------------------------------

_TERMINAL_TYPES = frozenset({"complete", "aborted"})


class EventQueue:
    """Thread-safe event store shared between the StdoutReader and SSE endpoint.

    Events are pushed by the reader thread and consumed by SSE generators via
    subscribe(). A single global instance is used in production; tests create
    their own instances.

    Args:
        max_size:   Maximum number of events retained in the buffer.
        idle_grace: Seconds to wait for the first event before yielding
                    ``idle`` in subscribe().  Default 3.0 s covers typical
                    subprocess startup latency.  Set to 0 in tests that
                    exercise the idle path to avoid waiting.
    """

    def __init__(self, max_size: int = 5000, idle_grace: float = 3.0) -> None:
        self._lock = threading.Lock()
        self._events: list[dict] = []
        self._next_id = 1
        self._max_size = max_size
        self._idle_grace = idle_grace
        self._new_event = threading.Event()
        self.run_id: str = str(uuid.uuid4())
        self._connection_count = 0
        self._cleanup_timer: threading.Timer | None = None

    @property
    def connection_count(self) -> int:
        return self._connection_count

    def connect(self) -> None:
        """Increment the active SSE connection counter."""
        with self._lock:
            self._connection_count += 1

    def disconnect(self) -> None:
        """Decrement the active SSE connection counter.

        When the count reaches zero and a terminal event exists, starts a
        60-second cleanup timer. If no new connections arrive before it fires,
        the queue is cleared to free memory.
        """
        with self._lock:
            self._connection_count = max(0, self._connection_count - 1)
            if self._connection_count == 0 and self._has_terminal_unlocked():
                self._start_cleanup_timer()

    def _start_cleanup_timer(self) -> None:
        """Schedule queue cleanup 60s after last SSE disconnect."""
        if self._cleanup_timer is not None:
            self._cleanup_timer.cancel()
            self._cleanup_timer = None
        self._cleanup_timer = threading.Timer(60.0, self._cleanup_if_idle)
        self._cleanup_timer.daemon = True
        self._cleanup_timer.start()

    def _cleanup_if_idle(self) -> None:
        """Clear the queue if no SSE connections are active."""
        with self._lock:
            if self._connection_count == 0:
                self._events.clear()
                self._next_id = 1
                self._new_event.clear()

    def push(self, event: dict) -> None:
        """Append an event with an auto-incrementing ID and wake subscribers.

        Terminal events (complete/aborted) are never subject to eviction —
        they must always be reachable so subscribers can terminate cleanly.
        Non-terminal events are evicted oldest-first when the buffer exceeds
        max_size.
        """
        with self._lock:
            event = {**event, "id": self._next_id, "run_id": self.run_id}
            self._next_id += 1
            self._events.append(event)
            # Evict oldest *non-terminal* event when over cap.
            #
            # INVARIANT: terminal events (complete/aborted) are sacred — they must
            # never be evicted regardless of what type is being pushed.  The original
            # plan spec guarded only on the *incoming* event type, which is wrong:
            # a non-terminal push can still evict a terminal event that is already
            # sitting at the front of the queue.  We instead walk from the front and
            # remove the first non-terminal we find.  If the queue is entirely
            # terminals we prefer exceeding max_size over losing a run-end signal
            # (dropping complete/aborted leaves SSE clients spinning forever).
            if len(self._events) > self._max_size:
                # Search only the events that existed *before* this push (all but
                # the last element).  The newly-appended event must never evict
                # itself — that would silently drop the event being pushed.
                for i, e in enumerate(self._events[:-1]):
                    if e["type"] not in _TERMINAL_TYPES:
                        del self._events[i]
                        break
                # If no non-terminal was found among prior events the queue is all
                # terminals; leave it over-cap rather than evict a sacred event.
            self._new_event.set()

    def clear(self) -> None:
        """Reset for a new ingest run."""
        with self._lock:
            self._events.clear()
            self._next_id = 1
            self.run_id = str(uuid.uuid4())
            self._new_event.clear()

    def _has_terminal_unlocked(self) -> bool:
        """Check if the queue contains a terminal event. Caller must hold _lock."""
        return any(e["type"] in _TERMINAL_TYPES for e in self._events)

    def has_terminal(self) -> bool:
        """Check if the queue contains a terminal event."""
        with self._lock:
            return self._has_terminal_unlocked()

    def is_empty(self) -> bool:
        """Check if the queue has no events."""
        with self._lock:
            return len(self._events) == 0

    def get_latest_summary(self) -> str:
        """Return the summary from the most recent complete event, or '' if none."""
        with self._lock:
            for ev in reversed(self._events):
                if ev.get("type") == "complete" and ev.get("detail", {}).get("summary"):
                    return ev["detail"]["summary"]
        return ""

    def subscribe(self, last_id: int = 0, idle_grace: float | None = None) -> Generator[dict, None, None]:
        """Yield events from last_id onward, blocking when caught up.

        - Normal path: events with id > last_id are yielded in order; when
          caught up the generator blocks on ``_new_event`` until new events
          arrive.  ``last_id`` enables resume after a reconnect by skipping
          events the client has already seen.
        - Idle path: if the queue is empty and no run is active (no terminal
          event present), the generator waits up to ``idle_grace`` seconds for
          events to arrive before yielding a synthetic ``idle`` event.  This
          grace period prevents a race condition where a run has just started
          (Popen succeeded) but the subprocess hasn't emitted its first log
          line yet — without it, the SSE client would receive ``idle``
          immediately and close the connection, missing all subsequent events.
          The generator returns after yielding ``idle``.
        - Termination: the generator returns after yielding a terminal event
          (``complete`` or ``aborted``), signalling the SSE response to close.

        Args:
            last_id:    Resume cursor; events with id <= last_id are skipped.
            idle_grace: Seconds to wait for events before declaring idle.
                        Defaults to ``self._idle_grace`` (set at construction,
                        default 3.0 s).  Pass 0 in tests that exercise the
                        idle path to avoid sleeping.
        """
        if idle_grace is None:
            idle_grace = self._idle_grace

        # Empty queue with no active run -> wait briefly for a run to start
        # before declaring idle.  A run may have just been launched (Popen
        # returned) but not yet produced any log output, causing a transient
        # empty-queue window.  Without this grace period the SSE client
        # receives 'idle' and closes the connection, silently dropping every
        # event from the run.
        if self.is_empty() and not self.has_terminal():
            self._new_event.wait(timeout=idle_grace)
            # If still empty after the grace period, declare idle.
            if self.is_empty() and not self.has_terminal():
                yield {
                    "id": 0,
                    "run_id": self.run_id,
                    "type": "idle",
                    "source": None,
                    "title": None,
                    "url": None,
                    "detail": {},
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
                return
            # Events arrived during the grace period — fall through to the
            # normal while True loop below.

        while True:
            # Grab pending events beyond our cursor
            with self._lock:
                pending = [e for e in self._events if e["id"] > last_id]
                self._new_event.clear()

            for event in pending:
                yield event
                last_id = event["id"]
                if event["type"] in _TERMINAL_TYPES:
                    return

            # Block until new events arrive or timeout
            if not self._new_event.wait(timeout=1.0):
                # Timeout — check if a terminal event exists that we already yielded
                if self.has_terminal():
                    return


# Module-level singleton for production use
event_queue = EventQueue()
