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
        # SSE format: "id: {run_id}:{N}\ndata: {json}\n\n"
        assert "id: " in text
        assert "data: " in text
        # Both events should appear
        lines = text.split("\n")
        id_lines = [l for l in lines if l.startswith("id: ")]
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
