# Ingest Log Stream Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add real-time streaming of ingest pipeline events to the web UI via a slide-out drawer, so users can watch listings being fetched, filtered, scored, or failed as the ingest runs.

**Architecture:** `ingest.py` subprocess stdout is piped (not temp-filed) to a `StdoutReader` daemon thread in `app.py`, which feeds lines through an `IngestEventParser` into a thread-safe `EventQueue`. A new `GET /ingest/stream` SSE endpoint yields events from the queue. The frontend opens an `EventSource`, renders events in a slide-out drawer with rolling tallies and per-source breakdown.

**Tech Stack:** Python 3 (Flask, threading, subprocess, re, uuid), Server-Sent Events, vanilla JS, HTMX, CSS transitions.

**Spec:** `docs/superpowers/specs/2026-04-07-ingest-log-stream-design.md`

**Issues:** #93 → #94 → #95 → #96

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `ingest_events.py` | CREATE | `IngestEventParser` (stateful log line parser) + `EventQueue` (thread-safe store) |
| `app.py` | MODIFY | `StdoutReader` thread, `GET /ingest/stream` SSE endpoint, subprocess piping change |
| `templates/_ingest_drawer.html` | CREATE | Drawer partial (shell, event list, tally footer, FAB) |
| `templates/index.html` | MODIFY | Include drawer partial |
| `static/ingest-drawer.js` | CREATE | Drawer JS: EventSource lifecycle, event rendering, auto-scroll, tally |
| `static/style.css` | MODIFY | Drawer component styles |
| `docs/STYLE_GUIDE.md` | MODIFY | Document new drawer component classes |
| `tests/test_ingest_events.py` | CREATE | Unit tests for parser + queue |
| `tests/test_ingest_stream.py` | CREATE | Integration tests for SSE endpoint |

---

## Task 1: IngestEventParser — Tests & Implementation (Issue #93, part 1)

**Files:**
- Create: `tests/test_ingest_events.py`
- Create: `ingest_events.py`

### Step 1.1: Write failing tests for log line parsing

- [x] Create `tests/test_ingest_events.py` with `TestIngestEventParser`:

```python
"""Unit tests for IngestEventParser and EventQueue."""

import pytest

from ingest_events import IngestEventParser


class TestIngestEventParser:
    """Tests for stateful log-line → structured-event parsing."""

    def setup_method(self):
        self.parser = IngestEventParser()

    # -- SCORED --
    def test_scored_event(self):
        line = "INFO ingest: SCORED 8/10  [Adzuna] Senior Python Developer"
        event = self.parser.parse(line)
        assert event is not None
        assert event["type"] == "scored"
        assert event["detail"]["score"] == 8
        assert event["source"] == "Adzuna"
        assert event["title"] == "Senior Python Developer"
        assert event["detail"]["scraped"] is True  # default when no fallback

    def test_scored_after_scrape_fallback(self):
        self.parser.parse("INFO ingest: SCRAPE FALLBACK  [Adzuna] Some Job")
        event = self.parser.parse("INFO ingest: SCORED 6/10  [Adzuna] Some Job")
        assert event["detail"]["scraped"] is False
        # Flag resets after use
        next_event = self.parser.parse("INFO ingest: SCORED 9/10  [Adzuna] Another Job")
        assert next_event["detail"]["scraped"] is True

    # -- FILTERED --
    def test_filtered_event(self):
        line = "INFO ingest: FILTERED  [Jooble] Junior Dev — title_exclude"
        event = self.parser.parse(line)
        assert event["type"] == "filtered"
        assert event["source"] == "Jooble"
        assert event["title"] == "Junior Dev"
        assert event["detail"]["reason"] == "title_exclude"

    def test_filtered_geo(self):
        line = "INFO ingest: FILTERED  [Adzuna] Remote Job — outside 80km radius"
        event = self.parser.parse(line)
        assert event["type"] == "filtered"
        assert event["detail"]["reason"] == "outside 80km radius"

    # -- DUPE --
    def test_dupe_event(self):
        line = "INFO ingest: DUPE      [USAJobs] Already Seen Role"
        event = self.parser.parse(line)
        assert event["type"] == "dupe"
        assert event["source"] == "USAJobs"
        assert event["title"] == "Already Seen Role"

    # -- SCORE FAILED --
    def test_score_failed_event(self):
        line = "WARNING ingest: SCORE FAILED  [Adzuna] Bad Listing"
        event = self.parser.parse(line)
        assert event["type"] == "score_failed"
        assert event["source"] == "Adzuna"
        assert event["title"] == "Bad Listing"

    # -- SCRAPE SKIP --
    def test_scrape_skip_full(self):
        line = "INFO ingest: SCRAPE SKIP (full) [JSearch] Full Desc Job"
        event = self.parser.parse(line)
        assert event["type"] == "scrape_skip"
        assert event["source"] == "JSearch"
        assert event["title"] == "Full Desc Job"
        assert event["detail"]["reason"] == "full"

    def test_scrape_skip_snippet(self):
        line = "INFO ingest: SCRAPE SKIP (snippet) [JSearch] Short Job"
        event = self.parser.parse(line)
        assert event["type"] == "scrape_skip"
        assert event["detail"]["reason"] == "snippet"

    # -- SCRAPE FALLBACK (sets flag, no event returned) --
    def test_scrape_fallback_returns_none(self):
        line = "INFO ingest: SCRAPE FALLBACK  [Adzuna] Fallback Job"
        event = self.parser.parse(line)
        assert event is None  # flag set, no event emitted

    # -- FETCHED --
    def test_fetched_event(self):
        line = "INFO ingest: Fetched 47 listing(s) from Adzuna"
        event = self.parser.parse(line)
        assert event["type"] == "fetched"
        assert event["source"] == "Adzuna"
        assert event["detail"]["fetched_count"] == 47

    # -- RUN COMPLETE --
    def test_run_complete_event(self):
        line = (
            "INFO ingest: Run complete: 2 source(s) | 47 fetched | "
            "10 pre-filtered | 5 dupes skipped | 7 scored (3 failed) | "
            "0 scrape skipped | 0 scrape fallbacks | ~1,234 tok | ~$0.0012"
        )
        event = self.parser.parse(line)
        assert event["type"] == "complete"

    # -- RESCORED --
    def test_rescored_event(self):
        line = "INFO ingest: RESCORED 7/10  Backend Engineer"
        event = self.parser.parse(line)
        assert event["type"] == "rescored"
        assert event["detail"]["score"] == 7
        assert event["title"] == "Backend Engineer"
        assert event["source"] is None  # no source in rescore mode

    # -- RESCORE FAILED --
    def test_rescore_failed_event(self):
        line = "WARNING ingest: RESCORE FAILED  Some Title"
        event = self.parser.parse(line)
        assert event["type"] == "rescore_failed"
        assert event["title"] == "Some Title"

    # -- RESCORE COMPLETE --
    def test_rescore_complete_event(self):
        line = "INFO ingest: Rescore complete: 50 listings | 48 rescored (2 failed) | ~5,000 tok | ~$0.0050"
        event = self.parser.parse(line)
        assert event["type"] == "complete"

    # -- PREFIX STRIPPING --
    def test_strips_info_prefix(self):
        line = "INFO ingest: DUPE      [Adzuna] Test"
        event = self.parser.parse(line)
        assert event is not None

    def test_strips_warning_prefix(self):
        line = "WARNING ingest: SCORE FAILED  [Adzuna] Test"
        event = self.parser.parse(line)
        assert event is not None

    # -- UNRECOGNIZED --
    def test_unrecognized_line_returns_none(self):
        assert self.parser.parse("some random debug output") is None
        assert self.parser.parse("") is None
        assert self.parser.parse("INFO ingest:   verdict: good fit") is None

    # -- MULTI-SOURCE TRACKING --
    def test_source_tracks_from_fetched(self):
        self.parser.parse("INFO ingest: Fetched 10 listing(s) from Jooble")
        event = self.parser.parse("INFO ingest: SCORED 5/10  [Jooble] Some Job")
        assert event["source"] == "Jooble"

    # -- TIMESTAMP --
    def test_events_have_timestamp(self):
        line = "INFO ingest: DUPE      [Adzuna] Test"
        event = self.parser.parse(line)
        assert "timestamp" in event
```

