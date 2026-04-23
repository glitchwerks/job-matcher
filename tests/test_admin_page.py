"""
tests/test_admin_page.py — Tests for the GET /admin route and admin.html template.

Verifies the page shell: correct HTTP status, required headings, four sub-tab
panes, four tab buttons, and that every other page's nav contains an admin link.
"""

import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import services.profile_store as _profile_store_module
import web.settings as _settings_module
from app import app as flask_app


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def client():
    flask_app.config["TESTING"] = True
    with flask_app.test_client() as c:
        yield c


@pytest.fixture()
def tmp_providers_path(tmp_path, monkeypatch):
    """Isolate providers.json from the real config directory."""
    path = str(tmp_path / "providers.json")
    monkeypatch.setattr(_profile_store_module, "_PROVIDERS_PATH", path)
    monkeypatch.setattr(_settings_module, "_PROVIDERS_PATH", path)
    return path


@pytest.fixture()
def tmp_keys_path(tmp_path, monkeypatch):
    """Isolate keys.json from the real config directory."""
    path = str(tmp_path / "keys.json")
    monkeypatch.setattr(_profile_store_module, "_KEYS_PATH", path)
    monkeypatch.setattr(_settings_module, "_KEYS_PATH", path)
    return path


# ---------------------------------------------------------------------------
# /admin page
# ---------------------------------------------------------------------------

class TestAdminPage:
    def test_admin_page_returns_200(self, client):
        """GET /admin returns HTTP 200."""
        resp = client.get("/admin")
        assert resp.status_code == 200

    def test_admin_page_has_heading(self, client):
        """Response body contains the 'Administration' heading."""
        resp = client.get("/admin")
        body = resp.data.decode()
        assert "Administration" in body

    def test_admin_page_has_four_panes(self, client):
        """Response body contains all four expected tab pane IDs."""
        resp = client.get("/admin")
        body = resp.data.decode()
        assert "pane-runtime" in body
        assert "pane-logs" in body
        assert "pane-schedule" in body
        assert "pane-danger" in body

    def test_admin_page_has_four_tab_buttons(self, client):
        """Response body contains all four tab button labels."""
        resp = client.get("/admin")
        body = resp.data.decode()
        assert "Runtime" in body
        assert "Logs" in body
        assert "Schedule" in body
        assert "Danger Zone" in body

    def test_admin_runtime_pane_has_content(self, client):
        """Runtime pane contains the Runtime heading and at least the flask row."""
        with patch("db.get_listing_count", return_value=0):
            resp = client.get("/admin")
        body = resp.data.decode()
        assert "Runtime" in body
        assert "flask" in body.lower()


# ---------------------------------------------------------------------------
# Admin nav link present on all pages
# ---------------------------------------------------------------------------

# Minimal stub returns for DB-backed routes so no real Postgres connection is needed.
_FEED_STUB = []
_STATS_STUB = {
    "total_scored": 0,
    "total_tokens_input": 0,
    "total_tokens_output": 0,
    "estimated_cost_usd": None,
    "by_date": [],
}


class TestNavHasAdminLink:
    """Every page in the app should render the admin nav link.

    Routes that query the database are patched to return empty stubs so no
    real Postgres connection is required.
    """

    def test_nav_has_admin_link_on_feed(self, client):
        """GET / renders the feed page with an admin nav link."""
        with patch("db.get_feed", return_value=_FEED_STUB), \
             patch("db.get_job_types", return_value=[]), \
             patch("db.get_last_fetch_time", return_value=None), \
             patch("web.feed._config_warnings", return_value=[]), \
             patch("services.ingest_control._ingest_running", return_value=False):
            resp = client.get("/")
        assert resp.status_code == 200
        assert 'href="/admin"' in resp.data.decode()

    def test_nav_has_admin_link_on_stats(self, client):
        """GET /stats renders the stats page with an admin nav link."""
        with patch("db.get_usage_stats", return_value=_STATS_STUB), \
             patch("web.feed._config_warnings", return_value=[]):
            resp = client.get("/stats")
        assert resp.status_code == 200
        assert 'href="/admin"' in resp.data.decode()

    def test_nav_has_admin_link_on_profile(self, client):
        """GET /profile renders the profile page with an admin nav link."""
        resp = client.get("/profile")
        assert resp.status_code == 200
        assert 'href="/admin"' in resp.data.decode()

    def test_nav_has_admin_link_on_settings(
        self, client, tmp_providers_path, tmp_keys_path
    ):
        """GET /settings renders the settings page with an admin nav link."""
        resp = client.get("/settings")
        assert resp.status_code == 200
        assert 'href="/admin"' in resp.data.decode()
