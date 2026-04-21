"""
tests/test_toggle_source.py — Tests for POST /api/job-sources/<source_key>/toggle.

Covered cases:
- Happy path: toggle ON for a source with all required credentials → 200 {"ok": true}
- Happy path: toggle OFF for any source → 200, no credential check performed
- Toggle OFF for a source with NO credentials → 200 (credentials not checked on disable)
- 404: unknown source_key
- 422: toggle ON for a source missing required credentials
- 422 error message includes the source display name
- 400: missing 'enabled' field in request body
- 400: non-JSON body
- 500: OSError on save_providers
- File is written correctly on success (enabled=true and enabled=false)
- No-credential sources (e.g. arbeitnow) can be toggled ON without credentials → 200
"""
from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app as app_module
from app import app as flask_app


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def tmp_providers_path(tmp_path, monkeypatch):
    """Point _PROVIDERS_PATH at a temp file for full isolation.

    Also clears credential env vars so load_providers() cannot fall back to
    environment-supplied credentials when the temp file is absent.  Without
    this, a caller shell that exports ADZUNA_APP_ID / ADZUNA_APP_KEY (or any
    LLM key) causes _load_providers_safe() to return real credentials even
    though the temp providers.json does not exist — making tests that expect a
    422 (missing credentials) receive a 200 instead.
    """
    path = str(tmp_path / "providers.json")
    monkeypatch.setattr(app_module, "_PROVIDERS_PATH", path)
    for env_var in (
        "ADZUNA_APP_ID",
        "ADZUNA_APP_KEY",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "GOOGLE_API_KEY",
    ):
        monkeypatch.delenv(env_var, raising=False)
    return path


@pytest.fixture()
def client():
    flask_app.config["TESTING"] = True
    with flask_app.test_client() as c:
        yield c


def _write_providers(path: str, job_sources: dict | None = None) -> None:
    """Write a minimal providers.json fixture with optional job_sources section."""
    data = {
        "provider_order": [],
        "llm": {},
        "job_sources": job_sources or {},
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)


def _post_toggle(client, source_key: str, enabled: bool):
    """Helper: POST the toggle endpoint for the given source_key."""
    return client.post(
        f"/api/job-sources/{source_key}/toggle",
        data=json.dumps({"enabled": enabled}),
        content_type="application/json",
    )


# ---------------------------------------------------------------------------
# Happy path — toggle ON with credentials present
# ---------------------------------------------------------------------------

class TestToggleOnWithCredentials:
    def test_returns_200(self, client, tmp_providers_path):
        _write_providers(tmp_providers_path, job_sources={
            "adzuna": {"app_id": "my-app-id", "app_key": "my-app-key", "enabled": False}
        })
        resp = _post_toggle(client, "adzuna", enabled=True)
        assert resp.status_code == 200

    def test_response_body_is_ok_true(self, client, tmp_providers_path):
        _write_providers(tmp_providers_path, job_sources={
            "adzuna": {"app_id": "my-app-id", "app_key": "my-app-key", "enabled": False}
        })
        resp = _post_toggle(client, "adzuna", enabled=True)
        assert resp.get_json() == {"ok": True}

    def test_writes_enabled_true_to_file(self, client, tmp_providers_path):
        _write_providers(tmp_providers_path, job_sources={
            "adzuna": {"app_id": "my-app-id", "app_key": "my-app-key", "enabled": False}
        })
        _post_toggle(client, "adzuna", enabled=True)
        with open(tmp_providers_path, encoding="utf-8") as fh:
            saved = json.load(fh)
        assert saved["job_sources"]["adzuna"]["enabled"] is True


# ---------------------------------------------------------------------------
# Happy path — toggle OFF (no credential check needed)
# ---------------------------------------------------------------------------

class TestToggleOff:
    def test_returns_200_when_credentials_present(self, client, tmp_providers_path):
        _write_providers(tmp_providers_path, job_sources={
            "adzuna": {"app_id": "my-app-id", "app_key": "my-app-key", "enabled": True}
        })
        resp = _post_toggle(client, "adzuna", enabled=False)
        assert resp.status_code == 200

    def test_returns_200_when_credentials_missing(self, client, tmp_providers_path):
        # Disable does NOT require credentials — should still succeed.
        _write_providers(tmp_providers_path, job_sources={
            "adzuna": {"app_id": "", "app_key": "", "enabled": True}
        })
        resp = _post_toggle(client, "adzuna", enabled=False)
        assert resp.status_code == 200

    def test_writes_enabled_false_to_file(self, client, tmp_providers_path):
        _write_providers(tmp_providers_path, job_sources={
            "adzuna": {"app_id": "my-app-id", "app_key": "my-app-key", "enabled": True}
        })
        _post_toggle(client, "adzuna", enabled=False)
        with open(tmp_providers_path, encoding="utf-8") as fh:
            saved = json.load(fh)
        assert saved["job_sources"]["adzuna"]["enabled"] is False


# ---------------------------------------------------------------------------
# No-credential sources (e.g. arbeitnow) — toggle ON without credentials
# ---------------------------------------------------------------------------

