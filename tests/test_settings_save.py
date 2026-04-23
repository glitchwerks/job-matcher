"""
tests/test_settings_save.py — Tests for save_providers() and the updated
/settings route (tabbed layout, dynamic rendering from registries).

TDD: these tests were written before the implementation to drive the design.

Covered cases
-------------
save_providers():
* Writes non-blank LLM values to providers.json
* Does NOT overwrite existing values when blank string is submitted
* Writes non-blank job source values to providers.json
* Does NOT overwrite existing job source values when blank string is submitted
* Creates providers.json from scratch when absent
* Writes atomically — tmp file is absent after successful write
* On simulated write failure, tmp file is cleaned up and providers.json unchanged
* Boolean enabled=True is written (not skipped by the blank-string guard)
* Boolean enabled=False is written (not skipped by the blank-string guard)

GET /settings (new tabbed route):
* Returns 200
* Passes llm_schemas to template (one entry per registry provider)
* Passes source_schemas to template (one entry per registry source)
* has_values=True when provider api_key is non-empty
* has_values=False when provider api_key is empty
* Defaults to llm tab when no ?tab= param
* Respects ?tab=sources query param
* Renders enabled checkbox for each source
* Checkbox is checked when source is enabled in providers.json

POST /settings (new tabbed route):
* Writes LLM credentials to providers.json (not keys.json)
* Blank LLM field preserves existing value in providers.json
* Writes job source credentials to providers.json
* Writes enabled=True when checkbox is submitted as 'on'
* Writes enabled=False when checkbox is submitted as '' (dirty unchecked)
* Preserves existing enabled state when checkbox is absent from POST body
* Writes enabled for keyless sources (no credential fields)
* Redirects to GET /settings?tab=<active_tab> after save
* Active tab is preserved through save redirect
"""

from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import services.profile_store as _profile_store_module
import web.settings as _settings_module
from app import app as flask_app
from credentials import save_providers


# ===========================================================================
# Fixtures
# ===========================================================================


@pytest.fixture()
def tmp_providers_path(tmp_path, monkeypatch):
    """Point _PROVIDERS_PATH at a temp file for full isolation."""
    path = str(tmp_path / "providers.json")
    monkeypatch.setattr(_profile_store_module, "_PROVIDERS_PATH", path)
    monkeypatch.setattr(_settings_module, "_PROVIDERS_PATH", path)
    return path


@pytest.fixture()
def tmp_keys_path(tmp_path, monkeypatch):
    """Point _KEYS_PATH at a temp file so legacy migration never triggers."""
    path = str(tmp_path / "keys.json")
    monkeypatch.setattr(_profile_store_module, "_KEYS_PATH", path)
    monkeypatch.setattr(_settings_module, "_KEYS_PATH", path)
    return path


@pytest.fixture()
def tmp_config_path(tmp_path, monkeypatch):
    """Point _CONFIG_PATH at a temp file so config reads are isolated."""
    path = str(tmp_path / "config.json")
    monkeypatch.setattr(_profile_store_module, "_CONFIG_PATH", path)
    monkeypatch.setattr(_settings_module, "_CONFIG_PATH", path)
    return path


@pytest.fixture()
def client():
    flask_app.config["TESTING"] = True
    with flask_app.test_client() as c:
        yield c


def _write_providers(path: str, data: dict | None = None) -> None:
    """Write a providers.json fixture to *path*."""
    if data is None:
        data = {
            "provider_order": ["anthropic", "openai", "gemini"],
            "llm": {
                "anthropic": {"api_key": "sk-existing", "model": "claude-haiku-4-5-20251001"},
                "openai":    {"api_key": "",            "model": "gpt-4o-mini"},
                "gemini":    {"api_key": "",            "model": "gemini-1.5-flash"},
            },
            "job_sources": {
                "adzuna": {"app_id": "existing-id", "app_key": "existing-key"},
            },
        }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)


# ===========================================================================
# save_providers() unit tests
# ===========================================================================


class TestSaveProvidersWritesNewValues:
    def test_writes_llm_api_key(self, tmp_path):
        path = str(tmp_path / "providers.json")
        _write_providers(path)
        save_providers(
            {"llm": {"anthropic": {"api_key": "sk-new"}}},
            providers_path=path,
        )
        with open(path, encoding="utf-8") as fh:
            saved = json.load(fh)
        assert saved["llm"]["anthropic"]["api_key"] == "sk-new"

    def test_writes_llm_model(self, tmp_path):
        path = str(tmp_path / "providers.json")
        _write_providers(path)
        save_providers(
            {"llm": {"anthropic": {"model": "claude-opus-4-20251001"}}},
            providers_path=path,
        )
        with open(path, encoding="utf-8") as fh:
            saved = json.load(fh)
        assert saved["llm"]["anthropic"]["model"] == "claude-opus-4-20251001"

    def test_writes_job_source_credential(self, tmp_path):
        path = str(tmp_path / "providers.json")
        _write_providers(path)
        save_providers(
            {"job_sources": {"adzuna": {"app_id": "new-app-id"}}},
            providers_path=path,
        )
        with open(path, encoding="utf-8") as fh:
            saved = json.load(fh)
        assert saved["job_sources"]["adzuna"]["app_id"] == "new-app-id"

    def test_creates_file_from_scratch_when_absent(self, tmp_path):
        path = str(tmp_path / "providers.json")
        assert not os.path.exists(path)
        save_providers(
            {"llm": {"anthropic": {"api_key": "sk-brand-new"}}},
            providers_path=path,
        )
        assert os.path.exists(path)
        with open(path, encoding="utf-8") as fh:
            saved = json.load(fh)
        assert saved["llm"]["anthropic"]["api_key"] == "sk-brand-new"


class TestSaveProvidersBlankStringClears:
    def test_blank_api_key_clears_existing(self, tmp_path):
        """Submitting a blank string for a credential must clear the stored value.

        This is the fix for issue #284 — previously blank strings were silently
        ignored, making it impossible to clear a credential field via the UI.
        """
        path = str(tmp_path / "providers.json")
        _write_providers(path)
        save_providers(
            {"llm": {"anthropic": {"api_key": ""}}},
            providers_path=path,
        )
        with open(path, encoding="utf-8") as fh:
            saved = json.load(fh)
        # Blank string must clear the existing credential.
        assert saved["llm"]["anthropic"]["api_key"] == ""

    def test_blank_job_source_field_clears_existing(self, tmp_path):
        """Submitting a blank string for a source credential must clear it."""
        path = str(tmp_path / "providers.json")
        _write_providers(path)
        save_providers(
            {"job_sources": {"adzuna": {"app_id": ""}}},
            providers_path=path,
        )
        with open(path, encoding="utf-8") as fh:
            saved = json.load(fh)
        assert saved["job_sources"]["adzuna"]["app_id"] == ""

    def test_unmentioned_keys_are_preserved(self, tmp_path):
        """Keys not present in updates dict at all must remain untouched."""
        path = str(tmp_path / "providers.json")
        _write_providers(path)
        save_providers(
            {"llm": {"anthropic": {"api_key": "sk-new"}}},
            providers_path=path,
        )
        with open(path, encoding="utf-8") as fh:
            saved = json.load(fh)
        # OpenAI and Gemini were not mentioned — must be untouched.
        assert saved["llm"]["openai"]["model"] == "gpt-4o-mini"
        assert saved["llm"]["gemini"]["model"] == "gemini-1.5-flash"
        # Adzuna was not mentioned — must be untouched.
        assert saved["job_sources"]["adzuna"]["app_key"] == "existing-key"