- [x] Run tests to verify they fail:

```
pytest tests/test_ingest_events.py::TestIngestEventParser -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'ingest_events'`

### Step 1.2: Implement IngestEventParser

- [x] Create `ingest_events.py`:

```python
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
_FILTERED_RE = re.compile(r"FILTERED\s+\[(.+?)\]\s+(.+?)\s+—\s+(.+)")
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

    Tracks current source and a scrape-fallback flag that propagates to the
    next scored event.
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

        # -- SCRAPE FALLBACK (sets flag, returns None) --
        m = _SCRAPE_FALLBACK_RE.match(stripped)
        if m:
            self._scrape_fallback = True
            return None

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
            return {
                "type": "rescored",
                "source": None,
                "title": m.group(2),
                "url": None,
                "detail": {"score": int(m.group(1))},
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
```

- [x] Run parser tests:

```
pytest tests/test_ingest_events.py::TestIngestEventParser -v
```

Expected: ALL PASS

- [x] Commit:

```
git add ingest_events.py tests/test_ingest_events.py
git commit -m "feat(#93): add IngestEventParser with TDD tests

Stateful parser converts raw ingest.py log lines into structured event
dicts. Handles all event types: scored, filtered, dupe, score_failed,
scrape_skip, scrape_fallback (flag), fetched, complete, rescored,
rescore_failed.

closes #93 (partial — parser only, queue in next commit)"
```

---

## Task 2: EventQueue — Tests & Implementation (Issue #93, part 2)

**Files:**
- Modify: `tests/test_ingest_events.py` (add `TestEventQueue`)
- Modify: `ingest_events.py` (add `EventQueue` class)

### Step 2.1: Write failing tests for EventQueue

- [x] Append to `tests/test_ingest_events.py`:

```python
import time
from ingest_events import EventQueue


class TestEventQueue:
    """Tests for thread-safe event storage and subscription."""

    def setup_method(self):
        self.queue = EventQueue()

    def _make_event(self, type_: str = "scored", source: str = "Adzuna") -> dict:
        return {
            "type": type_,
            "source": source,
            "title": "Test Job",
            "url": None,
            "detail": {},
            "timestamp": "2026-04-09T00:00:00+00:00",
        }

    # -- PUSH / SUBSCRIBE --
    def test_push_and_subscribe_from_zero(self):
        self.queue.push(self._make_event())
        self.queue.push(self._make_event(type_="filtered"))
        # Push terminal to end subscription
        self.queue.push(self._make_event(type_="complete"))
        events = list(self.queue.subscribe(last_id=0))
        assert len(events) == 3
        assert events[0]["id"] == 1
        assert events[1]["id"] == 2
        assert events[2]["id"] == 3

    def test_subscribe_from_cursor(self):
        for _ in range(3):
            self.queue.push(self._make_event())
        self.queue.push(self._make_event(type_="complete"))
        events = list(self.queue.subscribe(last_id=2))
        assert len(events) == 2
        assert events[0]["id"] == 3
        assert events[1]["id"] == 4  # complete

    def test_replay_order_is_stable(self):
        for i in range(5):
            self.queue.push(self._make_event(source=f"Source{i}"))
        self.queue.push(self._make_event(type_="complete"))
        events = list(self.queue.subscribe(last_id=0))
        ids = [e["id"] for e in events]
        assert ids == sorted(ids)

    # -- CLEAR --
    def test_clear_resets_state(self):
        self.queue.push(self._make_event())
        old_run_id = self.queue.run_id
        self.queue.clear()
        assert self.queue.run_id != old_run_id
        # Queue should be empty after clear — subscribe yields idle
        events = list(self.queue.subscribe(last_id=0))
        assert len(events) == 1
        assert events[0]["type"] == "idle"

    # -- IDLE --
    def test_empty_queue_yields_idle(self):
        events = list(self.queue.subscribe(last_id=0))
        assert len(events) == 1
        assert events[0]["type"] == "idle"

    # -- RUN_ID --
    def test_run_id_generated_on_init(self):
        assert self.queue.run_id is not None
        # Should be a valid UUID string
        uuid.UUID(self.queue.run_id)

    def test_clear_generates_new_run_id(self):
        old = self.queue.run_id
        self.queue.clear()
        assert self.queue.run_id != old

    # -- MEMORY CAP --
    def test_evicts_oldest_when_over_cap(self):
        small_queue = EventQueue(max_size=5)
        for i in range(7):
            small_queue.push(self._make_event(source=f"S{i}"))
        small_queue.push(self._make_event(type_="complete"))
        events = list(small_queue.subscribe(last_id=0))
        # 5 kept + 1 complete = should start from id 3 (evicted 1,2)
        assert events[0]["id"] == 3

    # -- TERMINAL EVENT ENDS SUBSCRIPTION --
    def test_complete_ends_subscription(self):
        self.queue.push(self._make_event())
        self.queue.push(self._make_event(type_="complete"))
        self.queue.push(self._make_event())  # after terminal — should not appear
        events = list(self.queue.subscribe(last_id=0))
        assert events[-1]["type"] == "complete"
        assert len(events) == 2

    def test_aborted_ends_subscription(self):
        self.queue.push(self._make_event())
        self.queue.push(self._make_event(type_="aborted"))
        events = list(self.queue.subscribe(last_id=0))
        assert events[-1]["type"] == "aborted"
        assert len(events) == 2

    # -- THREAD SAFETY --
    def test_concurrent_push_and_subscribe(self):
        """Push from one thread, subscribe from another — no corruption."""
        results = []

        def producer():
            for i in range(20):
                self.queue.push(self._make_event(source=f"S{i}"))
                time.sleep(0.01)
            self.queue.push(self._make_event(type_="complete"))

        def consumer():
            for event in self.queue.subscribe(last_id=0):
                results.append(event)

        t1 = threading.Thread(target=producer)
        t2 = threading.Thread(target=consumer)
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        assert len(results) == 21  # 20 scored + 1 complete
        assert results[-1]["type"] == "complete"
        ids = [e["id"] for e in results]
        assert ids == sorted(ids)

    # -- CONNECTION TRACKING --
    def test_connection_counter(self):
        self.queue.connect()
        assert self.queue.connection_count == 1
        self.queue.connect()
        assert self.queue.connection_count == 2
        self.queue.disconnect()
        assert self.queue.connection_count == 1
        self.queue.disconnect()
        assert self.queue.connection_count == 0

    # -- CLEANUP TIMER --
    def test_cleanup_timer_clears_after_disconnect(self):
        """Queue clears 60s after last SSE disconnect (tested with short timer)."""
        self.queue.push(self._make_event(type_="complete"))
        self.queue.connect()
        self.queue.disconnect()
        # Timer was started — verify the cleanup method works directly
        self.queue._cleanup_if_idle()
        assert self.queue.is_empty()

    def test_cleanup_skipped_if_connections_active(self):
        """Queue NOT cleared if a connection is still active."""
        self.queue.push(self._make_event(type_="complete"))
        self.queue.connect()
        self.queue.connect()
        self.queue.disconnect()  # still 1 active
        self.queue._cleanup_if_idle()
        assert not self.queue.is_empty()
```

