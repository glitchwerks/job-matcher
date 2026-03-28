"""
tests/test_settings.py — Tests for the /settings GET and POST routes.

Uses Flask's built-in test client so no real HTTP is involved. A temporary
directory is used for keys.json so tests are fully isolated from the real
project file.
"""

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
def tmp_keys_path(tmp_path, monkeypatch):
    """Point _KEYS_PATH at a temp file so tests don't touch the real keys.json."""
    path = str(tmp_path / "keys.json")
    monkeypatch.setattr(app_module, "_KEYS_PATH", path)
    return path


@pytest.fixture()
def client():
    flask_app.config["TESTING"] = True
    with flask_app.test_client() as c:
        yield c


# ---------------------------------------------------------------------------
# GET /settings — no keys.json present
# ---------------------------------------------------------------------------

class TestSettingsGetNoFile:
    def test_returns_200(self, client, tmp_keys_path):
        resp = client.get("/settings")
        assert resp.status_code == 200

    def test_shows_not_set_for_all_providers(self, client, tmp_keys_path, tmp_config_path):
        resp = client.get("/settings")
        body = resp.data.decode()
        # All three LLM providers + 2 Adzuna fields should show "not set"
        assert body.count("not-set") == 5

    def test_never_exposes_key_values(self, client, tmp_keys_path):
        """GET must not render any actual API key string even if the file exists."""
        with open(tmp_keys_path, "w") as f:
            json.dump({
                "providers": {
                    "anthropic": {"api_key": "sk-secret-abc", "model": "claude-haiku-4-5-20251001"},
                    "openai":    {"api_key": "", "model": "gpt-4o-mini"},
                    "gemini":    {"api_key": "", "model": "gemini-1.5-flash"},
                },
                "preferred_provider": "anthropic",
            }, f)

        resp = client.get("/settings")
        body = resp.data.decode()
        assert "sk-secret-abc" not in body

    def test_default_model_values_are_pre_filled(self, client, tmp_keys_path):
        resp = client.get("/settings")
        body = resp.data.decode()
        assert "claude-haiku-4-5-20251001" in body
        assert "gpt-4o-mini" in body
        assert "gemini-1.5-flash" in body

    def test_settings_tab_is_active(self, client, tmp_keys_path):
        resp = client.get("/settings")
        body = resp.data.decode()
        # The active nav-tab should link to /settings
        assert 'href="/settings"' in body
        # There must be exactly one active nav link on this page
        assert body.count("nav-tab active") == 1
        assert 'href="/settings"\n         class="nav-tab active"' in body


# ---------------------------------------------------------------------------
# GET /settings — keys.json exists with a configured key
# ---------------------------------------------------------------------------

class TestSettingsGetWithFile:
    def _write_keys(self, path, anthropic_key="sk-real", openai_key="", gemini_key=""):
        data = {
            "providers": {
                "anthropic": {"api_key": anthropic_key, "model": "claude-haiku-4-5-20251001"},
                "openai":    {"api_key": openai_key,    "model": "gpt-4o-mini"},
                "gemini":    {"api_key": gemini_key,    "model": "gemini-1.5-flash"},
            },
            "preferred_provider": "anthropic",
        }
        with open(path, "w") as f:
            json.dump(data, f)

    def test_configured_badge_shown_when_key_present(self, client, tmp_keys_path):
        self._write_keys(tmp_keys_path, anthropic_key="sk-real")
        resp = client.get("/settings")
        body = resp.data.decode()
        assert "configured" in body

    def test_not_set_badge_shown_for_empty_key(self, client, tmp_keys_path, tmp_config_path):
        self._write_keys(tmp_keys_path, openai_key="")
        resp = client.get("/settings")
        body = resp.data.decode()
        # openai and gemini LLM fields + 2 Adzuna fields (also empty by default) — four not-set badges
        assert body.count("not-set") == 4

    def test_no_key_values_in_response(self, client, tmp_keys_path):
        self._write_keys(tmp_keys_path, anthropic_key="sk-real")
        resp = client.get("/settings")
        body = resp.data.decode()
        assert "sk-real" not in body

    def test_preferred_provider_selected(self, client, tmp_keys_path):
        self._write_keys(tmp_keys_path)
        resp = client.get("/settings")
        body = resp.data.decode()
        # The select option for anthropic should carry the selected attribute.
        assert 'value="anthropic" selected' in body