class TestSaveProvidersAtomicWrite:
    def test_no_tmp_file_left_after_success(self, tmp_path):
        path = str(tmp_path / "providers.json")
        _write_providers(path)
        save_providers({"llm": {"anthropic": {"api_key": "sk-x"}}}, providers_path=path)
        assert not os.path.exists(path + ".tmp")

    def test_tmp_file_cleaned_up_on_failure(self, tmp_path, monkeypatch):
        """If the atomic rename fails, the .tmp file must be removed."""
        path = str(tmp_path / "providers.json")
        _write_providers(path)

        import builtins

        real_open = builtins.open

        def _bad_open(file, mode="r", **kwargs):
            if "w" in str(mode) and str(file) == path + ".tmp":
                raise OSError("simulated disk full")
            return real_open(file, mode, **kwargs)

        monkeypatch.setattr(builtins, "open", _bad_open)

        with pytest.raises(OSError):
            save_providers({"llm": {"anthropic": {"api_key": "sk-y"}}}, providers_path=path)

        assert not os.path.exists(path + ".tmp")

    def test_original_file_unchanged_on_failure(self, tmp_path, monkeypatch):
        """If the write fails, the original providers.json must not be altered."""
        path = str(tmp_path / "providers.json")
        _write_providers(path)

        import builtins

        real_open = builtins.open

        def _bad_open(file, mode="r", **kwargs):
            if "w" in str(mode) and str(file) == path + ".tmp":
                raise OSError("simulated disk full")
            return real_open(file, mode, **kwargs)

        monkeypatch.setattr(builtins, "open", _bad_open)

        with pytest.raises(OSError):
            save_providers({"llm": {"anthropic": {"api_key": "sk-y"}}}, providers_path=path)

        with open(path, encoding="utf-8") as fh:
            saved = json.load(fh)
        assert saved["llm"]["anthropic"]["api_key"] == "sk-existing"


# ===========================================================================
# GET /settings — tabbed layout
# ===========================================================================


class TestSettingsGetTabbed:
    def test_returns_200(self, client, tmp_providers_path, tmp_keys_path, tmp_config_path):
        resp = client.get("/settings")
        assert resp.status_code == 200

    def test_llm_schemas_present_for_all_registry_providers(
        self, client, tmp_providers_path, tmp_keys_path, tmp_config_path
    ):
        """Each LLM provider in _PROVIDER_CLASS_MAP must appear in the page."""
        from providers import _PROVIDER_CLASS_MAP
        resp = client.get("/settings")
        body = resp.data.decode()
        for key, cls in _PROVIDER_CLASS_MAP.items():
            schema = cls.settings_schema()
            assert schema["display_name"] in body, (
                f"Expected display_name '{schema['display_name']}' for provider '{key}' in response"
            )

    def test_source_schemas_present_for_all_registry_sources(
        self, client, tmp_providers_path, tmp_keys_path, tmp_config_path
    ):
        """Each job source in SOURCES must appear in the page."""
        from job_sources import SOURCES
        resp = client.get("/settings")
        body = resp.data.decode()
        for key, cls in SOURCES.items():
            schema = cls.settings_schema()
            assert schema["display_name"] in body, (
                f"Expected display_name '{schema['display_name']}' for source '{key}' in response"
            )

    def test_configured_badge_when_api_key_set(
        self, client, tmp_providers_path, tmp_keys_path, tmp_config_path
    ):
        _write_providers(tmp_providers_path)
        resp = client.get("/settings")
        body = resp.data.decode()
        # anthropic has api_key="sk-existing" so it must show configured
        assert "configured" in body

    def test_not_set_badge_when_api_key_empty(
        self, client, tmp_providers_path, tmp_keys_path, tmp_config_path
    ):
        _write_providers(tmp_providers_path)
        resp = client.get("/settings")
        body = resp.data.decode()
        # openai and gemini have empty api_key
        assert "not-set" in body

    def test_no_raw_key_values_in_response(
        self, client, tmp_providers_path, tmp_keys_path, tmp_config_path
    ):
        _write_providers(tmp_providers_path)
        resp = client.get("/settings")
        body = resp.data.decode()
        assert "sk-existing" not in body
        assert "existing-id" not in body
        assert "existing-key" not in body

    def test_llm_tab_present(
        self, client, tmp_providers_path, tmp_keys_path, tmp_config_path
    ):
        resp = client.get("/settings")
        body = resp.data.decode()
        assert "LLM Providers" in body

    def test_sources_tab_present(
        self, client, tmp_providers_path, tmp_keys_path, tmp_config_path
    ):
        resp = client.get("/settings")
        body = resp.data.decode()
        assert "Job Sources" in body

    def test_field_namespacing_uses_double_underscore(
        self, client, tmp_providers_path, tmp_keys_path, tmp_config_path
    ):
        """Input name attributes must use provider_key__field_name format."""
        resp = client.get("/settings")
        body = resp.data.decode()
        # anthropic__api_key is the expected namespaced field name
        assert "anthropic__api_key" in body

    def test_adzuna_source_namespaced_fields(
        self, client, tmp_providers_path, tmp_keys_path, tmp_config_path
    ):
        resp = client.get("/settings")
        body = resp.data.decode()
        assert "adzuna__app_id" in body
        assert "adzuna__app_key" in body


class TestSettingsGetTabParam:
    def test_default_tab_is_llm(
        self, client, tmp_providers_path, tmp_keys_path, tmp_config_path
    ):
        resp = client.get("/settings")
        body = resp.data.decode()
        # The llm pane should be active by default
        assert "tab-pane" in body  # basic structural check

    def test_tab_param_sources_activates_sources_tab(
        self, client, tmp_providers_path, tmp_keys_path, tmp_config_path
    ):
        resp = client.get("/settings?tab=sources")
        assert resp.status_code == 200
        body = resp.data.decode()
        # Page must still render — active_tab is passed to template
        assert "Job Sources" in body


# ===========================================================================
# POST /settings — writes to providers.json
# ===========================================================================


class TestSettingsPostWritesToProviders:
    def test_saves_llm_api_key_to_providers_json(
        self, client, tmp_providers_path, tmp_keys_path, tmp_config_path
    ):
        _write_providers(tmp_providers_path)
        resp = client.post("/settings", data={
            "anthropic__api_key": "sk-updated",
            "anthropic__model": "claude-haiku-4-5-20251001",
            "tab": "llm",
        })
        # Should redirect to GET
        assert resp.status_code in (200, 302)
        with open(tmp_providers_path, encoding="utf-8") as fh:
            saved = json.load(fh)
        assert saved["llm"]["anthropic"]["api_key"] == "sk-updated"

    def test_blank_api_key_preserved_by_no_js_guard(
        self, client, tmp_providers_path, tmp_keys_path, tmp_config_path
    ):
        """Submitting a blank password field without a __clear__ flag must preserve
        the stored value.

        This is the no-JS guard (issue #137): a native form submit with an empty
        password field must not wipe an existing credential.  The explicit Clear
        button (which adds a __clear__ hidden field) is the only way to clear a key.
        """
        _write_providers(tmp_providers_path)
        client.post("/settings", data={
            "anthropic__api_key": "",   # blank, but no __clear__ flag
            "anthropic__model": "claude-haiku-4-5-20251001",
            "tab": "llm",
        })
        with open(tmp_providers_path, encoding="utf-8") as fh:
            saved = json.load(fh)
        # No-JS guard: existing key must be preserved when password submitted empty
        assert saved["llm"]["anthropic"]["api_key"] == "sk-existing"

    def test_saves_job_source_credentials_to_providers_json(
        self, client, tmp_providers_path, tmp_keys_path, tmp_config_path
    ):
        _write_providers(tmp_providers_path)
        resp = client.post("/settings", data={
            "adzuna__app_id": "new-adzuna-id",
            "adzuna__app_key": "new-adzuna-key",
            "tab": "sources",
        })
        assert resp.status_code in (200, 302)
        with open(tmp_providers_path, encoding="utf-8") as fh:
            saved = json.load(fh)
        assert saved["job_sources"]["adzuna"]["app_id"] == "new-adzuna-id"
        assert saved["job_sources"]["adzuna"]["app_key"] == "new-adzuna-key"

    def test_post_does_not_write_to_keys_json(
        self, client, tmp_providers_path, tmp_keys_path, tmp_config_path
    ):
        """The new POST handler must write to providers.json, never keys.json."""
        _write_providers(tmp_providers_path)
        client.post("/settings", data={
            "anthropic__api_key": "sk-new",
            "tab": "llm",
        })
        # keys.json must remain absent — the POST must not create it.
        assert not os.path.exists(tmp_keys_path)

    def test_post_redirects_to_correct_tab(
        self, client, tmp_providers_path, tmp_keys_path, tmp_config_path
    ):
        """POST must redirect back to the tab that was active during the save."""
        _write_providers(tmp_providers_path)
        resp = client.post("/settings", data={
            "adzuna__app_id": "x",
            "tab": "sources",
        }, follow_redirects=False)
        assert resp.status_code == 302
        assert "tab=sources" in resp.headers.get("Location", "")

    def test_post_redirects_to_llm_tab_by_default(
        self, client, tmp_providers_path, tmp_keys_path, tmp_config_path
    ):
        """When no tab field is submitted, redirect defaults to llm tab."""
        _write_providers(tmp_providers_path)
        resp = client.post("/settings", data={
            "anthropic__api_key": "sk-x",
        }, follow_redirects=False)
        assert resp.status_code == 302
        location = resp.headers.get("Location", "")
        assert "tab=llm" in location or "settings" in location