- [x] Run tests to verify new ones fail:

```
pytest tests/test_ingest_events.py::TestEventQueue -v
```

Expected: FAIL — `ImportError: cannot import name 'EventQueue'`

### Step 2.2: Implement EventQueue

- [x] Add to `ingest_events.py` (after `IngestEventParser` class):

```python
# ---------------------------------------------------------------------------
# Event queue
# ---------------------------------------------------------------------------

_TERMINAL_TYPES = frozenset({"complete", "aborted"})


class EventQueue:
    """Thread-safe event store shared between the StdoutReader and SSE endpoint.

    Events are pushed by the reader thread and consumed by SSE generators via
    subscribe(). A single global instance is used in production; tests create
    their own instances.
    """

    def __init__(self, max_size: int = 5000) -> None:
        self._lock = threading.Lock()
        self._events: list[dict] = []
        self._next_id = 1
        self._max_size = max_size
        self._new_event = threading.Event()
        self.run_id: str = str(uuid.uuid4())
        self._connection_count = 0

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
        if hasattr(self, "_cleanup_timer") and self._cleanup_timer is not None:
            self._cleanup_timer.cancel()
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
        """Append an event with an auto-incrementing ID and wake subscribers."""
        with self._lock:
            event = {**event, "id": self._next_id, "run_id": self.run_id}
            self._next_id += 1
            self._events.append(event)
            # Evict oldest if over cap
            if len(self._events) > self._max_size:
                self._events = self._events[-self._max_size:]
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

    def subscribe(self, last_id: int = 0) -> Generator[dict, None, None]:
        """Yield events from last_id onward, blocking when caught up.

        Returns when a terminal event (complete/aborted) is yielded.
        If the queue is empty and has no terminal event, yields a synthetic
        idle event and returns immediately.
        """
        # Empty queue with no active run → immediate idle
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
```

- [x] Run queue tests:

```
pytest tests/test_ingest_events.py::TestEventQueue -v
```

Expected: ALL PASS

- [x] Run full test file:

```
pytest tests/test_ingest_events.py -v
```

Expected: ALL PASS

- [x] Commit:

```
git add ingest_events.py tests/test_ingest_events.py
git commit -m "feat(#93): add EventQueue with thread-safe subscribe/push

Thread-safe event store with auto-incrementing IDs, run_id tracking,
FIFO eviction at 5000 events, and blocking subscribe() generator that
terminates on complete/aborted events. Empty queue yields idle.

closes #93"
```

---

## Task 3: StdoutReader Thread & SSE Endpoint (Issue #94)

**Files:**
- Modify: `app.py` (lines ~685–862)
- Create: `tests/test_ingest_stream.py`

### Step 3.1: Write failing tests for SSE endpoint

- [ ] Create `tests/test_ingest_stream.py`:

```python
"""Integration tests for the /ingest/stream SSE endpoint."""

import json
import pytest

import app as app_module
from app import app as flask_app
from ingest_events import EventQueue, event_queue


@pytest.fixture(autouse=True)
def fresh_queue(monkeypatch):
    """Replace global event_queue with a fresh instance for each test."""
    q = EventQueue()
    monkeypatch.setattr(app_module, "event_queue", q)
    monkeypatch.setattr("ingest_events.event_queue", q)
    yield q


@pytest.fixture()
def client():
    flask_app.config["TESTING"] = True
    with flask_app.test_client() as c:
        yield c


class TestIngestStream:
    """Tests for GET /ingest/stream SSE endpoint."""

    def test_empty_queue_returns_idle(self, client, fresh_queue):
        resp = client.get("/ingest/stream")
        assert resp.status_code == 200
        assert resp.content_type.startswith("text/event-stream")
        data = resp.get_data(as_text=True)
        assert '"type": "idle"' in data or '"type":"idle"' in data

    def test_sse_wire_format(self, client, fresh_queue):
        fresh_queue.push({
            "type": "scored", "source": "Adzuna", "title": "Test",
            "url": None, "detail": {"score": 8}, "timestamp": "2026-04-09T00:00:00Z",
        })
        fresh_queue.push({
            "type": "complete", "source": None, "title": None,
            "url": None, "detail": {}, "timestamp": "2026-04-09T00:00:01Z",
        })
        resp = client.get("/ingest/stream")
        text = resp.get_data(as_text=True)
        # SSE format: "id: N\ndata: {json}\n\n"
        assert "id: 1\n" in text
        assert "data: " in text
        assert "id: 2\n" in text

    def test_last_event_id_replay(self, client, fresh_queue):
        for i in range(5):
            fresh_queue.push({
                "type": "scored", "source": f"S{i}", "title": f"Job {i}",
                "url": None, "detail": {"score": i}, "timestamp": "2026-04-09T00:00:00Z",
            })
        fresh_queue.push({
            "type": "complete", "source": None, "title": None,
            "url": None, "detail": {}, "timestamp": "2026-04-09T00:00:01Z",
        })
        # Request replay from id=2
        run_id = fresh_queue.run_id
        resp = client.get(
            "/ingest/stream",
            headers={"Last-Event-ID": f"{run_id}:{2}"},
        )
        text = resp.get_data(as_text=True)
        # Should get events 3, 4, 5, and complete (6)
        assert "id: 3\n" in text
        assert "id: 6\n" in text
        assert "id: 1\n" not in text
        assert "id: 2\n" not in text

    def test_stale_run_id_replays_from_start(self, client, fresh_queue):
        fresh_queue.push({
            "type": "scored", "source": "A", "title": "Job",
            "url": None, "detail": {"score": 5}, "timestamp": "2026-04-09T00:00:00Z",
        })
        fresh_queue.push({
            "type": "complete", "source": None, "title": None,
            "url": None, "detail": {}, "timestamp": "2026-04-09T00:00:01Z",
        })
        # Stale run_id
        resp = client.get(
            "/ingest/stream",
            headers={"Last-Event-ID": "stale-uuid:5"},
        )
        text = resp.get_data(as_text=True)
        # Should replay from beginning
        assert "id: 1\n" in text

    def test_complete_closes_stream(self, client, fresh_queue):
        fresh_queue.push({
            "type": "scored", "source": "A", "title": "J",
            "url": None, "detail": {}, "timestamp": "2026-04-09T00:00:00Z",
        })
        fresh_queue.push({
            "type": "complete", "source": None, "title": None,
            "url": None, "detail": {}, "timestamp": "2026-04-09T00:00:01Z",
        })
        resp = client.get("/ingest/stream")
        text = resp.get_data(as_text=True)
        lines = [l for l in text.split("\n") if l.startswith("data: ")]
        last_data = json.loads(lines[-1].removeprefix("data: "))
        assert last_data["type"] == "complete"

    def test_aborted_closes_stream(self, client, fresh_queue):
        fresh_queue.push({
            "type": "aborted", "source": None, "title": None,
            "url": None, "detail": {"error": "crash"}, "timestamp": "2026-04-09T00:00:00Z",
        })
        resp = client.get("/ingest/stream")
        text = resp.get_data(as_text=True)
        lines = [l for l in text.split("\n") if l.startswith("data: ")]
        last_data = json.loads(lines[-1].removeprefix("data: "))
        assert last_data["type"] == "aborted"

    def test_max_connections_returns_429(self, client, fresh_queue, monkeypatch):
        monkeypatch.setattr(app_module, "MAX_SSE_CONNECTIONS", 0)
        resp = client.get("/ingest/stream")
        assert resp.status_code == 429
```

