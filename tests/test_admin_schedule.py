"""
tests/test_admin_schedule.py — Tests for /admin/schedule-state route.

Covers badge logic (green/amber/red/none), empty-state rendering, run-table
output, and the presence of the HTMX trigger on the /admin page.  All DB
calls are mocked so no PostgreSQL connection is required.

``create=True`` is passed to every ``patch`` of ``app.db.get_recent_ingest_runs``
so the mock works even when the test runner resolves the ``db`` module from
the project root (which may not have the new function during worktree runs).
"""

import os
import sys
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import app as flask_app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_run(trigger="manual_cli", status="success", hours_ago=1, **kwargs):
    """Return a minimal ingest_runs dict for use in tests.

    Args:
        trigger:    ``trigger_source`` value.
        status:     Run status string.
        hours_ago:  How many hours ago the run started (controls ``started_at``).
        **kwargs:   Override any other field by name.
    """
    started = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    finished = started + timedelta(minutes=5)
    return {
        "id": kwargs.get("id", 1),
        "trigger_source": trigger,
        "started_at": started,
        "finished_at": finished,
        "status": status,
        "fetched": kwargs.get("fetched", 10),
        "filtered": kwargs.get("filtered", 3),
        "scored": kwargs.get("scored", 7),
        "failed_count": kwargs.get("failed_count", 0),
        "cost_usd": kwargs.get("cost_usd", 0.0050),
        "log_filename": kwargs.get("log_filename", "ingest_20260412_120000.log"),
        "error_message": kwargs.get("error_message", None),
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def client():
    flask_app.config["TESTING"] = True
    with flask_app.test_client() as c:
        yield c


# ---------------------------------------------------------------------------
# Empty state
# ---------------------------------------------------------------------------

class TestScheduleStateEmpty:
    def test_schedule_state_empty(self, client):
        """When no runs exist the response contains the empty-state message."""
        with patch("db.get_recent_ingest_runs", return_value=[], create=True):
            resp = client.get("/admin/schedule-state")
        assert resp.status_code == 200
        assert "No runs recorded yet" in resp.data.decode()

    def test_schedule_state_db_error_falls_back_to_empty(self, client):
        """When db.get_recent_ingest_runs raises, the route still returns 200."""
        with patch("db.get_recent_ingest_runs",
                   side_effect=Exception("db down"), create=True):
            resp = client.get("/admin/schedule-state")
        assert resp.status_code == 200
        assert "No runs recorded yet" in resp.data.decode()


# ---------------------------------------------------------------------------
# Run table rendering
# ---------------------------------------------------------------------------

class TestScheduleStateWithRuns:
    def test_response_contains_run_data(self, client):
        """When runs exist the response renders a table with trigger and status."""
        runs = [_make_run(trigger="manual_cli", status="success", hours_ago=2)]
        with patch("db.get_recent_ingest_runs", return_value=runs, create=True):
            resp = client.get("/admin/schedule-state")
        body = resp.data.decode()
        assert resp.status_code == 200
        assert "manual_cli" in body
        assert "success" in body

    def test_response_contains_cost(self, client):
        """Cost column is rendered with four decimal places."""
        runs = [_make_run(cost_usd=0.0123)]
        with patch("db.get_recent_ingest_runs", return_value=runs, create=True):
            resp = client.get("/admin/schedule-state")
        assert "0.0123" in resp.data.decode()

    def test_response_contains_log_download_link(self, client):
        """Log filename is rendered as a download link."""
        runs = [_make_run(log_filename="ingest_20260412_120000.log")]
        with patch("db.get_recent_ingest_runs", return_value=runs, create=True):
            resp = client.get("/admin/schedule-state")
        body = resp.data.decode()
        assert "/admin/logs/ingest_20260412_120000.log/download" in body

    def test_no_log_filename_shows_dash(self, client):
        """When log_filename is None the cell shows an em-dash placeholder."""
        runs = [_make_run(log_filename=None)]
        with patch("db.get_recent_ingest_runs", return_value=runs, create=True):
            resp = client.get("/admin/schedule-state")
        assert "—" in resp.data.decode()

    def test_error_message_shown_for_failed_run(self, client):
        """Error message is shown when the most recent run failed."""
        runs = [_make_run(status="failed", error_message="timeout connecting to DB")]
        with patch("db.get_recent_ingest_runs", return_value=runs, create=True):
            resp = client.get("/admin/schedule-state")
        assert "timeout connecting to DB" in resp.data.decode()


# ---------------------------------------------------------------------------
# Badge logic
# ---------------------------------------------------------------------------

class TestScheduleBadgeGreen:
    def test_recent_scheduled_success_is_green(self, client):
        """A successful scheduled run within 25 hours -> badge='green'."""
        runs = [_make_run(trigger="scheduled", status="success", hours_ago=12)]
        with patch("db.get_recent_ingest_runs", return_value=runs, create=True):
            resp = client.get("/admin/schedule-state")
        body = resp.data.decode()
        assert "schedule-badge--green" in body
        assert "Scheduler healthy" in body


class TestScheduleBadgeAmber:
    def test_scheduled_run_30h_ago_is_amber(self, client):
        """A successful scheduled run 30 hours ago -> badge='amber' (overdue)."""
        runs = [_make_run(trigger="scheduled", status="success", hours_ago=30)]
        with patch("db.get_recent_ingest_runs", return_value=runs, create=True):
            resp = client.get("/admin/schedule-state")
        body = resp.data.decode()
        assert "schedule-badge--amber" in body
        assert "25+ hours ago" in body

    def test_scheduled_run_running_is_amber(self, client):
        """A scheduled run currently in 'running' state -> badge='amber'."""
        runs = [_make_run(trigger="scheduled", status="running", hours_ago=0.1)]
        with patch("db.get_recent_ingest_runs", return_value=runs, create=True):
            resp = client.get("/admin/schedule-state")
        body = resp.data.decode()
        assert "schedule-badge--amber" in body
        assert "in progress" in body

    def test_no_scheduled_runs_is_none_badge(self, client):
        """Runs exist but none are scheduled -> badge='none'."""
        runs = [_make_run(trigger="manual_cli", status="success", hours_ago=1)]
        with patch("db.get_recent_ingest_runs", return_value=runs, create=True):
            resp = client.get("/admin/schedule-state")
        body = resp.data.decode()
        assert "schedule-badge--none" in body
        assert "No scheduled runs recorded" in body


class TestScheduleBadgeRed:
    def test_last_scheduled_failed_is_red(self, client):
        """A scheduled run with status='failed' -> badge='red'."""
        runs = [_make_run(trigger="scheduled", status="failed", hours_ago=5,
                          error_message="LLM timeout")]
        with patch("db.get_recent_ingest_runs", return_value=runs, create=True):
            resp = client.get("/admin/schedule-state")
        body = resp.data.decode()
        assert "schedule-badge--red" in body
        assert "failed" in body

    def test_no_scheduled_run_in_50h_is_red(self, client):
        """No scheduled run in 50+ hours -> badge='red', scheduler-down message."""
        runs = [_make_run(trigger="scheduled", status="success", hours_ago=50)]
        with patch("db.get_recent_ingest_runs", return_value=runs, create=True):
            resp = client.get("/admin/schedule-state")
        body = resp.data.decode()
        assert "schedule-badge--red" in body
        assert "Scheduler may be down" in body


# ---------------------------------------------------------------------------
# /admin page contains HTMX trigger
# ---------------------------------------------------------------------------

class TestSchedulePaneLoads:
    def test_admin_page_has_schedule_state_hx_get(self, client):
        """GET /admin contains the HTMX trigger for the schedule-state fragment."""
        resp = client.get("/admin")
        assert resp.status_code == 200
        body = resp.data.decode()
        assert 'hx-get="/admin/schedule-state"' in body
