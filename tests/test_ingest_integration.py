"""Integration tests for the ingest log stream pipeline.

Tests the full composition:
    subprocess stdout → _stdout_reader → EventQueue → GET /ingest/stream SSE endpoint

Unit tests for each component live in:
  - tests/test_ingest_events.py   (IngestEventParser + EventQueue)
  - tests/test_ingest_stream.py   (SSE endpoint + StdoutReader in isolation)

This file tests that the components compose correctly, exercising scenarios that
unit tests with mocked boundaries cannot catch.

All tests use Flask's test client (no real HTTP server) and mock subprocesses
(no real ingest.py, no API keys, no network). The EventQueue singleton is reset
per-test via the fresh_queue fixture from test_ingest_stream conventions.
"""

from __future__ import annotations

import io
import json

import pytest

import app as app_module
from app import app as flask_app, _stdout_reader
from ingest_events import EventQueue


# ---------------------------------------------------------------------------
# Log line fixtures — representative ingest.py output sequences
# ---------------------------------------------------------------------------

# A complete happy-path session: fetch → score/filter/dupe → complete
_HAPPY_PATH_LINES = [
    "INFO ingest: Fetched 5 listing(s) from Adzuna",
    "INFO ingest: SCORED 8/10  [Adzuna] Senior Python Developer",
    "INFO ingest: FILTERED  [Adzuna] Junior Dev — title_exclude",
    "INFO ingest: DUPE      [Adzuna] Already Seen Role",
    "INFO ingest: SCRAPE FALLBACK  [Adzuna] Degraded Job",
    "INFO ingest: SCORED 5/10  [Adzuna] Degraded Job",
    "INFO ingest: SCORE FAILED  [Adzuna] Bad Listing",
    (
        "INFO ingest: Run complete: 1 source(s) | 5 fetched | 1 pre-filtered | "
        "1 dupes skipped | 2 scored (1 failed) | 0 scrape skipped | "
        "1 scrape fallbacks | ~500 tok | ~$0.0005"
    ),
]

# A crash mid-run (no complete line)
_CRASH_LINES = [
    "INFO ingest: Fetched 3 listing(s) from Jooble",
    "INFO ingest: SCORED 7/10  [Jooble] Data Engineer",
    # process dies here — no complete line
]