# ---------------------------------------------------------------------------
# POST /settings — saves new keys
# ---------------------------------------------------------------------------

class TestSettingsPost:
    def test_saves_new_key_and_returns_200(self, client, tmp_keys_path):
        resp = client.post("/settings", data={
            "anthropic_key": "sk-new-key",
            "anthropic_model": "claude-haiku-4-5-20251001",
            "openai_key": "",
            "openai_model": "gpt-4o-mini",
            "gemini_key": "",
            "gemini_model": "gemini-1.5-flash",
            "preferred_provider": "anthropic",
        })
        assert resp.status_code == 200

        with open(tmp_keys_path) as f:
            saved = json.load(f)
        assert saved["providers"]["anthropic"]["api_key"] == "sk-new-key"

    def test_shows_saved_notice_on_success(self, client, tmp_keys_path):
        resp = client.post("/settings", data={
            "anthropic_key": "sk-abc",
            "anthropic_model": "claude-haiku-4-5-20251001",
            "openai_key": "",
            "openai_model": "gpt-4o-mini",
            "gemini_key": "",
            "gemini_model": "gemini-1.5-flash",
            "preferred_provider": "anthropic",
        })
        body = resp.data.decode()
        assert "Settings saved." in body

    def test_blank_key_field_preserves_existing_key(self, client, tmp_keys_path):
        # Pre-populate file with an existing key.
        with open(tmp_keys_path, "w") as f:
            json.dump({
                "providers": {
                    "anthropic": {"api_key": "sk-existing", "model": "claude-haiku-4-5-20251001"},
                    "openai":    {"api_key": "", "model": "gpt-4o-mini"},
                    "gemini":    {"api_key": "", "model": "gemini-1.5-flash"},
                },
                "preferred_provider": "anthropic",
            }, f)

        # Submit with blank anthropic_key — should NOT overwrite.
        client.post("/settings", data={
            "anthropic_key": "",
            "anthropic_model": "claude-haiku-4-5-20251001",
            "openai_key": "",
            "openai_model": "gpt-4o-mini",
            "gemini_key": "",
            "gemini_model": "gemini-1.5-flash",
            "preferred_provider": "anthropic",
        })

        with open(tmp_keys_path) as f:
            saved = json.load(f)
        assert saved["providers"]["anthropic"]["api_key"] == "sk-existing"

    def test_model_is_always_updated(self, client, tmp_keys_path):
        client.post("/settings", data={
            "anthropic_key": "",
            "anthropic_model": "claude-opus-4-20251001",
            "openai_key": "",
            "openai_model": "gpt-4o-mini",
            "gemini_key": "",
            "gemini_model": "gemini-1.5-flash",
            "preferred_provider": "anthropic",
        })
        with open(tmp_keys_path) as f:
            saved = json.load(f)
        assert saved["providers"]["anthropic"]["model"] == "claude-opus-4-20251001"

    def test_preferred_provider_is_saved(self, client, tmp_keys_path):
        client.post("/settings", data={
            "anthropic_key": "",
            "anthropic_model": "claude-haiku-4-5-20251001",
            "openai_key": "sk-openai",
            "openai_model": "gpt-4o-mini",
            "gemini_key": "",
            "gemini_model": "gemini-1.5-flash",
            "preferred_provider": "openai",
        })
        with open(tmp_keys_path) as f:
            saved = json.load(f)
        assert saved["preferred_provider"] == "openai"

    def test_invalid_preferred_provider_is_rejected(self, client, tmp_keys_path):
        """An unrecognised preferred_provider value must not be written to disk."""
        client.post("/settings", data={
            "anthropic_key": "",
            "anthropic_model": "claude-haiku-4-5-20251001",
            "openai_key": "",
            "openai_model": "gpt-4o-mini",
            "gemini_key": "",
            "gemini_model": "gemini-1.5-flash",
            "preferred_provider": "malicious_provider",
        })
        with open(tmp_keys_path) as f:
            saved = json.load(f)
        # Should fall back to the default.
        assert saved["preferred_provider"] == "anthropic"

    def test_saved_key_not_echoed_in_response(self, client, tmp_keys_path):
        """Even after a successful POST the raw key must not appear in the HTML."""
        resp = client.post("/settings", data={
            "anthropic_key": "sk-supersecret",
            "anthropic_model": "claude-haiku-4-5-20251001",
            "openai_key": "",
            "openai_model": "gpt-4o-mini",
            "gemini_key": "",
            "gemini_model": "gemini-1.5-flash",
            "preferred_provider": "anthropic",
        })
        body = resp.data.decode()
        assert "sk-supersecret" not in body

    def test_creates_keys_json_from_scratch(self, client, tmp_keys_path):
        assert not os.path.exists(tmp_keys_path)
        client.post("/settings", data={
            "anthropic_key": "sk-brand-new",
            "anthropic_model": "claude-haiku-4-5-20251001",
            "openai_key": "",
            "openai_model": "gpt-4o-mini",
            "gemini_key": "",
            "gemini_model": "gemini-1.5-flash",
            "preferred_provider": "anthropic",
        })
        assert os.path.exists(tmp_keys_path)
        with open(tmp_keys_path) as f:
            saved = json.load(f)
        assert saved["providers"]["anthropic"]["api_key"] == "sk-brand-new"