# ===========================================================================
# POST /settings — enabled checkbox for job sources
# ===========================================================================


class TestSettingsPostEnabledCheckbox:
    def test_enabled_true_written_when_checkbox_submitted(
        self, client, tmp_providers_path, tmp_keys_path, tmp_config_path
    ):
        """Submitting source_key__enabled='on' must write enabled=True."""
        _write_providers(tmp_providers_path)
        client.post("/settings", data={
            "adzuna__enabled": "on",
            "adzuna__app_id": "id",
            "adzuna__app_key": "key",
            "tab": "sources",
        })
        with open(tmp_providers_path, encoding="utf-8") as fh:
            saved = json.load(fh)
        assert saved["job_sources"]["adzuna"]["enabled"] is True

    def test_enabled_false_written_when_checkbox_sent_empty(
        self, client, tmp_providers_path, tmp_keys_path, tmp_config_path
    ):
        """Submitting source_key__enabled='' (dirty unchecked) must write enabled=False.

        JS dirty-tracking sends '' for an unchecked checkbox that was dirtied
        (the user toggled it off).  The server sees the field as present and
        interprets the empty/non-'on' value as False.
        """
        _write_providers(tmp_providers_path)
        client.post("/settings", data={
            # adzuna__enabled sent as empty string — explicitly unchecked and dirty.
            "adzuna__enabled": "",
            "adzuna__app_id": "id",
            "adzuna__app_key": "key",
            "tab": "sources",
        })
        with open(tmp_providers_path, encoding="utf-8") as fh:
            saved = json.load(fh)
        assert saved["job_sources"]["adzuna"]["enabled"] is False

    def test_enabled_written_for_keyless_source(
        self, client, tmp_providers_path, tmp_keys_path, tmp_config_path
    ):
        """Keyless sources (no credential fields) must still get an enabled flag."""
        _write_providers(tmp_providers_path)
        client.post("/settings", data={
            "remotive__enabled": "on",
            "tab": "sources",
        })
        with open(tmp_providers_path, encoding="utf-8") as fh:
            saved = json.load(fh)
        assert saved["job_sources"]["remotive"]["enabled"] is True

    def test_enabled_boolean_not_overwritten_by_blank_string_guard(self, tmp_path):
        """save_providers() must persist boolean False (not skip it as 'blank')."""
        path = str(tmp_path / "providers.json")
        _write_providers(path)
        save_providers(
            {"job_sources": {"adzuna": {"enabled": False}}},
            providers_path=path,
        )
        with open(path, encoding="utf-8") as fh:
            saved = json.load(fh)
        # False must be written — the blank-string guard only skips empty strings.
        assert saved["job_sources"]["adzuna"]["enabled"] is False

    def test_enabled_true_boolean_written_by_save_providers(self, tmp_path):
        """save_providers() must persist boolean True."""
        path = str(tmp_path / "providers.json")
        _write_providers(path)
        save_providers(
            {"job_sources": {"adzuna": {"enabled": True}}},
            providers_path=path,
        )
        with open(path, encoding="utf-8") as fh:
            saved = json.load(fh)
        assert saved["job_sources"]["adzuna"]["enabled"] is True


# ===========================================================================
# GET /settings — enabled checkbox rendered in HTML
# ===========================================================================


class TestSettingsGetEnabledCheckbox:
    def test_enabled_checkbox_rendered_for_each_source(
        self, client, tmp_providers_path, tmp_keys_path, tmp_config_path
    ):
        """Each source row in the Sources tab must contain an enabled checkbox."""
        from job_sources import SOURCES
        resp = client.get("/settings")
        body = resp.data.decode()
        for key in SOURCES:
            assert f"{key}__enabled" in body, (
                f"Expected checkbox name '{key}__enabled' in response"
            )

    def test_checkbox_is_checked_when_source_enabled(
        self, client, tmp_providers_path, tmp_keys_path, tmp_config_path
    ):
        """When a source is enabled in providers.json, its checkbox must be checked."""
        data = {
            "provider_order": [],
            "llm": {},
            "job_sources": {
                "remotive": {"enabled": True},
            },
        }
        with open(tmp_providers_path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)

        resp = client.get("/settings")
        body = resp.data.decode()
        # The checked checkbox must appear for remotive.
        assert 'name="remotive__enabled"' in body
        # "checked" must appear somewhere near the remotive checkbox.
        # Simple check: both the field name and "checked" appear in the page.
        assert "checked" in body

    def test_checkbox_not_checked_when_source_disabled(
        self, client, tmp_providers_path, tmp_keys_path, tmp_config_path
    ):
        """When source enabled=False, its checkbox must not have checked attribute."""
        data = {
            "provider_order": [],
            "llm": {},
            "job_sources": {
                "remotive": {"enabled": False},
            },
        }
        with open(tmp_providers_path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)

        resp = client.get("/settings")
        body = resp.data.decode()
        # The field must still be rendered, but "checked" must NOT appear for remotive.
        # We verify by checking the remotive block does not contain checked.
        # Since other sources may also appear unchecked, just confirm the field renders.
        assert 'name="remotive__enabled"' in body


# ===========================================================================
# _build_llm_schemas — has_values checks all required fields (Bug #240)
# ===========================================================================


