"""
tests/test_settings.py — Tests for the /settings GET and POST routes.

Uses Flask's built-in test client so no real HTTP is involved. A temporary
directory is used for providers.json/keys.json so tests are fully isolated
from the real project files.
"""

import json
import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app as app_module
from app import app as flask_app
from providers.anthropic_provider import AnthropicProvider
from providers.openai_provider import OpenAIProvider
from providers.gemini_provider import GeminiProvider


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def tmp_keys_path(tmp_path, monkeypatch):
    """Point _KEYS_PATH at a temp file so legacy migration never touches keys.json."""
    path = str(tmp_path / "keys.json")
    monkeypatch.setattr(app_module, "_KEYS_PATH", path)
    return path


@pytest.fixture()
def tmp_providers_path(tmp_path, monkeypatch):
    """Point _PROVIDERS_PATH at a temp file so tests don't touch providers.json."""
    path = str(tmp_path / "providers.json")
    monkeypatch.setattr(app_module, "_PROVIDERS_PATH", path)
    return path


@pytest.fixture()
def client():
    flask_app.config["TESTING"] = True
    with flask_app.test_client() as c:
        yield c


def _write_providers(path: str, **overrides) -> None:
    """Write a providers.json fixture to *path* with sane defaults."""
    data = {
        "provider_order": ["anthropic", "openai", "gemini"],
        "llm": {
            "anthropic": {"api_key": overrides.get("anthropic_key", "sk-real"),
                          "model": "claude-haiku-4-5-20251001"},
            "openai":    {"api_key": overrides.get("openai_key", ""),
                          "model": "gpt-4o-mini"},
            "gemini":    {"api_key": overrides.get("gemini_key", ""),
                          "model": "gemini-1.5-flash"},
        },
        "job_sources": {
            "adzuna": {
                "app_id":  overrides.get("adzuna_app_id", ""),
                "app_key": overrides.get("adzuna_app_key", ""),
            },
        },
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)


# ---------------------------------------------------------------------------
# GET /settings — no providers.json present
# ---------------------------------------------------------------------------

class TestSettingsGetNoFile:
    def test_returns_200(self, client, tmp_providers_path, tmp_keys_path):
        resp = client.get("/settings")
        assert resp.status_code == 200

    def test_shows_not_set_for_all_unconfigured_providers(
        self, client, tmp_providers_path, tmp_keys_path, tmp_config_path
    ):
        resp = client.get("/settings")
        body = resp.data.decode()
        # All LLM providers + Adzuna have empty keys → all show "not-set"
        assert "not-set" in body

    def test_never_exposes_key_values(self, client, tmp_providers_path, tmp_keys_path):
        """GET must not render any actual API key string even if the file exists."""
        _write_providers(tmp_providers_path, anthropic_key="sk-secret-abc")
        resp = client.get("/settings")
        body = resp.data.decode()
        assert "sk-secret-abc" not in body

    def test_settings_tab_is_active(self, client, tmp_providers_path, tmp_keys_path):
        resp = client.get("/settings")
        body = resp.data.decode()
        # The active nav-tab should link to /settings
        assert 'href="/settings"' in body
        # There must be exactly one active nav link on this page
        assert body.count("nav-tab active") == 1
        assert 'href="/settings"\n         class="nav-tab active"' in body


# ---------------------------------------------------------------------------
# GET /settings — providers.json exists with a configured key
# ---------------------------------------------------------------------------

class TestSettingsGetWithFile:
    def test_configured_badge_shown_when_key_present(
        self, client, tmp_providers_path, tmp_keys_path
    ):
        _write_providers(tmp_providers_path, anthropic_key="sk-real")
        resp = client.get("/settings")
        body = resp.data.decode()
        assert "configured" in body

    def test_not_set_badge_shown_for_empty_key(
        self, client, tmp_providers_path, tmp_keys_path, tmp_config_path
    ):
        _write_providers(tmp_providers_path, anthropic_key="sk-real", openai_key="", gemini_key="")
        resp = client.get("/settings")
        body = resp.data.decode()
        # openai and gemini LLM fields have empty api_key — at least 2 not-set badges
        assert body.count("not-set") >= 2

    def test_no_key_values_in_response(self, client, tmp_providers_path, tmp_keys_path):
        _write_providers(tmp_providers_path, anthropic_key="sk-real")
        resp = client.get("/settings")
        body = resp.data.decode()
        assert "sk-real" not in body


# ---------------------------------------------------------------------------
# POST /settings — saves new keys to providers.json
# ---------------------------------------------------------------------------

