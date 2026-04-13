"""
tests/test_ingest_runs_db.py — Integration tests for ingest_runs DB helpers.

Requires DATABASE_URL to be set in the environment (PostgreSQL).
All tests are skipped when DATABASE_URL is absent so CI without a live DB
still passes.

Each test cleans up the rows it inserts so tests can be run repeatedly
against a shared dev database.
"""

import os
import sys

import pytest

# Prepend the worktree's directory so that ``import db`` picks up the worktree's
# db.py (which has the new ingest_runs functions) rather than the main repo's.
_WORKTREE_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, _WORKTREE_DIR)

_DB_URL = os.environ.get("DATABASE_URL", "")
_SKIP_DB = not _DB_URL or "dummy" in _DB_URL

pytestmark = pytest.mark.skipif(
    _SKIP_DB,
    reason="DATABASE_URL not set or points to dummy — skipping live-DB ingest_runs tests",
)

import db  # noqa: E402  (import after sys.path manipulation)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cleanup_run(run_id: int) -> None:
    """Delete an ingest_runs row by id."""
    with db.get_connection() as conn:
        conn.execute("DELETE FROM ingest_runs WHERE id = %s", (run_id,))


def _get_run(run_id: int) -> dict | None:
    """Fetch a single ingest_runs row as a dict, or None if not found."""
    with db.get_connection() as conn:
        cur = conn.execute(
            "SELECT * FROM ingest_runs WHERE id = %s", (run_id,)
        )
        columns = [desc[0] for desc in cur.description]
        row = cur.fetchone()
    if row is None:
        return None
    return dict(zip(columns, row))


# ---------------------------------------------------------------------------
# create_ingest_run / finish_ingest_run / get_recent_ingest_runs
# ---------------------------------------------------------------------------

class TestCreateAndFinishRun:
    def test_create_and_finish_success(self):
        """create_ingest_run then finish with success stores all metrics."""
        run_id = db.create_ingest_run(
            trigger_source="manual_cli",
            log_filename="ingest_20260412_120000.log",
        )
        try:
            # Row should exist immediately with status='running'
            row = _get_run(run_id)
            assert row is not None
            assert row["status"] == "running"
            assert row["trigger_source"] == "manual_cli"
            assert row["log_filename"] == "ingest_20260412_120000.log"
            assert row["finished_at"] is None

            db.finish_ingest_run(
                run_id,
                status="success",
                counts={"fetched": 20, "filtered": 5, "scored": 14, "failed": 1},
                cost_usd=0.0250,
            )

            row = _get_run(run_id)
            assert row["status"] == "success"
            assert row["fetched"] == 20
            assert row["filtered"] == 5
            assert row["scored"] == 14
            assert row["failed_count"] == 1
            assert float(row["cost_usd"]) == pytest.approx(0.0250, abs=1e-5)
            assert row["finished_at"] is not None
            assert row["error_message"] is None
        finally:
            _cleanup_run(run_id)

    def test_finish_run_failed_stores_error_message(self):
        """finish_ingest_run with status='failed' persists the error_message."""
        run_id = db.create_ingest_run(trigger_source="scheduled")
        try:
            db.finish_ingest_run(
                run_id,
                status="failed",
                error_message="Connection refused to PostgreSQL",
            )

            row = _get_run(run_id)
            assert row["status"] == "failed"
            assert "Connection refused" in row["error_message"]
            assert row["finished_at"] is not None
        finally:
            _cleanup_run(run_id)

    def test_finish_run_truncates_long_error_message(self):
        """error_message longer than 500 chars is truncated to 500."""
        long_msg = "x" * 600
        run_id = db.create_ingest_run(trigger_source="manual_cli")
        try:
            db.finish_ingest_run(run_id, status="failed", error_message=long_msg)
            row = _get_run(run_id)
            assert len(row["error_message"]) == 500
        finally:
            _cleanup_run(run_id)

    def test_get_recent_ingest_runs_returns_newest_first(self):
        """get_recent_ingest_runs returns rows newest-first."""
        id1 = db.create_ingest_run(trigger_source="manual_cli")
        id2 = db.create_ingest_run(trigger_source="scheduled")
        try:
            db.finish_ingest_run(id1, status="success")
            db.finish_ingest_run(id2, status="success")

            recent = db.get_recent_ingest_runs(limit=5)
            ids = [r["id"] for r in recent]
            # id2 was created after id1, so it should appear first
            assert ids.index(id2) < ids.index(id1)
        finally:
            _cleanup_run(id1)
            _cleanup_run(id2)

    def test_get_recent_ingest_runs_respects_limit(self):
        """get_recent_ingest_runs returns at most `limit` rows."""
        ids = []
        for _ in range(3):
            rid = db.create_ingest_run(trigger_source="manual_cli")
            db.finish_ingest_run(rid, status="success")
            ids.append(rid)
        try:
            recent = db.get_recent_ingest_runs(limit=2)
            # Should return at most 2 rows (though there may be more in the DB)
            assert len(recent) <= 2
        finally:
            for rid in ids:
                _cleanup_run(rid)


# ---------------------------------------------------------------------------
# Stale-row sweep in init_db
# ---------------------------------------------------------------------------

class TestStaleSweep:
    def test_stale_running_row_swept_to_failed(self):
        """init_db() marks stale 'running' rows (>1h old) as 'failed'."""
        # Insert a row with started_at 2 hours ago and status='running'
        with db.get_connection() as conn:
            cur = conn.execute(
                """INSERT INTO ingest_runs
                       (trigger_source, started_at, status)
                   VALUES ('manual_cli', NOW() - INTERVAL '2 hours', 'running')
                   RETURNING id""",
            )
            run_id = cur.fetchone()["id"]

        try:
            db.init_db()  # should sweep the row

            row = _get_run(run_id)
            assert row["status"] == "failed"
            assert "process died" in (row["error_message"] or "")
        finally:
            _cleanup_run(run_id)

    def test_recent_running_row_not_swept(self):
        """init_db() does NOT sweep rows started within the last hour."""
        run_id = db.create_ingest_run(trigger_source="manual_cli")
        try:
            db.init_db()  # should not sweep this fresh row

            row = _get_run(run_id)
            assert row["status"] == "running"
        finally:
            _cleanup_run(run_id)