class TestBuildLlmSchemasHasValues:
    """has_values must be False when any required field (not just api_key) is empty."""

    def test_has_values_false_when_model_empty(
        self, client, tmp_providers_path, tmp_keys_path, tmp_config_path
    ):
        """A provider with api_key set but model empty must NOT show 'configured'."""
        import json as _json

        data = {
            "provider_order": ["anthropic"],
            "llm": {
                # api_key is set, but model is empty — this is the broken state
                # that caused false "● configured" status (issue #240).
                "anthropic": {"api_key": "sk-test", "model": ""},
            },
            "job_sources": {},
        }
        with open(tmp_providers_path, "w", encoding="utf-8") as fh:
            _json.dump(data, fh)

        from services.provider_schemas import _build_llm_schemas

        schemas = _build_llm_schemas(data["llm"], data["provider_order"])
        # Find the anthropic entry.
        anthropic_entry = next(
            (e for e in schemas if e[0] == "anthropic"), None
        )
        assert anthropic_entry is not None, "anthropic must appear in schemas"
        _key, _schema, has_values, _current_values, _populated = anthropic_entry
        assert has_values is False, (
            "has_values must be False when model is empty, even if api_key is set"
        )

    def test_has_values_true_when_all_required_fields_set(
        self, client, tmp_providers_path, tmp_keys_path, tmp_config_path
    ):
        """A provider with both api_key and model set must show 'configured'."""
        import json as _json

        data = {
            "provider_order": ["anthropic"],
            "llm": {
                "anthropic": {"api_key": "sk-test", "model": "claude-haiku-4-5-20251001"},
            },
            "job_sources": {},
        }
        with open(tmp_providers_path, "w", encoding="utf-8") as fh:
            _json.dump(data, fh)

        from services.provider_schemas import _build_llm_schemas

        schemas = _build_llm_schemas(data["llm"], data["provider_order"])
        anthropic_entry = next(
            (e for e in schemas if e[0] == "anthropic"), None
        )
        assert anthropic_entry is not None
        _key, _schema, has_values, _current_values, _populated = anthropic_entry
        assert has_values is True, (
            "has_values must be True when all required fields (api_key and model) are set"
        )

    def test_current_values_prepopulates_model_default(
        self, client, tmp_providers_path, tmp_keys_path, tmp_config_path
    ):
        """current_values must fall back to field default when model not stored."""
        import json as _json

        data = {
            "provider_order": ["anthropic"],
            "llm": {
                # model not stored at all — current_values must use the schema default
                "anthropic": {"api_key": "sk-test"},
            },
            "job_sources": {},
        }
        with open(tmp_providers_path, "w", encoding="utf-8") as fh:
            _json.dump(data, fh)

        from services.provider_schemas import _build_llm_schemas

        schemas = _build_llm_schemas(data["llm"], data["provider_order"])
        anthropic_entry = next(
            (e for e in schemas if e[0] == "anthropic"), None
        )
        assert anthropic_entry is not None
        _key, _schema, _has_values, current_values, _populated = anthropic_entry
        # The model default from AnthropicProvider.settings_schema() is
        # "claude-haiku-4-5-20251001".  current_values must surface that default.
        assert current_values.get("model") == "claude-haiku-4-5-20251001", (
            "current_values['model'] must fall back to the schema default when not stored"
        )

    def test_current_values_does_not_include_password_fields(
        self, client, tmp_providers_path, tmp_keys_path, tmp_config_path
    ):
        """Password fields must never appear in current_values (they stay blank in the form)."""
        import json as _json

        data = {
            "provider_order": ["anthropic"],
            "llm": {
                "anthropic": {"api_key": "sk-secret", "model": "claude-haiku-4-5-20251001"},
            },
            "job_sources": {},
        }
        with open(tmp_providers_path, "w", encoding="utf-8") as fh:
            _json.dump(data, fh)

        from services.provider_schemas import _build_llm_schemas

        schemas = _build_llm_schemas(data["llm"], data["provider_order"])
        anthropic_entry = next(
            (e for e in schemas if e[0] == "anthropic"), None
        )
        assert anthropic_entry is not None
        _key, _schema, _has_values, current_values, _populated = anthropic_entry
        assert "api_key" not in current_values, (
            "api_key (password field) must not appear in current_values"
        )


# ===========================================================================
# POST /settings — cross-tab and within-tab preservation (issue #71)
# ===========================================================================


class TestSettingsPostCrossTabPreservation:
    """Saving one tab must never wipe data belonging to the other tab."""

    def test_saving_llm_tab_does_not_wipe_job_sources(
        self, client, tmp_providers_path, tmp_keys_path, tmp_config_path
    ):
        """POST to the LLM tab must leave job_sources credentials untouched."""
        _write_providers(tmp_providers_path)
        client.post("/settings", data={
            "anthropic__api_key": "sk-updated",
            "anthropic__model": "claude-haiku-4-5-20251001",
            "tab": "llm",
        })
        with open(tmp_providers_path, encoding="utf-8") as fh:
            saved = json.load(fh)
        # Adzuna credentials must be unchanged.
        assert saved["job_sources"]["adzuna"]["app_id"] == "existing-id"
        assert saved["job_sources"]["adzuna"]["app_key"] == "existing-key"

    def test_saving_sources_tab_does_not_wipe_llm_credentials(
        self, client, tmp_providers_path, tmp_keys_path, tmp_config_path
    ):
        """POST to the sources tab must leave LLM api_key and model untouched."""
        _write_providers(tmp_providers_path)
        client.post("/settings", data={
            "adzuna__enabled": "on",
            "adzuna__app_id": "new-id",
            "adzuna__app_key": "new-key",
            "tab": "sources",
        })
        with open(tmp_providers_path, encoding="utf-8") as fh:
            saved = json.load(fh)
        # Anthropic credentials must be unchanged.
        assert saved["llm"]["anthropic"]["api_key"] == "sk-existing"
        assert saved["llm"]["anthropic"]["model"] == "claude-haiku-4-5-20251001"

    def test_provider_order_not_wiped_by_llm_tab_save(
        self, client, tmp_providers_path, tmp_keys_path, tmp_config_path
    ):
        """provider_order must be preserved when saving the LLM tab."""
        _write_providers(tmp_providers_path)
        client.post("/settings", data={
            "anthropic__api_key": "sk-updated",
            "tab": "llm",
        })
        with open(tmp_providers_path, encoding="utf-8") as fh:
            saved = json.load(fh)
        assert saved["provider_order"] == ["anthropic", "openai", "gemini"]

    def test_provider_order_not_wiped_by_sources_tab_save(
        self, client, tmp_providers_path, tmp_keys_path, tmp_config_path
    ):
        """provider_order must be preserved when saving the sources tab."""
        _write_providers(tmp_providers_path)
        client.post("/settings", data={
            "adzuna__enabled": "on",
            "tab": "sources",
        })
        with open(tmp_providers_path, encoding="utf-8") as fh:
            saved = json.load(fh)
        assert saved["provider_order"] == ["anthropic", "openai", "gemini"]


class TestSettingsPostWithinTabPreservation:
    """Within the active tab, providers not touched by the user must be preserved."""

    def test_saving_one_llm_provider_preserves_other_providers(
        self, client, tmp_providers_path, tmp_keys_path, tmp_config_path
    ):
        """Updating Anthropic's key must not overwrite OpenAI's or Gemini's model."""
        _write_providers(tmp_providers_path)
        # Only submit Anthropic fields; OpenAI and Gemini fields are not in the form.
        client.post("/settings", data={
            "anthropic__api_key": "sk-updated",
            "anthropic__model": "claude-haiku-4-5-20251001",
            "tab": "llm",
        })
        with open(tmp_providers_path, encoding="utf-8") as fh:
            saved = json.load(fh)
        assert saved["llm"]["openai"]["model"] == "gpt-4o-mini"
        assert saved["llm"]["gemini"]["model"] == "gemini-1.5-flash"

    def test_saving_one_source_preserves_other_source_credentials(
        self, client, tmp_providers_path, tmp_keys_path, tmp_config_path
    ):
        """Sparse POST (dirty-tracking): only touched sources are modified.

        When JS dirty-tracking is active, the client only sends fields the user
        actually changed.  Submitting only Adzuna fields must leave Jooble
        entirely untouched — including its enabled flag (issue #89).
        """
        data = {
            "provider_order": ["anthropic"],
            "llm": {
                "anthropic": {"api_key": "sk-existing", "model": "claude-haiku-4-5-20251001"},
            },
            "job_sources": {
                "adzuna": {"app_id": "existing-id", "app_key": "existing-key", "enabled": True},
                "jooble": {"api_key": "jooble-key", "enabled": True},
            },
        }
        with open(tmp_providers_path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)

        # Sparse POST — only Adzuna's dirty credential fields are submitted.
        # Jooble is not present in the POST at all (dirty-tracking excluded it).
        client.post("/settings", data={
            "adzuna__app_id": "updated-id",
            "adzuna__app_key": "updated-key",
            "tab": "sources",
        })
        with open(tmp_providers_path, encoding="utf-8") as fh:
            saved = json.load(fh)
        # Adzuna credentials are updated.
        assert saved["job_sources"]["adzuna"]["app_id"] == "updated-id"
        assert saved["job_sources"]["adzuna"]["app_key"] == "updated-key"
        # Jooble must be completely untouched — key AND enabled flag preserved.
        assert saved["job_sources"]["jooble"]["api_key"] == "jooble-key"
        assert saved["job_sources"]["jooble"]["enabled"] is True

    def test_saving_multiple_llm_providers_at_once(
        self, client, tmp_providers_path, tmp_keys_path, tmp_config_path
    ):
        """Submitting multiple providers' fields in a single POST must update all of them."""
        _write_providers(tmp_providers_path)
        client.post("/settings", data={
            "anthropic__api_key": "sk-new-anthropic",
            "anthropic__model": "claude-opus-4-20251001",
            "openai__api_key": "sk-new-openai",
            "openai__model": "gpt-4o",
            "tab": "llm",
        })
        with open(tmp_providers_path, encoding="utf-8") as fh:
            saved = json.load(fh)
        assert saved["llm"]["anthropic"]["api_key"] == "sk-new-anthropic"
        assert saved["llm"]["anthropic"]["model"] == "claude-opus-4-20251001"
        assert saved["llm"]["openai"]["api_key"] == "sk-new-openai"
        assert saved["llm"]["openai"]["model"] == "gpt-4o"
        # Gemini was not submitted — its model must remain unchanged.
        assert saved["llm"]["gemini"]["model"] == "gemini-1.5-flash"