class TestSettingsPost:
    def test_saves_new_key_and_redirects(self, client, tmp_providers_path, tmp_keys_path):
        _write_providers(tmp_providers_path)
        resp = client.post("/settings", data={
            "anthropic__api_key": "sk-new-key",
            "anthropic__model": "claude-haiku-4-5-20251001",
            "tab": "llm",
        }, follow_redirects=True)
        assert resp.status_code == 200

        with open(tmp_providers_path, encoding="utf-8") as f:
            saved = json.load(f)
        assert saved["llm"]["anthropic"]["api_key"] == "sk-new-key"

    def test_blank_key_field_preserves_existing_key(
        self, client, tmp_providers_path, tmp_keys_path
    ):
        _write_providers(tmp_providers_path, anthropic_key="sk-existing")

        # Submit with blank anthropic api_key — should NOT overwrite.
        client.post("/settings", data={
            "anthropic__api_key": "",
            "anthropic__model": "claude-haiku-4-5-20251001",
            "tab": "llm",
        })

        with open(tmp_providers_path, encoding="utf-8") as f:
            saved = json.load(f)
        assert saved["llm"]["anthropic"]["api_key"] == "sk-existing"

    def test_model_is_updated(self, client, tmp_providers_path, tmp_keys_path):
        _write_providers(tmp_providers_path)
        client.post("/settings", data={
            "anthropic__api_key": "",
            "anthropic__model": "claude-opus-4-20251001",
            "tab": "llm",
        })
        with open(tmp_providers_path, encoding="utf-8") as f:
            saved = json.load(f)
        assert saved["llm"]["anthropic"]["model"] == "claude-opus-4-20251001"

    def test_saved_key_not_echoed_in_response(
        self, client, tmp_providers_path, tmp_keys_path
    ):
        """After a successful POST+redirect the raw key must not appear in HTML."""
        resp = client.post("/settings", data={
            "anthropic__api_key": "sk-supersecret",
            "tab": "llm",
        }, follow_redirects=True)
        body = resp.data.decode()
        assert "sk-supersecret" not in body

    def test_creates_providers_json_from_scratch(
        self, client, tmp_providers_path, tmp_keys_path
    ):
        assert not os.path.exists(tmp_providers_path)
        client.post("/settings", data={
            "anthropic__api_key": "sk-brand-new",
            "tab": "llm",
        })
        assert os.path.exists(tmp_providers_path)
        with open(tmp_providers_path, encoding="utf-8") as f:
            saved = json.load(f)
        assert saved["llm"]["anthropic"]["api_key"] == "sk-brand-new"


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
    """Tests for the Adzuna credentials section on /settings (Job Sources tab)."""

    def test_get_shows_not_set_when_adzuna_empty(
        self, client, tmp_providers_path, tmp_keys_path, tmp_config_path
    ):
        """When adzuna credentials are absent the Job Sources tab shows the not-set badge."""
        _write_providers(tmp_providers_path, adzuna_app_id="", adzuna_app_key="")
        resp = client.get("/settings")
        body = resp.data.decode()
        assert "not-set" in body

    def test_get_shows_configured_when_adzuna_present(
        self, client, tmp_providers_path, tmp_keys_path, tmp_config_path
    ):
        """When both Adzuna credentials are present the badge should read 'configured'."""
        _write_providers(
            tmp_providers_path, adzuna_app_id="myid123", adzuna_app_key="mykey456"
        )
        resp = client.get("/settings")
        body = resp.data.decode()
        assert "configured" in body

    def test_get_never_exposes_raw_adzuna_values(
        self, client, tmp_providers_path, tmp_keys_path, tmp_config_path
    ):
        """Raw Adzuna credential values must never appear in the HTML response."""
        _write_providers(
            tmp_providers_path, adzuna_app_id="raw-id-secret", adzuna_app_key="raw-key-secret"
        )
        resp = client.get("/settings")
        body = resp.data.decode()
        assert "raw-id-secret" not in body
        assert "raw-key-secret" not in body

    def test_post_writes_adzuna_credentials_to_providers_json(
        self, client, tmp_providers_path, tmp_keys_path, tmp_config_path
    ):
        """Submitting non-blank Adzuna fields should write them to providers.json."""
        _write_providers(tmp_providers_path)
        resp = client.post("/settings", data={
            "adzuna__app_id": "new-app-id",
            "adzuna__app_key": "new-app-key",
            "tab": "sources",
        }, follow_redirects=True)
        assert resp.status_code == 200

        with open(tmp_providers_path, encoding="utf-8") as f:
            saved = json.load(f)
        assert saved["job_sources"]["adzuna"]["app_id"] == "new-app-id"
        assert saved["job_sources"]["adzuna"]["app_key"] == "new-app-key"

    def test_post_blank_adzuna_fields_preserve_existing_values(
        self, client, tmp_providers_path, tmp_keys_path, tmp_config_path
    ):
        """Submitting blank Adzuna fields must NOT overwrite existing values."""
        _write_providers(
            tmp_providers_path, adzuna_app_id="existing-id", adzuna_app_key="existing-key"
        )
        client.post("/settings", data={
            "adzuna__app_id": "",
            "adzuna__app_key": "",
            "tab": "sources",
        })

        with open(tmp_providers_path, encoding="utf-8") as f:
            saved = json.load(f)
        assert saved["job_sources"]["adzuna"]["app_id"] == "existing-id"
        assert saved["job_sources"]["adzuna"]["app_key"] == "existing-key"

    def test_post_saved_adzuna_values_not_echoed_in_response(
        self, client, tmp_providers_path, tmp_keys_path, tmp_config_path
    ):
        """Even after a successful POST+redirect the raw Adzuna credentials must not appear."""
        _write_providers(tmp_providers_path)
        resp = client.post("/settings", data={
            "adzuna__app_id": "super-secret-id",
            "adzuna__app_key": "super-secret-key",
            "tab": "sources",
        }, follow_redirects=True)
        body = resp.data.decode()
        assert "super-secret-id" not in body
        assert "super-secret-key" not in body

    def test_post_write_failure_returns_200_with_error_message(
        self, client, tmp_providers_path, tmp_keys_path, tmp_config_path, monkeypatch
    ):
        """If providers.json cannot be written the response should show an error notice."""
        _write_providers(tmp_providers_path)
        original_open = open

        def patched_open(file, mode="r", **kwargs):
            # Raise OSError only when writing the tmp file used by save_providers.
            if "w" in str(mode) and str(file) == tmp_providers_path + ".tmp":
                raise OSError("Permission denied")
            return original_open(file, mode, **kwargs)

        monkeypatch.setattr("builtins.open", patched_open)

        resp = client.post("/settings", data={
            "adzuna__app_id": "some-id",
            "adzuna__app_key": "some-key",
            "tab": "sources",
        })
        assert resp.status_code == 200
        body = resp.data.decode()
        assert "Could not save settings" in body
        # Raw credential values must not leak even on error.
        assert "some-id" not in body
        assert "some-key" not in body


# ---------------------------------------------------------------------------
# GET /profile (#21, updated #90)
# ---------------------------------------------------------------------------

class TestSettingsConfigGet:
    """Tests for the GET /profile route (config editor)."""

    def test_returns_200(self, client, tmp_config_path):
        resp = client.get("/profile")
        assert resp.status_code == 200

    def test_renders_textarea_with_json(self, client, tmp_config_path):
        with open(tmp_config_path, "w") as f:
            json.dump({"scoring": {"threshold": 7.5}}, f)

        resp = client.get("/profile")
        body = resp.data.decode()
        assert "<textarea" in body
        assert "7.5" in body

    def test_masks_app_id_field(self, client, tmp_config_path):
        with open(tmp_config_path, "w") as f:
            json.dump({"adzuna_app_id": "real-id-secret", "adzuna_app_key": "real-key-secret"}, f)

        resp = client.get("/profile")
        body = resp.data.decode()
        assert "real-id-secret" not in body
        assert "real-key-secret" not in body
        assert "***" in body

    def test_masks_api_key_field(self, client, tmp_config_path):
        with open(tmp_config_path, "w") as f:
            json.dump({"some_api_key": "super-secret-api-key"}, f)

        resp = client.get("/profile")
        body = resp.data.decode()
        assert "super-secret-api-key" not in body
        assert "***" in body

    def test_non_sensitive_fields_are_visible(self, client, tmp_config_path):
        with open(tmp_config_path, "w") as f:
            json.dump({"scoring": {"threshold": 8.0}, "adzuna_app_id": "secret"}, f)

        resp = client.get("/profile")
        body = resp.data.decode()
        assert "8.0" in body

    def test_profile_tab_on_settings_page(
        self, client, tmp_providers_path, tmp_keys_path, tmp_config_path
    ):
        """The /settings page nav must contain a link to /profile."""
        resp = client.get("/settings")
        body = resp.data.decode()
        assert "/profile" in body

    def test_settings_config_redirects_to_profile(self, client, tmp_config_path):
        """/settings/config must 301-redirect to /profile."""
        resp = client.get("/settings/config")
        assert resp.status_code == 301
        assert resp.headers["Location"].endswith("/profile")

    def test_works_when_config_absent(self, client, tmp_config_path):
        """GET should still return 200 even when config.json does not exist."""
        assert not os.path.exists(tmp_config_path)
        resp = client.get("/profile")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# POST /profile (#21, updated #90)
# ---------------------------------------------------------------------------

