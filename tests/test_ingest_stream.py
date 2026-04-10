"""Integration tests for the /ingest/stream SSE endpoint."""

import json
import socket
import threading
import time
import urllib.request

import pytest
import werkzeug.serving

import app as app_module
from app import app as flask_app
from ingest_events import EventQueue


@pytest.fixture(autouse=True)
def fresh_queue(monkeypatch):
    """Replace global event_queue with a fresh instance for each test.

    idle_grace=0 disables the startup grace-period wait so tests that
    exercise the idle path complete immediately rather than sleeping 3 s.
    """
    q = EventQueue(idle_grace=0)
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
        # SSE format: "id: {run_id}:{N}\ndata: {json}\n\n"
        assert "id: " in text
        assert "data: " in text
        # Both events should appear
        lines = text.split("\n")
        id_lines = [line for line in lines if line.startswith("id: ")]
        assert len(id_lines) == 2

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
        # Request replay from id=2 using run_id:event_id format
        run_id = fresh_queue.run_id
        resp = client.get(
            "/ingest/stream",
            headers={"Last-Event-ID": f"{run_id}:2"},
        )
        text = resp.get_data(as_text=True)
        # Should get events 3, 4, 5, and complete (6) — not 1 or 2
        assert f"{run_id}:3" in text
        assert f"{run_id}:6" in text
        assert f"{run_id}:1" not in text
        assert f"{run_id}:2" not in text

    def test_stale_run_id_replays_from_start(self, client, fresh_queue):
        fresh_queue.push({
            "type": "scored", "source": "A", "title": "Job",
            "url": None, "detail": {"score": 5}, "timestamp": "2026-04-09T00:00:00Z",
        })
        fresh_queue.push({
            "type": "complete", "source": None, "title": None,
            "url": None, "detail": {}, "timestamp": "2026-04-09T00:00:01Z",
        })
        # Stale run_id — should replay from beginning
        resp = client.get(
            "/ingest/stream",
            headers={"Last-Event-ID": "stale-uuid:5"},
        )
        text = resp.get_data(as_text=True)
        # Should replay from beginning — first event (id=1) must be present
        run_id = fresh_queue.run_id
        assert f"{run_id}:1" in text

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
        lines = [line for line in text.split("\n") if line.startswith("data: ")]
        last_data = json.loads(lines[-1].removeprefix("data: "))
        assert last_data["type"] == "complete"

    def test_aborted_closes_stream(self, client, fresh_queue):
        fresh_queue.push({
            "type": "aborted", "source": None, "title": None,
            "url": None, "detail": {"error": "crash"}, "timestamp": "2026-04-09T00:00:00Z",
        })
        resp = client.get("/ingest/stream")
        text = resp.get_data(as_text=True)
        lines = [line for line in text.split("\n") if line.startswith("data: ")]
        last_data = json.loads(lines[-1].removeprefix("data: "))
        assert last_data["type"] == "aborted"

    def test_max_connections_returns_429(self, client, fresh_queue, monkeypatch):
        monkeypatch.setattr(app_module, "MAX_SSE_CONNECTIONS", 0)
        resp = client.get("/ingest/stream")
        assert resp.status_code == 429

    def test_response_headers(self, client, fresh_queue):
        """SSE response must include correct cache-control headers."""
        resp = client.get("/ingest/stream")
        assert resp.headers.get("Cache-Control") == "no-cache"
        assert resp.headers.get("X-Accel-Buffering") == "no"


class TestStdoutReader:
    """Unit tests for _stdout_reader exception handling."""

    def test_parser_exception_on_one_line_does_not_kill_reader(
        self, monkeypatch, caplog
    ):
        """A parse error on one line must not kill the reader thread.

        Regression test for Fix 2: before the narrow try/except, an exception
        from parser.parse() would propagate out of the inner loop, be caught by
        the outer except, and push an 'aborted' event — dropping all subsequent
        lines including the terminal 'complete' event.
        """
        import io
        import logging
        import app as app_module
        from app import _stdout_reader

        # Build a fake EventQueue that records pushed events
        pushed = []

        class FakeQueue:
            def push(self, event):
                pushed.append(event)

        # Build a fake parser: line 1 → event, line 2 → raises, line 3 → event
        good_event_1 = {
            "type": "scored", "source": "A", "title": "Job 1",
            "url": None, "detail": {}, "timestamp": "2026-04-10T00:00:00Z",
        }
        good_event_3 = {
            "type": "complete", "source": None, "title": None,
            "url": None, "detail": {}, "timestamp": "2026-04-10T00:00:01Z",
        }
        call_count = [0]

        class FakeParser:
            def parse(self, line):
                call_count[0] += 1
                if call_count[0] == 1:
                    return good_event_1
                if call_count[0] == 2:
                    raise ValueError("malformed line")
                return good_event_3

        # Fake subprocess with 3 stdout lines
        fake_stdout = io.StringIO("line one\nline two\nline three\n")

        class FakeProc:
            stdout = fake_stdout
            def wait(self):
                return 0
            def kill(self):
                pass

        monkeypatch.setattr(app_module, "event_queue", FakeQueue())
        monkeypatch.setattr("app.IngestEventParser", FakeParser)

        with caplog.at_level(logging.ERROR, logger="app"):
            _stdout_reader(FakeProc())

        # 1. Reader did NOT exit after the exception — it continued and pushed
        #    the third event (complete).
        types_pushed = [e["type"] for e in pushed]
        assert "complete" in types_pushed, (
            f"complete event was never pushed — reader likely died after the exception. "
            f"Events received: {types_pushed}"
        )

        # 2. The event from the third line reached the queue.
        assert good_event_3 in pushed

        # 3. The exception was logged.
        assert any("IngestEventParser failed" in r.message for r in caplog.records), (
            f"Expected a logged error about the parse failure. Log records: "
            f"{[r.message for r in caplog.records]}"
        )