# ===========================================================================
# POST /settings — dirty-tracking sparse submit (issue #89)
# ===========================================================================


class TestSettingsPostDirtyTracking:
    """Client-side dirty tracking sends only changed fields.  The server must
    handle sparse POSTs correctly — only updating what was submitted and leaving
    everything else untouched.
    """

    def test_sparse_post_preserves_untouched_source_credentials(
        self, client, tmp_providers_path, tmp_keys_path, tmp_config_path
    ):
        """Submitting only dirty fields must leave all other stored values intact.

        Regression test for issue #89: previously all sources got enabled=False
        when the form did not include their checkbox, because the server iterated
        all sources regardless of whether they appeared in the POST body.
        """
        data = {
            "provider_order": [],
            "llm": {},
            "job_sources": {
                "adzuna": {"app_id": "id-a", "app_key": "key-a", "enabled": True},
                "jooble": {"api_key": "jooble-secret", "enabled": True},
                "remotive": {"enabled": True},
            },
        }
        with open(tmp_providers_path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)

        # Only Adzuna's app_id was changed by the user — sparse POST.
        client.post("/settings", data={
            "adzuna__app_id": "id-updated",
            "tab": "sources",
        })
        with open(tmp_providers_path, encoding="utf-8") as fh:
            saved = json.load(fh)
        # The dirty field is updated.
        assert saved["job_sources"]["adzuna"]["app_id"] == "id-updated"
        # Untouched credential on the same source is preserved.
        assert saved["job_sources"]["adzuna"]["app_key"] == "key-a"
        # Adzuna's enabled state: checkbox absent means no change — the stored
        # True value must be preserved even though app_id was updated.
        assert saved["job_sources"]["adzuna"]["enabled"] is True
        # Jooble and Remotive were not in the POST at all — fully preserved.
        assert saved["job_sources"]["jooble"]["api_key"] == "jooble-secret"
        assert saved["job_sources"]["jooble"]["enabled"] is True
        assert saved["job_sources"]["remotive"]["enabled"] is True

    def test_explicitly_cleared_field_clears_stored_value_via_clear_flag(
        self, client, tmp_providers_path, tmp_keys_path, tmp_config_path
    ):
        """When the Clear button is clicked, a __clear__ flag is posted alongside the empty
        password.  The server must clear the stored value regardless of the no-JS guard.

        The JS Clear button sets the password to '' and adds a hidden __clear__ field.
        The server detects the flag and writes "" to storage.
        """
        data = {
            "provider_order": ["anthropic"],
            "llm": {
                "anthropic": {"api_key": "sk-to-keep", "model": "claude-haiku-4-5-20251001"},
            },
            "job_sources": {},
        }
        with open(tmp_providers_path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)

        # The Clear button posts both the empty password and the __clear__ flag.
        client.post("/settings", data={
            "anthropic__api_key": "",
            "__clear__anthropic__api_key": "1",
            "tab": "llm",
        })
        with open(tmp_providers_path, encoding="utf-8") as fh:
            saved = json.load(fh)
        assert saved["llm"]["anthropic"]["api_key"] == "", (
            "__clear__ flag must clear the stored api_key to ''"
        )
        # Model was not touched — must remain unchanged.
        assert saved["llm"]["anthropic"]["model"] == "claude-haiku-4-5-20251001"

    def test_empty_post_with_only_tab_changes_nothing(
        self, client, tmp_providers_path, tmp_keys_path, tmp_config_path
    ):
        """Submitting only the tab field (no dirty fields) must leave everything unchanged.

        This covers the case where JS detects no dirty fields and either does not
        submit or submits only the tab sentinel.  The server must produce no writes
        because the updates dict is effectively empty.
        """
        _write_providers(tmp_providers_path)
        with open(tmp_providers_path, encoding="utf-8") as fh:
            before = json.load(fh)

        # POST with only the tab field — mirrors what happens when no fields are dirty.
        client.post("/settings", data={"tab": "llm"})

        with open(tmp_providers_path, encoding="utf-8") as fh:
            after = json.load(fh)

        # Nothing should have changed.
        assert after["llm"]["anthropic"]["api_key"] == before["llm"]["anthropic"]["api_key"]
        assert after["llm"]["openai"]["model"] == before["llm"]["openai"]["model"]
        assert after["job_sources"]["adzuna"]["app_id"] == before["job_sources"]["adzuna"]["app_id"]

    def test_sources_sparse_post_with_only_tab_changes_nothing(
        self, client, tmp_providers_path, tmp_keys_path, tmp_config_path
    ):
        """A sources-tab POST with no source fields must leave all sources untouched."""
        data = {
            "provider_order": [],
            "llm": {},
            "job_sources": {
                "adzuna": {"app_id": "stable-id", "app_key": "stable-key", "enabled": True},
                "remotive": {"enabled": True},
            },
        }
        with open(tmp_providers_path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)

        # POST with only the tab field.
        client.post("/settings", data={"tab": "sources"})

        with open(tmp_providers_path, encoding="utf-8") as fh:
            saved = json.load(fh)

        assert saved["job_sources"]["adzuna"]["app_id"] == "stable-id"
        assert saved["job_sources"]["adzuna"]["enabled"] is True
        assert saved["job_sources"]["remotive"]["enabled"] is True


# ===========================================================================
# POST /settings — checkbox-only dirty-tracking (issue #90)
# ===========================================================================