class TestSettingsConfigPost:
    """Tests for the POST /profile route (config editor)."""

    def test_valid_json_updates_file(self, client, tmp_config_path):
        original = {"scoring": {"threshold": 7.0}, "adzuna_app_id": "orig-id"}
        with open(tmp_config_path, "w") as f:
            json.dump(original, f)

        new_config = json.dumps({"scoring": {"threshold": 9.0}, "adzuna_app_id": "orig-id"})
        resp = client.post("/profile", data={"config_json": new_config})
        assert resp.status_code == 200

        with open(tmp_config_path) as f:
            saved = json.load(f)
        assert saved["scoring"]["threshold"] == 9.0

    def test_valid_save_shows_success_notice(self, client, tmp_config_path):
        resp = client.post(
            "/profile",
            data={"config_json": json.dumps({"scoring": {"threshold": 7.0}})},
        )
        body = resp.data.decode()
        assert "saved" in body.lower()

    def test_invalid_json_returns_400(self, client, tmp_config_path):
        resp = client.post("/profile", data={"config_json": "not valid json {{{"})
        assert resp.status_code == 400

    def test_invalid_json_shows_error_message(self, client, tmp_config_path):
        resp = client.post("/profile", data={"config_json": "not valid json {{{"})
        body = resp.data.decode()
        assert "Invalid JSON" in body

    def test_invalid_json_leaves_file_unchanged(self, client, tmp_config_path):
        original = {"scoring": {"threshold": 7.0}}
        with open(tmp_config_path, "w") as f:
            json.dump(original, f)

        client.post("/profile", data={"config_json": "not valid json {{{"})

        with open(tmp_config_path) as f:
            after = json.load(f)
        assert after == original

    def test_masked_sentinel_not_written_to_disk(self, client, tmp_config_path):
        """If a sensitive field still contains '***', the original value is preserved."""
        original = {
            "adzuna_app_id": "keep-this-id",
            "adzuna_app_key": "keep-this-key",
            "scoring": {"threshold": 7.0},
        }
        with open(tmp_config_path, "w") as f:
            json.dump(original, f)

        # Submit with the masked sentinel values (as the browser would receive them).
        # scoring.threshold must be included so the new validation gate allows the save.
        masked_submission = json.dumps({
            "adzuna_app_id": "***",
            "adzuna_app_key": "***",
            "scoring": {"threshold": 7.0},
        })
        resp = client.post("/profile", data={"config_json": masked_submission})
        assert resp.status_code == 200

        with open(tmp_config_path) as f:
            saved = json.load(f)
        assert saved["adzuna_app_id"] == "keep-this-id"
        assert saved["adzuna_app_key"] == "keep-this-key"

    def test_response_never_contains_real_sensitive_values(self, client, tmp_config_path):
        """After a successful POST the response must not echo raw sensitive values."""
        original = {"adzuna_app_id": "do-not-echo-me"}
        with open(tmp_config_path, "w") as f:
            json.dump(original, f)

        resp = client.post(
            "/profile",
            data={"config_json": json.dumps({"adzuna_app_id": "do-not-echo-me"})},
        )
        body = resp.data.decode()
        assert "do-not-echo-me" not in body


# ---------------------------------------------------------------------------
# _mask_config_keys helper — unit tests (#21)
# ---------------------------------------------------------------------------

class TestMaskConfigKeys:
    """Unit tests for the _mask_config_keys helper."""

    def test_masks_app_id(self):
        data = {"adzuna_app_id": "real-id"}
        result = app_module._mask_config_keys(data)
        assert result["adzuna_app_id"] == "***"

    def test_masks_app_key(self):
        data = {"adzuna_app_key": "real-key"}
        result = app_module._mask_config_keys(data)
        assert result["adzuna_app_key"] == "***"

    def test_masks_api_key(self):
        data = {"some_api_key": "sk-secret"}
        result = app_module._mask_config_keys(data)
        assert result["some_api_key"] == "***"

    def test_case_insensitive_matching(self):
        data = {"ADZUNA_APP_ID": "id-value", "My_API_Key": "key-value"}
        result = app_module._mask_config_keys(data)
        assert result["ADZUNA_APP_ID"] == "***"
        assert result["My_API_Key"] == "***"

    def test_non_sensitive_keys_unchanged(self):
        data = {"scoring": {"threshold": 7.0}, "country": "us"}
        result = app_module._mask_config_keys(data)
        assert result["scoring"]["threshold"] == 7.0
        assert result["country"] == "us"

    def test_recursive_masking_in_nested_dict(self):
        data = {"providers": {"anthropic": {"anthropic_api_key": "sk-secret", "model": "claude"}}}
        result = app_module._mask_config_keys(data)
        assert result["providers"]["anthropic"]["anthropic_api_key"] == "***"
        assert result["providers"]["anthropic"]["model"] == "claude"

    def test_does_not_mutate_original(self):
        data = {"adzuna_app_id": "original"}
        result = app_module._mask_config_keys(data)
        assert data["adzuna_app_id"] == "original"
        assert result["adzuna_app_id"] == "***"

    def test_list_values_preserved(self):
        data = {"title_exclude": ["junior", "intern"], "adzuna_app_id": "secret"}
        result = app_module._mask_config_keys(data)
        assert result["title_exclude"] == ["junior", "intern"]
        assert result["adzuna_app_id"] == "***"


# ---------------------------------------------------------------------------
# _load_providers_safe helper — unit tests
# ---------------------------------------------------------------------------

class TestLoadProvidersSafe:
    """Unit tests for the _load_providers_safe() helper added in issue #149.

    This replaces the old TestLoadKeys tests which covered the removed
    _load_keys() shim.
    """

    def test_returns_empty_skeleton_when_no_file(
        self, tmp_providers_path, tmp_keys_path, tmp_config_path
    ):
        """When providers.json is absent and no migration sources exist, returns
        safe empty dict rather than raising."""
        assert not os.path.exists(tmp_providers_path)
        result = app_module._load_providers_safe()
        assert "llm" in result
        assert "job_sources" in result
        assert "provider_order" in result

    def test_returns_empty_skeleton_on_corrupt_json(
        self, tmp_providers_path, tmp_keys_path
    ):
        with open(tmp_providers_path, "w") as f:
            f.write("not valid json {{{{")
        result = app_module._load_providers_safe()
        assert result["llm"] == {}
        assert result["job_sources"] == {}

    def test_loads_existing_providers_json(self, tmp_providers_path, tmp_keys_path):
        _write_providers(tmp_providers_path, anthropic_key="sk-x")
        result = app_module._load_providers_safe()
        assert result["llm"]["anthropic"]["api_key"] == "sk-x"

    def test_never_raises(self, tmp_providers_path, tmp_keys_path, tmp_config_path):
        """_load_providers_safe() must never propagate CredentialError."""
        assert not os.path.exists(tmp_providers_path)
        result = app_module._load_providers_safe()
        assert isinstance(result, dict)


# ---------------------------------------------------------------------------
# POST /api/validate-keys — endpoint tests
# ---------------------------------------------------------------------------

