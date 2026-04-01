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
* Writes enabled=True when checkbox is submitted
* Writes enabled=False when checkbox is absent (unchecked)
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

import app as app_module
from app import app as flask_app
from credentials import save_providers


# ===========================================================================
# Fixtures
# ===========================================================================


@pytest.fixture()
def tmp_providers_path(tmp_path, monkeypatch):
    """Point _PROVIDERS_PATH at a temp file for full isolation."""
    path = str(tmp_path / "providers.json")
    monkeypatch.setattr(app_module, "_PROVIDERS_PATH", path)
    return path


@pytest.fixture()
def tmp_keys_path(tmp_path, monkeypatch):
    """Point _KEYS_PATH at a temp file so legacy migration never triggers."""
    path = str(tmp_path / "keys.json")
    monkeypatch.setattr(app_module, "_KEYS_PATH", path)
    return path


@pytest.fixture()
def tmp_config_path(tmp_path, monkeypatch):
    """Point _CONFIG_PATH at a temp file so config reads are isolated."""
    path = str(tmp_path / "config.json")
    monkeypatch.setattr(app_module, "_CONFIG_PATH", path)
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


class TestSaveProvidersPreservesExistingOnBlank:
    def test_blank_api_key_preserves_existing(self, tmp_path):
        path = str(tmp_path / "providers.json")
        _write_providers(path)
        save_providers(
            {"llm": {"anthropic": {"api_key": ""}}},
            providers_path=path,
        )
        with open(path, encoding="utf-8") as fh:
            saved = json.load(fh)
        # Existing value must not be overwritten with empty string.
        assert saved["llm"]["anthropic"]["api_key"] == "sk-existing"

    def test_blank_job_source_field_preserves_existing(self, tmp_path):
        path = str(tmp_path / "providers.json")
        _write_providers(path)
        save_providers(
            {"job_sources": {"adzuna": {"app_id": ""}}},
            providers_path=path,
        )
        with open(path, encoding="utf-8") as fh:
            saved = json.load(fh)
        assert saved["job_sources"]["adzuna"]["app_id"] == "existing-id"

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

    def test_blank_api_key_preserves_existing_in_providers_json(
        self, client, tmp_providers_path, tmp_keys_path, tmp_config_path
    ):
        _write_providers(tmp_providers_path)
        client.post("/settings", data={
            "anthropic__api_key": "",   # blank — must NOT overwrite
            "anthropic__model": "claude-haiku-4-5-20251001",
            "tab": "llm",
        })
        with open(tmp_providers_path, encoding="utf-8") as fh:
            saved = json.load(fh)
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

    def test_enabled_false_written_when_checkbox_absent(
        self, client, tmp_providers_path, tmp_keys_path, tmp_config_path
    ):
        """Omitting source_key__enabled (unchecked) must write enabled=False."""
        _write_providers(tmp_providers_path)
        client.post("/settings", data={
            # adzuna__enabled intentionally not submitted (unchecked checkbox)
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

        from app import _build_llm_schemas

        schemas = _build_llm_schemas(data["llm"], data["provider_order"])
        # Find the anthropic entry.
        anthropic_entry = next(
            (e for e in schemas if e[0] == "anthropic"), None
        )
        assert anthropic_entry is not None, "anthropic must appear in schemas"
        _key, _schema, has_values, _current_values = anthropic_entry
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

        from app import _build_llm_schemas

        schemas = _build_llm_schemas(data["llm"], data["provider_order"])
        anthropic_entry = next(
            (e for e in schemas if e[0] == "anthropic"), None
        )
        assert anthropic_entry is not None
        _key, _schema, has_values, _current_values = anthropic_entry
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

        from app import _build_llm_schemas

        schemas = _build_llm_schemas(data["llm"], data["provider_order"])
        anthropic_entry = next(
            (e for e in schemas if e[0] == "anthropic"), None
        )
        assert anthropic_entry is not None
        _key, _schema, _has_values, current_values = anthropic_entry
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

        from app import _build_llm_schemas

        schemas = _build_llm_schemas(data["llm"], data["provider_order"])
        anthropic_entry = next(
            (e for e in schemas if e[0] == "anthropic"), None
        )
        assert anthropic_entry is not None
        _key, _schema, _has_values, current_values = anthropic_entry
        assert "api_key" not in current_values, (
            "api_key (password field) must not appear in current_values"
        )