class TestToggleNoCredentialSource:
    def test_no_credential_source_toggle_on_returns_200(self, client, tmp_providers_path):
        # arbeitnow has no required credentials — should enable freely.
        _write_providers(tmp_providers_path)
        resp = _post_toggle(client, "arbeitnow", enabled=True)
        assert resp.status_code == 200

    def test_no_credential_source_writes_enabled_to_file(self, client, tmp_providers_path):
        _write_providers(tmp_providers_path)
        _post_toggle(client, "arbeitnow", enabled=True)
        with open(tmp_providers_path, encoding="utf-8") as fh:
            saved = json.load(fh)
        assert saved["job_sources"]["arbeitnow"]["enabled"] is True


# ---------------------------------------------------------------------------
# 404 — unknown source_key
# ---------------------------------------------------------------------------

class TestUnknownSourceKey:
    def test_unknown_key_returns_404(self, client, tmp_providers_path):
        resp = _post_toggle(client, "not_a_real_source", enabled=True)
        assert resp.status_code == 404

    def test_unknown_key_returns_json_error(self, client, tmp_providers_path):
        resp = _post_toggle(client, "not_a_real_source", enabled=True)
        body = resp.get_json()
        assert "error" in body

    def test_unknown_key_does_not_write_file(self, client, tmp_providers_path):
        _post_toggle(client, "not_a_real_source", enabled=True)
        assert not os.path.exists(tmp_providers_path)


# ---------------------------------------------------------------------------
# 422 — toggle ON with missing required credentials
# ---------------------------------------------------------------------------

class TestToggleOnMissingCredentials:
    def test_missing_credentials_returns_422(self, client, tmp_providers_path):
        # adzuna requires app_id and app_key — both empty here.
        _write_providers(tmp_providers_path, job_sources={
            "adzuna": {"app_id": "", "app_key": "", "enabled": False}
        })
        resp = _post_toggle(client, "adzuna", enabled=True)
        assert resp.status_code == 422

    def test_422_response_contains_error_key(self, client, tmp_providers_path):
        _write_providers(tmp_providers_path, job_sources={
            "adzuna": {"app_id": "", "app_key": "", "enabled": False}
        })
        resp = _post_toggle(client, "adzuna", enabled=True)
        body = resp.get_json()
        assert "error" in body

    def test_422_error_message_includes_display_name(self, client, tmp_providers_path):
        _write_providers(tmp_providers_path, job_sources={
            "adzuna": {"app_id": "", "app_key": "", "enabled": False}
        })
        resp = _post_toggle(client, "adzuna", enabled=True)
        body = resp.get_json()
        assert "Adzuna" in body["error"]

    def test_missing_one_of_two_required_fields_returns_422(self, client, tmp_providers_path):
        # Only app_id present — app_key missing.
        _write_providers(tmp_providers_path, job_sources={
            "adzuna": {"app_id": "my-app-id", "app_key": "", "enabled": False}
        })
        resp = _post_toggle(client, "adzuna", enabled=True)
        assert resp.status_code == 422

    def test_422_does_not_write_to_file_when_no_prior_file(self, client, tmp_providers_path):
        # No providers.json at all — treated as all-empty credentials.
        resp = _post_toggle(client, "adzuna", enabled=True)
        assert resp.status_code == 422
        # File should not have been created by the failed toggle.
        assert not os.path.exists(tmp_providers_path)


# ---------------------------------------------------------------------------
# 400 — malformed request body
# ---------------------------------------------------------------------------

class TestMalformedBody:
    def test_non_json_body_returns_400(self, client, tmp_providers_path):
        resp = client.post(
            "/api/job-sources/adzuna/toggle",
            data="not json",
            content_type="text/plain",
        )
        assert resp.status_code == 400

    def test_missing_enabled_field_returns_400(self, client, tmp_providers_path):
        resp = client.post(
            "/api/job-sources/adzuna/toggle",
            data=json.dumps({"wrong_key": True}),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_non_boolean_enabled_returns_400(self, client, tmp_providers_path):
        """String 'false' should be rejected, not coerced to bool."""
        resp = client.post(
            "/api/job-sources/adzuna/toggle",
            data=json.dumps({"enabled": "false"}),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_numeric_enabled_returns_400(self, client, tmp_providers_path):
        """Numeric 0/1 should be rejected, not coerced to bool."""
        resp = client.post(
            "/api/job-sources/adzuna/toggle",
            data=json.dumps({"enabled": 0}),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_form_encoded_body_returns_400(self, client, tmp_providers_path):
        """HTMX without json-enc sends form data — endpoint must reject it."""
        resp = client.post(
            "/api/job-sources/adzuna/toggle",
            data={"enabled": "true"},  # form-encoded, not JSON
            content_type="application/x-www-form-urlencoded",
        )
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# 500 — write failure
# ---------------------------------------------------------------------------

class TestWriteFailure:
    def test_oserror_on_save_returns_500(self, client, tmp_providers_path, monkeypatch):
        _write_providers(tmp_providers_path, job_sources={
            "adzuna": {"app_id": "my-app-id", "app_key": "my-app-key", "enabled": False}
        })

        def _failing_save(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(app_module, "save_providers", _failing_save)
        resp = _post_toggle(client, "adzuna", enabled=True)
        assert resp.status_code == 500