class TestValidateKeys:
    """Tests for the POST /api/validate-keys endpoint.

    All LLM calls are monkeypatched so no real network traffic is made.
    The endpoint must return HTML (not JSON) with one row per provider.
    """

    def _write_providers_for_validate(
        self, path,
        anthropic_key="sk-ant",
        openai_key="sk-oai",
        gemini_key="gm-key",
    ):
        """Write providers.json in the new unified format."""
        data = {
            "provider_order": ["anthropic", "openai", "gemini"],
            "llm": {
                "anthropic": {"api_key": anthropic_key, "model": "claude-haiku-4-5-20251001"},
                "openai":    {"api_key": openai_key,    "model": "gpt-4o-mini"},
                "gemini":    {"api_key": gemini_key,    "model": "gemini-1.5-flash"},
            },
            "job_sources": {},
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)

    # ------------------------------------------------------------------
    # Endpoint basics
    # ------------------------------------------------------------------

    def test_returns_200(self, client, tmp_providers_path, tmp_keys_path, monkeypatch):
        self._write_providers_for_validate(tmp_providers_path)
        monkeypatch.setattr(AnthropicProvider, "validate_credentials", classmethod(lambda cls, k, m: ("valid", None)))
        monkeypatch.setattr(OpenAIProvider,    "validate_credentials", classmethod(lambda cls, k, m: ("valid", None)))
        monkeypatch.setattr(GeminiProvider,    "validate_credentials", classmethod(lambda cls, k, m: ("valid", None)))
        resp = client.post("/api/validate-keys")
        assert resp.status_code == 200

    def test_returns_html_not_json(
        self, client, tmp_providers_path, tmp_keys_path, monkeypatch
    ):
        """HTMX expects HTML — the endpoint must not return a JSON object."""
        self._write_providers_for_validate(tmp_providers_path)
        monkeypatch.setattr(AnthropicProvider, "validate_credentials", classmethod(lambda cls, k, m: ("valid", None)))
        monkeypatch.setattr(OpenAIProvider,    "validate_credentials", classmethod(lambda cls, k, m: ("valid", None)))
        monkeypatch.setattr(GeminiProvider,    "validate_credentials", classmethod(lambda cls, k, m: ("valid", None)))
        resp = client.post("/api/validate-keys")
        body = resp.data.decode()
        assert "<" in body
        assert body.strip()[0] != "{"

    def test_shows_provider_names(
        self, client, tmp_providers_path, tmp_keys_path, monkeypatch
    ):
        self._write_providers_for_validate(tmp_providers_path)
        monkeypatch.setattr(AnthropicProvider, "validate_credentials", classmethod(lambda cls, k, m: ("valid", None)))
        monkeypatch.setattr(OpenAIProvider,    "validate_credentials", classmethod(lambda cls, k, m: ("valid", None)))
        monkeypatch.setattr(GeminiProvider,    "validate_credentials", classmethod(lambda cls, k, m: ("valid", None)))
        resp = client.post("/api/validate-keys")
        body = resp.data.decode()
        assert "Anthropic" in body
        assert "OpenAI" in body
        assert "Gemini" in body

    # ------------------------------------------------------------------
    # State rendering — valid
    # ------------------------------------------------------------------

    def test_valid_state_shown_for_all_providers(
        self, client, tmp_providers_path, tmp_keys_path, monkeypatch
    ):
        self._write_providers_for_validate(tmp_providers_path)
        monkeypatch.setattr(AnthropicProvider, "validate_credentials", classmethod(lambda cls, k, m: ("valid", None)))
        monkeypatch.setattr(OpenAIProvider,    "validate_credentials", classmethod(lambda cls, k, m: ("valid", None)))
        monkeypatch.setattr(GeminiProvider,    "validate_credentials", classmethod(lambda cls, k, m: ("valid", None)))
        resp = client.post("/api/validate-keys")
        body = resp.data.decode()
        assert "validation-valid" in body
        assert body.count("validation-valid") == 3

    # ------------------------------------------------------------------
    # State rendering — invalid key
    # ------------------------------------------------------------------

    def test_invalid_key_state_shown(
        self, client, tmp_providers_path, tmp_keys_path, monkeypatch
    ):
        self._write_providers_for_validate(tmp_providers_path)
        monkeypatch.setattr(AnthropicProvider, "validate_credentials", classmethod(lambda cls, k, m: ("invalid_key", "Test invalid key detail")))
        monkeypatch.setattr(OpenAIProvider,    "validate_credentials", classmethod(lambda cls, k, m: ("valid", None)))
        monkeypatch.setattr(GeminiProvider,    "validate_credentials", classmethod(lambda cls, k, m: ("valid", None)))
        resp = client.post("/api/validate-keys")
        body = resp.data.decode()
        assert "validation-invalid" in body
        assert "Invalid key" in body

    # ------------------------------------------------------------------
    # State rendering — unknown model
    # ------------------------------------------------------------------

    def test_unknown_model_state_shown(
        self, client, tmp_providers_path, tmp_keys_path, monkeypatch
    ):
        self._write_providers_for_validate(tmp_providers_path)
        monkeypatch.setattr(AnthropicProvider, "validate_credentials", classmethod(lambda cls, k, m: ("valid", None)))
        monkeypatch.setattr(OpenAIProvider,    "validate_credentials", classmethod(lambda cls, k, m: ("unknown_model", "Test unknown model detail")))
        monkeypatch.setattr(GeminiProvider,    "validate_credentials", classmethod(lambda cls, k, m: ("valid", None)))
        resp = client.post("/api/validate-keys")
        body = resp.data.decode()
        assert "validation-warning" in body
        assert "Unknown model" in body

    # ------------------------------------------------------------------
    # State rendering — unreachable
    # ------------------------------------------------------------------

    def test_unreachable_state_shown(
        self, client, tmp_providers_path, tmp_keys_path, monkeypatch
    ):
        self._write_providers_for_validate(tmp_providers_path)
        monkeypatch.setattr(AnthropicProvider, "validate_credentials", classmethod(lambda cls, k, m: ("valid", None)))
        monkeypatch.setattr(OpenAIProvider,    "validate_credentials", classmethod(lambda cls, k, m: ("valid", None)))
        monkeypatch.setattr(GeminiProvider,    "validate_credentials", classmethod(lambda cls, k, m: ("unreachable", "Test unreachable detail")))
        resp = client.post("/api/validate-keys")
        body = resp.data.decode()
        assert "validation-warning" in body
        assert "Unreachable" in body

    # ------------------------------------------------------------------
    # State rendering — not configured
    # ------------------------------------------------------------------

    def test_not_configured_when_key_absent(
        self, client, tmp_providers_path, tmp_keys_path, monkeypatch
    ):
        """Providers with no key set must show 'not_configured' without calling validator."""
        self._write_providers_for_validate(
            tmp_providers_path, anthropic_key="", openai_key="", gemini_key=""
        )
        def _should_not_be_called(cls, k, m):
            raise AssertionError("should not be called")

        monkeypatch.setattr(AnthropicProvider, "validate_credentials", classmethod(_should_not_be_called))
        monkeypatch.setattr(OpenAIProvider,    "validate_credentials", classmethod(_should_not_be_called))
        monkeypatch.setattr(GeminiProvider,    "validate_credentials", classmethod(_should_not_be_called))
        resp = client.post("/api/validate-keys")
        body = resp.data.decode()
        assert "validation-muted" in body
        assert body.count("validation-muted") == 3

    def test_not_configured_shown_when_no_providers_file(
        self, client, tmp_providers_path, tmp_keys_path, monkeypatch
    ):
        """When providers.json is absent all providers must show not_configured."""
        assert not os.path.exists(tmp_providers_path)
        monkeypatch.setattr(AnthropicProvider, "validate_credentials", classmethod(lambda cls, k, m: ("valid", None)))
        monkeypatch.setattr(OpenAIProvider,    "validate_credentials", classmethod(lambda cls, k, m: ("valid", None)))
        monkeypatch.setattr(GeminiProvider,    "validate_credentials", classmethod(lambda cls, k, m: ("valid", None)))
        resp = client.post("/api/validate-keys")
        body = resp.data.decode()
        assert "validation-muted" in body
        assert body.count("validation-muted") == 3

    # ------------------------------------------------------------------
    # Isolation — failure in one provider must not block others
    # ------------------------------------------------------------------

    def test_one_provider_failure_does_not_block_others(
        self, client, tmp_providers_path, tmp_keys_path, monkeypatch
    ):
        """If a validator raises unexpectedly, the other providers still run."""
        self._write_providers_for_validate(tmp_providers_path)

        def _bad_validator(cls, key, model):
            raise RuntimeError("network exploded")

        monkeypatch.setattr(AnthropicProvider, "validate_credentials", classmethod(_bad_validator))
        monkeypatch.setattr(OpenAIProvider,    "validate_credentials", classmethod(lambda cls, k, m: ("valid", None)))
        monkeypatch.setattr(GeminiProvider,    "validate_credentials", classmethod(lambda cls, k, m: ("valid", None)))
        resp = client.post("/api/validate-keys")
        assert resp.status_code == 200
        body = resp.data.decode()
        assert "Unreachable" in body
        assert "validation-valid" in body

    # ------------------------------------------------------------------
    # Detail string — rendered for failure states, hidden for valid/not_configured
    # ------------------------------------------------------------------

    def test_detail_string_rendered_for_invalid_key(
        self, client, tmp_providers_path, tmp_keys_path, monkeypatch
    ):
        """A non-None detail on invalid_key must appear in the HTML partial."""
        self._write_providers_for_validate(tmp_providers_path)
        monkeypatch.setattr(
            AnthropicProvider,
            "validate_credentials",
            classmethod(lambda cls, k, m: ("invalid_key", "401 — Bad credentials")),
        )
        monkeypatch.setattr(OpenAIProvider,  "validate_credentials", classmethod(lambda cls, k, m: ("valid", None)))
        monkeypatch.setattr(GeminiProvider,  "validate_credentials", classmethod(lambda cls, k, m: ("valid", None)))
        resp = client.post("/api/validate-keys")
        body = resp.data.decode()
        assert "401 — Bad credentials" in body
        assert '<span class="validation-detail">' in body

    def test_detail_string_rendered_for_unreachable(
        self, client, tmp_providers_path, tmp_keys_path, monkeypatch
    ):
        """A non-None detail on unreachable must appear in the HTML partial."""
        self._write_providers_for_validate(tmp_providers_path)
        monkeypatch.setattr(AnthropicProvider, "validate_credentials", classmethod(lambda cls, k, m: ("valid", None)))
        monkeypatch.setattr(OpenAIProvider,    "validate_credentials", classmethod(lambda cls, k, m: ("valid", None)))
        monkeypatch.setattr(
            GeminiProvider,
            "validate_credentials",
            classmethod(lambda cls, k, m: ("unreachable", "Connection refused")),
        )
        resp = client.post("/api/validate-keys")
        body = resp.data.decode()
        assert "Connection refused" in body
        assert '<span class="validation-detail">' in body

    def test_detail_not_rendered_for_valid_state(
        self, client, tmp_providers_path, tmp_keys_path, monkeypatch
    ):
        """The validation-detail span must not appear when all providers are valid."""
        self._write_providers_for_validate(tmp_providers_path)
        monkeypatch.setattr(AnthropicProvider, "validate_credentials", classmethod(lambda cls, k, m: ("valid", None)))
        monkeypatch.setattr(OpenAIProvider,    "validate_credentials", classmethod(lambda cls, k, m: ("valid", None)))
        monkeypatch.setattr(GeminiProvider,    "validate_credentials", classmethod(lambda cls, k, m: ("valid", None)))
        resp = client.post("/api/validate-keys")
        body = resp.data.decode()
        assert '<span class="validation-detail">' not in body

    def test_detail_not_rendered_for_not_configured(
        self, client, tmp_providers_path, tmp_keys_path, monkeypatch
    ):
        """The validation-detail span must not appear when providers are not_configured."""
        self._write_providers_for_validate(
            tmp_providers_path, anthropic_key="", openai_key="", gemini_key=""
        )
        resp = client.post("/api/validate-keys")
        body = resp.data.decode()
        assert '<span class="validation-detail">' not in body

    # ------------------------------------------------------------------
    # Security — no key values in response
    # ------------------------------------------------------------------

    def test_key_values_not_in_response(
        self, client, tmp_providers_path, tmp_keys_path, monkeypatch
    ):
        """API key strings must never appear in the HTML partial."""
        self._write_providers_for_validate(
            tmp_providers_path,
            anthropic_key="sk-ant-supersecret",
            openai_key="sk-oai-supersecret",
            gemini_key="gm-supersecret",
        )
        monkeypatch.setattr(AnthropicProvider, "validate_credentials", classmethod(lambda cls, k, m: ("valid", None)))
        monkeypatch.setattr(OpenAIProvider,    "validate_credentials", classmethod(lambda cls, k, m: ("valid", None)))
        monkeypatch.setattr(GeminiProvider,    "validate_credentials", classmethod(lambda cls, k, m: ("valid", None)))
        resp = client.post("/api/validate-keys")
        body = resp.data.decode()
        assert "sk-ant-supersecret" not in body
        assert "sk-oai-supersecret" not in body
        assert "gm-supersecret" not in body