class TestIngestStreamEdgeCases:
    """Edge cases for the SSE endpoint."""

    def test_last_event_id_without_run_id(self, client, fresh_queue):
        """Plain numeric Last-Event-ID (no run_id prefix) should be handled gracefully."""
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
        """Garbage Last-Event-ID should cause a replay from the beginning."""
        fresh_queue.push({
            "type": "complete", "source": None, "title": None,
            "url": None, "detail": {}, "timestamp": "2026-04-09T00:00:00Z",
        })
        resp = client.get("/ingest/stream", headers={"Last-Event-ID": "garbage"})
        assert resp.status_code == 200
        text = resp.get_data(as_text=True)
        assert "id:" in text  # replays from start


def _find_free_port() -> int:
    """Return an available TCP port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TestStreamingLatency:
    """Regression test: events must arrive at the HTTP client one-by-one in
    real time, not buffered and delivered in a single burst at the end.

    Uses a real Werkzeug dev server (not the Flask test client, which consumes
    the generator eagerly and cannot measure per-event delivery timing).

    Pass criteria: each of N events must arrive at the client within 200 ms of
    being pushed into the EventQueue, even though pushes are spaced 100 ms apart.
    If the stack were buffering, all events would arrive at roughly t=N*100 ms.
    """

    N_EVENTS = 5
    PUSH_INTERVAL_S = 0.1   # 100 ms between pushes
    MAX_LAG_S = 0.200        # 200 ms max acceptable push-to-arrival lag

    @pytest.fixture()
    def live_server(self, monkeypatch):
        """Start a real Werkzeug HTTP server in a daemon thread.

        Patches the global event_queue used by app.py with a fresh EventQueue
        that has a 5 s idle_grace (long enough for the pusher thread to start).
        Yields (server_url, queue).
        """
        q = EventQueue(idle_grace=5.0)
        monkeypatch.setattr(app_module, "event_queue", q)
        monkeypatch.setattr("ingest_events.event_queue", q)

        port = _find_free_port()
        server = werkzeug.serving.make_server("127.0.0.1", port, flask_app, threaded=True)
        srv_thread = threading.Thread(target=server.serve_forever, daemon=True)
        srv_thread.start()
        time.sleep(0.1)  # let the server socket come up
        yield f"http://127.0.0.1:{port}", q
        server.shutdown()

    def test_events_arrive_per_push_not_all_at_end(self, live_server):
        """Each pushed event must reach the HTTP client within 200 ms.

        The pusher thread emits N_EVENTS with PUSH_INTERVAL_S spacing.
        The consumer measures wall-clock arrival time for each SSE chunk.
        If any event's (arrival_time - push_time) > MAX_LAG_S, the test fails —
        that would indicate the stack is buffering events instead of streaming.
        """
        server_url, q = live_server

        push_times: list[float] = []

        def pusher():
            # Small startup gap so the HTTP connection is open before first push
            time.sleep(0.15)
            for i in range(self.N_EVENTS):
                push_times.append(time.monotonic())
                q.push({
                    "type": "scored",
                    "source": "TestSource",
                    "title": f"Job {i}",
                    "url": None,
                    "detail": {"score": i},
                    "timestamp": "2026-04-10T00:00:00Z",
                })
                time.sleep(self.PUSH_INTERVAL_S)
            # Terminal event to close the stream
            push_times.append(time.monotonic())
            q.push({
                "type": "complete",
                "source": None,
                "title": None,
                "url": None,
                "detail": {},
                "timestamp": "2026-04-10T00:00:00Z",
            })

        push_thread = threading.Thread(target=pusher, daemon=True)
        push_thread.start()

        # Open SSE connection and record when each \n\n-terminated chunk arrives
        arrival_times: list[float] = []
        event_types: list[str] = []
        req = urllib.request.Request(f"{server_url}/ingest/stream")
        with urllib.request.urlopen(req, timeout=10) as resp:
            buf = b""
            while True:
                chunk = resp.read(1)
                if not chunk:
                    break
                buf += chunk
                if buf.endswith(b"\n\n"):
                    arrival_t = time.monotonic()
                    for line in buf.decode().split("\n"):
                        if line.startswith("data: "):
                            try:
                                ev = json.loads(line[6:])
                                arrival_times.append(arrival_t)
                                event_types.append(ev.get("type", "?"))
                            except (json.JSONDecodeError, KeyError):
                                pass
                    buf = b""
                    if event_types and event_types[-1] == "complete":
                        break

        push_thread.join(timeout=5)

        assert len(arrival_times) == self.N_EVENTS + 1, (
            f"Expected {self.N_EVENTS + 1} events (scored×{self.N_EVENTS} + complete), "
            f"got {len(arrival_times)}: {event_types}"
        )

        # Check per-event push-to-arrival lag
        lags = [
            arrival_times[i] - push_times[i]
            for i in range(len(arrival_times))
        ]
        over_budget = [
            (i, lag, event_types[i])
            for i, lag in enumerate(lags)
            if lag > self.MAX_LAG_S
        ]

        assert not over_budget, (
            f"Events arrived too late — possible buffering.\n"
            f"Events over {self.MAX_LAG_S * 1000:.0f} ms budget: "
            + ", ".join(
                f"event[{i}] ({etype}) lag={lag*1000:.1f}ms"
                for i, lag, etype in over_budget
            )
            + f"\nAll lags (ms): {[round(lag_ms*1000, 1) for lag_ms in lags]}"
        )