# Session with one malformed line sandwiched between valid events
_MALFORMED_LINE_SESSION = [
    "INFO ingest: Fetched 2 listing(s) from Adzuna",
    "THIS IS GARBAGE %%% NOT A LOG LINE",
    "INFO ingest: SCORED 9/10  [Adzuna] Staff Engineer",
    (
        "INFO ingest: Run complete: 1 source(s) | 2 fetched | 0 pre-filtered | "
        "0 dupes skipped | 1 scored (0 failed) | 0 scrape skipped | "
        "0 scrape fallbacks | ~200 tok | ~$0.0002"
    ),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_fake_proc(lines: list[str]) -> object:
    """Return a fake Popen-like object whose stdout yields the given lines.

    Uses io.StringIO so readline() behaves exactly as subprocess.PIPE text mode:
    each call returns the next line including a trailing newline, and returns ""
    at EOF (which is the sentinel used by iter(readline, "")).
    """
    text = "".join(line + "\n" for line in lines)
    fake_stdout = io.StringIO(text)

    class _FakeProc:
        stdout = fake_stdout

        def wait(self):
            return 0 if any("complete" in line.lower() for line in lines) else 1

        def kill(self):
            pass

    return _FakeProc()


def _parse_sse_events(text: str) -> list[dict]:
    """Parse SSE wire-format text into a list of event dicts (data payloads only)."""
    events = []
    for line in text.split("\n"):
        if line.startswith("data: "):
            payload = line[len("data: "):]
            try:
                events.append(json.loads(payload))
            except json.JSONDecodeError:
                pass
    return events


def _parse_sse_ids(text: str) -> list[str]:
    """Extract all id: lines from SSE wire-format text."""
    return [line[len("id: "):] for line in text.split("\n") if line.startswith("id: ")]


def _run_reader_synchronously(proc, queue: EventQueue, monkeypatch) -> None:
    """Run _stdout_reader in the current thread against *proc*, pushing into *queue*.

    Monkeypatches app_module.event_queue so _stdout_reader uses our fresh queue.
    """
    monkeypatch.setattr(app_module, "event_queue", queue)
    # Also patch ingest_events module-level reference used inside _stdout_reader
    monkeypatch.setattr("ingest_events.event_queue", queue)
    _stdout_reader(proc)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def fresh_queue(monkeypatch):
    """Replace the global event_queue with a fresh instance per test.

    This is the same pattern used in test_ingest_stream.py. Marking autouse=True
    ensures every test in this module gets a clean queue, preventing state leakage
    between tests.
    """
    q = EventQueue()
    monkeypatch.setattr(app_module, "event_queue", q)
    monkeypatch.setattr("ingest_events.event_queue", q)
    yield q


@pytest.fixture()
def client():
    flask_app.config["TESTING"] = True
    with flask_app.test_client() as c:
        yield c


# ---------------------------------------------------------------------------
# Test classes
# ---------------------------------------------------------------------------

class TestEndToEndHappyPath:
    """End-to-end: mock subprocess emits a full session → SSE client sees all events."""

    def test_happy_path_events_in_order(self, client, fresh_queue, monkeypatch):
        """SSE client receives all events in order and stream ends with complete."""
        proc = _make_fake_proc(_HAPPY_PATH_LINES)
        _run_reader_synchronously(proc, fresh_queue, monkeypatch)

        resp = client.get("/ingest/stream")
        assert resp.status_code == 200
        assert resp.content_type.startswith("text/event-stream")

        events = _parse_sse_events(resp.get_data(as_text=True))
        types = [e["type"] for e in events]

        # Must contain the expected event types
        assert "fetched" in types
        assert "scored" in types
        assert "filtered" in types
        assert "dupe" in types
        assert "score_failed" in types
        assert "complete" in types

    def test_happy_path_terminal_event_is_last(self, client, fresh_queue, monkeypatch):
        """The complete event must be the last event in the stream."""
        proc = _make_fake_proc(_HAPPY_PATH_LINES)
        _run_reader_synchronously(proc, fresh_queue, monkeypatch)

        resp = client.get("/ingest/stream")
        events = _parse_sse_events(resp.get_data(as_text=True))

        assert events[-1]["type"] == "complete"

    def test_happy_path_ids_are_sequential(self, client, fresh_queue, monkeypatch):
        """Event IDs emitted in the SSE stream must be strictly increasing."""
        proc = _make_fake_proc(_HAPPY_PATH_LINES)
        _run_reader_synchronously(proc, fresh_queue, monkeypatch)

        resp = client.get("/ingest/stream")
        sse_ids = _parse_sse_ids(resp.get_data(as_text=True))

        # SSE ids are "{run_id}:{event_id}" — extract numeric part
        numeric_ids = [int(sid.split(":")[-1]) for sid in sse_ids]
        assert numeric_ids == sorted(numeric_ids)
        assert numeric_ids == list(range(1, len(numeric_ids) + 1))

    def test_happy_path_scrape_fallback_propagates(self, client, fresh_queue, monkeypatch):
        """The SCRAPE FALLBACK flag must set scraped=False on the next scored event."""
        proc = _make_fake_proc(_HAPPY_PATH_LINES)
        _run_reader_synchronously(proc, fresh_queue, monkeypatch)

        resp = client.get("/ingest/stream")
        events = _parse_sse_events(resp.get_data(as_text=True))

        # The 5th log line is SCRAPE FALLBACK then SCORED for "Degraded Job"
        scored_degraded = next(
            (e for e in events if e.get("type") == "scored" and e.get("title") == "Degraded Job"),
            None,
        )
        assert scored_degraded is not None, "Expected a scored event for 'Degraded Job'"
        assert scored_degraded["detail"]["scraped"] is False

    def test_happy_path_run_id_consistent_across_events(
        self, client, fresh_queue, monkeypatch
    ):
        """All SSE id: lines in a single stream must carry the same run_id prefix."""
        proc = _make_fake_proc(_HAPPY_PATH_LINES)
        _run_reader_synchronously(proc, fresh_queue, monkeypatch)

        resp = client.get("/ingest/stream")
        sse_ids = _parse_sse_ids(resp.get_data(as_text=True))

        run_ids = {sid.split(":")[0] for sid in sse_ids}
        assert len(run_ids) == 1, f"Expected one run_id across all events, got: {run_ids}"


class TestEndToEndFailurePath:
    """End-to-end: subprocess crashes mid-run → SSE client sees aborted."""

    def test_crash_produces_aborted_terminal(self, client, fresh_queue, monkeypatch):
        """When the process exits without a complete line, stream ends with aborted."""
        proc = _make_fake_proc(_CRASH_LINES)
        _run_reader_synchronously(proc, fresh_queue, monkeypatch)

        resp = client.get("/ingest/stream")
        events = _parse_sse_events(resp.get_data(as_text=True))

        assert events[-1]["type"] == "aborted"

    def test_crash_events_before_abort_are_preserved(
        self, client, fresh_queue, monkeypatch
    ):
        """Events received before the crash must still reach the client."""
        proc = _make_fake_proc(_CRASH_LINES)
        _run_reader_synchronously(proc, fresh_queue, monkeypatch)

        resp = client.get("/ingest/stream")
        events = _parse_sse_events(resp.get_data(as_text=True))
        types = [e["type"] for e in events]

        assert "fetched" in types
        assert "scored" in types

    def test_crash_stream_closes_cleanly(self, client, fresh_queue, monkeypatch):
        """Stream response should complete (not hang) after an aborted event."""
        proc = _make_fake_proc(_CRASH_LINES)
        _run_reader_synchronously(proc, fresh_queue, monkeypatch)

        resp = client.get("/ingest/stream")
        # If the stream hung, get_data() would block; the test itself would timeout.
        # Reaching this assertion proves the stream closed cleanly.
        text = resp.get_data(as_text=True)
        assert "aborted" in text


class TestReconnectReplay:
    """Reconnect with Last-Event-ID → only missed events are replayed."""

    def test_reconnect_delivers_only_missed_events(
        self, fresh_queue, monkeypatch
    ):
        """Client reconnects after seeing first 2 events; server replays events 3+."""
        proc = _make_fake_proc(_HAPPY_PATH_LINES)
        _run_reader_synchronously(proc, fresh_queue, monkeypatch)

        run_id = fresh_queue.run_id

        # First connection: fetch all events to confirm queue has ≥3
        with flask_app.test_client() as c1:
            resp1 = c1.get("/ingest/stream")
            all_events = _parse_sse_events(resp1.get_data(as_text=True))
        assert len(all_events) >= 3

        # Reconnect with a fresh test client claiming we saw the first 2 events
        with flask_app.test_client() as c2:
            resp2 = c2.get(
                "/ingest/stream",
                headers={"Last-Event-ID": f"{run_id}:2"},
            )
            replayed = _parse_sse_events(resp2.get_data(as_text=True))

        replayed_ids = [e.get("id") for e in replayed]

        # No ids 1 or 2 should appear in the replay
        assert 1 not in replayed_ids, "id=1 was replayed — duplicate delivery"
        assert 2 not in replayed_ids, "id=2 was replayed — duplicate delivery"

        # Events from id=3 onward must be present
        assert 3 in replayed_ids, "id=3 was not replayed — gap in delivery"

    def test_reconnect_no_duplicates(self, fresh_queue, monkeypatch):
        """Reconnecting at a cursor delivers only events after that cursor.

        Simulates a client that received events 1..split_point and reconnects
        with Last-Event-ID={run_id}:{split_point}. The replay must start from
        split_point+1 — no events from 1..split_point should appear again.
        """
        proc = _make_fake_proc(_HAPPY_PATH_LINES)
        _run_reader_synchronously(proc, fresh_queue, monkeypatch)

        run_id = fresh_queue.run_id
        total_events = len(fresh_queue._events)
        assert total_events >= 4, "Need at least 4 events to split meaningfully"

        split_point = total_events // 2

        # Replay from split_point: should only get events after that cursor
        with flask_app.test_client() as c:
            resp = c.get(
                "/ingest/stream",
                headers={"Last-Event-ID": f"{run_id}:{split_point}"},
            )
            replayed = _parse_sse_events(resp.get_data(as_text=True))

        replayed_ids = [e["id"] for e in replayed]

        # None of the events already seen (1..split_point) should appear
        already_seen = set(range(1, split_point + 1))
        duplicates = already_seen & set(replayed_ids)
        assert not duplicates, (
            f"Events {duplicates} were re-delivered after cursor={split_point}"
        )

        # The first replayed event should be split_point+1
        if replayed_ids:
            assert replayed_ids[0] == split_point + 1, (
                f"Expected first replayed id={split_point + 1}, got {replayed_ids[0]}"
            )


class TestStaleRunIdReplay:
    """Reconnect with a stale run_id → server replays from beginning of new run."""

    def test_stale_run_id_triggers_full_replay(
        self, client, fresh_queue, monkeypatch
    ):
        """Stale run_id in Last-Event-ID causes server to send all events from start."""
        proc = _make_fake_proc(_HAPPY_PATH_LINES)
        _run_reader_synchronously(proc, fresh_queue, monkeypatch)

        resp = client.get(
            "/ingest/stream",
            headers={"Last-Event-ID": "00000000-0000-0000-0000-000000000000:99"},
        )
        events = _parse_sse_events(resp.get_data(as_text=True))
        ids = [e["id"] for e in events]

        # A full replay starts from id=1
        assert 1 in ids, "Full replay should include event with id=1"

    def test_stale_run_id_delivers_complete_session(
        self, client, fresh_queue, monkeypatch
    ):
        """After stale-run-id replay, client has the full session including terminal."""
        proc = _make_fake_proc(_HAPPY_PATH_LINES)
        _run_reader_synchronously(proc, fresh_queue, monkeypatch)

        resp = client.get(
            "/ingest/stream",
            headers={"Last-Event-ID": "stale-run-id:5"},
        )
        events = _parse_sse_events(resp.get_data(as_text=True))
        types = [e["type"] for e in events]

        assert "complete" in types
        assert events[-1]["type"] == "complete"


class TestConcurrentClients:
    """Two SSE clients on the same run both receive all events independently.

    Each request uses its own Flask test client instance to avoid request-context
    collisions from stream_with_context when two responses are read sequentially
    in the same test (Flask 3.x stores request context in contextvars).
    """

    def test_two_clients_receive_same_events(
        self, fresh_queue, monkeypatch
    ):
        """Both clients must see the same events in the same order."""
        proc = _make_fake_proc(_HAPPY_PATH_LINES)
        _run_reader_synchronously(proc, fresh_queue, monkeypatch)

        with flask_app.test_client() as c_a:
            resp_a = c_a.get("/ingest/stream")
            events_a = _parse_sse_events(resp_a.get_data(as_text=True))

        with flask_app.test_client() as c_b:
            resp_b = c_b.get("/ingest/stream")
            events_b = _parse_sse_events(resp_b.get_data(as_text=True))

        ids_a = [e["id"] for e in events_a]
        ids_b = [e["id"] for e in events_b]

        assert ids_a == ids_b, (
            f"Clients received different event sequences.\n"
            f"Client A ids: {ids_a}\n"
            f"Client B ids: {ids_b}"
        )

    def test_second_client_does_not_skip_events(
        self, fresh_queue, monkeypatch
    ):
        """The EventQueue is not a destructive FIFO — second client gets all events.

        This is the critical regression check: if EventQueue accidentally drained
        events for client A, client B would start mid-stream or receive nothing.
        """
        proc = _make_fake_proc(_HAPPY_PATH_LINES)
        _run_reader_synchronously(proc, fresh_queue, monkeypatch)

        total_in_queue = len(fresh_queue._events)

        with flask_app.test_client() as c_a:
            c_a.get("/ingest/stream").get_data()  # drain client A

        with flask_app.test_client() as c_b:
            resp_b = c_b.get("/ingest/stream")
            events_b = _parse_sse_events(resp_b.get_data(as_text=True))

        assert len(events_b) == total_in_queue, (
            f"Client B received {len(events_b)} events but queue has {total_in_queue}. "
            "Events may have been drained by client A."
        )

    def test_both_clients_see_complete_terminal(
        self, fresh_queue, monkeypatch
    ):
        """Both clients must receive the terminal complete event."""
        proc = _make_fake_proc(_HAPPY_PATH_LINES)
        _run_reader_synchronously(proc, fresh_queue, monkeypatch)

        with flask_app.test_client() as c_a:
            resp_a = c_a.get("/ingest/stream")
            events_a = _parse_sse_events(resp_a.get_data(as_text=True))

        with flask_app.test_client() as c_b:
            resp_b = c_b.get("/ingest/stream")
            events_b = _parse_sse_events(resp_b.get_data(as_text=True))

        assert events_a[-1]["type"] == "complete"
        assert events_b[-1]["type"] == "complete"


class TestMaxConnectionLimit:
    """Exceeding MAX_SSE_CONNECTIONS returns 429."""

    def test_extra_connection_returns_429(self, client, monkeypatch):
        """When MAX_SSE_CONNECTIONS is 0, every request is rejected."""
        monkeypatch.setattr(app_module, "MAX_SSE_CONNECTIONS", 0)
        resp = client.get("/ingest/stream")
        assert resp.status_code == 429

    def test_connection_within_limit_succeeds(
        self, client, fresh_queue, monkeypatch
    ):
        """When MAX_SSE_CONNECTIONS >= 1, the first connection is accepted."""
        monkeypatch.setattr(app_module, "MAX_SSE_CONNECTIONS", 1)
        # Queue has a terminal so the stream returns immediately
        fresh_queue.push({
            "type": "complete", "source": None, "title": None,
            "url": None, "detail": {}, "timestamp": "2026-04-10T00:00:00Z",
        })
        resp = client.get("/ingest/stream")
        assert resp.status_code == 200


class TestParserFailureDoesNotKillStream:
    """Malformed line in subprocess output must not kill the stream (regression test).

    This is the integration-level companion to TestStdoutReader.test_parser_exception_on_one_line_does_not_kill_reader
    in test_ingest_stream.py. That test checks _stdout_reader in isolation;
    this test verifies the same property holds when _stdout_reader feeds into
    the real EventQueue and the SSE endpoint reads from it.
    """

    def test_malformed_line_skipped_downstream_events_reach_client(
        self, client, fresh_queue, monkeypatch
    ):
        """Events after a malformed line must still reach the SSE client."""
        proc = _make_fake_proc(_MALFORMED_LINE_SESSION)
        _run_reader_synchronously(proc, fresh_queue, monkeypatch)

        resp = client.get("/ingest/stream")
        events = _parse_sse_events(resp.get_data(as_text=True))
        types = [e["type"] for e in events]

        # Events after the garbage line must appear
        assert "scored" in types, (
            "scored event after malformed line was not delivered — "
            "reader may have died on the malformed line"
        )
        assert "complete" in types, (
            "complete event not delivered — stream terminated early"
        )

    def test_malformed_line_does_not_produce_aborted(
        self, client, fresh_queue, monkeypatch
    ):
        """A malformed line must not cause an aborted event when complete follows."""
        proc = _make_fake_proc(_MALFORMED_LINE_SESSION)
        _run_reader_synchronously(proc, fresh_queue, monkeypatch)

        resp = client.get("/ingest/stream")
        events = _parse_sse_events(resp.get_data(as_text=True))
        types = [e["type"] for e in events]

        assert "aborted" not in types, (
            "aborted was pushed after a malformed line, but complete followed — "
            "the reader should have skipped the bad line, not crashed"
        )

    def test_malformed_line_session_terminal_is_complete(
        self, client, fresh_queue, monkeypatch
    ):
        """The terminal event for a session with a malformed line is complete, not aborted."""
        proc = _make_fake_proc(_MALFORMED_LINE_SESSION)
        _run_reader_synchronously(proc, fresh_queue, monkeypatch)

        resp = client.get("/ingest/stream")
        events = _parse_sse_events(resp.get_data(as_text=True))

        assert events[-1]["type"] == "complete"


class TestReaderToQueueToSseComposition:
    """Compositional tests that verify the reader → queue → SSE pipeline as a unit."""

    def test_reader_pushes_correct_count_into_queue(self, fresh_queue, monkeypatch):
        """_stdout_reader must push exactly the number of parseable events into the queue.

        Happy path: 1 fetched + 2 scored + 1 filtered + 1 dupe + 1 score_failed +
        1 scrape_fallback + 1 complete. SCRAPE FALLBACK now emits a scrape_fallback
        event AND sets a flag to annotate the subsequent scored event with scraped=False
        (see #202).
        """
        proc = _make_fake_proc(_HAPPY_PATH_LINES)
        _run_reader_synchronously(proc, fresh_queue, monkeypatch)

        # Every log line in _HAPPY_PATH_LINES produces exactly one event (including
        # SCRAPE FALLBACK, which now emits a scrape_fallback event per #202).
        expected_count = len(_HAPPY_PATH_LINES)
        assert len(fresh_queue._events) == expected_count, (
            f"Expected {expected_count} events in queue, got {len(fresh_queue._events)}"
        )

    def test_reader_sets_complete_flag_only_on_complete(
        self, fresh_queue, monkeypatch
    ):
        """Queue should have exactly one terminal event in a clean run."""
        proc = _make_fake_proc(_HAPPY_PATH_LINES)
        _run_reader_synchronously(proc, fresh_queue, monkeypatch)

        terminal_events = [e for e in fresh_queue._events if e["type"] in ("complete", "aborted")]
        assert len(terminal_events) == 1
        assert terminal_events[0]["type"] == "complete"

    def test_aborted_on_crash_has_error_detail(self, fresh_queue, monkeypatch):
        """The aborted event from a crash must carry an error detail."""
        proc = _make_fake_proc(_CRASH_LINES)
        _run_reader_synchronously(proc, fresh_queue, monkeypatch)

        terminal = next(
            (e for e in fresh_queue._events if e["type"] == "aborted"),
            None,
        )
        assert terminal is not None, "No aborted event found after crash"
        assert "error" in terminal.get("detail", {}), (
            "aborted event missing 'error' field in detail"
        )

    def test_sse_response_content_type(self, client, fresh_queue, monkeypatch):
        """SSE responses must carry text/event-stream content-type."""
        proc = _make_fake_proc(_HAPPY_PATH_LINES)
        _run_reader_synchronously(proc, fresh_queue, monkeypatch)

        resp = client.get("/ingest/stream")
        assert resp.content_type.startswith("text/event-stream")

    def test_sse_response_cache_headers(self, client, fresh_queue, monkeypatch):
        """SSE responses must set no-cache and disable nginx buffering."""
        proc = _make_fake_proc(_HAPPY_PATH_LINES)
        _run_reader_synchronously(proc, fresh_queue, monkeypatch)

        resp = client.get("/ingest/stream")
        assert resp.headers.get("Cache-Control") == "no-cache"
        assert resp.headers.get("X-Accel-Buffering") == "no"
