"""
tests/test_settings.py — Tests for the /settings GET and POST routes.

Uses Flask's built-in test client so no real HTTP is involved. A temporary
directory is used for keys.json so tests are fully isolated from the real
project file.
"""

import json
import os
import sys
import tempfile

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

    def test_shows_not_set_for_all_providers(self, client, tmp_keys_path):
        resp = client.get("/settings")
        body = resp.data.decode()
        # All three providers should show "not set"
        assert body.count("not-set") == 3

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

    def test_not_set_badge_shown_for_empty_key(self, client, tmp_keys_path):
        self._write_keys(tmp_keys_path, openai_key="")
        resp = client.get("/settings")
        body = resp.data.decode()
        # openai and gemini are both empty — two not-set badges
        assert body.count("not-set") == 2

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
