"""
tests/test_unread_badge.py — Tests for the "New" badge feature (Issue #236).

Covers:
  - db.mark_opened(): sets opened_at on first call, no-ops on repeat calls
  - POST /listings/<id>/open: returns 204, marks the listing opened, is idempotent
  - GET /: new_count passed to template equals count of listings with opened_at IS NULL
  - Schema migration: opened_at column is added to the listings table
"""

from __future__ import annotations

import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import db
import app as app_module
from app import app as flask_app


# ---------------------------------------------------------------------------
# Helpers shared with test_db.py
# ---------------------------------------------------------------------------

def make_listing(
    source_id: str = "test-001",
    score: float = 8.0,
    dismissed: int = 0,
    applied: int = 0,
) -> dict:
    """Return a minimal listing dict suitable for db.insert_listing()."""
    return {
        "source": "adzuna",
        "source_id": source_id,
        "title": "Software Engineer",
        "company": "Acme Corp",
        "location": "New York, NY",
        "salary_min": None,
        "salary_max": None,
        "salary_is_predicted": 0,
        "contract_type": "permanent",
        "contract_time": "full_time",
        "description": "A great job.",
        "redirect_url": f"https://example.com/{source_id}",
        "created_at": "2026-01-01T00:00:00Z",
        "fetched_at": "2026-01-02T00:00:00Z",
        "score": score,
        "matched_skills": ["Python"],
        "missing_skills": [],
        "concerns": [],
        "verdict": "Good match.",
        "bookmarked": 0,
        "dismissed": dismissed,
        "seen": 1,
        "applied": applied,
        "job_type": None,
        "model_used": None,
        "posted_at": None,
    }


class TempDB:
    """Context manager: temp SQLite file, removed on exit."""

    def __enter__(self) -> str:
        self._fh = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._fh.close()
        self.path = self._fh.name
        db.init_db(self.path)
        return self.path

    def __exit__(self, *_):
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass


# ---------------------------------------------------------------------------
# Schema: opened_at column added by init_db
# ---------------------------------------------------------------------------

class TestOpenedAtColumn:
    def test_opened_at_column_exists_on_fresh_db(self):
        """init_db() creates the opened_at column on a fresh database."""
        with TempDB() as path:
            conn = db.get_connection(path)
            try:
                cols = {row["name"] for row in conn.execute("PRAGMA table_info(listings)").fetchall()}
                assert "opened_at" in cols
            finally:
                conn.close()

    def test_opened_at_defaults_to_null(self):
        """Newly inserted listings have opened_at = NULL."""
        with TempDB() as path:
            db.insert_listing(make_listing(source_id="default-null"), db_path=path)
            conn = db.get_connection(path)
            try:
                row = conn.execute("SELECT opened_at FROM listings WHERE source_id = ?", ("default-null",)).fetchone()
                assert row["opened_at"] is None
            finally:
                conn.close()

    def test_migration_adds_column_to_existing_db(self):
        """init_db() migration (path D) adds opened_at to an existing DB that lacks it."""
        with TempDB() as path:
            # Simulate a pre-existing DB without opened_at by rebuilding the table
            # to match the schema that would exist before this column was added.
            conn = db.get_connection(path)
            try:
                # SQLite does not support DROP COLUMN before 3.35. Use table-copy.
                conn.execute("ALTER TABLE listings RENAME TO listings_old")
                conn.execute("""
                    CREATE TABLE listings (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        source TEXT NOT NULL DEFAULT 'adzuna',
                        source_id TEXT NOT NULL,
                        title TEXT,
                        score REAL,
                        dismissed INTEGER DEFAULT 0,
                        applied INTEGER DEFAULT 0,
                        seen INTEGER DEFAULT 0,
                        bookmarked INTEGER DEFAULT 0,
                        redirect_url TEXT,
                        UNIQUE(source, source_id)
                    )
                """)
                conn.execute("DROP TABLE listings_old")
                conn.commit()
            finally:
                conn.close()

            # Re-running init_db should add opened_at via path D migrations.
            db.init_db(path)

            conn = db.get_connection(path)
            try:
                cols = {row["name"] for row in conn.execute("PRAGMA table_info(listings)").fetchall()}
                assert "opened_at" in cols
            finally:
                conn.close()


# ---------------------------------------------------------------------------
# db.mark_opened
# ---------------------------------------------------------------------------