# ---------------------------------------------------------------------------
# Adzuna credentials on /settings (#20)
# ---------------------------------------------------------------------------

@pytest.fixture()
def tmp_config_path(tmp_path, monkeypatch):
    """Point _CONFIG_PATH at a temp file so tests don't touch the real config.json."""
    path = str(tmp_path / "config.json")
    monkeypatch.setattr(app_module, "_CONFIG_PATH", path)
    return path


class TestSettingsAdzuna:
    """Tests for the Adzuna credentials section on /settings."""

    def test_get_no_config_shows_not_set_badges(self, client, tmp_keys_path, tmp_config_path):
        """When config.json is absent both Adzuna fields should show the not-set badge."""
        resp = client.get("/settings")
        body = resp.data.decode()
        assert "App ID not set" in body
        assert "App Key not set" in body

    def test_get_with_credentials_shows_configured_badges(self, client, tmp_keys_path, tmp_config_path):
        """When both Adzuna credentials are present both badges should read 'configured'."""
        with open(tmp_config_path, "w") as f:
            json.dump({"adzuna_app_id": "myid123", "adzuna_app_key": "mykey456"}, f)

        resp = client.get("/settings")
        body = resp.data.decode()
        assert "App ID configured" in body
        assert "App Key configured" in body

    def test_get_never_exposes_raw_adzuna_values(self, client, tmp_keys_path, tmp_config_path):
        """Raw Adzuna credential values must never appear in the HTML response."""
        with open(tmp_config_path, "w") as f:
            json.dump({"adzuna_app_id": "raw-id-secret", "adzuna_app_key": "raw-key-secret"}, f)

        resp = client.get("/settings")
        body = resp.data.decode()
        assert "raw-id-secret" not in body
        assert "raw-key-secret" not in body

    def test_post_writes_adzuna_credentials_to_config(self, client, tmp_keys_path, tmp_config_path):
        """Submitting non-blank Adzuna fields should write them to config.json."""
        resp = client.post("/settings", data={
            "anthropic_key": "", "anthropic_model": "claude-haiku-4-5-20251001",
            "openai_key": "", "openai_model": "gpt-4o-mini",
            "gemini_key": "", "gemini_model": "gemini-1.5-flash",
            "preferred_provider": "anthropic",
            "adzuna_app_id": "new-app-id",
            "adzuna_app_key": "new-app-key",
        })
        assert resp.status_code == 200

        with open(tmp_config_path) as f:
            saved = json.load(f)
        assert saved["adzuna_app_id"] == "new-app-id"
        assert saved["adzuna_app_key"] == "new-app-key"

    def test_post_blank_adzuna_fields_preserve_existing_values(self, client, tmp_keys_path, tmp_config_path):
        """Submitting blank Adzuna fields must NOT overwrite existing values."""
        with open(tmp_config_path, "w") as f:
            json.dump({"adzuna_app_id": "existing-id", "adzuna_app_key": "existing-key"}, f)

        client.post("/settings", data={
            "anthropic_key": "", "anthropic_model": "claude-haiku-4-5-20251001",
            "openai_key": "", "openai_model": "gpt-4o-mini",
            "gemini_key": "", "gemini_model": "gemini-1.5-flash",
            "preferred_provider": "anthropic",
            "adzuna_app_id": "",
            "adzuna_app_key": "",
        })

        with open(tmp_config_path) as f:
            saved = json.load(f)
        assert saved["adzuna_app_id"] == "existing-id"
        assert saved["adzuna_app_key"] == "existing-key"

    def test_post_saved_adzuna_values_not_echoed_in_response(self, client, tmp_keys_path, tmp_config_path):
        """Even after a successful POST the raw Adzuna credentials must not appear in the HTML."""
        resp = client.post("/settings", data={
            "anthropic_key": "", "anthropic_model": "claude-haiku-4-5-20251001",
            "openai_key": "", "openai_model": "gpt-4o-mini",
            "gemini_key": "", "gemini_model": "gemini-1.5-flash",
            "preferred_provider": "anthropic",
            "adzuna_app_id": "super-secret-id",
            "adzuna_app_key": "super-secret-key",
        })
        body = resp.data.decode()
        assert "super-secret-id" not in body
        assert "super-secret-key" not in body

    def test_post_config_write_failure_returns_200_with_error_message(
        self, client, tmp_keys_path, tmp_config_path, monkeypatch
    ):
        """If config.json cannot be written the response should be 200 with an error notice."""
        original_open = open

        def patched_open(file, mode="r", **kwargs):
            # Raise OSError only when writing config.json.
            if "w" in mode and str(file) == tmp_config_path:
                raise OSError("Permission denied")
            return original_open(file, mode, **kwargs)

        monkeypatch.setattr("builtins.open", patched_open)

        resp = client.post("/settings", data={
            "anthropic_key": "", "anthropic_model": "claude-haiku-4-5-20251001",
            "openai_key": "", "openai_model": "gpt-4o-mini",
            "gemini_key": "", "gemini_model": "gemini-1.5-flash",
            "preferred_provider": "anthropic",
            "adzuna_app_id": "some-id",
            "adzuna_app_key": "some-key",
        })
        assert resp.status_code == 200
        body = resp.data.decode()
        assert "Could not save settings" in body
        # Raw credential values must not leak even on error.
        assert "some-id" not in body
        assert "some-key" not in body


