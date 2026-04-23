"""
tests/test_security.py — Tests for the two security fixes:

  #135 — CSRF Origin/Referer guard (before_request hook)
  #136 — /profile POST validates required config keys before writing to disk

All tests use Flask's built-in test client and temp paths so the real
config files on disk are never read or written.

Note: TestValidateConfigDict and the raw-JSON TestProfilePostValidation tests
were removed in issue #319, which replaced the raw JSON textarea with a
structured form.  Validation of the new structured form is covered in
tests/test_profile.py.  The CSRF guard tests below are unaffected by that
change.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import app as flask_app
from web.security import _is_localhost_request, _is_trusted_host


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def client():
    flask_app.config["TESTING"] = True
    with flask_app.test_client() as c:
        yield c


@pytest.fixture()
def tmp_config_path(tmp_path, monkeypatch):
    """Point _CONFIG_PATH at a temp file so profile tests don't touch the real file."""
    import services.profile_store as _profile_store_module
    import web.profile as _profile_module
    path = str(tmp_path / "config.json")
    monkeypatch.setattr(_profile_store_module, "_CONFIG_PATH", path)
    monkeypatch.setattr(_profile_module, "_CONFIG_PATH", path)
    return path


def _write_config(path: str, data: dict | None = None) -> None:
    """Write a minimal valid config.json fixture to *path*."""
    if data is None:
        data = {
            "search": {
                "country": "us",
                "what": "software engineer",
                "where": "miami",
                "results_per_page": 50,
                "max_pages": 5,
            },
            "scoring": {"threshold": 7.0},
            "prefilter": {
                "title_include": [],
                "title_exclude": [],
                "require_contract_time": None,
                "require_contract_type": None,
            },
        }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)


# ===========================================================================
# Origin/Referer CSRF guard (Issue #135)
# ===========================================================================

class TestCsrfLocalhostGuard:
    """Tests for the before_request CSRF guard on state-mutating routes."""

    # Minimal valid structured-form payload so the profile route does not 422.
    _VALID_PROFILE_FORM = {
        "scoring_threshold": "7.0",
        "search_country": "us",
        "search_what": "engineer",
        "search_where": "miami",
        "location_geocode_fallback": "pass",
    }

    # --- POST requests that should be allowed ---

    def test_post_with_localhost_origin_is_allowed(self, client, tmp_config_path):
        _write_config(tmp_config_path)
        resp = client.post(
            "/profile",
            data=self._VALID_PROFILE_FORM,
            headers={"Origin": "http://localhost:5000"},
        )
        # 200 (saved) not 403 — the guard let it through
        assert resp.status_code != 403

    def test_post_with_127_origin_is_allowed(self, client, tmp_config_path):
        _write_config(tmp_config_path)
        resp = client.post(
            "/profile",
            data=self._VALID_PROFILE_FORM,
            headers={"Origin": "http://127.0.0.1:5000"},
        )
        assert resp.status_code != 403

    def test_post_with_referer_localhost_is_allowed(self, client, tmp_config_path):
        _write_config(tmp_config_path)
        resp = client.post(
            "/profile",
            data=self._VALID_PROFILE_FORM,
            headers={"Referer": "http://localhost:5000/profile"},
        )
        assert resp.status_code != 403

    def test_post_with_no_origin_is_allowed(self, client, tmp_config_path):
        """curl / test clients that omit Origin must not be blocked."""
        _write_config(tmp_config_path)
        resp = client.post("/profile", data=self._VALID_PROFILE_FORM)
        assert resp.status_code != 403

    # --- POST requests that should be rejected ---

    def test_post_with_external_origin_returns_403(self, client):
        resp = client.post(
            "/profile",
            data=self._VALID_PROFILE_FORM,
            headers={"Origin": "http://evil.example.com"},
        )
        assert resp.status_code == 403

    def test_post_with_external_referer_returns_403(self, client):
        resp = client.post(
            "/profile",
            data=self._VALID_PROFILE_FORM,
            headers={"Referer": "http://evil.example.com/attack"},
        )
        assert resp.status_code == 403

    def test_403_body_is_json_with_error_key(self, client):
        resp = client.post(
            "/ingest/trigger",
            headers={"Origin": "http://evil.example.com"},
        )
        assert resp.status_code == 403
        data = json.loads(resp.data)
        assert "error" in data

    def test_get_requests_are_never_blocked(self, client):
        """GET requests must pass through the guard regardless of Origin."""
        resp = client.get(
            "/",
            headers={"Origin": "http://evil.example.com"},
        )
        assert resp.status_code == 200

    def test_ingest_trigger_blocked_from_external_origin(self, client):
        resp = client.post(
            "/ingest/trigger",
            headers={"Origin": "https://malicious.io"},
        )
        assert resp.status_code == 403

    def test_settings_post_blocked_from_external_origin(self, client):
        resp = client.post(
            "/settings",
            data={"tab": "llm"},
            headers={"Origin": "http://attacker.net"},
        )
        assert resp.status_code == 403

    # --- _is_localhost_request unit tests ---

    def test_is_localhost_request_true_for_localhost_origin(self):
        with flask_app.test_request_context(
            "/profile",
            method="POST",
            headers={"Origin": "http://localhost:5000"},
        ):
            assert _is_localhost_request() is True

    def test_is_localhost_request_true_for_127_origin(self):
        with flask_app.test_request_context(
            "/profile",
            method="POST",
            headers={"Origin": "http://127.0.0.1:5000"},
        ):
            assert _is_localhost_request() is True

    def test_is_localhost_request_false_for_external_origin(self):
        with flask_app.test_request_context(
            "/profile",
            method="POST",
            headers={"Origin": "http://attacker.io"},
        ):
            assert _is_localhost_request() is False

    def test_is_localhost_request_true_when_no_origin(self):
        with flask_app.test_request_context("/profile", method="POST"):
            assert _is_localhost_request() is True

    def test_is_trusted_host_true_for_localhost(self):
        assert _is_trusted_host("localhost") is True

    def test_is_trusted_host_true_for_private_ip(self):
        assert _is_trusted_host("192.168.1.1") is True

    def test_is_trusted_host_false_for_public_ip(self):
        assert _is_trusted_host("8.8.8.8") is False

    def test_is_trusted_host_false_for_domain(self):
        assert _is_trusted_host("evil.com") is False