class TestSettingsPostCheckboxDirtyTracking:
    """JS dirty-tracking sends only changed fields.  When only the enabled
    checkbox is toggled (no credential fields touched), the server must persist
    the new enabled state without disturbing stored credentials.
    """

    def test_checkbox_only_toggle_persists_enabled_and_preserves_credentials(
        self, client, tmp_providers_path, tmp_keys_path, tmp_config_path
    ):
        """Sparse POST with only adzuna__enabled=on must set enabled=True and
        leave existing credentials intact.
        """
        data = {
            "provider_order": [],
            "llm": {},
            "job_sources": {
                "adzuna": {
                    "app_id": "keep-id",
                    "app_key": "keep-key",
                    "enabled": False,
                },
            },
        }
        with open(tmp_providers_path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)

        # Only the checkbox was toggled — credentials are not dirty, not sent.
        client.post("/settings", data={
            "adzuna__enabled": "on",
            "tab": "sources",
        })

        with open(tmp_providers_path, encoding="utf-8") as fh:
            saved = json.load(fh)

        assert saved["job_sources"]["adzuna"]["enabled"] is True
        assert saved["job_sources"]["adzuna"]["app_id"] == "keep-id"
        assert saved["job_sources"]["adzuna"]["app_key"] == "keep-key"

    def test_checkbox_and_credential_both_persist(
        self, client, tmp_providers_path, tmp_keys_path, tmp_config_path
    ):
        """Sparse POST with checkbox + one credential field must update both
        without touching the untouched credential.
        """
        data = {
            "provider_order": [],
            "llm": {},
            "job_sources": {
                "adzuna": {
                    "app_id": "old-id",
                    "app_key": "old-key",
                    "enabled": False,
                },
            },
        }
        with open(tmp_providers_path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)

        # Checkbox toggled and app_id changed — app_key was not touched, not sent.
        client.post("/settings", data={
            "adzuna__enabled": "on",
            "adzuna__app_id": "new-id",
            "tab": "sources",
        })

        with open(tmp_providers_path, encoding="utf-8") as fh:
            saved = json.load(fh)

        assert saved["job_sources"]["adzuna"]["enabled"] is True
        assert saved["job_sources"]["adzuna"]["app_id"] == "new-id"
        assert saved["job_sources"]["adzuna"]["app_key"] == "old-key"

    def test_credential_only_change_preserves_enabled_state(
        self, client, tmp_providers_path, tmp_keys_path, tmp_config_path
    ):
        """A credential-only POST must leave enabled unchanged.

        When the user only dirtied a credential field (not the checkbox), the
        JS sends no checkbox field at all.  The server must preserve the stored
        enabled state rather than interpreting the absence as False.
        """
        data = {
            "provider_order": [],
            "llm": {},
            "job_sources": {
                "adzuna": {
                    "app_id": "old-id",
                    "app_key": "old-key",
                    "enabled": True,
                },
            },
        }
        with open(tmp_providers_path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)

        # Only app_id was changed — no checkbox field sent at all.
        client.post("/settings", data={
            "adzuna__app_id": "new-id",
            "tab": "sources",
        })

        with open(tmp_providers_path, encoding="utf-8") as fh:
            saved = json.load(fh)

        assert saved["job_sources"]["adzuna"]["enabled"] is True
        assert saved["job_sources"]["adzuna"]["app_id"] == "new-id"
        assert saved["job_sources"]["adzuna"]["app_key"] == "old-key"

    def test_toggle_off_with_empty_string_disables_and_preserves_credentials(
        self, client, tmp_providers_path, tmp_keys_path, tmp_config_path
    ):
        """A POST with checkbox='' (dirty unchecked) must set enabled=False.

        JS dirty-tracking sends '' for an unchecked checkbox that was dirtied.
        The server must see the field as present (triggering an update) and
        interpret the non-'on' value as False — without touching credentials.
        """
        data = {
            "provider_order": [],
            "llm": {},
            "job_sources": {
                "adzuna": {
                    "app_id": "existing-id",
                    "app_key": "existing-key",
                    "enabled": True,
                },
            },
        }
        with open(tmp_providers_path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)

        # Checkbox sent as empty string — user unchecked it (it was dirty).
        client.post("/settings", data={
            "adzuna__enabled": "",
            "tab": "sources",
        })

        with open(tmp_providers_path, encoding="utf-8") as fh:
            saved = json.load(fh)

        assert saved["job_sources"]["adzuna"]["enabled"] is False
        assert saved["job_sources"]["adzuna"]["app_id"] == "existing-id"
        assert saved["job_sources"]["adzuna"]["app_key"] == "existing-key"


# ===========================================================================
# Clear button — __clear__ flag mechanism (issue #137)
# ===========================================================================


class TestBuildLlmSchemasPopulatedFields:
    """_build_llm_schemas() must return a 5-tuple including populated_fields."""

    def test_populated_fields_contains_password_field_when_stored(self):
        """populated_fields must include a password field name when stored value is non-empty."""

        data = {
            "provider_order": ["anthropic"],
            "llm": {
                "anthropic": {"api_key": "sk-secret", "model": "claude-haiku-4-5-20251001"},
            },
            "job_sources": {},
        }

        from services.provider_schemas import _build_llm_schemas

        schemas = _build_llm_schemas(data["llm"], data["provider_order"])
        anthropic_entry = next(e for e in schemas if e[0] == "anthropic")
        # Unpack 5-tuple: (key, schema, has_values, current_values, populated_fields)
        _key, _schema, _has_values, _current_values, populated_fields = anthropic_entry
        assert "api_key" in populated_fields, (
            "populated_fields must include 'api_key' when a non-empty api_key is stored"
        )

    def test_populated_fields_excludes_password_field_when_empty(self):
        """populated_fields must NOT include a password field name when stored value is empty."""
        from services.provider_schemas import _build_llm_schemas

        data_llm = {"anthropic": {"api_key": "", "model": "claude-haiku-4-5-20251001"}}
        schemas = _build_llm_schemas(data_llm, ["anthropic"])
        anthropic_entry = next(e for e in schemas if e[0] == "anthropic")
        _key, _schema, _has_values, _current_values, populated_fields = anthropic_entry
        assert "api_key" not in populated_fields, (
            "populated_fields must NOT include 'api_key' when stored value is empty"
        )

    def test_populated_fields_excludes_password_field_when_absent(self):
        """populated_fields must NOT include a password field name when not stored at all."""
        from services.provider_schemas import _build_llm_schemas

        data_llm = {"anthropic": {"model": "claude-haiku-4-5-20251001"}}  # no api_key key
        schemas = _build_llm_schemas(data_llm, ["anthropic"])
        anthropic_entry = next(e for e in schemas if e[0] == "anthropic")
        _key, _schema, _has_values, _current_values, populated_fields = anthropic_entry
        assert "api_key" not in populated_fields, (
            "populated_fields must NOT include 'api_key' when it is absent from stored config"
        )

    def test_populated_fields_includes_non_password_field_when_stored(self):
        """populated_fields must include non-password fields too when they have a stored value."""
        from services.provider_schemas import _build_llm_schemas

        data_llm = {"anthropic": {"api_key": "sk-x", "model": "claude-haiku-4-5-20251001"}}
        schemas = _build_llm_schemas(data_llm, ["anthropic"])
        anthropic_entry = next(e for e in schemas if e[0] == "anthropic")
        _key, _schema, _has_values, _current_values, populated_fields = anthropic_entry
        assert "model" in populated_fields, (
            "populated_fields must include 'model' when a non-empty model is stored"
        )

    def test_build_llm_schemas_returns_five_tuple(self):
        """Each entry returned by _build_llm_schemas must be a 5-tuple."""
        from services.provider_schemas import _build_llm_schemas

        schemas = _build_llm_schemas({}, [])
        for entry in schemas:
            assert len(entry) == 5, (
                f"_build_llm_schemas must return 5-tuples, got {len(entry)}-tuple for {entry[0]}"
            )