class TestMarkOpened:
    def test_sets_opened_at_on_first_call(self):
        """mark_opened() sets opened_at to a non-null timestamp on first call."""
        with TempDB() as path:
            db.insert_listing(make_listing(source_id="open-001"), db_path=path)
            conn = db.get_connection(path)
            try:
                listing_id = conn.execute(
                    "SELECT id FROM listings WHERE source_id = ?", ("open-001",)
                ).fetchone()["id"]
            finally:
                conn.close()

            db.mark_opened(listing_id, db_path=path)

            conn = db.get_connection(path)
            try:
                row = conn.execute(
                    "SELECT opened_at FROM listings WHERE id = ?", (listing_id,)
                ).fetchone()
                assert row["opened_at"] is not None
            finally:
                conn.close()

    def test_opened_at_is_idempotent(self):
        """Calling mark_opened() twice preserves the first timestamp."""
        with TempDB() as path:
            db.insert_listing(make_listing(source_id="open-002"), db_path=path)
            conn = db.get_connection(path)
            try:
                listing_id = conn.execute(
                    "SELECT id FROM listings WHERE source_id = ?", ("open-002",)
                ).fetchone()["id"]
            finally:
                conn.close()

            db.mark_opened(listing_id, db_path=path)
            conn = db.get_connection(path)
            try:
                first_ts = conn.execute(
                    "SELECT opened_at FROM listings WHERE id = ?", (listing_id,)
                ).fetchone()["opened_at"]
            finally:
                conn.close()

            # Second call must not overwrite the first timestamp.
            db.mark_opened(listing_id, db_path=path)
            conn = db.get_connection(path)
            try:
                second_ts = conn.execute(
                    "SELECT opened_at FROM listings WHERE id = ?", (listing_id,)
                ).fetchone()["opened_at"]
            finally:
                conn.close()

            assert first_ts == second_ts

    def test_mark_opened_nonexistent_id_is_silent(self):
        """mark_opened() with a non-existent id does not raise."""
        with TempDB() as path:
            db.mark_opened(99999, db_path=path)  # Should not raise.

    def test_opened_at_format_is_iso_utc(self):
        """opened_at is stored as an ISO 8601 UTC string ending in 'Z'."""
        with TempDB() as path:
            db.insert_listing(make_listing(source_id="open-fmt"), db_path=path)
            conn = db.get_connection(path)
            try:
                listing_id = conn.execute(
                    "SELECT id FROM listings WHERE source_id = ?", ("open-fmt",)
                ).fetchone()["id"]
            finally:
                conn.close()

            db.mark_opened(listing_id, db_path=path)

            conn = db.get_connection(path)
            try:
                ts = conn.execute(
                    "SELECT opened_at FROM listings WHERE id = ?", (listing_id,)
                ).fetchone()["opened_at"]
            finally:
                conn.close()

            assert ts.endswith("Z"), f"Expected ISO UTC format ending in Z, got: {ts!r}"
            # Verify it parses as a valid ISO datetime.
            import datetime
            dt = datetime.datetime.fromisoformat(ts.rstrip("Z"))
            assert isinstance(dt, datetime.datetime)


# ---------------------------------------------------------------------------
# POST /listings/<id>/open  route
# ---------------------------------------------------------------------------

@pytest.fixture()
def client(tmp_path, monkeypatch):
    """Flask test client pointing at a temp database."""
    db_path = str(tmp_path / "test.db")
    db.init_db(db_path)
    monkeypatch.setattr(app_module, "DB_PATH", db_path)
    flask_app.config["TESTING"] = True
    with flask_app.test_client() as c:
        yield c, db_path