# ---------------------------------------------------------------------------
# Dynamic registry — new providers added to _PROVIDER_CLASS_MAP appear
# automatically without any template changes (#151)
# ---------------------------------------------------------------------------

class TestValidateKeysDynamic:
    """Verify that validate_keys() loops _PROVIDER_CLASS_MAP dynamically.

    We inject a fake provider class into the registry and confirm its
    display_name appears in the HTML partial without touching the template.
    The fake provider is removed from the registry after each test so it
    does not leak into other test cases.
    """

    def _write_providers_with_fake(self, path, fake_key="fake-api-key"):
        """Write providers.json that includes the fake 'testprovider' entry."""
        data = {
            "provider_order": ["anthropic", "openai", "gemini", "testprovider"],
            "llm": {
                "anthropic":    {"api_key": "sk-ant", "model": "claude-haiku-4-5-20251001"},
                "openai":       {"api_key": "sk-oai", "model": "gpt-4o-mini"},
                "gemini":       {"api_key": "gm-key", "model": "gemini-1.5-flash"},
                "testprovider": {"api_key": fake_key,  "model": "test-model"},
            },
            "job_sources": {},
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)

    def test_new_provider_in_registry_appears_in_output(
        self, client, tmp_providers_path, tmp_keys_path, monkeypatch
    ):
        """A provider added to _PROVIDER_CLASS_MAP shows up without template changes.

        This test also verifies the core goal of the refactor: a new provider
        with a ``validate_credentials`` classmethod is picked up automatically
        by ``validate_keys()`` with zero changes to app.py.
        """
        from providers import _PROVIDER_CLASS_MAP, LLMProvider

        # Build a minimal fake provider class — note validate_credentials is
        # defined on the class itself, no _validator_map entry needed.
        class _FakeProviderCls(LLMProvider):
            @classmethod
            def settings_schema(cls):
                return {"display_name": "TestProvider", "fields": []}

            @classmethod
            def validate_credentials(cls, api_key: str, model: str) -> tuple:
                return ("valid", None)

            def complete(self, prompt):
                raise NotImplementedError

            @property
            def input_cost_per_mtok(self):
                return 0.0

            @property
            def output_cost_per_mtok(self):
                return 0.0

        # Patch the three real providers so no network calls are made.
        monkeypatch.setattr(AnthropicProvider, "validate_credentials", classmethod(lambda cls, k, m: ("valid", None)))
        monkeypatch.setattr(OpenAIProvider,    "validate_credentials", classmethod(lambda cls, k, m: ("valid", None)))
        monkeypatch.setattr(GeminiProvider,    "validate_credentials", classmethod(lambda cls, k, m: ("valid", None)))

        # Register the fake provider in the map.
        _PROVIDER_CLASS_MAP["testprovider"] = _FakeProviderCls
        try:
            self._write_providers_with_fake(tmp_providers_path)
            resp = client.post("/api/validate-keys")
            assert resp.status_code == 200
            body = resp.data.decode()
            # The display_name from the fake provider's settings_schema() must appear.
            assert "TestProvider" in body
            # There should now be 4 rows (3 real + 1 fake).
            assert body.count("validation-row") == 4
        finally:
            _PROVIDER_CLASS_MAP.pop("testprovider", None)

    def test_new_provider_not_configured_when_key_empty(
        self, client, tmp_providers_path, tmp_keys_path, monkeypatch
    ):
        """A registered provider with no api_key shows 'not_configured'."""
        from providers import _PROVIDER_CLASS_MAP, LLMProvider

        class _FakeProviderCls(LLMProvider):
            @classmethod
            def settings_schema(cls):
                return {"display_name": "TestProvider", "fields": []}

            @classmethod
            def validate_credentials(cls, api_key: str, model: str) -> tuple:
                return ("valid", None)

            def complete(self, prompt):
                raise NotImplementedError

            @property
            def input_cost_per_mtok(self):
                return 0.0

            @property
            def output_cost_per_mtok(self):
                return 0.0

        monkeypatch.setattr(AnthropicProvider, "validate_credentials", classmethod(lambda cls, k, m: ("valid", None)))
        monkeypatch.setattr(OpenAIProvider,    "validate_credentials", classmethod(lambda cls, k, m: ("valid", None)))
        monkeypatch.setattr(GeminiProvider,    "validate_credentials", classmethod(lambda cls, k, m: ("valid", None)))

        _PROVIDER_CLASS_MAP["testprovider"] = _FakeProviderCls
        try:
            # Write the fake provider with an empty api_key.
            self._write_providers_with_fake(tmp_providers_path, fake_key="")
            resp = client.post("/api/validate-keys")
            body = resp.data.decode()
            # Fake provider row must show not_configured (muted badge).
            assert "validation-muted" in body
        finally:
            _PROVIDER_CLASS_MAP.pop("testprovider", None)

    def test_new_provider_validate_credentials_called_without_validator_map(
        self, client, tmp_providers_path, tmp_keys_path, monkeypatch
    ):
        """Adding a provider to _PROVIDER_CLASS_MAP requires no app.py changes.

        This is the definitive regression test for the refactor: a new provider
        class only needs to implement ``validate_credentials`` on itself — there
        is no ``_validator_map`` entry required in app.py.
        """
        from providers import _PROVIDER_CLASS_MAP, LLMProvider

        calls: list[tuple[str, str]] = []

        class _TrackedProvider(LLMProvider):
            @classmethod
            def settings_schema(cls):
                return {"display_name": "TrackedProvider", "fields": []}

            @classmethod
            def validate_credentials(cls, api_key: str, model: str) -> tuple:
                calls.append((api_key, model))
                return ("valid", None)

            def complete(self, prompt):
                raise NotImplementedError

            @property
            def input_cost_per_mtok(self):
                return 0.0

            @property
            def output_cost_per_mtok(self):
                return 0.0

        monkeypatch.setattr(AnthropicProvider, "validate_credentials", classmethod(lambda cls, k, m: ("valid", None)))
        monkeypatch.setattr(OpenAIProvider,    "validate_credentials", classmethod(lambda cls, k, m: ("valid", None)))
        monkeypatch.setattr(GeminiProvider,    "validate_credentials", classmethod(lambda cls, k, m: ("valid", None)))

        _PROVIDER_CLASS_MAP["tracked"] = _TrackedProvider
        try:
            data = {
                "provider_order": ["anthropic", "openai", "gemini", "tracked"],
                "llm": {
                    "anthropic": {"api_key": "sk-ant", "model": "claude-haiku-4-5-20251001"},
                    "openai":    {"api_key": "sk-oai", "model": "gpt-4o-mini"},
                    "gemini":    {"api_key": "gm-key", "model": "gemini-1.5-flash"},
                    "tracked":   {"api_key": "tr-key", "model": "tracked-model"},
                },
                "job_sources": {},
            }
            with open(tmp_providers_path, "w", encoding="utf-8") as f:
                json.dump(data, f)

            resp = client.post("/api/validate-keys")
            assert resp.status_code == 200
            # validate_credentials on the tracked provider must have been called.
            assert calls == [("tr-key", "tracked-model")]
            body = resp.data.decode()
            assert "TrackedProvider" in body
        finally:
            _PROVIDER_CLASS_MAP.pop("tracked", None)