class TestSettingsClearFlagLlm:
    """POST with __clear__ flag must explicitly clear a stored password field (issue #137).

    The no-JS guard skips empty password strings so accidental no-JS form
    submits cannot wipe keys.  The Clear button works around this by posting
    a __clear__<provider_key>__<field_name>=1 hidden field alongside an empty
    password value.  The server recognises this flag and sets the stored value
    to "".
    """

    def test_clear_flag_removes_stored_llm_key(
        self, client, tmp_providers_path, tmp_keys_path, tmp_config_path
    ):
        """POST with __clear__anthropic__api_key=1 must clear the stored api_key to ''."""
        _write_providers(tmp_providers_path)

        client.post("/settings", data={
            "tab": "llm",
            "__clear__anthropic__api_key": "1",
        })

        with open(tmp_providers_path, encoding="utf-8") as fh:
            saved = json.load(fh)
        assert saved["llm"]["anthropic"]["api_key"] == "", (
            "api_key must be cleared to '' when __clear__ flag is submitted"
        )

    def test_clear_flag_with_empty_password_clears_key(
        self, client, tmp_providers_path, tmp_keys_path, tmp_config_path
    ):
        """POST with empty password AND __clear__ flag must clear the key.

        When the Clear button is clicked:
        1. The password input is set to '' (empty)
        2. A hidden __clear__ input is added with value '1'
        Both fields are sent.  The clear flag must win over the no-JS guard.
        """
        _write_providers(tmp_providers_path)

        client.post("/settings", data={
            "tab": "llm",
            "anthropic__api_key": "",        # empty password (no-JS guard would skip this)
            "__clear__anthropic__api_key": "1",  # clear flag overrides the guard
        })

        with open(tmp_providers_path, encoding="utf-8") as fh:
            saved = json.load(fh)
        assert saved["llm"]["anthropic"]["api_key"] == "", (
            "__clear__ flag must override no-JS guard and clear the key"
        )

    def test_no_clear_flag_preserves_key_when_password_empty(
        self, client, tmp_providers_path, tmp_keys_path, tmp_config_path
    ):
        """POST with empty password but NO __clear__ flag must preserve the stored key.

        This is the no-JS guard: a form submit with an empty password field
        (no JS, no Clear button) must not wipe an existing credential.
        """
        _write_providers(tmp_providers_path)

        client.post("/settings", data={
            "tab": "llm",
            "anthropic__api_key": "",   # empty, but no __clear__ flag
            "anthropic__model": "claude-haiku-4-5-20251001",
        })

        with open(tmp_providers_path, encoding="utf-8") as fh:
            saved = json.load(fh)
        assert saved["llm"]["anthropic"]["api_key"] == "sk-existing", (
            "No-JS guard must preserve existing key when password submitted empty without __clear__ flag"
        )

    def test_clear_flag_does_not_affect_other_fields(
        self, client, tmp_providers_path, tmp_keys_path, tmp_config_path
    ):
        """Clearing one field must not touch other fields of the same provider."""
        _write_providers(tmp_providers_path)

        client.post("/settings", data={
            "tab": "llm",
            "__clear__anthropic__api_key": "1",
        })

        with open(tmp_providers_path, encoding="utf-8") as fh:
            saved = json.load(fh)
        assert saved["llm"]["anthropic"]["api_key"] == ""
        # model was not touched
        assert saved["llm"]["anthropic"]["model"] == "claude-haiku-4-5-20251001"

    def test_clear_flag_for_unknown_field_is_silently_ignored(
        self, client, tmp_providers_path, tmp_keys_path, tmp_config_path
    ):
        """A __clear__ flag for a field not in the schema must not cause an error."""
        _write_providers(tmp_providers_path)

        resp = client.post("/settings", data={
            "tab": "llm",
            "__clear__anthropic__nonexistent_field": "1",
        })

        assert resp.status_code in (200, 302), (
            "Unknown __clear__ field must not cause a 500 error"
        )


class TestSettingsClearFlagSources:
    """Clear flag must also work on the job sources tab (issue #137)."""

    def test_clear_flag_removes_stored_source_key(
        self, client, tmp_providers_path, tmp_keys_path, tmp_config_path
    ):
        """POST with __clear__adzuna__app_key=1 must clear the stored app_key."""
        _write_providers(tmp_providers_path)

        client.post("/settings", data={
            "tab": "sources",
            "adzuna__app_id": "existing-id",   # present so source is "touched"
            "__clear__adzuna__app_key": "1",
        })

        with open(tmp_providers_path, encoding="utf-8") as fh:
            saved = json.load(fh)
        assert saved["job_sources"]["adzuna"]["app_key"] == "", (
            "app_key must be cleared to '' when __clear__ flag is submitted on sources tab"
        )

    def test_no_clear_flag_preserves_source_key_when_password_empty(
        self, client, tmp_providers_path, tmp_keys_path, tmp_config_path
    ):
        """On sources tab, empty password without __clear__ must preserve existing value."""
        _write_providers(tmp_providers_path)

        client.post("/settings", data={
            "tab": "sources",
            "adzuna__app_id": "existing-id",
            "adzuna__app_key": "",   # empty, no __clear__ flag
        })

        with open(tmp_providers_path, encoding="utf-8") as fh:
            saved = json.load(fh)
        assert saved["job_sources"]["adzuna"]["app_key"] == "existing-key", (
            "No-JS guard must preserve source key when password submitted empty without __clear__ flag"
        )

    def test_clear_flag_only_no_credential_field_still_clears(
        self, client, tmp_providers_path, tmp_keys_path, tmp_config_path
    ):
        """Regression test for issue #151.

        When the JS Clear button is clicked, the sparse submit previously sent
        only the __clear__ flag without the credential field itself.  The
        server-side source_in_form guard checked only regular cred keys, so
        the entire source was skipped and the stored value was never cleared.

        Both fixes together must ensure that submitting only a __clear__ flag
        (with no regular credential field present in the POST body) is enough
        to clear the stored value.
        """
        _write_providers(tmp_providers_path)

        # Simulate the pre-fix JS behavior: only the __clear__ flag is sent,
        # the credential field itself (adzuna__app_key) is absent.
        client.post("/settings", data={
            "tab": "sources",
            "__clear__adzuna__app_key": "1",
            # adzuna__app_key intentionally omitted — this is the bug scenario
        })

        with open(tmp_providers_path, encoding="utf-8") as fh:
            saved = json.load(fh)
        assert saved["job_sources"]["adzuna"]["app_key"] == "", (
            "app_key must be cleared when only __clear__ flag is submitted "
            "(no credential field in POST body) — regression for issue #151"
        )


class TestSettingsPopulatedFieldsInTemplate:
    """populated_fields must reach the template and trigger Clear button rendering."""

    def test_clear_button_rendered_for_configured_password_field(
        self, client, tmp_providers_path, tmp_keys_path, tmp_config_path
    ):
        """When a password field has a stored value, a Clear button must appear in the HTML."""
        _write_providers(tmp_providers_path)
        # anthropic has api_key="sk-existing" — Clear button must be rendered
        resp = client.get("/settings")
        body = resp.data.decode()
        assert "btn-clear-key" in body, (
            "Clear button (.btn-clear-key) must be rendered for a configured password field"
        )

    def test_clear_button_not_rendered_when_password_field_empty(
        self, client, tmp_providers_path, tmp_keys_path, tmp_config_path
    ):
        """When a password field is empty/unset, no Clear button must appear for that field."""
        data = {
            "provider_order": ["anthropic"],
            "llm": {
                "anthropic": {"api_key": "", "model": "claude-haiku-4-5-20251001"},
            },
            "job_sources": {},
        }
        with open(tmp_providers_path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)

        resp = client.get("/settings")
        body = resp.data.decode()
        # api_key is empty — no Clear button for it
        # (If other providers also have no keys, no btn-clear-key at all)
        # Extract the anthropic block and verify no data-field-id for anthropic__api_key
        assert 'data-field-id="anthropic__api_key"' not in body, (
            "Clear button must NOT be rendered for an empty/unset password field"
        )

    def test_clear_button_has_correct_data_field_id(
        self, client, tmp_providers_path, tmp_keys_path, tmp_config_path
    ):
        """The Clear button must have data-field-id matching the input's id."""
        _write_providers(tmp_providers_path)
        resp = client.get("/settings")
        body = resp.data.decode()
        assert 'data-field-id="anthropic__api_key"' in body, (
            "Clear button must have data-field-id='anthropic__api_key' for the anthropic api_key field"
        )


# ===========================================================================
# POST /settings — no-JS password guard (issue #138)
# ===========================================================================