class TestMarkListingOpenedRoute:
    def test_returns_204(self, client):
        """POST /listings/<id>/open returns 204 No Content."""
        c, db_path = client
        db.insert_listing(make_listing(source_id="route-001"), db_path=db_path)
        conn = db.get_connection(db_path)
        try:
            listing_id = conn.execute(
                "SELECT id FROM listings WHERE source_id = ?", ("route-001",)
            ).fetchone()["id"]
        finally:
            conn.close()

        resp = c.post(f"/listings/{listing_id}/open")
        assert resp.status_code == 204

    def test_marks_listing_as_opened(self, client):
        """POST /listings/<id>/open sets opened_at on the listing."""
        c, db_path = client
        db.insert_listing(make_listing(source_id="route-002"), db_path=db_path)
        conn = db.get_connection(db_path)
        try:
            listing_id = conn.execute(
                "SELECT id FROM listings WHERE source_id = ?", ("route-002",)
            ).fetchone()["id"]
        finally:
            conn.close()

        c.post(f"/listings/{listing_id}/open")

        conn = db.get_connection(db_path)
        try:
            row = conn.execute(
                "SELECT opened_at FROM listings WHERE id = ?", (listing_id,)
            ).fetchone()
            assert row["opened_at"] is not None
        finally:
            conn.close()

    def test_is_idempotent_via_route(self, client):
        """Calling POST /listings/<id>/open twice preserves the first timestamp."""
        c, db_path = client
        db.insert_listing(make_listing(source_id="route-003"), db_path=db_path)
        conn = db.get_connection(db_path)
        try:
            listing_id = conn.execute(
                "SELECT id FROM listings WHERE source_id = ?", ("route-003",)
            ).fetchone()["id"]
        finally:
            conn.close()

        c.post(f"/listings/{listing_id}/open")
        conn = db.get_connection(db_path)
        try:
            first_ts = conn.execute(
                "SELECT opened_at FROM listings WHERE id = ?", (listing_id,)
            ).fetchone()["opened_at"]
        finally:
            conn.close()

        c.post(f"/listings/{listing_id}/open")
        conn = db.get_connection(db_path)
        try:
            second_ts = conn.execute(
                "SELECT opened_at FROM listings WHERE id = ?", (listing_id,)
            ).fetchone()["opened_at"]
        finally:
            conn.close()

        assert first_ts == second_ts

    def test_nonexistent_listing_returns_204(self, client):
        """POST /listings/<id>/open for a missing id still returns 204 (benign)."""
        c, _ = client
        resp = c.post("/listings/99999/open")
        assert resp.status_code == 204


# ---------------------------------------------------------------------------
# GET / — new_count in feed template context
# ---------------------------------------------------------------------------

class TestFeedNewCount:
    def test_new_count_equals_unread_listings(self, client):
        """GET / passes new_count equal to listings with opened_at IS NULL."""
        c, db_path = client
        # Insert 3 listings: 2 unread, 1 opened.
        db.insert_listing(make_listing(source_id="feed-a"), db_path=db_path)
        db.insert_listing(make_listing(source_id="feed-b"), db_path=db_path)
        db.insert_listing(make_listing(source_id="feed-c"), db_path=db_path)

        # Mark one as opened.
        conn = db.get_connection(db_path)
        try:
            listing_id = conn.execute(
                "SELECT id FROM listings WHERE source_id = ?", ("feed-c",)
            ).fetchone()["id"]
        finally:
            conn.close()
        db.mark_opened(listing_id, db_path=db_path)

        resp = c.get("/")
        assert resp.status_code == 200
        # "2 new" should appear in the feed-meta section.
        assert b"2 new" in resp.data

    def test_new_count_zero_hides_label(self, client):
        """GET / does not render '· N new' when all listings are opened."""
        c, db_path = client
        db.insert_listing(make_listing(source_id="feed-z"), db_path=db_path)
        conn = db.get_connection(db_path)
        try:
            listing_id = conn.execute(
                "SELECT id FROM listings WHERE source_id = ?", ("feed-z",)
            ).fetchone()["id"]
        finally:
            conn.close()
        db.mark_opened(listing_id, db_path=db_path)

        resp = c.get("/")
        assert resp.status_code == 200
        assert b"new" not in resp.data or b"feed-new-count" not in resp.data

    def test_new_badge_present_for_unread_listing(self, client):
        """Unread cards render the badge-new span."""
        c, db_path = client
        db.insert_listing(make_listing(source_id="badge-present"), db_path=db_path)

        resp = c.get("/")
        assert resp.status_code == 200
        assert b'class="badge-new"' in resp.data

    def test_new_badge_absent_for_opened_listing(self, client):
        """Opened cards do not render the badge-new span."""
        c, db_path = client
        db.insert_listing(make_listing(source_id="badge-absent"), db_path=db_path)
        conn = db.get_connection(db_path)
        try:
            listing_id = conn.execute(
                "SELECT id FROM listings WHERE source_id = ?", ("badge-absent",)
            ).fetchone()["id"]
        finally:
            conn.close()
        db.mark_opened(listing_id, db_path=db_path)

        resp = c.get("/")
        assert resp.status_code == 200
        assert b'class="badge-new"' not in resp.data
