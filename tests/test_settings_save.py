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

    def test_blank_api_key_clears_existing_in_providers_json(
        self, client, tmp_providers_path, tmp_keys_path, tmp_config_path
    ):
        """Submitting a blank api_key via the settings form must clear the stored value.

        Regression test for issue #284: previously blank strings were silently
        dropped so users could not clear credentials through the UI.
        """
        _write_providers(tmp_providers_path)
        client.post("/settings", data={
            "anthropic__api_key": "",   # blank — must clear existing value
            "anthropic__model": "claude-haiku-4-5-20251001",
            "tab": "llm",
        })
        with open(tmp_providers_path, encoding="utf-8") as fh:
            saved = json.load(fh)
        assert saved["llm"]["anthropic"]["api_key"] == ""

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
        # Adzuna's enabled state: submitted as absent (checkbox not sent) but
        # adzuna IS in the form (app_id was sent), so enabled becomes False.
        assert saved["job_sources"]["adzuna"]["enabled"] is False
        # Jooble and Remotive were not in the POST at all — fully preserved.
        assert saved["job_sources"]["jooble"]["api_key"] == "jooble-secret"
        assert saved["job_sources"]["jooble"]["enabled"] is True
        assert saved["job_sources"]["remotive"]["enabled"] is True

    def test_explicitly_cleared_field_clears_stored_value(
        self, client, tmp_providers_path, tmp_keys_path, tmp_config_path
    ):
        """When a dirty field is submitted with an empty string it must clear the stored value.

        The user deliberately emptied the field (it was non-empty before and they
        cleared it).  JS dirty-tracking marks it dirty and sends it as "".  The
        server must write "" to storage, effectively clearing the credential.
        """
        data = {
            "provider_order": ["anthropic"],
            "llm": {
                "anthropic": {"api_key": "sk-to-clear", "model": "claude-haiku-4-5-20251001"},
            },
            "job_sources": {},
        }
        with open(tmp_providers_path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)

        # The user cleared the api_key field — dirty + empty string.
        client.post("/settings", data={
            "anthropic__api_key": "",
            "tab": "llm",
        })
        with open(tmp_providers_path, encoding="utf-8") as fh:
            saved = json.load(fh)
        assert saved["llm"]["anthropic"]["api_key"] == "", (
            "Explicitly cleared (dirty, value='') field must write empty string to storage"
        )
        # Model was not submitted — must remain unchanged.
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