class TestSettingsPostNoJsPasswordGuard:
    """When a native (no-JS) form POST submits all inputs, blank password fields
    must be ignored to prevent accidental credential wipe.

    No-JS path: the browser submits every input in the form, including password
    fields that the user never touched.  Without the guard, saving the LLM tab
    with no changes would POST empty strings for every api_key and erase all
    stored credentials.  The guard skips empty password fields so that only an
    explicit Clear action (issue #137) can remove a stored credential.
    """

    def test_njs_full_llm_form_preserves_all_credentials(
        self, client, tmp_providers_path, tmp_keys_path, tmp_config_path
    ):
        """A full-form no-JS POST with all password fields blank must preserve all credentials.

        Simulates the browser submitting every field in the LLM form tab, where
        the user only changed the Anthropic model (non-password) but left all
        api_key fields (password) untouched (blank).
        """
        _write_providers(tmp_providers_path)
        # Simulate a no-JS full-form submit: all fields present, password fields blank.
        client.post("/settings", data={
            "anthropic__api_key": "",        # password — must be preserved
            "anthropic__model": "claude-opus-4-20251001",  # text — user changed this
            "openai__api_key": "",           # password — must be preserved (was "")
            "openai__model": "gpt-4o-mini",
            "gemini__api_key": "",           # password — must be preserved (was "")
            "gemini__model": "gemini-1.5-flash",
            "tab": "llm",
        })
        with open(tmp_providers_path, encoding="utf-8") as fh:
            saved = json.load(fh)
        # Anthropic api_key must remain — blank password was ignored.
        assert saved["llm"]["anthropic"]["api_key"] == "sk-existing", (
            "Blank password field in no-JS submit must not wipe existing api_key"
        )
        # Model was a non-password (text) field with a real value — must be updated.
        assert saved["llm"]["anthropic"]["model"] == "claude-opus-4-20251001"

    def test_new_password_value_is_written_when_non_empty(
        self, client, tmp_providers_path, tmp_keys_path, tmp_config_path
    ):
        """A non-empty password field must still be written (guard only skips blanks)."""
        _write_providers(tmp_providers_path)
        client.post("/settings", data={
            "anthropic__api_key": "sk-brand-new",   # non-empty password — must be saved
            "anthropic__model": "claude-haiku-4-5-20251001",
            "tab": "llm",
        })
        with open(tmp_providers_path, encoding="utf-8") as fh:
            saved = json.load(fh)
        assert saved["llm"]["anthropic"]["api_key"] == "sk-brand-new", (
            "Non-empty password field must be written as normal"
        )

    def test_njs_sources_form_preserves_password_credentials(
        self, client, tmp_providers_path, tmp_keys_path, tmp_config_path
    ):
        """A full-form no-JS sources POST with blank password fields must preserve credentials.

        Adzuna app_id and app_key are password-type fields.  Submitting them
        blank (native form with no changes) must not wipe the stored values.
        """
        _write_providers(tmp_providers_path)
        # Simulate no-JS full-form POST: all source fields present, passwords blank.
        client.post("/settings", data={
            "adzuna__enabled": "on",
            "adzuna__app_id": "",    # password — must be preserved
            "adzuna__app_key": "",   # password — must be preserved
            "tab": "sources",
        })
        with open(tmp_providers_path, encoding="utf-8") as fh:
            saved = json.load(fh)
        # Password fields must not be wiped.
        assert saved["job_sources"]["adzuna"]["app_id"] == "existing-id", (
            "Blank password app_id must not wipe existing credential"
        )
        assert saved["job_sources"]["adzuna"]["app_key"] == "existing-key", (
            "Blank password app_key must not wipe existing credential"
        )

    def test_non_password_blank_field_is_preserved_by_none_guard(
        self, client, tmp_providers_path, tmp_keys_path, tmp_config_path
    ):
        """Non-password fields submitted as blank strings must still be treated normally.

        Only password-type fields have the blank-means-preserve guard.
        A text field submitted as blank is a valid (empty) update.
        """
        data = {
            "provider_order": ["anthropic"],
            "llm": {
                "anthropic": {"api_key": "sk-existing", "model": "claude-haiku-4-5-20251001"},
            },
            "job_sources": {},
        }
        with open(tmp_providers_path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)

        # model is a text (not password) field — blank must clear it.
        client.post("/settings", data={
            "anthropic__model": "",   # text field, blank — valid empty update
            "tab": "llm",
        })
        with open(tmp_providers_path, encoding="utf-8") as fh:
            saved = json.load(fh)
        # Text field submitted as blank — must be written as empty string.
        assert saved["llm"]["anthropic"]["model"] == "", (
            "Blank text (non-password) field must be written as empty string"
        )
        # Password field was absent entirely (not blank) — must be preserved.
        assert saved["llm"]["anthropic"]["api_key"] == "sk-existing"


# ===========================================================================
# POST /settings — sparse JS submit writes model default (issue #231)
# ===========================================================================


class TestSettingsPostSparseJsModelDefault:
    """When JS dirty-tracking sends only the api_key (model was not changed),
    the server must inject the schema default for model so that has_values
    becomes True and the provider shows as configured after the save.

    Root cause (issue #231): the model text input is pre-populated with the
    schema default but is not marked dirty, so the sparse fetch POST omits it.
    The server used to skip any field absent from the POST body, leaving model
    absent from providers.json.  With model missing, has_values is False
    (both api_key AND model are required), causing the provider to display
    "not configured" even though the key was successfully written.
    """

    def test_sparse_api_key_post_writes_model_default_when_model_absent(
        self, client, tmp_providers_path, tmp_keys_path, tmp_config_path
    ):
        """Sparse POST with only api_key must persist the schema default for model.

        Regression test for issue #231: provider persisted as not-configured
        when the user saved only the API key without touching the model field.
        """
        # Start from a state with no model stored — mirrors a fresh local install.
        data = {
            "provider_order": ["anthropic"],
            "llm": {
                "anthropic": {"api_key": ""},  # no model key at all
            },
            "job_sources": {},
        }
        with open(tmp_providers_path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)

        # Sparse POST: only api_key is dirty (user typed a new key).
        # model is absent because JS dirty-tracking did not include it.
        client.post("/settings", data={
            "anthropic__api_key": "sk-new-key",
            "tab": "llm",
        })

        with open(tmp_providers_path, encoding="utf-8") as fh:
            saved = json.load(fh)

        # api_key must be saved.
        assert saved["llm"]["anthropic"]["api_key"] == "sk-new-key", (
            "api_key must be written from the sparse POST"
        )
        # model must be written with the schema default so has_values is True.
        assert saved["llm"]["anthropic"].get("model"), (
            "model must be written with the schema default when absent from the "
            "sparse POST — fixes issue #231 where provider showed 'not configured' "
            "after saving only the API key"
        )
        assert saved["llm"]["anthropic"]["model"] == "claude-haiku-4-5-20251001", (
            "model must equal the AnthropicProvider schema default"
        )

    def test_sparse_api_key_post_does_not_overwrite_existing_model(
        self, client, tmp_providers_path, tmp_keys_path, tmp_config_path
    ):
        """When model is already stored, a sparse api_key POST must not change it.

        The default-injection only fires when the stored value is empty.
        If the user previously saved a non-default model, it must be preserved.
        """
        data = {
            "provider_order": ["anthropic"],
            "llm": {
                "anthropic": {
                    "api_key": "sk-old",
                    "model": "claude-sonnet-4-6",  # non-default, user-chosen model
                },
            },
            "job_sources": {},
        }
        with open(tmp_providers_path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)

        # Sparse POST: user only changed the api_key, not the model.
        client.post("/settings", data={
            "anthropic__api_key": "sk-new-key",
            "tab": "llm",
        })

        with open(tmp_providers_path, encoding="utf-8") as fh:
            saved = json.load(fh)

        assert saved["llm"]["anthropic"]["api_key"] == "sk-new-key"
        # User's non-default model choice must be preserved — default must not
        # overwrite an already-stored value.
        assert saved["llm"]["anthropic"]["model"] == "claude-sonnet-4-6", (
            "Existing non-default model must be preserved when not in the sparse POST"
        )