- [ ] Run tests to verify they fail:

```
pytest tests/test_ingest_stream.py -v
```

Expected: FAIL — no `/ingest/stream` route exists yet

### Step 3.2: Implement StdoutReader and SSE endpoint in app.py

- [ ] Add imports at top of `app.py` (near existing imports):

```python
from datetime import datetime, timezone
from ingest_events import IngestEventParser, event_queue
```

(If `datetime` is already imported, just add the `timezone` name to the existing import.)

- [ ] Add constant after `_last_run` declaration (around line 702):

```python
MAX_SSE_CONNECTIONS: int = 2
```

- [ ] Add the StdoutReader function (after the `_parse_ingest_summary` function, around line 740):

```python
def _stdout_reader(proc: subprocess.Popen) -> None:
    """Daemon thread: reads ingest subprocess stdout line-by-line,
    parses each into a structured event, and pushes to the global queue.

    On exception: kills the subprocess and pushes an aborted event.
    On EOF: pushes complete (if summary seen) or aborted (if not).
    """
    parser = IngestEventParser()
    saw_complete = False
    try:
        for raw_line in iter(proc.stdout.readline, ""):
            line = raw_line.rstrip("\n")
            if not line:
                continue
            event = parser.parse(line)
            if event is not None:
                if event["type"] == "complete":
                    saw_complete = True
                event_queue.push(event)
    except Exception:
        logger.exception("StdoutReader crashed")
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
```

- [ ] Modify the `/ingest/trigger` route to use PIPE + reader thread instead of temp file.

In `app.py`, replace the subprocess launch block inside `ingest_trigger()` (the `try` block around lines 829–834):

**Old:**
```python
        try:
            log_file = tempfile.TemporaryFile(mode="w+", suffix=".log", prefix="ingest_")
            proc = subprocess.Popen(
                cmd,
                stdout=log_file,
                stderr=subprocess.STDOUT,
            )
        except (OSError, PermissionError) as e:
            return jsonify({"error": f"Failed to start ingestion: {e}"}), 500

        _ingest_log_file = log_file
        _ingest_process = proc
```

**New:**
```python
        event_queue.clear()
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,  # line-buffered
            )
        except (OSError, PermissionError) as e:
            return jsonify({"error": f"Failed to start ingestion: {e}"}), 500

        _ingest_process = proc
        _ingest_log_file = None  # no longer used; kept for backward compat

        reader = threading.Thread(
            target=_stdout_reader,
            args=(proc,),
            daemon=True,
        )
        reader.start()
```

- [ ] Update the `_ingest_running()` function: remove the temp file read logic since `StdoutReader` now handles parsing. The function still needs to detect process completion and parse the summary for backward compatibility with `/ingest/status`.

Replace the `_ingest_running()` exit-handling block. The section that reads from `_ingest_log_file` should be simplified — the StdoutReader handles event streaming, but `_ingest_running()` still needs to set `_last_run` for the `/ingest/status` endpoint. Since we no longer have a temp file, extract the summary from the event queue instead:

**Old** (the block inside `if _ingest_process.poll() is not None`):
```python
        # Process has exited — harvest the log and clear the handles.
        summary: str = ""
        if _ingest_log_file is not None:
            try:
                _ingest_log_file.seek(0)
                summary = _ingest_log_file.read()
            except (OSError, ValueError):
                pass
            finally:
                try:
                    _ingest_log_file.close()
                except (OSError, ValueError):
                    pass
        _last_run = _parse_ingest_summary(summary)
        _ingest_process = None
        _ingest_log_file = None
```

**New:**
```python
        # Process has exited — extract summary from event queue for backward compat.
        summary = ""
        # Find the complete event's summary text in the queue
        with event_queue._lock:
            for ev in reversed(event_queue._events):
                if ev["type"] == "complete" and ev.get("detail", {}).get("summary"):
                    summary = ev["detail"]["summary"]
                    break
        _last_run = _parse_ingest_summary(summary)
        _ingest_process = None
        _ingest_log_file = None
```

- [ ] Add the SSE endpoint (after the `/ingest/status` route, around line 863):

```python
@app.route("/ingest/stream")
def ingest_stream():
    """SSE endpoint streaming real-time ingest events.

    Yields events from the EventQueue in SSE wire format. Supports replay
    via Last-Event-ID header. Returns 429 if max connections exceeded.
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
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
```

- [ ] Also add `import json` to `app.py` imports if not already present.

- [ ] Run SSE integration tests:

```
pytest tests/test_ingest_stream.py -v
```

Expected: ALL PASS

- [ ] Run existing ingest trigger tests to verify backward compat:

```
pytest tests/test_ingest_trigger.py -v
```