# ---------------------------------------------------------------------------
# _validate_with_timeout — timeout path
# ---------------------------------------------------------------------------

class TestValidateWithTimeout:
    """Unit tests for the _validate_with_timeout() helper.

    Verifies that a validator call that blocks longer than the configured
    timeout is interrupted and returns 'unreachable'.
    """

    def test_returns_validator_result_when_fast(self):
        """A fast validator's (state, detail) tuple passes through unchanged."""
        result = app_module._validate_with_timeout(
            lambda k, m: ("valid", None), "key", "model"
        )
        assert result == ("valid", None)

    def test_returns_unreachable_on_timeout(self, monkeypatch):
        """A validator that never returns is treated as unreachable after timeout."""
        import time

        # Reduce timeout to 0.05 s so the test completes quickly.
        monkeypatch.setattr(app_module, "_VALIDATE_TIMEOUT_SECONDS", 0.05)

        def _hanging_validator(k, m):
            time.sleep(10)  # much longer than the patched timeout
            return ("valid", None)

        state, detail = app_module._validate_with_timeout(_hanging_validator, "key", "model")
        assert state == "unreachable"
        assert "0.05" in detail

    def test_returns_unreachable_when_validator_raises(self):
        """An exception inside the validator is caught and mapped to unreachable."""
        def _exploding(k, m):
            raise RuntimeError("boom")

        state, detail = app_module._validate_with_timeout(_exploding, "key", "model")
        assert state == "unreachable"
        assert "boom" in detail

    def test_validator_state_tuples_pass_through(self):
        """All non-timeout state tuples are forwarded verbatim."""
        for state in ("valid", "invalid_key", "unknown_model", "unreachable"):
            result = app_module._validate_with_timeout(
                lambda k, m, s=state: (s, None), "key", "model"
            )
            assert result == (state, None)


# ---------------------------------------------------------------------------
# Timeout integration — endpoint maps timed-out providers to unreachable
# ---------------------------------------------------------------------------

class TestValidateKeysTimeout:
    """Integration test: a provider that times out shows unreachable in the page."""

    def _write_providers(self, path):
        data = {
            "provider_order": ["anthropic", "openai", "gemini"],
            "llm": {
                "anthropic": {"api_key": "sk-ant", "model": "claude-haiku-4-5-20251001"},
                "openai":    {"api_key": "sk-oai", "model": "gpt-4o-mini"},
                "gemini":    {"api_key": "gm-key", "model": "gemini-1.5-flash"},
            },
            "job_sources": {},
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)

    def test_timed_out_provider_shows_unreachable_others_unaffected(
        self, client, tmp_providers_path, tmp_keys_path, monkeypatch
    ):
        """A validator that blocks is mapped to unreachable; others still validate."""
        import time

        # Reduce timeout so the test completes quickly.
        monkeypatch.setattr(app_module, "_VALIDATE_TIMEOUT_SECONDS", 0.05)

        self._write_providers(tmp_providers_path)

        def _slow_validator(cls, k, m):
            time.sleep(10)  # exceeds patched timeout
            return ("valid", None)

        monkeypatch.setattr(AnthropicProvider, "validate_credentials", classmethod(_slow_validator))
        monkeypatch.setattr(OpenAIProvider,    "validate_credentials", classmethod(lambda cls, k, m: ("valid", None)))
        monkeypatch.setattr(GeminiProvider,    "validate_credentials", classmethod(lambda cls, k, m: ("valid", None)))

        resp = client.post("/api/validate-keys")
        assert resp.status_code == 200
        body = resp.data.decode()
        # Timed-out provider must show Unreachable.
        assert "Unreachable" in body
        # Other two providers must still show valid.
        assert body.count("validation-valid") == 2


# ---------------------------------------------------------------------------
# validate_credentials classmethod unit tests — error type classification
# ---------------------------------------------------------------------------

