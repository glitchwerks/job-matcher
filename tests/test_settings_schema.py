"""
tests/test_settings_schema.py — Tests for settings_schema() classmethods.

Verifies that every LLM provider and job source class exposes a
``settings_schema()`` classmethod returning a well-formed dict, and that the
schemas are reachable via the existing registries (``_PROVIDER_CLASS_MAP`` and
``SOURCES``).

Covered cases
-------------
* Every LLM provider returns a dict with ``display_name`` (str) and
  ``fields`` (list).
* Every field in an LLM provider schema has the mandatory keys:
  ``name``, ``label``, ``type``, ``required``.
* Every field ``type`` is one of ``{"text", "password"}``.
* LLM providers with a ``model`` field expose a sensible ``default`` value.
* Every job source returns a dict with ``display_name`` (str) and
  ``fields`` (list).
* Sources with no credentials return ``fields: []``.
* Sources with credentials have all mandatory field keys.
* Schemas are reachable via the registries.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from providers import AnthropicProvider, OpenAIProvider, GeminiProvider
from providers import _PROVIDER_CLASS_MAP
from job_sources import SOURCES

AdzunaClient = SOURCES["adzuna"]
ArbeitnowClient = SOURCES["arbeitnow"]
HimalayasClient = SOURCES["himalayas"]
RemoteOKClient = SOURCES["remoteok"]
USAJobsClient = SOURCES["usajobs"]
TheMuseClient = SOURCES["the_muse"]
RemotiveClient = SOURCES["remotive"]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_VALID_FIELD_TYPES = {"text", "password"}
_MANDATORY_FIELD_KEYS = {"name", "label", "type", "required"}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _assert_schema_shape(schema: dict, context: str) -> None:
    """Assert top-level ``display_name`` and ``fields`` keys are well-formed."""
    assert isinstance(schema, dict), f"{context}: schema must be a dict"
    assert "display_name" in schema, f"{context}: missing 'display_name'"
    assert isinstance(schema["display_name"], str), (
        f"{context}: 'display_name' must be a str"
    )
    assert "fields" in schema, f"{context}: missing 'fields'"
    assert isinstance(schema["fields"], list), f"{context}: 'fields' must be a list"


def _assert_field_shape(field: dict, context: str) -> None:
    """Assert a single field dict has all mandatory keys and a valid type."""
    missing = _MANDATORY_FIELD_KEYS - set(field)
    assert not missing, f"{context}: field missing keys {missing}: {field!r}"
    assert field["type"] in _VALID_FIELD_TYPES, (
        f"{context}: field type {field['type']!r} not in {_VALID_FIELD_TYPES}"
    )
    assert isinstance(field["required"], bool), (
        f"{context}: 'required' must be bool, got {type(field['required'])!r}"
    )


# ===========================================================================
# LLM Providers
# ===========================================================================


class TestAnthropicProviderSchema:
    def test_returns_well_formed_schema(self):
        schema = AnthropicProvider.settings_schema()
        _assert_schema_shape(schema, "AnthropicProvider")

    def test_display_name(self):
        assert AnthropicProvider.settings_schema()["display_name"] == "Anthropic"

    def test_all_fields_have_mandatory_keys(self):
        for field in AnthropicProvider.settings_schema()["fields"]:
            _assert_field_shape(field, "AnthropicProvider")

    def test_has_api_key_field(self):
        fields_by_name = {f["name"]: f for f in AnthropicProvider.settings_schema()["fields"]}
        assert "api_key" in fields_by_name
        assert fields_by_name["api_key"]["type"] == "password"
        assert fields_by_name["api_key"]["required"] is True

    def test_has_model_field_with_default(self):
        fields_by_name = {f["name"]: f for f in AnthropicProvider.settings_schema()["fields"]}
        assert "model" in fields_by_name
        assert fields_by_name["model"]["type"] == "text"
        assert fields_by_name["model"]["required"] is True
        assert "default" in fields_by_name["model"]
        assert isinstance(fields_by_name["model"]["default"], str)
        assert fields_by_name["model"]["default"]  # non-empty


class TestOpenAIProviderSchema:
    def test_returns_well_formed_schema(self):
        schema = OpenAIProvider.settings_schema()
        _assert_schema_shape(schema, "OpenAIProvider")

    def test_display_name(self):
        assert OpenAIProvider.settings_schema()["display_name"] == "OpenAI"

    def test_all_fields_have_mandatory_keys(self):
        for field in OpenAIProvider.settings_schema()["fields"]:
            _assert_field_shape(field, "OpenAIProvider")

    def test_has_api_key_field(self):
        fields_by_name = {f["name"]: f for f in OpenAIProvider.settings_schema()["fields"]}
        assert "api_key" in fields_by_name
        assert fields_by_name["api_key"]["type"] == "password"
        assert fields_by_name["api_key"]["required"] is True

    def test_has_model_field_with_default(self):
        fields_by_name = {f["name"]: f for f in OpenAIProvider.settings_schema()["fields"]}
        assert "model" in fields_by_name
        assert fields_by_name["model"]["type"] == "text"
        assert fields_by_name["model"]["required"] is True
        assert "default" in fields_by_name["model"]
        assert isinstance(fields_by_name["model"]["default"], str)
        assert fields_by_name["model"]["default"]


class TestGeminiProviderSchema:
    def test_returns_well_formed_schema(self):
        schema = GeminiProvider.settings_schema()
        _assert_schema_shape(schema, "GeminiProvider")

    def test_display_name(self):
        assert GeminiProvider.settings_schema()["display_name"] == "Gemini"

    def test_all_fields_have_mandatory_keys(self):
        for field in GeminiProvider.settings_schema()["fields"]:
            _assert_field_shape(field, "GeminiProvider")

    def test_has_api_key_field(self):
        fields_by_name = {f["name"]: f for f in GeminiProvider.settings_schema()["fields"]}
        assert "api_key" in fields_by_name
        assert fields_by_name["api_key"]["type"] == "password"
        assert fields_by_name["api_key"]["required"] is True

    def test_has_model_field_with_default(self):
        fields_by_name = {f["name"]: f for f in GeminiProvider.settings_schema()["fields"]}
        assert "model" in fields_by_name
        assert fields_by_name["model"]["type"] == "text"
        assert fields_by_name["model"]["required"] is True
        assert "default" in fields_by_name["model"]
        assert isinstance(fields_by_name["model"]["default"], str)
        assert fields_by_name["model"]["default"]


class TestProviderRegistryAccess:
    """settings_schema() must be reachable via _PROVIDER_CLASS_MAP."""

    def test_anthropic_via_registry(self):
        schema = _PROVIDER_CLASS_MAP["anthropic"].settings_schema()
        _assert_schema_shape(schema, "_PROVIDER_CLASS_MAP['anthropic']")

    def test_openai_via_registry(self):
        schema = _PROVIDER_CLASS_MAP["openai"].settings_schema()
        _assert_schema_shape(schema, "_PROVIDER_CLASS_MAP['openai']")

    def test_gemini_via_registry(self):
        schema = _PROVIDER_CLASS_MAP["gemini"].settings_schema()
        _assert_schema_shape(schema, "_PROVIDER_CLASS_MAP['gemini']")

    def test_all_registered_providers_have_schema(self):
        for key, cls in _PROVIDER_CLASS_MAP.items():
            schema = cls.settings_schema()
            _assert_schema_shape(schema, f"_PROVIDER_CLASS_MAP[{key!r}]")


# ===========================================================================
# Job Sources
# ===========================================================================


class TestAdzunaSourceSchema:
    def test_returns_well_formed_schema(self):
        schema = AdzunaClient.settings_schema()
        _assert_schema_shape(schema, "AdzunaClient")

    def test_display_name(self):
        assert AdzunaClient.settings_schema()["display_name"] == "Adzuna"

    def test_all_fields_have_mandatory_keys(self):
        for field in AdzunaClient.settings_schema()["fields"]:
            _assert_field_shape(field, "AdzunaClient")

    def test_has_app_id_field(self):
        fields_by_name = {f["name"]: f for f in AdzunaClient.settings_schema()["fields"]}
        assert "app_id" in fields_by_name
        assert fields_by_name["app_id"]["type"] == "password"
        assert fields_by_name["app_id"]["required"] is True

    def test_has_app_key_field(self):
        fields_by_name = {f["name"]: f for f in AdzunaClient.settings_schema()["fields"]}
        assert "app_key" in fields_by_name
        assert fields_by_name["app_key"]["type"] == "password"
        assert fields_by_name["app_key"]["required"] is True


class TestUSAJobsSourceSchema:
    def test_returns_well_formed_schema(self):
        schema = USAJobsClient.settings_schema()
        _assert_schema_shape(schema, "USAJobsClient")

    def test_display_name(self):
        assert USAJobsClient.settings_schema()["display_name"] == "USAJobs"

    def test_all_fields_have_mandatory_keys(self):
        for field in USAJobsClient.settings_schema()["fields"]:
            _assert_field_shape(field, "USAJobsClient")

    def test_has_api_key_field(self):
        fields_by_name = {f["name"]: f for f in USAJobsClient.settings_schema()["fields"]}
        assert "api_key" in fields_by_name
        assert fields_by_name["api_key"]["type"] == "password"
        assert fields_by_name["api_key"]["required"] is True

    def test_has_user_agent_field(self):
        fields_by_name = {f["name"]: f for f in USAJobsClient.settings_schema()["fields"]}
        assert "user_agent" in fields_by_name
        assert fields_by_name["user_agent"]["required"] is True


class TestNoCredentialSources:
    """Sources with no credentials must return an empty fields list."""

    @pytest.mark.parametrize("cls,expected_name", [
        (ArbeitnowClient, "Arbeitnow"),
        (HimalayasClient, "Himalayas"),
        (RemoteOKClient, "Remote OK"),
        (TheMuseClient, "The Muse"),
        (RemotiveClient, "Remotive"),
    ])
    def test_returns_well_formed_schema(self, cls, expected_name):
        schema = cls.settings_schema()
        _assert_schema_shape(schema, cls.__name__)

    @pytest.mark.parametrize("cls,expected_name", [
        (ArbeitnowClient, "Arbeitnow"),
        (HimalayasClient, "Himalayas"),
        (RemoteOKClient, "Remote OK"),
        (TheMuseClient, "The Muse"),
        (RemotiveClient, "Remotive"),
    ])
    def test_display_name(self, cls, expected_name):
        assert cls.settings_schema()["display_name"] == expected_name

    @pytest.mark.parametrize("cls,expected_name", [
        (ArbeitnowClient, "Arbeitnow"),
        (HimalayasClient, "Himalayas"),
        (RemoteOKClient, "Remote OK"),
        (TheMuseClient, "The Muse"),
        (RemotiveClient, "Remotive"),
    ])
    def test_empty_fields(self, cls, expected_name):
        assert cls.settings_schema()["fields"] == []


class TestSourceHomeUrl:
    """Every job source schema must expose a non-empty ``home_url`` string."""

    @pytest.mark.parametrize("cls,expected_url", [
        (AdzunaClient, "https://www.adzuna.com"),
        (ArbeitnowClient, "https://www.arbeitnow.com"),
        (HimalayasClient, "https://himalayas.app"),
        (RemoteOKClient, "https://remoteok.com"),
        (RemotiveClient, "https://remotive.com"),
        (TheMuseClient, "https://www.themuse.com"),
        (USAJobsClient, "https://www.usajobs.gov"),
    ])
    def test_home_url_present(self, cls, expected_url):
        schema = cls.settings_schema()
        assert "home_url" in schema, f"{cls.__name__}: missing 'home_url' key"
        assert schema["home_url"] == expected_url, (
            f"{cls.__name__}: expected home_url={expected_url!r}, "
            f"got {schema['home_url']!r}"
        )

    @pytest.mark.parametrize("cls,_", [
        (AdzunaClient, None),
        (ArbeitnowClient, None),
        (HimalayasClient, None),
        (RemoteOKClient, None),
        (RemotiveClient, None),
        (TheMuseClient, None),
        (USAJobsClient, None),
    ])
    def test_home_url_is_https(self, cls, _):
        url = cls.settings_schema()["home_url"]
        assert url.startswith("https://"), (
            f"{cls.__name__}: home_url must start with 'https://', got {url!r}"
        )


class TestSourceRegistryAccess:
    """settings_schema() must be reachable via the SOURCES registry."""

    def test_adzuna_via_registry(self):
        schema = SOURCES["adzuna"].settings_schema()
        _assert_schema_shape(schema, "SOURCES['adzuna']")

    def test_arbeitnow_via_registry(self):
        schema = SOURCES["arbeitnow"].settings_schema()
        _assert_schema_shape(schema, "SOURCES['arbeitnow']")

    def test_himalayas_via_registry(self):
        schema = SOURCES["himalayas"].settings_schema()
        _assert_schema_shape(schema, "SOURCES['himalayas']")

    def test_remoteok_via_registry(self):
        schema = SOURCES["remoteok"].settings_schema()
        _assert_schema_shape(schema, "SOURCES['remoteok']")

    def test_usajobs_via_registry(self):
        schema = SOURCES["usajobs"].settings_schema()
        _assert_schema_shape(schema, "SOURCES['usajobs']")

    def test_the_muse_via_registry(self):
        schema = SOURCES["the_muse"].settings_schema()
        _assert_schema_shape(schema, "SOURCES['the_muse']")

    def test_remotive_via_registry(self):
        schema = SOURCES["remotive"].settings_schema()
        _assert_schema_shape(schema, "SOURCES['remotive']")

    def test_all_registered_sources_have_schema(self):
        for key, cls in SOURCES.items():
            schema = cls.settings_schema()
            _assert_schema_shape(schema, f"SOURCES[{key!r}]")