Expected: ALL PASS (may need minor fixture updates for the subprocess change — if `tempfile.TemporaryFile` is still patched, it should be harmless since it's no longer called)

- [ ] Commit:

```
git add app.py tests/test_ingest_stream.py
git commit -m "feat(#94): add StdoutReader thread and SSE endpoint

- Change subprocess stdout from temp file to PIPE with line buffering
- StdoutReader daemon thread feeds lines through IngestEventParser
  into the global EventQueue
- New GET /ingest/stream SSE endpoint with Last-Event-ID replay,
  run_id stale detection, and max 2 concurrent connections (429)
- Backward compat: /ingest/status still works via _ingest_running()

closes #94"
```

---

## Task 4: Ingest Drawer — HTML Partial & CSS (Issue #95, part 1)

**Files:**
- Create: `templates/_ingest_drawer.html`
- Modify: `templates/index.html`
- Modify: `static/style.css`

### Step 4.1: Create the drawer HTML partial

- [ ] Create `templates/_ingest_drawer.html`:

```html
{# ── Ingest log stream drawer ──
   Slide-out panel showing real-time ingest events.
   Included in index.html; JS in static/ingest-drawer.js. #}

{# -- Floating action button (visible when drawer is closed) -- #}
<button id="ingest-fab" class="ingest-fab ingest-fab--hidden" type="button"
        aria-label="Open ingest log">
  <span class="ingest-pulse-dot" id="ingest-pulse"></span>
  <span class="ingest-fab-icon" aria-hidden="true">&#9776;</span>
</button>

{# -- Drawer shell -- #}
<aside id="ingest-drawer" class="ingest-drawer" aria-label="Ingest log stream">
  <header class="ingest-drawer-header">
    <span class="ingest-drawer-title">
      <span class="ingest-pulse-dot" id="ingest-pulse-header"></span>
      Ingest Log
    </span>
    <button id="ingest-drawer-close" class="ingest-drawer-close" type="button"
            aria-label="Close drawer">&times;</button>
  </header>

  {# -- Event list (auto-scrolls) -- #}
  <div id="ingest-event-list" class="ingest-event-list"></div>

  {# -- Rolling tally (sticky footer) -- #}
  <footer class="ingest-tally" id="ingest-tally">
    <div class="ingest-tally-row">
      <span class="ingest-tally-item ingest-tally--fetched">
        Fetched <strong id="tally-fetched">0</strong>
      </span>
      <span class="ingest-tally-item ingest-tally--filtered">
        Filtered <strong id="tally-filtered">0</strong>
      </span>
      <span class="ingest-tally-item ingest-tally--dupes">
        Dupes <strong id="tally-dupes">0</strong>
      </span>
      <span class="ingest-tally-item ingest-tally--skipped">
        Skipped <strong id="tally-skipped">0</strong>
      </span>
      <span class="ingest-tally-item ingest-tally--scored">
        Scored <strong id="tally-scored">0</strong>
      </span>
      <span class="ingest-tally-item ingest-tally--failed">
        Failed <strong id="tally-failed">0</strong>
      </span>
    </div>
    {# Per-source breakdown rows injected by JS #}
    <div id="ingest-source-breakdown" class="ingest-source-breakdown"></div>
  </footer>
</aside>
```

### Step 4.2: Include drawer in index.html

- [ ] In `templates/index.html`, add the include just before the closing `</body>` tag (or at the end of the page wrapper). Find the end of the template and add:

```html
{% include '_ingest_drawer.html' %}
<script src="{{ url_for('static', filename='ingest-drawer.js') }}"></script>
```

### Step 4.3: Add drawer CSS to style.css

- [ ] Append to `static/style.css`:

```css
/* ── Ingest Drawer ─────────────────────────────────────────── */

.ingest-drawer {
  position: fixed;
  top: 0;
  right: 0;
  width: 400px;
  height: 100vh;
  background: var(--bg-surface);
  border-left: 1px solid var(--border-subtle);
  display: flex;
  flex-direction: column;
  z-index: 1000;
  transform: translateX(100%);
  transition: transform 0.3s ease;
}

.ingest-drawer--open {
  transform: translateX(0);
}

.ingest-drawer-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0.75rem 1rem;
  border-bottom: 1px solid var(--border-subtle);
  flex-shrink: 0;
}

.ingest-drawer-title {
  font-family: var(--font-mono);
  font-size: 0.72rem;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: var(--text-secondary);
  display: flex;
  align-items: center;
  gap: 0.5rem;
}

.ingest-drawer-close {
  background: none;
  border: none;
  color: var(--text-muted);
  font-size: 1.25rem;
  cursor: pointer;
  padding: 0.25rem;
  line-height: 1;
}

.ingest-drawer-close:hover {
  color: var(--text-primary);
}

/* -- Pulse dot -- */

.ingest-pulse-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--text-muted);
  display: inline-block;
  flex-shrink: 0;
}

.ingest-pulse-dot--live {
  background: var(--score-high-text);
  animation: ingest-pulse 1.5s ease-in-out infinite;
}

@keyframes ingest-pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.4; }
}

/* -- FAB -- */

.ingest-fab {
  position: fixed;
  bottom: 1.5rem;
  right: 1.5rem;
  width: 48px;
  height: 48px;
  border-radius: 50%;
  background: var(--bg-raised);
  border: 1px solid var(--border-mid);
  color: var(--text-accent);
  font-size: 1.1rem;
  cursor: pointer;
  z-index: 999;
  display: flex;
  align-items: center;
  justify-content: center;
  transition: opacity 0.2s ease, transform 0.2s ease;
}

.ingest-fab:hover {
  background: var(--bg-hover);
  border-color: var(--border-strong);
}

.ingest-fab--hidden {
  opacity: 0;
  pointer-events: none;
  transform: scale(0.8);
}

.ingest-fab .ingest-pulse-dot {
  position: absolute;
  top: 6px;
  right: 6px;
  width: 6px;
  height: 6px;
}

/* -- Event list -- */

.ingest-event-list {
  flex: 1;
  overflow-y: auto;
  padding: 0.5rem 0;
}

.ingest-event {
  padding: 0.35rem 1rem;
  font-size: 0.78rem;
  border-bottom: 1px solid var(--border-subtle);
  animation: ingest-slide-in 0.2s ease;
}

.ingest-event--replay {
  animation: none;
}

@keyframes ingest-slide-in {
  from { opacity: 0; transform: translateX(20px); }
  to   { opacity: 1; transform: translateX(0); }
}

.ingest-event-title {
  font-family: var(--font-ui);
  font-weight: 600;
  color: var(--text-primary);
  font-size: 0.82rem;
}

.ingest-event-source {
  font-family: var(--font-mono);
  font-size: 0.65rem;
  color: var(--text-muted);
  float: right;
  text-transform: uppercase;
}

.ingest-event-tag {
  font-family: var(--font-mono);
  font-size: 0.65rem;
  padding: 0.1rem 0.4rem;
  border-radius: var(--radius-sm);
  display: inline-block;
  margin-left: 0.4rem;
}

/* Event type styling */
.ingest-event--scored .ingest-event-tag { background: var(--score-high-bg); color: var(--score-high-text); border: 1px solid var(--score-high-border); }
.ingest-event--filtered { color: var(--text-muted); }
.ingest-event--filtered .ingest-event-tag { background: var(--bg-raised); color: var(--text-muted); border: 1px solid var(--border-subtle); }
.ingest-event--dupe { color: var(--text-muted); }
.ingest-event--score_failed .ingest-event-tag { background: var(--score-low-bg); color: var(--score-low-text); border: 1px solid var(--score-low-border); }
.ingest-event--scrape_skip { color: var(--text-muted); }

.ingest-event--fetched {
  font-family: var(--font-mono);
  font-size: 0.7rem;
  color: var(--text-accent);
  text-transform: uppercase;
  letter-spacing: 0.04em;
  text-align: center;
  padding: 0.6rem 1rem;
  border-bottom: none;
}

.ingest-event--fetched::before,
.ingest-event--fetched::after {
  content: "";
  display: inline-block;
  width: 2rem;
  height: 1px;
  background: var(--border-mid);
  vertical-align: middle;
  margin: 0 0.5rem;
}

.ingest-event--aborted {
  background: var(--score-low-bg);
  color: var(--score-low-text);
  border: 1px solid var(--score-low-border);
  border-radius: var(--radius-md);
  margin: 0.5rem;
  padding: 0.75rem 1rem;
  font-family: var(--font-mono);
  font-size: 0.76rem;
}

/* -- Tally footer -- */

.ingest-tally {
  flex-shrink: 0;
  padding: 0.6rem 1rem;
  border-top: 1px solid var(--border-subtle);
  background: var(--bg-base);
}

.ingest-tally-row {
  display: flex;
  flex-wrap: wrap;
  gap: 0.6rem;
}

.ingest-tally-item {
  font-family: var(--font-mono);
  font-size: 0.65rem;
  text-transform: uppercase;
  color: var(--text-muted);
}

.ingest-tally-item strong {
  font-weight: 600;
}

.ingest-tally--fetched strong { color: var(--text-accent); }
.ingest-tally--scored strong  { color: var(--score-high-text); }
.ingest-tally--failed strong  { color: var(--score-low-text); }

/* -- Source breakdown -- */

.ingest-source-breakdown {
  margin-top: 0.4rem;
  font-family: var(--font-mono);
  font-size: 0.63rem;
  color: var(--text-muted);
}

.ingest-source-row {
  display: flex;
  justify-content: space-between;
  padding: 0.15rem 0;
}

.ingest-source-name {
  text-transform: uppercase;
  color: var(--text-secondary);
}
```

- [ ] Commit:

```
git add templates/_ingest_drawer.html templates/index.html static/style.css
git commit -m "feat(#95): add ingest drawer HTML partial and CSS

Slide-out drawer with event list, rolling tally footer, per-source
breakdown, pulse dot indicator, and FAB. Follows STYLE_GUIDE.md
conventions: all values via CSS custom properties, font-mono for
metadata, score tier colors for event types.

closes #95 (partial — CSS/HTML only, JS in next commit)"
```

---

## Task 5: Ingest Drawer — JavaScript (Issue #95, part 2)

**Files:**
- Create: `static/ingest-drawer.js`

### Step 5.1: Implement drawer JS

- [ ] Create `static/ingest-drawer.js`:

```javascript
/**
 * Ingest Log Stream Drawer
 *
 * Manages the slide-out drawer that displays real-time ingest events via SSE.
 * Handles: EventSource lifecycle, event rendering, auto-scroll, tally updates,
 * per-source breakdown, and open/closed state persistence.
 */
(function () {
  "use strict";

  // -- DOM refs --
  var drawer    = document.getElementById("ingest-drawer");
  var fab       = document.getElementById("ingest-fab");
  var closeBtn  = document.getElementById("ingest-drawer-close");
  var eventList = document.getElementById("ingest-event-list");
  var tallyEl   = document.getElementById("ingest-tally");
  var breakdownEl = document.getElementById("ingest-source-breakdown");

  // Pulse dots (header + FAB)
  var pulseHeader = document.getElementById("ingest-pulse-header");
  var pulseFab    = document.getElementById("ingest-pulse");

  // Tally counters
  var tallyIds = {
    fetched:  document.getElementById("tally-fetched"),
    filtered: document.getElementById("tally-filtered"),
    dupes:    document.getElementById("tally-dupes"),
    skipped:  document.getElementById("tally-skipped"),
    scored:   document.getElementById("tally-scored"),
    failed:   document.getElementById("tally-failed"),
  };

  if (!drawer || !fab) return;  // not on a page with the drawer

  // -- State --
  var tally = { fetched: 0, filtered: 0, dupes: 0, skipped: 0, scored: 0, failed: 0 };
  var sourceTally = {};  // { "Adzuna": { fetched: 0, filtered: 0, passed: 0 } }
  var eventSource = null;
  var isLive = false;
  var isReplay = true;  // true until first live event after connection
  var autoScrollPinned = true;
  var SCROLL_THRESHOLD = 40;

  // -- Drawer open/close --

  function openDrawer() {
    drawer.classList.add("ingest-drawer--open");
    fab.classList.add("ingest-fab--hidden");
    sessionStorage.setItem("ingest-drawer-open", "1");
  }

  function closeDrawer() {
    drawer.classList.remove("ingest-drawer--open");
    fab.classList.remove("ingest-fab--hidden");
    sessionStorage.setItem("ingest-drawer-open", "0");
  }

  closeBtn.addEventListener("click", closeDrawer);
  fab.addEventListener("click", openDrawer);

  // Restore state from sessionStorage
  if (sessionStorage.getItem("ingest-drawer-open") === "1") {
    openDrawer();
  }

  // -- Auto-scroll --

  eventList.addEventListener("scroll", function () {
    var distFromBottom = eventList.scrollHeight - eventList.scrollTop - eventList.clientHeight;
    autoScrollPinned = distFromBottom < SCROLL_THRESHOLD;
  });

  function scrollToBottom() {
    if (autoScrollPinned) {
      eventList.scrollTop = eventList.scrollHeight;
    }
  }

  // -- Pulse dot --

  function setPulseLive(live) {
    var cls = "ingest-pulse-dot--live";
    if (live) {
      pulseHeader.classList.add(cls);
      pulseFab.classList.add(cls);
    } else {
      pulseHeader.classList.remove(cls);
      pulseFab.classList.remove(cls);
    }
  }

  // -- Tally --

  function updateTallyDisplay() {
    Object.keys(tally).forEach(function (key) {
      if (tallyIds[key]) tallyIds[key].textContent = tally[key];
    });
  }

  function updateSourceBreakdown() {
    var html = "";
    Object.keys(sourceTally).forEach(function (name) {
      var s = sourceTally[name];
      html += '<div class="ingest-source-row">' +
        '<span class="ingest-source-name">' + escapeHtml(name) + '</span>' +
        '<span>' + s.fetched + ' fetched / ' + s.filtered + ' filtered / ' + s.passed + ' passed</span>' +
        '</div>';
    });
    breakdownEl.innerHTML = html;
  }

  function trackTally(event) {
    var type = event.type;
    var source = event.source;

    if (type === "fetched") {
      tally.fetched += event.detail.fetched_count || 0;
      ensureSource(source);
      sourceTally[source].fetched += event.detail.fetched_count || 0;
    } else if (type === "filtered") {
      tally.filtered++;
      ensureSource(source);
      sourceTally[source].filtered++;
    } else if (type === "dupe") {
      tally.dupes++;
    } else if (type === "scrape_skip") {
      tally.skipped++;
    } else if (type === "scored" || type === "rescored") {
      tally.scored++;
      if (source) {
        ensureSource(source);
        sourceTally[source].passed++;
      }
    } else if (type === "score_failed" || type === "rescore_failed") {
      tally.failed++;
    }

    updateTallyDisplay();
    updateSourceBreakdown();
  }

  function ensureSource(name) {
    if (name && !sourceTally[name]) {
      sourceTally[name] = { fetched: 0, filtered: 0, passed: 0 };
    }
  }

  // -- Event rendering --

  function renderEvent(event, replay) {
    var el = document.createElement("div");
    var cls = "ingest-event ingest-event--" + event.type;
    if (replay) cls += " ingest-event--replay";
    el.className = cls;

    switch (event.type) {
      case "fetched":
        el.textContent = "Fetched " + (event.detail.fetched_count || 0) + " from " + (event.source || "?");
        break;

      case "scored":
      case "rescored":
        el.innerHTML =
          (event.source ? '<span class="ingest-event-source">' + escapeHtml(event.source) + '</span>' : '') +
          '<span class="ingest-event-title">' + escapeHtml(event.title || "") + '</span>' +
          '<span class="ingest-event-tag">' + (event.detail.score || 0) + '/10</span>' +
          (event.detail.scraped === false ? '<span class="ingest-event-tag">SNIPPET</span>' :
           event.type === "scored" ? '<span class="ingest-event-tag">FULL</span>' : '');
        break;

      case "filtered":
        el.innerHTML =
          (event.source ? '<span class="ingest-event-source">' + escapeHtml(event.source) + '</span>' : '') +
          '<span class="ingest-event-title">' + escapeHtml(event.title || "") + '</span>' +
          '<span class="ingest-event-tag">' + escapeHtml(event.detail.reason || "filtered") + '</span>';
        break;

      case "dupe":
        el.innerHTML =
          (event.source ? '<span class="ingest-event-source">' + escapeHtml(event.source) + '</span>' : '') +
          '<span class="ingest-event-title">' + escapeHtml(event.title || "") + '</span>' +
          '<span class="ingest-event-tag">already seen</span>';
        break;

      case "score_failed":
      case "rescore_failed":
        el.innerHTML =
          (event.source ? '<span class="ingest-event-source">' + escapeHtml(event.source) + '</span>' : '') +
          '<span class="ingest-event-title">' + escapeHtml(event.title || "") + '</span>' +
          '<span class="ingest-event-tag">FAILED</span>';
        break;

      case "scrape_skip":
        el.innerHTML =
          (event.source ? '<span class="ingest-event-source">' + escapeHtml(event.source) + '</span>' : '') +
          '<span class="ingest-event-title">' + escapeHtml(event.title || "") + '</span>' +
          '<span class="ingest-event-tag">full from source</span>';
        break;

      case "complete":
        el.className = "ingest-event ingest-event--complete";
        el.textContent = "Run complete";
        break;

      case "aborted":
        el.textContent = "Ingest run failed unexpectedly";
        break;

      case "idle":
        // Don't render idle events
        return;

      default:
        return;
    }

    eventList.appendChild(el);
    trackTally(event);
    scrollToBottom();
  }

  // -- SSE connection --

  function connectSSE() {
    if (eventSource) return;

    isReplay = true;
    isLive = true;
    setPulseLive(true);

    // Open drawer automatically when stream starts
    openDrawer();

    eventSource = new EventSource("/ingest/stream");

    eventSource.onmessage = function (e) {
      var data;
      try {
        data = JSON.parse(e.data);
      } catch (_) {
        return;
      }

      if (data.type === "idle") {
        isLive = false;
        setPulseLive(false);
        closeSSE();
        return;
      }

      renderEvent(data, isReplay);

      // After first batch of replayed events, switch to live mode
      // (the browser delivers all buffered events synchronously)
      if (isReplay) {
        requestAnimationFrame(function () { isReplay = false; });
      }

      if (data.type === "complete" || data.type === "aborted") {
        isLive = false;
        setPulseLive(false);
        closeSSE();
      }
    };

    eventSource.onerror = function () {
      isLive = false;
      setPulseLive(false);
      closeSSE();
    };
  }

  function closeSSE() {
    if (eventSource) {
      eventSource.close();
      eventSource = null;
    }
  }

  // -- Reset on new ingest run --

  function resetDrawer() {
    eventList.innerHTML = "";
    tally = { fetched: 0, filtered: 0, dupes: 0, skipped: 0, scored: 0, failed: 0 };
    sourceTally = {};
    updateTallyDisplay();
    breakdownEl.innerHTML = "";
  }

  // -- Escape HTML --

  function escapeHtml(str) {
    var div = document.createElement("div");
    div.appendChild(document.createTextNode(str));
    return div.innerHTML;
  }

  // -- Integration with existing ingest trigger --
  // Listen for the form submission that triggers ingest
  document.body.addEventListener("htmx:afterRequest", function (e) {
    if (e.detail.pathInfo && e.detail.pathInfo.requestPath === "/ingest/trigger" && e.detail.successful) {
      // New ingest started — reset drawer and connect SSE
      resetDrawer();
      // Small delay to let the server start the subprocess
      setTimeout(connectSSE, 500);
    }
  });

  // On page load: try connecting to see if there's an active/completed run
  connectSSE();
})();
```

- [ ] Commit:

```
git add static/ingest-drawer.js
git commit -m "feat(#95): add ingest drawer JavaScript

EventSource lifecycle with Last-Event-ID replay, event rendering by
type (scored/filtered/dupe/fetched/etc), rolling tally counters,
per-source breakdown, auto-scroll with pin detection, sessionStorage
persistence for drawer open/close state, and HTMX integration to
auto-connect on ingest trigger.

closes #95"
```

---

## Task 6: Update Style Guide (Issue #95, docs)

**Files:**
- Modify: `docs/STYLE_GUIDE.md`

### Step 6.1: Document drawer component classes

- [ ] Add a new section to `docs/STYLE_GUIDE.md` under the Component Reference:

```markdown
### Ingest Drawer

Slide-out panel for real-time ingest log stream. Fixed to the right edge of the viewport, full height, 400px wide.

| Class | Purpose |
|---|---|
| `.ingest-drawer` | Drawer shell — starts off-screen (`translateX(100%)`) |
| `.ingest-drawer--open` | Slides drawer into view (`translateX(0)`) |
| `.ingest-drawer-header` | Header bar with title and close button |
| `.ingest-drawer-title` | `--font-mono` 0.72rem uppercase title |
| `.ingest-drawer-close` | × close button, `--text-muted` → `--text-primary` on hover |
| `.ingest-pulse-dot` | 8px indicator dot — `--text-muted` at rest |
| `.ingest-pulse-dot--live` | Green animated pulse (`--score-high-text`) |
| `.ingest-fab` | Floating action button (bottom-right), visible when drawer closed |
| `.ingest-fab--hidden` | Hides FAB with opacity 0 + `pointer-events: none` |
| `.ingest-event-list` | Scrollable event container, `flex: 1` |
| `.ingest-event` | Single event row, `--font-mono` |
| `.ingest-event--{type}` | Type modifier: `scored`, `filtered`, `dupe`, `fetched`, `aborted`, etc. |
| `.ingest-event--replay` | Suppresses slide-in animation for replayed events |
| `.ingest-event-title` | Job title, `--font-ui` weight 600 |
| `.ingest-event-source` | Right-aligned source label, `--font-mono` 0.65rem uppercase |
| `.ingest-event-tag` | Inline badge (score, reason, status) |
| `.ingest-tally` | Sticky footer with running counters |
| `.ingest-tally-item` | Single counter label, `--font-mono` 0.65rem uppercase |
| `.ingest-tally--{type}` | Counter color modifier: `fetched` (amber), `scored` (green), `failed` (red) |
| `.ingest-source-breakdown` | Per-source stats grid below tally |
| `.ingest-source-row` | Single source row: name + counts |
```

- [ ] Commit:

```
git add docs/STYLE_GUIDE.md
git commit -m "docs(#95): add ingest drawer component classes to style guide"
```

---

## Task 7: Test Updates and Full Suite Verification (Issue #96)

**Files:**
- Modify: `tests/test_ingest_events.py` (if any gaps from Tasks 1–2)
- Modify: `tests/test_ingest_stream.py` (if any gaps from Task 3)
- Modify: `tests/test_ingest_trigger.py` (fix any broken tests from subprocess change)

### Step 7.1: Verify and fix existing test suite

- [ ] Run the full test suite:

```
pytest -v
```

- [ ] If `tests/test_ingest_trigger.py` tests fail because they patch `tempfile.TemporaryFile` but the code now uses `subprocess.PIPE`, update the affected fixtures:

The `_make_mock_process` helper needs to return a mock with a `stdout` attribute that behaves like a file-like iterable (for the `StdoutReader` thread). Update the mock process factory to include `stdout`:

```python
def _make_mock_process(*, exited: bool = False, stdout_lines: list[str] | None = None):
    proc = MagicMock()
    proc.poll.return_value = None if not exited else 0
    proc.pid = 12345
    if stdout_lines is not None:
        proc.stdout.readline = MagicMock(side_effect=stdout_lines + [""])
    else:
        proc.stdout.readline = MagicMock(return_value="")
    return proc
```

Also, since the trigger no longer creates a `tempfile.TemporaryFile`, tests that patch it should be updated to no longer assert it was called.

- [ ] Run the full suite again after fixes:

```
pytest -v
```

Expected: ALL PASS

### Step 7.2: Add edge case tests

- [ ] Add to `tests/test_ingest_events.py`:

```python
class TestIngestEventParserEdgeCases:
    """Edge cases and boundary conditions for the parser."""

    def setup_method(self):
        self.parser = IngestEventParser()

    def test_verbose_mode_multiline_ignored(self):
        """Verbose breakdown lines after SCORED should be ignored."""
        self.parser.parse("INFO ingest: SCORED 8/10  [Adzuna] Job")
        assert self.parser.parse("INFO ingest:   verdict: great fit") is None
        assert self.parser.parse("INFO ingest:   matched: Python, AWS") is None

    def test_score_zero(self):
        line = "INFO ingest: SCORED 0/10  [Adzuna] Bad Match"
        event = self.parser.parse(line)
        assert event["detail"]["score"] == 0

    def test_score_ten(self):
        line = "INFO ingest: SCORED 10/10  [Adzuna] Perfect Match"
        event = self.parser.parse(line)
        assert event["detail"]["score"] == 10

    def test_source_name_with_spaces(self):
        """Source names may contain spaces (e.g. 'The Muse')."""
        line = "INFO ingest: SCORED 5/10  [The Muse] Some Job"
        event = self.parser.parse(line)
        assert event["source"] == "The Muse"

    def test_title_with_special_characters(self):
        line = "INFO ingest: SCORED 7/10  [Adzuna] Sr. Engineer — Platform (Remote)"
        event = self.parser.parse(line)
        assert event["title"] == "Sr. Engineer — Platform (Remote)"

    def test_filtered_long_reason(self):
        line = "INFO ingest: FILTERED  [Adzuna] Some Job — created_at older than 25 hours"
        event = self.parser.parse(line)
        assert event["detail"]["reason"] == "created_at older than 25 hours"
```

- [ ] Add to `tests/test_ingest_stream.py`:

```python
class TestIngestStreamEdgeCases:
    """Edge cases for the SSE endpoint."""

    def test_last_event_id_without_run_id(self, client, fresh_queue):
        """Plain numeric Last-Event-ID (no run_id prefix)."""
        fresh_queue.push({
            "type": "scored", "source": "A", "title": "J1",
            "url": None, "detail": {"score": 5}, "timestamp": "2026-04-09T00:00:00Z",
        })
        fresh_queue.push({
            "type": "complete", "source": None, "title": None,
            "url": None, "detail": {}, "timestamp": "2026-04-09T00:00:01Z",
        })
        resp = client.get("/ingest/stream", headers={"Last-Event-ID": "1"})
        text = resp.get_data(as_text=True)
        # Should replay from id=2 onward (complete event)
        assert "id:" in text

    def test_malformed_last_event_id(self, client, fresh_queue):
        """Garbage Last-Event-ID → replay from beginning."""
        fresh_queue.push({
            "type": "complete", "source": None, "title": None,
            "url": None, "detail": {}, "timestamp": "2026-04-09T00:00:00Z",
        })
        resp = client.get("/ingest/stream", headers={"Last-Event-ID": "garbage"})
        assert resp.status_code == 200
        text = resp.get_data(as_text=True)
        assert "id:" in text  # replays from start
```

- [ ] Run full suite:

```
pytest -v
```

Expected: ALL PASS

- [ ] Commit:

```
git add tests/test_ingest_events.py tests/test_ingest_stream.py tests/test_ingest_trigger.py
git commit -m "test(#96): add edge case tests and fix trigger test compat

- Edge cases: verbose mode multiline, score 0/10, source with spaces,
  special chars in titles, malformed Last-Event-ID
- Fix test_ingest_trigger.py fixtures for subprocess.PIPE change

closes #96"
```

---

## Task 8: Final Integration Verification

### Step 8.1: Full test suite

- [ ] Run the complete test suite one final time:

```
pytest -v --tb=short
```

Expected: ALL PASS, zero failures

### Step 8.2: Manual smoke test (if Docker dev stack available)

- [ ] Start the dev stack and verify:
  1. Open the feed page → drawer should show idle state
  2. Click "Run Ingestion" → drawer slides open, events stream in real time
  3. Tally counters update as events arrive
  4. Navigate away and back → events replay from queue
  5. Close drawer → FAB appears; click FAB → drawer reopens with all events
  6. Run completes → pulse dot goes idle

---

## Summary

| Task | Issue | What it builds |
|---|---|---|
| 1 | #93 | IngestEventParser (TDD) |
| 2 | #93 | EventQueue (TDD) |
| 3 | #94 | StdoutReader thread + SSE endpoint (TDD) |
| 4 | #95 | Drawer HTML + CSS |
| 5 | #95 | Drawer JavaScript |
| 6 | #95 | Style guide update |
| 7 | #96 | Edge case tests + trigger test compat |
| 8 | — | Final verification |
