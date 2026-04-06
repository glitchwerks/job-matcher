"""
tests/test_status_bar.py — Tests for the environment status bar rendering.

The status bar is rendered via templates/_status_bar.html, which is included
in every top-level template.  APP_ENV and APP_VERSION are injected as Jinja2
globals at app-creation time (not per-request), so tests update
app.jinja_env.globals directly rather than relying on monkeypatch.setenv
(which would have no effect after the module is imported).

psycopg2 is stubbed in sys.modules before any project import because the
test environment does not have Postgres drivers installed.  The feed() route's
db calls are patched to return empty/neutral values so no real database is
needed.
"""

from __future__ import annotations

import json
import os
import sys
import types
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Stub psycopg2 and psycopg2.extras before any project import so that db.py
# can be imported without a Postgres installation present.
# ---------------------------------------------------------------------------
_psycopg2_stub = types.ModuleType("psycopg2")
_psycopg2_stub.connect = MagicMock()
_psycopg2_stub.extras = types.ModuleType("psycopg2.extras")
_psycopg2_stub.extras.RealDictCursor = MagicMock()
_psycopg2_stub.OperationalError = Exception
_psycopg2_stub.InterfaceError = Exception
sys.modules.setdefault("psycopg2", _psycopg2_stub)
sys.modules.setdefault("psycopg2.extras", _psycopg2_stub.extras)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

import app as app_module
from app import app as flask_app
import db as db_module


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def client(tmp_path, monkeypatch):
    """Test client with config patched and db calls stubbed so / renders
    without a real database connection."""
    config_path = str(tmp_path / "config.json")
    with open(config_path, "w") as f:
        json.dump({
            "search": {
                "country": "us",
                "what": "software engineer",
                "where": "miami",
                "results_per_page": 50,
                "max_pages": 5,
            },
            "scoring": {"threshold": 7.0},
        }, f)
    monkeypatch.setattr(app_module, "_CONFIG_PATH", config_path)
    monkeypatch.setattr(app_module, "CONFIG", json.loads(open(config_path).read()))

    # Stub db functions called by feed() so no real Postgres connection is made.
    monkeypatch.setattr(db_module, "get_feed", lambda **kw: [])
    monkeypatch.setattr(db_module, "get_job_types", lambda **kw: [])
    monkeypatch.setattr(db_module, "get_last_fetch_time", lambda **kw: None)

    flask_app.config["TESTING"] = True
    with flask_app.test_client() as c:
        yield c


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _set_app_env(env_value: str) -> None:
    """Directly update the Jinja2 global so the template sees the new value."""
    flask_app.jinja_env.globals["APP_ENV"] = env_value


def _reset_app_env() -> None:
    """Restore APP_ENV to 'local' (module-level default) after each test."""
    flask_app.jinja_env.globals["APP_ENV"] = "local"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestStatusBarRendering:
    """The status bar element appears/is absent based on APP_ENV."""

    def test_dev_env_renders_dev_status_bar(self, client):
        """When APP_ENV=dev the response contains the dev-specific CSS modifier."""
        _set_app_env("dev")
        try:
            resp = client.get("/")
            html = resp.data.decode()
            assert "env-status-bar--dev" in html, (
                "Expected env-status-bar--dev class in response when APP_ENV=dev"
            )
        finally:
            _reset_app_env()

    def test_local_env_omits_status_bar(self, client):
        """When APP_ENV is not set (defaults to 'local') no status bar is rendered."""
        _set_app_env("local")
        try:
            resp = client.get("/")
            html = resp.data.decode()
            assert "env-status-bar" not in html, (
                "Expected no env-status-bar element when APP_ENV=local"
            )
        finally:
            _reset_app_env()

    def test_prod_env_renders_prod_status_bar(self, client):
        """When APP_ENV=prod the response contains the prod-specific CSS modifier."""
        _set_app_env("prod")
        try:
            resp = client.get("/")
            html = resp.data.decode()
            assert "env-status-bar--prod" in html, (
                "Expected env-status-bar--prod class in response when APP_ENV=prod"
            )
        finally:
            _reset_app_env()