class TestValidateHelpers:
    """Unit tests for the per-provider ``validate_credentials`` classmethods.

    All SDK calls are monkeypatched so no real network traffic is made.
    We verify that the right exception types map to the right state strings.

    Because anthropic.AuthenticationError / openai.AuthenticationError etc.
    require an httpx.Response to construct, we patch the exception *classes*
    themselves on the SDK modules to lightweight stand-ins. This keeps the
    isinstance checks in the validators working correctly.
    """

    # ------------------------------------------------------------------
    # Anthropic
    # ------------------------------------------------------------------

    def test_anthropic_valid(self, monkeypatch):
        import anthropic as _anthropic

        class _FakeMessages:
            def create(self, **kwargs):
                return object()

        class _FakeClient:
            messages = _FakeMessages()

        monkeypatch.setattr(_anthropic, "Anthropic", lambda api_key: _FakeClient())
        state, detail = AnthropicProvider.validate_credentials("sk-key", "claude-haiku-4-5-20251001")
        assert state == "valid"
        assert detail is None

    def test_anthropic_invalid_key(self, monkeypatch):
        """AuthenticationError maps to 'invalid_key' with a non-None detail string."""
        import anthropic as _anthropic

        # Patch AuthenticationError to a plain Exception subclass so we can
        # raise it without needing a real httpx.Response.
        class _FakeAuthError(Exception):
            pass

        class _FakeMessages:
            def create(self, **kwargs):
                raise _FakeAuthError("401 — Invalid API key")

        class _FakeClient:
            messages = _FakeMessages()

        monkeypatch.setattr(_anthropic, "Anthropic", lambda api_key: _FakeClient())
        monkeypatch.setattr(_anthropic, "AuthenticationError", _FakeAuthError)
        monkeypatch.setattr(_anthropic, "PermissionDeniedError", type("_NeverRaised", (Exception,), {}))
        monkeypatch.setattr(_anthropic, "NotFoundError", type("_NeverRaised2", (Exception,), {}))
        state, detail = AnthropicProvider.validate_credentials("sk-bad", "claude-haiku-4-5-20251001")
        assert state == "invalid_key"
        assert detail is not None
        assert len(detail) > 0

    def test_anthropic_permission_denied_maps_to_invalid_key(self, monkeypatch):
        """PermissionDeniedError (403) also maps to 'invalid_key' with detail."""
        import anthropic as _anthropic

        class _FakePermError(Exception):
            pass

        class _FakeMessages:
            def create(self, **kwargs):
                raise _FakePermError("403 — Permission denied")

        class _FakeClient:
            messages = _FakeMessages()

        monkeypatch.setattr(_anthropic, "Anthropic", lambda api_key: _FakeClient())
        monkeypatch.setattr(_anthropic, "AuthenticationError", type("_NeverRaised", (Exception,), {}))
        monkeypatch.setattr(_anthropic, "PermissionDeniedError", _FakePermError)
        monkeypatch.setattr(_anthropic, "NotFoundError", type("_NeverRaised2", (Exception,), {}))
        state, detail = AnthropicProvider.validate_credentials("sk-bad", "claude-haiku-4-5-20251001")
        assert state == "invalid_key"
        assert detail is not None

    def test_anthropic_unknown_model(self, monkeypatch):
        import anthropic as _anthropic

        class _FakeNotFoundError(Exception):
            pass

        class _FakeMessages:
            def create(self, **kwargs):
                raise _FakeNotFoundError("404 — Model not found")

        class _FakeClient:
            messages = _FakeMessages()

        monkeypatch.setattr(_anthropic, "Anthropic", lambda api_key: _FakeClient())
        monkeypatch.setattr(_anthropic, "AuthenticationError", type("_NeverRaised", (Exception,), {}))
        monkeypatch.setattr(_anthropic, "PermissionDeniedError", type("_NeverRaised2", (Exception,), {}))
        monkeypatch.setattr(_anthropic, "NotFoundError", _FakeNotFoundError)
        state, detail = AnthropicProvider.validate_credentials("sk-key", "claude-unknown-xyz")
        assert state == "unknown_model"
        assert detail is not None

    def test_anthropic_unreachable(self, monkeypatch):
        import anthropic as _anthropic

        class _FakeMessages:
            def create(self, **kwargs):
                raise ConnectionError("timeout")

        class _FakeClient:
            messages = _FakeMessages()

        monkeypatch.setattr(_anthropic, "Anthropic", lambda api_key: _FakeClient())
        # Real SDK error classes left in place — ConnectionError won't match them.
        state, detail = AnthropicProvider.validate_credentials("sk-key", "claude-haiku-4-5-20251001")
        assert state == "unreachable"
        assert detail is not None
        assert "timeout" in detail

    def test_anthropic_detail_never_contains_api_key(self, monkeypatch):
        """The api_key value must be redacted from any detail string."""
        import anthropic as _anthropic

        secret_key = "sk-ant-supersecret-12345"

        class _FakeAuthError(Exception):
            pass

        class _FakeMessages:
            def create(self, **kwargs):
                # Simulate an exception message that inadvertently contains the key.
                raise _FakeAuthError(f"Request failed for key {secret_key}")

        class _FakeClient:
            messages = _FakeMessages()

        monkeypatch.setattr(_anthropic, "Anthropic", lambda api_key: _FakeClient())
        monkeypatch.setattr(_anthropic, "AuthenticationError", _FakeAuthError)
        monkeypatch.setattr(_anthropic, "PermissionDeniedError", type("_NeverRaised", (Exception,), {}))
        monkeypatch.setattr(_anthropic, "NotFoundError", type("_NeverRaised2", (Exception,), {}))
        state, detail = AnthropicProvider.validate_credentials(secret_key, "claude-haiku-4-5-20251001")
        assert state == "invalid_key"
        assert secret_key not in (detail or "")
        assert "[REDACTED]" in (detail or "")

    def test_anthropic_detail_redacts_key_after_200_chars(self, monkeypatch):
        """Key appearing after char 200 must still be fully redacted after truncation."""
        import anthropic as _anthropic

        secret_key = "sk-ant-supersecret"
        prefix = "A" * 185  # push key past the 200-char mark

        class _FakeAuthError(Exception):
            pass

        class _FakeMessages:
            def create(self, **kwargs):
                raise _FakeAuthError(f"{prefix} key={secret_key}")

        class _FakeClient:
            messages = _FakeMessages()

        monkeypatch.setattr(_anthropic, "Anthropic", lambda api_key: _FakeClient())
        monkeypatch.setattr(_anthropic, "AuthenticationError", _FakeAuthError)
        monkeypatch.setattr(_anthropic, "PermissionDeniedError", type("_NeverRaised", (Exception,), {}))
        monkeypatch.setattr(_anthropic, "NotFoundError", type("_NeverRaised2", (Exception,), {}))
        state, detail = AnthropicProvider.validate_credentials(secret_key, "claude-haiku-4-5-20251001")
        assert state == "invalid_key"
        assert secret_key not in (detail or "")
        assert "[REDACTED]" in (detail or "")
        assert len(detail or "") <= 200

    # ------------------------------------------------------------------
    # OpenAI
    # ------------------------------------------------------------------

    def test_openai_valid(self, monkeypatch):
        import openai as _openai

        class _FakeCompletions:
            def create(self, **kwargs):
                return object()

        class _FakeChat:
            completions = _FakeCompletions()

        class _FakeClient:
            chat = _FakeChat()

        monkeypatch.setattr(_openai, "OpenAI", lambda api_key: _FakeClient())
        state, detail = OpenAIProvider.validate_credentials("sk-oai", "gpt-4o-mini")
        assert state == "valid"
        assert detail is None

    def test_openai_invalid_key(self, monkeypatch):
        import openai as _openai

        class _FakeAuthError(Exception):
            pass

        class _FakeCompletions:
            def create(self, **kwargs):
                raise _FakeAuthError("401 — Incorrect API key")

        class _FakeChat:
            completions = _FakeCompletions()

        class _FakeClient:
            chat = _FakeChat()

        monkeypatch.setattr(_openai, "OpenAI", lambda api_key: _FakeClient())
        monkeypatch.setattr(_openai, "AuthenticationError", _FakeAuthError)
        monkeypatch.setattr(_openai, "PermissionDeniedError", type("_NeverRaised", (Exception,), {}))
        monkeypatch.setattr(_openai, "NotFoundError", type("_NeverRaised2", (Exception,), {}))
        state, detail = OpenAIProvider.validate_credentials("sk-bad", "gpt-4o-mini")
        assert state == "invalid_key"
        assert detail is not None
        assert len(detail) > 0

    def test_openai_unknown_model(self, monkeypatch):
        import openai as _openai

        class _FakeNotFoundError(Exception):
            pass

        class _FakeCompletions:
            def create(self, **kwargs):
                raise _FakeNotFoundError("404 — Model not found")

        class _FakeChat:
            completions = _FakeCompletions()

        class _FakeClient:
            chat = _FakeChat()

        monkeypatch.setattr(_openai, "OpenAI", lambda api_key: _FakeClient())
        monkeypatch.setattr(_openai, "AuthenticationError", type("_NeverRaised", (Exception,), {}))
        monkeypatch.setattr(_openai, "PermissionDeniedError", type("_NeverRaised2", (Exception,), {}))
        monkeypatch.setattr(_openai, "NotFoundError", _FakeNotFoundError)
        state, detail = OpenAIProvider.validate_credentials("sk-oai", "gpt-unknown-xyz")
        assert state == "unknown_model"
        assert detail is not None

    def test_openai_unreachable(self, monkeypatch):
        import openai as _openai

        class _FakeCompletions:
            def create(self, **kwargs):
                raise ConnectionError("timeout")

        class _FakeChat:
            completions = _FakeCompletions()

        class _FakeClient:
            chat = _FakeChat()

        monkeypatch.setattr(_openai, "OpenAI", lambda api_key: _FakeClient())
        state, detail = OpenAIProvider.validate_credentials("sk-oai", "gpt-4o-mini")
        assert state == "unreachable"
        assert detail is not None
        assert "timeout" in detail

    def test_openai_detail_never_contains_api_key(self, monkeypatch):
        """The api_key value must be redacted from any detail string."""
        import openai as _openai

        secret_key = "sk-oai-supersecret-12345"

        class _FakeAuthError(Exception):
            pass

        class _FakeCompletions:
            def create(self, **kwargs):
                raise _FakeAuthError(f"Auth failed for key {secret_key}")

        class _FakeChat:
            completions = _FakeCompletions()

        class _FakeClient:
            chat = _FakeChat()

        monkeypatch.setattr(_openai, "OpenAI", lambda api_key: _FakeClient())
        monkeypatch.setattr(_openai, "AuthenticationError", _FakeAuthError)
        monkeypatch.setattr(_openai, "PermissionDeniedError", type("_NeverRaised", (Exception,), {}))
        monkeypatch.setattr(_openai, "NotFoundError", type("_NeverRaised2", (Exception,), {}))
        state, detail = OpenAIProvider.validate_credentials(secret_key, "gpt-4o-mini")
        assert state == "invalid_key"
        assert secret_key not in (detail or "")
        assert "[REDACTED]" in (detail or "")

    def test_openai_detail_redacts_key_after_200_chars(self, monkeypatch):
        """Key appearing after char 200 must still be fully redacted after truncation."""
        import openai as _openai

        secret_key = "sk-oai-supersecret"
        prefix = "A" * 185  # push key past the 200-char mark

        class _FakeAuthError(Exception):
            pass

        class _FakeCompletions:
            def create(self, **kwargs):
                raise _FakeAuthError(f"{prefix} key={secret_key}")

        class _FakeChat:
            completions = _FakeCompletions()

        class _FakeClient:
            chat = _FakeChat()

        monkeypatch.setattr(_openai, "OpenAI", lambda api_key: _FakeClient())
        monkeypatch.setattr(_openai, "AuthenticationError", _FakeAuthError)
        monkeypatch.setattr(_openai, "PermissionDeniedError", type("_NeverRaised", (Exception,), {}))
        monkeypatch.setattr(_openai, "NotFoundError", type("_NeverRaised2", (Exception,), {}))
        state, detail = OpenAIProvider.validate_credentials(secret_key, "gpt-4o-mini")
        assert state == "invalid_key"
        assert secret_key not in (detail or "")
        assert "[REDACTED]" in (detail or "")
        assert len(detail or "") <= 200

    # ------------------------------------------------------------------
    # Gemini
    # ------------------------------------------------------------------

    def test_gemini_valid(self, monkeypatch):
        from google import genai as _genai

        class _FakeModels:
            def generate_content(self, model, contents):
                return object()

        class _FakeClient:
            models = _FakeModels()

        monkeypatch.setattr(_genai, "Client", lambda api_key: _FakeClient())
        state, detail = GeminiProvider.validate_credentials("gm-key", "gemini-1.5-flash")
        assert state == "valid"
        assert detail is None

    def test_gemini_invalid_key(self, monkeypatch):
        from google import genai as _genai

        class _FakeModels:
            def generate_content(self, model, contents):
                raise Exception("API_KEY_INVALID — invalid api key provided")

        class _FakeClient:
            models = _FakeModels()

        monkeypatch.setattr(_genai, "Client", lambda api_key: _FakeClient())
        state, detail = GeminiProvider.validate_credentials("gm-bad", "gemini-1.5-flash")
        assert state == "invalid_key"
        assert detail is not None
        assert len(detail) > 0

    def test_gemini_unreachable(self, monkeypatch):
        from google import genai as _genai

        class _FakeModels:
            def generate_content(self, model, contents):
                raise ConnectionError("network timeout")

        class _FakeClient:
            models = _FakeModels()

        monkeypatch.setattr(_genai, "Client", lambda api_key: _FakeClient())
        state, detail = GeminiProvider.validate_credentials("gm-key", "gemini-1.5-flash")
        assert state == "unreachable"
        assert detail is not None
        assert "timeout" in detail

    def test_gemini_not_found(self, monkeypatch):
        from google import genai as _genai

        class _FakeModels:
            def generate_content(self, model, contents):
                raise Exception("404 models/gemini-bogus is not found")

        class _FakeClient:
            models = _FakeModels()

        monkeypatch.setattr(_genai, "Client", lambda api_key: _FakeClient())
        state, detail = GeminiProvider.validate_credentials("gm-key", "gemini-bogus")
        assert state == "unknown_model"
        assert detail is not None

    def test_gemini_detail_never_contains_api_key(self, monkeypatch):
        """The api_key value must be redacted from any Gemini detail string."""
        from google import genai as _genai

        secret_key = "gm-supersecret-12345"

        class _FakeModels:
            def generate_content(self, model, contents):
                raise Exception(f"unauthenticated: key {secret_key} is invalid")

        class _FakeClient:
            models = _FakeModels()

        monkeypatch.setattr(_genai, "Client", lambda api_key: _FakeClient())
        state, detail = GeminiProvider.validate_credentials(secret_key, "gemini-1.5-flash")
        assert state == "invalid_key"
        assert secret_key not in (detail or "")
        assert "[REDACTED]" in (detail or "")

    def test_gemini_detail_redacts_key_after_200_chars(self, monkeypatch):
        """Key appearing after char 200 must still be fully redacted after truncation."""
        from google import genai as _genai

        secret_key = "gm-supersecret"
        prefix = "A" * 185  # push key past the 200-char mark

        class _FakeModels:
            def generate_content(self, model, contents):
                raise Exception(f"{prefix} key={secret_key}")

        class _FakeClient:
            models = _FakeModels()

        monkeypatch.setattr(_genai, "Client", lambda api_key: _FakeClient())
        state, detail = GeminiProvider.validate_credentials(secret_key, "gemini-1.5-flash")
        assert secret_key not in (detail or "")
        assert "[REDACTED]" in (detail or "")
        assert len(detail or "") <= 200
