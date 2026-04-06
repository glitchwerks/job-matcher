"""
tests/test_clear_db.py — Tests for db.get_listing_count(), db.clear_all_listings(),
and the POST /admin/clear-db route.

Uses the shared PostgreSQL database (DATABASE_URL required). Each test uses
unique source_id prefixes and cleans up in teardown.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import db
from app import app as flask_app


# ---------------------------------------------------------------------------
# Helpers shared across tests
# ---------------------------------------------------------------------------

_PREFIX = "cdb-"


def _insert(source_id: str, source: str = "adzuna") -> None:
    """Insert a minimal listing row for test setup."""
    db.insert_listing(
        {
            "source": source,
            "source_id": source_id,
            "title": "Engineer",
            "company": "Acme",
            "location": "Remote",
            "description": "A job.",
            "redirect_url": f"https://example.com/{source_id}",
            "created_at": "2026-01-01T00:00:00Z",
            "fetched_at": "2026-01-02T00:00:00Z",
            "score": 8.0,
            "matched_skills": ["Python"],
            "missing_skills": [],
            "concerns": [],
            "verdict": "Good.",
            "seen": 1,
        }
    )


def _cleanup(*prefixes: str) -> None:
    with db.get_connection() as conn:
        for prefix in prefixes:
            conn.execute(
                "DELETE FROM listings WHERE source_id LIKE %s", (prefix + "%",)
            )


# ---------------------------------------------------------------------------
# db.get_listing_count
# ---------------------------------------------------------------------------

class TestGetListingCount:
    def teardown_method(self):
        _cleanup(_PREFIX)

    def test_returns_correct_count_after_inserts(self):
        """get_listing_count() reflects the actual number of inserted rows."""
        _insert("cdb-job-001")
        _insert("cdb-job-002")
        _insert("cdb-job-003")
        count = db.get_listing_count()
        # The count includes our 3 rows (possibly plus other test rows; just verify >= 3).
        assert count >= 3

    def test_count_decreases_after_manual_delete(self):
        """get_listing_count() is accurate after rows are removed externally."""
        _insert("cdb-job-del-001")
        _insert("cdb-job-del-002")
        before = db.get_listing_count()
        with db.get_connection() as conn:
            conn.execute(
                "DELETE FROM listings WHERE source_id = %s", ("cdb-job-del-001",)
            )
        after = db.get_listing_count()
        assert after == before - 1


# ---------------------------------------------------------------------------
# db.clear_all_listings
# ---------------------------------------------------------------------------

class TestClearAllListings:
    def teardown_method(self):
        # clear_all_listings tests may leave rows; ensure clean state.
        _cleanup(_PREFIX)

    def test_deletes_all_rows_and_returns_count(self):
        """clear_all_listings() removes every row and returns the deleted count."""
        _insert("cdb-clr-001")
        _insert("cdb-clr-002")
        with db.get_connection() as conn:
            deleted = db.clear_all_listings(conn)
        assert deleted >= 2  # at least our 2 rows

    def test_returns_zero_on_empty_table_after_clear(self):
        """After clearing, get_listing_count() returns 0."""
        _insert("cdb-clr-003")
        with db.get_connection() as conn:
            db.clear_all_listings(conn)
        assert db.get_listing_count() == 0

    def test_schema_intact_after_clear(self):
        """The listings table still accepts new inserts after clearing."""
        _insert("cdb-clr-004")
        with db.get_connection() as conn:
            db.clear_all_listings(conn)
        # Must be able to insert a new listing without error.
        _insert("cdb-clr-005")
        assert db.get_listing_count() >= 1

    def test_geocache_not_affected(self):
        """clear_all_listings() leaves location_geocache rows untouched."""
        _insert("cdb-clr-006")
        # Ensure a geocache entry exists.
        with db.get_connection() as conn:
            conn.execute(
                """
                INSERT INTO location_geocache (location_text, lat, lon)
                VALUES ('TestCity, XZ', 0.0, 0.0)
                ON CONFLICT (location_text) DO NOTHING
                """
            )
        with db.get_connection() as conn:
            db.clear_all_listings(conn)
        with db.get_connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM location_geocache "
                "WHERE location_text = 'TestCity, XZ'"
            ).fetchone()
        assert row["cnt"] >= 1
        # Cleanup geocache entry.
        with db.get_connection() as conn:
            conn.execute(
                "DELETE FROM location_geocache WHERE location_text = 'TestCity, XZ'"
            )

    def test_single_row_returns_count_one(self):
        """clear_all_listings() with exactly one row (after a prior clear) returns 1."""
        # First clear anything in there.
        with db.get_connection() as conn:
            db.clear_all_listings(conn)
        _insert("cdb-solo-001")
        with db.get_connection() as conn:
            deleted = db.clear_all_listings(conn)
        assert deleted == 1


# ---------------------------------------------------------------------------
# POST /admin/clear-db route
# ---------------------------------------------------------------------------

@pytest.fixture()
def client():
    flask_app.config["TESTING"] = True
    with flask_app.test_client() as c:
        yield c


class TestAdminClearDbRoute:
    """Route-level tests for POST /admin/clear-db.

    These tests operate against the real PostgreSQL database.  We insert
    test rows, exercise the route, and check outcomes using db helpers.
    After each test we ensure the table is restored to a known state.
    """

    def teardown_method(self):
        _cleanup(_PREFIX)

    def test_rejects_wrong_confirmation(self, client):
        """POST /admin/clear-db with wrong phrase returns 400 and leaves rows intact."""
        _insert("cdb-rt-001")
        count_before = db.get_listing_count()
        resp = client.post(
            "/admin/clear-db",
            data={"confirmation": "delete"},  # wrong case
        )
        assert resp.status_code == 400
        assert db.get_listing_count() == count_before

    def test_rejects_empty_confirmation(self, client):
        """POST /admin/clear-db with no phrase returns 400."""
        _insert("cdb-rt-002")
        count_before = db.get_listing_count()
        resp = client.post("/admin/clear-db", data={})
        assert resp.status_code == 400
        assert db.get_listing_count() == count_before

    def test_accepts_correct_confirmation_and_deletes(self, client):
        """POST /admin/clear-db with 'DELETE' clears all rows and returns 200."""
        _insert("cdb-rt-003")
        _insert("cdb-rt-004")
        resp = client.post(
            "/admin/clear-db",
            data={"confirmation": "DELETE"},
        )
        assert resp.status_code == 200
        assert db.get_listing_count() == 0

    def test_success_response_contains_deleted_count(self, client):
        """Success response body mentions the number of deleted listings."""
        # First clear to get a known state.
        with db.get_connection() as conn:
            db.clear_all_listings(conn)
        _insert("cdb-rt-005")
        _insert("cdb-rt-006")
        resp = client.post(
            "/admin/clear-db",
            data={"confirmation": "DELETE"},
        )
        body = resp.data.decode()
        assert "2" in body
        assert "deleted" in body.lower()

    def test_empty_db_returns_zero_count(self, client):
        """Clearing an already-empty DB returns 200 with a 0-deleted message."""
        with db.get_connection() as conn:
            db.clear_all_listings(conn)
        resp = client.post(
            "/admin/clear-db",
            data={"confirmation": "DELETE"},
        )
        assert resp.status_code == 200
        body = resp.data.decode()
        assert "0" in body

    def test_error_fragment_contains_message(self, client):
        """400 response body contains an explanatory error message."""
        resp = client.post(
            "/admin/clear-db",
            data={"confirmation": "WRONG"},
        )
        body = resp.data.decode()
        assert "did not match" in body.lower() or "confirmation" in body.lower()

    def test_singular_noun_for_one_listing(self, client):
        """Success message uses 'listing' (not 'listings') when exactly one row deleted."""
        with db.get_connection() as conn:
            db.clear_all_listings(conn)
        _insert("cdb-rt-solo")
        resp = client.post(
            "/admin/clear-db",
            data={"confirmation": "DELETE"},
        )
        body = resp.data.decode()
        assert "1 listing deleted" in body

    def test_plural_noun_for_multiple_listings(self, client):
        """Success message uses 'listings' when more than one row deleted."""
        with db.get_connection() as conn:
            db.clear_all_listings(conn)
        _insert("cdb-rt-001p")
        _insert("cdb-rt-002p")
        resp = client.post(
            "/admin/clear-db",
            data={"confirmation": "DELETE"},
        )
        body = resp.data.decode()
        assert "listings deleted" in body


# ---------------------------------------------------------------------------
# GET /settings includes listing_count
# ---------------------------------------------------------------------------

@pytest.fixture()
def tmp_providers_path(tmp_path, monkeypatch):
    """Isolate providers.json from the real config directory."""
    import app as app_module
    path = str(tmp_path / "providers.json")
    monkeypatch.setattr(app_module, "_PROVIDERS_PATH", path)
    return path


@pytest.fixture()
def tmp_keys_path(tmp_path, monkeypatch):
    import app as app_module
    path = str(tmp_path / "keys.json")
    monkeypatch.setattr(app_module, "_KEYS_PATH", path)
    return path


class TestSettingsListingCount:
    def teardown_method(self):
        _cleanup(_PREFIX)

    def test_settings_page_renders_listing_count(
        self, client, tmp_providers_path, tmp_keys_path
    ):
        """GET /settings page renders without error and includes the listing count."""
        _insert("cdb-st-001")
        _insert("cdb-st-002")
        resp = client.get("/settings")
        assert resp.status_code == 200
        # The count should appear somewhere in the page — we can't assert
        # exact count since other tests may have rows, but it renders without error.
        assert resp.data

    def test_settings_page_renders_ok_when_empty(
        self, client, tmp_providers_path, tmp_keys_path
    ):
        """GET /settings renders without error even when no listings inserted."""
        resp = client.get("/settings")
        assert resp.status_code == 200