# ---------------------------------------------------------------------------
# _load_keys helper — unit tests
# ---------------------------------------------------------------------------

class TestLoadKeys:
    def test_returns_defaults_when_file_absent(self, tmp_keys_path):
        assert not os.path.exists(tmp_keys_path)
        result = app_module._load_keys()
        assert result["preferred_provider"] == "anthropic"
        assert result["providers"]["anthropic"]["api_key"] == ""

    def test_returns_defaults_on_corrupt_json(self, tmp_keys_path):
        with open(tmp_keys_path, "w") as f:
            f.write("not valid json {{{{")
        result = app_module._load_keys()
        assert result["providers"]["anthropic"]["api_key"] == ""

    def test_fills_missing_providers_with_defaults(self, tmp_keys_path):
        # Write a file that only has the anthropic provider.
        with open(tmp_keys_path, "w") as f:
            json.dump({
                "providers": {
                    "anthropic": {"api_key": "sk-x", "model": "claude-haiku-4-5-20251001"},
                },
                "preferred_provider": "anthropic",
            }, f)
        result = app_module._load_keys()
        assert "openai" in result["providers"]
        assert "gemini" in result["providers"]

    def test_does_not_mutate_module_defaults(self, tmp_keys_path):
        original = json.dumps(app_module._KEYS_DEFAULTS, sort_keys=True)
        result = app_module._load_keys()
        result["providers"]["anthropic"]["api_key"] = "mutated"
        after = json.dumps(app_module._KEYS_DEFAULTS, sort_keys=True)
        assert original == after
