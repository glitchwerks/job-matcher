"""
tests/test_security.py — Tests for the two security fixes:

  #135 — CSRF Origin/Referer guard (before_request hook)
  #136 — /profile POST validates required config keys before writing to disk

All tests use Flask's built-in test client and temp paths so the real
config files on disk are never read or written.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app as app_module
from app import app as flask_app, _validate_config_dict


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
    path = str(tmp_path / "config.json")
    monkeypatch.setattr(app_module, "_CONFIG_PATH", path)
    return path


def _write_config(path: str, data: dict | None = None) -> None:
    """Write a minimal valid config.json fixture to *path*."""
    if data is None:
        data = {
            "adzuna_app_id": "test-id",
            "adzuna_app_key": "test-key",
            "search": {
                "country": "us",
                "what": "software engineer",
                "results_per_page": 50,
                "max_pages": 5,
            },
            "scoring": {"threshold": 7.0},
        }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)


# ===========================================================================
# _validate_config_dict — unit tests (Issue #136)
# ===========================================================================

class TestValidateConfigDict:
    """Unit tests for the _validate_config_dict helper."""

    def test_valid_full_config_returns_no_missing_keys(self):
        data = {
            "adzuna_app_id": "id",
            "adzuna_app_key": "key",
            "search": {
                "country": "us",
                "what": "engineer",
                "results_per_page": 50,
                "max_pages": 5,
            },
            "scoring": {"threshold": 7.0},
        }
        assert _validate_config_dict(data) == []

    def test_missing_scoring_section_is_flagged(self):
        data = {"search": {"country": "us", "what": "eng", "results_per_page": 50, "max_pages": 5}}
        missing = _validate_config_dict(data)
        assert any("scoring" in m for m in missing)

    def test_missing_scoring_threshold_is_flagged(self):
        data = {"scoring": {}}
        missing = _validate_config_dict(data)
        assert "scoring.threshold" in missing

    def test_scoring_not_dict_is_flagged(self):
        data = {"scoring": "not-an-object"}
        missing = _validate_config_dict(data)
        assert any("scoring" in m for m in missing)

    def test_empty_dict_flags_scoring_missing(self):
        missing = _validate_config_dict({})
        assert any("scoring" in m for m in missing)

    def test_missing_search_subkey_flagged_when_search_present(self):
        data = {
            "scoring": {"threshold": 7.0},
            "search": {"country": "us"},  # missing what, results_per_page, max_pages
        }
        missing = _validate_config_dict(data)
        assert "search.what" in missing
        assert "search.results_per_page" in missing
        assert "search.max_pages" in missing

    def test_absent_search_block_not_flagged(self):
        """Omitting the search block entirely is valid — it just means no Adzuna source."""
        data = {"scoring": {"threshold": 7.0}}
        missing = _validate_config_dict(data)
        # Should only flag scoring.threshold (present here), so list must be empty
        assert missing == []

    def test_adzuna_credential_keys_not_required(self):
        """adzuna_app_id / adzuna_app_key must not be flagged — they can come from env vars."""
        data = {"scoring": {"threshold": 7.0}}
        missing = _validate_config_dict(data)
        assert not any("adzuna_app_id" in m for m in missing)
        assert not any("adzuna_app_key" in m for m in missing)


# ===========================================================================
# POST /profile — validation gate (Issue #136)
# ===========================================================================

class TestProfilePostValidation:
    """Integration tests for /profile POST validation."""

    def test_valid_config_is_saved(self, client, tmp_config_path):
        _write_config(tmp_config_path)
        payload = json.dumps({
            "adzuna_app_id": "id",
            "adzuna_app_key": "key",
            "search": {"country": "us", "what": "engineer", "results_per_page": 50, "max_pages": 5},
            "scoring": {"threshold": 8.0},
        })
        resp = client.post("/profile", data={"config_json": payload})
        assert resp.status_code == 200
        # Confirm the file was actually written with the new threshold
        with open(tmp_config_path, encoding="utf-8") as fh:
            saved = json.load(fh)
        assert saved["scoring"]["threshold"] == 8.0

    def test_empty_json_object_returns_422(self, client, tmp_config_path):
        _write_config(tmp_config_path)
        resp = client.post("/profile", data={"config_json": "{}"})
        assert resp.status_code == 422

    def test_missing_scoring_returns_422(self, client, tmp_config_path):
        _write_config(tmp_config_path)
        payload = json.dumps({"adzuna_app_id": "id", "search": {"country": "us", "what": "eng", "results_per_page": 50, "max_pages": 5}})
        resp = client.post("/profile", data={"config_json": payload})
        assert resp.status_code == 422

    def test_missing_scoring_threshold_returns_422(self, client, tmp_config_path):
        _write_config(tmp_config_path)
        payload = json.dumps({"scoring": {}})
        resp = client.post("/profile", data={"config_json": payload})
        assert resp.status_code == 422

    def test_422_response_contains_missing_key_name(self, client, tmp_config_path):
        _write_config(tmp_config_path)
        payload = json.dumps({"scoring": {}})
        resp = client.post("/profile", data={"config_json": payload})
        body = resp.data.decode()
        assert "scoring.threshold" in body

    def test_disk_not_written_on_validation_failure(self, client, tmp_config_path):
        """config.json must be unchanged when validation fails."""
        original_data = {
            "adzuna_app_id": "original-id",
            "adzuna_app_key": "original-key",
            "search": {"country": "us", "what": "eng", "results_per_page": 50, "max_pages": 5},
            "scoring": {"threshold": 7.0},
        }
        _write_config(tmp_config_path, original_data)
        resp = client.post("/profile", data={"config_json": "{}"})
        assert resp.status_code == 422
        with open(tmp_config_path, encoding="utf-8") as fh:
            on_disk = json.load(fh)
        assert on_disk["scoring"]["threshold"] == 7.0

    def test_invalid_json_still_returns_400(self, client, tmp_config_path):
        _write_config(tmp_config_path)
        resp = client.post("/profile", data={"config_json": "not-json{"})
        assert resp.status_code == 400

    def test_partial_search_block_returns_422(self, client, tmp_config_path):
        """A search block that is present but missing required sub-keys must be rejected."""
        _write_config(tmp_config_path)
        payload = json.dumps({
            "scoring": {"threshold": 7.0},
            "search": {"country": "us"},  # missing what, results_per_page, max_pages
        })
        resp = client.post("/profile", data={"config_json": payload})
        assert resp.status_code == 422


# ===========================================================================
# Origin/Referer CSRF guard (Issue #135)
# ===========================================================================

class TestCsrfLocalhostGuard:
    """Tests for the before_request CSRF guard on state-mutating routes."""

    # --- POST requests that should be allowed ---

    def test_post_with_localhost_origin_is_allowed(self, client, tmp_config_path):
        _write_config(tmp_config_path)
        payload = json.dumps({"scoring": {"threshold": 7.0}})
        resp = client.post(
            "/profile",
            data={"config_json": payload},
            headers={"Origin": "http://localhost:5000"},
        )
        # 422 (validation) not 403 — the guard let it through
        assert resp.status_code != 403

    def test_post_with_127_origin_is_allowed(self, client, tmp_config_path):
        _write_config(tmp_config_path)
        payload = json.dumps({"scoring": {"threshold": 7.0}})
        resp = client.post(
            "/profile",
            data={"config_json": payload},
            headers={"Origin": "http://127.0.0.1:5000"},
        )
        assert resp.status_code != 403

    def test_post_with_referer_localhost_is_allowed(self, client, tmp_config_path):
        _write_config(tmp_config_path)
        payload = json.dumps({"scoring": {"threshold": 7.0}})
        resp = client.post(
            "/profile",
            data={"config_json": payload},
            headers={"Referer": "http://localhost:5000/profile"},
        )
        assert resp.status_code != 403

    def test_post_with_no_origin_is_allowed(self, client, tmp_config_path):
        """curl / test clients that omit Origin must not be blocked."""
        _write_config(tmp_config_path)
        payload = json.dumps({"scoring": {"threshold": 7.0}})
        resp = client.post("/profile", data={"config_json": payload})
        assert resp.status_code != 403

    # --- POST requests that should be rejected ---

    def test_post_with_external_origin_returns_403(self, client):
        resp = client.post(
            "/profile",
            data={"config_json": "{}"},
            headers={"Origin": "http://evil.example.com"},
        )
        assert resp.status_code == 403

    def test_post_with_external_referer_returns_403(self, client):
        resp = client.post(
            "/profile",
            data={"config_json": "{}"},
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
            assert app_module._is_localhost_request() is True

    def test_is_localhost_request_true_for_127_origin(self):
        with flask_app.test_request_context(
            "/profile",
            method="POST",
            headers={"Origin": "http://127.0.0.1:5000"},
        ):
            assert app_module._is_localhost_request() is True

    def test_is_localhost_request_false_for_external_origin(self):
        with flask_app.test_request_context(
            "/profile",
            method="POST",
            headers={"Origin": "http://evil.example.com"},
        ):
            assert app_module._is_localhost_request() is False

    def test_is_localhost_request_true_when_no_headers(self):
        with flask_app.test_request_context("/profile", method="POST"):
            assert app_module._is_localhost_request() is True

    def test_is_localhost_request_true_for_bracketed_ipv6_origin(self):
        """Bracketed IPv6 ::1 — the standard URL form http://[::1]:5000 — must be accepted."""
        with flask_app.test_request_context(
            "/profile",
            method="POST",
            headers={"Origin": "http://[::1]:5000"},
        ):
            assert app_module._is_localhost_request() is True


# ===========================================================================
# _validate_config_dict — type validation (Issue #136 follow-up)
# ===========================================================================

class TestValidateConfigDictTypeChecks:
    """Type-validation tests for _validate_config_dict (threshold must be numeric)."""

    def test_threshold_string_is_flagged(self):
        data = {"scoring": {"threshold": "not-a-number"}}
        missing = _validate_config_dict(data)
        assert any("scoring.threshold" in m for m in missing)

    def test_threshold_none_is_flagged(self):
        data = {"scoring": {"threshold": None}}
        missing = _validate_config_dict(data)
        assert any("scoring.threshold" in m for m in missing)

    def test_threshold_int_is_valid(self):
        data = {"scoring": {"threshold": 7}}
        assert _validate_config_dict(data) == []

    def test_threshold_float_is_valid(self):
        data = {"scoring": {"threshold": 7.5}}
        assert _validate_config_dict(data) == []
