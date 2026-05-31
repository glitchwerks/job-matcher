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
    flask_app.config["SECRET_KEY"] = "test-secret-key"
    with flask_app.test_client() as c:
        yield c


def _get_csrf_token(client) -> str:
    """GET /admin to establish a session and return the CSRF token from the session."""
    with client.session_transaction() as sess:
        # Seed the session directly — avoids a real DB call in GET /admin.
        import secrets as _secrets
        token = _secrets.token_urlsafe(32)
        sess["csrf_token"] = token
    return token


class TestAdminClearDbRoute:
    """Route-level tests for POST /admin/clear-db.

    These tests operate against the real PostgreSQL database.  We insert
    test rows, exercise the route, and check outcomes using db helpers.
    After each test we ensure the table is restored to a known state.

    All POST requests must include a valid CSRF token — obtained by seeding
    the session via ``_get_csrf_token(client)``.
    """

    def teardown_method(self):
        _cleanup(_PREFIX)

    def test_rejects_wrong_confirmation(self, client):
        """POST /admin/clear-db with wrong phrase returns 400 and leaves rows intact."""
        _insert("cdb-rt-001")
        count_before = db.get_listing_count()
        token = _get_csrf_token(client)
        resp = client.post(
            "/admin/clear-db",
            data={"confirmation": "delete", "csrf_token": token},  # wrong case
        )
        assert resp.status_code == 400
        assert db.get_listing_count() == count_before

    def test_rejects_empty_confirmation(self, client):
        """POST /admin/clear-db with no phrase returns 400."""
        _insert("cdb-rt-002")
        count_before = db.get_listing_count()
        token = _get_csrf_token(client)
        resp = client.post("/admin/clear-db", data={"csrf_token": token})
        assert resp.status_code == 400
        assert db.get_listing_count() == count_before

    def test_accepts_correct_confirmation_and_deletes(self, client):
        """POST /admin/clear-db with 'DELETE' and valid CSRF clears all rows and returns 200."""
        _insert("cdb-rt-003")
        _insert("cdb-rt-004")
        token = _get_csrf_token(client)
        resp = client.post(
            "/admin/clear-db",
            data={"confirmation": "DELETE", "csrf_token": token},
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
        token = _get_csrf_token(client)
        resp = client.post(
            "/admin/clear-db",
            data={"confirmation": "DELETE", "csrf_token": token},
        )
        body = resp.data.decode()
        assert "2" in body
        assert "deleted" in body.lower()

    def test_empty_db_returns_zero_count(self, client):
        """Clearing an already-empty DB returns 200 with a 0-deleted message."""
        with db.get_connection() as conn:
            db.clear_all_listings(conn)
        token = _get_csrf_token(client)
        resp = client.post(
            "/admin/clear-db",
            data={"confirmation": "DELETE", "csrf_token": token},
        )
        assert resp.status_code == 200
        body = resp.data.decode()
        assert "0" in body

    def test_error_fragment_contains_message(self, client):
        """400 response body contains an explanatory error message."""
        token = _get_csrf_token(client)
        resp = client.post(
            "/admin/clear-db",
            data={"confirmation": "WRONG", "csrf_token": token},
        )
        body = resp.data.decode()
        assert "did not match" in body.lower() or "confirmation" in body.lower()

    def test_singular_noun_for_one_listing(self, client):
        """Success message uses 'listing' (not 'listings') when exactly one row deleted."""
        with db.get_connection() as conn:
            db.clear_all_listings(conn)
        _insert("cdb-rt-solo")
        token = _get_csrf_token(client)
        resp = client.post(
            "/admin/clear-db",
            data={"confirmation": "DELETE", "csrf_token": token},
        )
        body = resp.data.decode()
        assert "1 listing deleted" in body

    def test_plural_noun_for_multiple_listings(self, client):
        """Success message uses 'listings' when more than one row deleted."""
        with db.get_connection() as conn:
            db.clear_all_listings(conn)
        _insert("cdb-rt-001p")
        _insert("cdb-rt-002p")
        token = _get_csrf_token(client)
        resp = client.post(
            "/admin/clear-db",
            data={"confirmation": "DELETE", "csrf_token": token},
        )
        body = resp.data.decode()
        assert "listings deleted" in body


# ---------------------------------------------------------------------------
# CSRF protection tests — do not require a database connection
# ---------------------------------------------------------------------------

class TestAdminClearDbCsrf:
    """Verify that /admin/clear-db enforces CSRF token validation.

    These tests only need the Flask app; no PostgreSQL connection is required
    because CSRF rejection happens before any DB access.
    """

    def test_missing_csrf_token_returns_400(self, client):
        """POST with confirmation=DELETE but no csrf_token is rejected with 400."""
        resp = client.post(
            "/admin/clear-db",
            data={"confirmation": "DELETE"},
        )
        assert resp.status_code == 400
        body = resp.data.decode()
        assert "csrf" in body.lower() or "token" in body.lower()

    def test_wrong_csrf_token_returns_400(self, client):
        """POST with confirmation=DELETE and a mismatched csrf_token is rejected with 400."""
        # Seed a real token in the session, but send a different value.
        with client.session_transaction() as sess:
            sess["csrf_token"] = "correct-token"
        resp = client.post(
            "/admin/clear-db",
            data={"confirmation": "DELETE", "csrf_token": "wrong-token"},
        )
        assert resp.status_code == 400
        body = resp.data.decode()
        assert "csrf" in body.lower() or "token" in body.lower()

    def test_empty_csrf_token_returns_400(self, client):
        """POST with csrf_token='' is rejected with 400."""
        with client.session_transaction() as sess:
            sess["csrf_token"] = "correct-token"
        resp = client.post(
            "/admin/clear-db",
            data={"confirmation": "DELETE", "csrf_token": ""},
        )
        assert resp.status_code == 400

    def test_valid_csrf_and_confirmation_returns_200(self, client):
        """POST with a matching csrf_token and confirmation=DELETE succeeds (200)."""
        token = _get_csrf_token(client)
        # Clear DB first so we don't depend on DB state for this assertion.
        try:
            with db.get_connection() as conn:
                db.clear_all_listings(conn)
        except Exception:
            pytest.skip("No DATABASE_URL available — skipping DB-dependent CSRF success test")
        resp = client.post(
            "/admin/clear-db",
            data={"confirmation": "DELETE", "csrf_token": token},
        )
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# GET /settings and GET /admin page render tests
# ---------------------------------------------------------------------------

@pytest.fixture()
def tmp_providers_path(tmp_path, monkeypatch):
    """Isolate providers.json from the real config directory."""
    import services.profile_store as _profile_store_module
    import web.settings as _settings_module
    path = str(tmp_path / "providers.json")
    monkeypatch.setattr(_profile_store_module, "_PROVIDERS_PATH", path)
    monkeypatch.setattr(_settings_module, "_PROVIDERS_PATH", path)
    return path


@pytest.fixture()
def tmp_keys_path(tmp_path, monkeypatch):
    import services.profile_store as _profile_store_module
    import web.settings as _settings_module
    path = str(tmp_path / "keys.json")
    monkeypatch.setattr(_profile_store_module, "_KEYS_PATH", path)
    monkeypatch.setattr(_settings_module, "_KEYS_PATH", path)
    return path


@pytest.fixture()
def tmp_config_path(tmp_path, monkeypatch):
    """Isolate config.json from the real config directory.

    Prevents _get_search_validation_issues from reading or being affected by
    any real config/config.json that may exist on a developer machine, and
    ensures the lru_cache keyed on (providers_mtime, config_mtime) does not
    share entries with other tests that use different path pairs.

    Without this fixture the settings route uses the real _CONFIG_PATH
    (which may exist on some machines) while other fixtures redirect
    _PROVIDERS_PATH to a temp file — creating a mixed-path cache key that
    can interact with other test sessions or a stale lru_cache entry.
    """
    import services.profile_store as _profile_store_module
    import web.settings as _settings_module
    path = str(tmp_path / "config.json")
    monkeypatch.setattr(_profile_store_module, "_CONFIG_PATH", path)
    monkeypatch.setattr(_settings_module, "_CONFIG_PATH", path)
    return path


class TestSettingsPageRenders:
    def teardown_method(self):
        _cleanup(_PREFIX)

    def test_settings_page_renders_ok(
        self, client, tmp_providers_path, tmp_keys_path, tmp_config_path
    ):
        """GET /settings page renders without error (listing_count is no longer shown here)."""
        _insert("cdb-st-001")
        _insert("cdb-st-002")
        resp = client.get("/settings")
        assert resp.status_code == 200
        assert resp.data

    def test_settings_page_renders_ok_when_empty(
        self, client, tmp_providers_path, tmp_keys_path, tmp_config_path
    ):
        """GET /settings renders without error even when no listings inserted."""
        resp = client.get("/settings")
        assert resp.status_code == 200


class TestAdminPageRenders:
    """Tests for GET /admin that mock the DB so they run without PostgreSQL."""

    def test_admin_page_renders_with_listing_count(self, client, monkeypatch):
        """GET /admin renders without error and includes the danger zone markup."""
        import db
        monkeypatch.setattr(db, "get_listing_count", lambda: 42)
        resp = client.get("/admin")
        assert resp.status_code == 200
        body = resp.data.decode()
        # The danger zone section should be present.
        assert "Danger Zone" in body
        assert "Clear Database" in body

    def test_admin_page_sets_csrf_token_in_session(self, client, monkeypatch):
        """GET /admin establishes a CSRF token in the session."""
        import db
        monkeypatch.setattr(db, "get_listing_count", lambda: 0)
        client.get("/admin")
        with client.session_transaction() as sess:
            assert "csrf_token" in sess
            assert len(sess["csrf_token"]) > 0
