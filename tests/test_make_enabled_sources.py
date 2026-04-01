"""
tests/test_make_enabled_sources.py — Unit tests for make_enabled_sources().

Covered cases
-------------
* Sources with enabled=False are excluded from the result.
* Sources with enabled=True and all required credentials present are included.
* Sources with enabled=True but missing required credentials log a warning and
  are excluded.
* Sources with enabled=True and no required credentials (keyless) are included.
* An empty providers_data dict enables all keyless sources (missing enabled key defaults to True).
* Sources not present in providers_data default to enabled; keyed sources without credentials are skipped with a warning.
* The result list preserves SOURCES registry order for enabled sources.
"""

from __future__ import annotations

import logging
import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from job_sources import make_enabled_sources, JobSource


# ---------------------------------------------------------------------------
# Minimal config that satisfies AdzunaClient.__init__()
# ---------------------------------------------------------------------------

_BASE_CONFIG = {
    "adzuna_app_id": "test-id",
    "adzuna_app_key": "test-key",
    "search": {
        "country": "us",
        "what": "software engineer",
        "results_per_page": 10,
        "max_pages": 1,
    },
}


def _providers(sources_cfg: dict) -> dict:
    """Build a minimal providers_data dict with the given job_sources section."""
    return {
        "provider_order": [],
        "llm": {},
        "job_sources": sources_cfg,
    }


# ---------------------------------------------------------------------------
# Helper: a fake keyless source with enabled control
# ---------------------------------------------------------------------------

class _KeylessSource(JobSource):
    """Minimal keyless source for testing — no required credentials."""
    SOURCE = "_keyless_test"

    def __init__(self, config=None, **kwargs):
        pass

    def fetch_page(self, page):
        return []

    def total_pages(self):
        return 1

    def normalise(self, raw):
        return {}

    @classmethod
    def settings_schema(cls) -> dict:
        return {"display_name": "Keyless Test", "fields": []}


class _KeyedSource(JobSource):
    """Minimal keyed source for testing — requires 'api_key'."""
    SOURCE = "_keyed_test"

    def __init__(self, config=None, **kwargs):
        pass

    def fetch_page(self, page):
        return []

    def total_pages(self):
        return 1

    def normalise(self, raw):
        return {}

    @classmethod
    def settings_schema(cls) -> dict:
        return {
            "display_name": "Keyed Test",
            "fields": [
                {"name": "api_key", "label": "API Key", "type": "password", "required": True},
            ],
        }


# ---------------------------------------------------------------------------
# Tests using the real SOURCES registry (Adzuna = keyed, Remotive = keyless)
# ---------------------------------------------------------------------------

class TestMakeEnabledSourcesDisabled:
    def test_disabled_source_not_in_result(self):
        """enabled=False means the source is excluded."""
        providers = _providers({"adzuna": {"enabled": False, "app_id": "x", "app_key": "y"}})
        result = make_enabled_sources(providers, _BASE_CONFIG)
        types = [type(s) for s in result]
        from job_sources import AdzunaClient
        assert AdzunaClient not in types

    def test_missing_entry_defaults_to_enabled(self):
        """A source with no entry in providers_data is treated as enabled.

        Keyless sources (no required credentials) will be instantiated.
        Keyed sources (e.g. adzuna, jooble) will be skipped with a warning
        because required credentials are absent.
        """
        from job_sources import RemotiveClient
        result = make_enabled_sources(_providers({}), _BASE_CONFIG)
        # Keyless sources like remotive should be enabled by default.
        assert any(isinstance(s, RemotiveClient) for s in result)

    def test_empty_providers_data_enables_keyless_sources(self):
        """Completely empty providers_data enables all keyless sources."""
        from job_sources import RemotiveClient
        result = make_enabled_sources({}, _BASE_CONFIG)
        assert any(isinstance(s, RemotiveClient) for s in result)


class TestMakeEnabledSourcesEnabled:
    def test_enabled_keyed_source_with_credentials_is_included(self):
        """enabled=True with all required credentials → source is instantiated."""
        providers = _providers({
            "adzuna": {"enabled": True, "app_id": "real-id", "app_key": "real-key"},
        })
        config = {**_BASE_CONFIG, "adzuna_app_id": "real-id", "adzuna_app_key": "real-key"}
        result = make_enabled_sources(providers, config)
        from job_sources import AdzunaClient
        assert any(isinstance(s, AdzunaClient) for s in result)

    def test_enabled_keyless_source_is_included(self):
        """enabled=True on a source with no required fields → always included."""
        providers = _providers({"remotive": {"enabled": True}})
        result = make_enabled_sources(providers, _BASE_CONFIG)
        from job_sources import RemotiveClient
        assert any(isinstance(s, RemotiveClient) for s in result)

    def test_result_elements_are_job_source_instances(self):
        """Every element returned is a JobSource instance."""
        providers = _providers({"remotive": {"enabled": True}})
        result = make_enabled_sources(providers, _BASE_CONFIG)
        for source in result:
            assert isinstance(source, JobSource)


class TestMakeEnabledSourcesMissingCredentials:
    def test_enabled_keyed_source_missing_credentials_is_skipped(self, caplog):
        """enabled=True but missing a required field → skipped with a warning."""
        providers = _providers({
            "adzuna": {"enabled": True, "app_id": "", "app_key": ""},
        })
        with caplog.at_level(logging.WARNING, logger="ingest.sources"):
            result = make_enabled_sources(providers, _BASE_CONFIG)

        from job_sources import AdzunaClient
        assert not any(isinstance(s, AdzunaClient) for s in result)
        assert any("missing required credentials" in rec.message for rec in caplog.records)

    def test_warning_names_the_missing_fields(self, caplog):
        """The warning message must mention which fields are missing."""
        providers = _providers({
            "adzuna": {"enabled": True, "app_id": "", "app_key": ""},
        })
        with caplog.at_level(logging.WARNING, logger="ingest.sources"):
            make_enabled_sources(providers, _BASE_CONFIG)

        combined = " ".join(rec.message for rec in caplog.records)
        # Both app_id and app_key are required — at least one should appear.
        assert "app_id" in combined or "app_key" in combined

    def test_partially_missing_credentials_also_skipped(self, caplog):
        """Only one of two required fields missing is still enough to skip."""
        providers = _providers({
            "adzuna": {"enabled": True, "app_id": "real-id", "app_key": ""},
        })
        with caplog.at_level(logging.WARNING, logger="ingest.sources"):
            result = make_enabled_sources(providers, _BASE_CONFIG)

        from job_sources import AdzunaClient
        assert not any(isinstance(s, AdzunaClient) for s in result)


class TestMakeEnabledSourcesOrdering:
    def test_multiple_enabled_sources_preserve_registry_order(self):
        """Result list order matches SOURCES insertion order."""
        # Enable two sources that are in the registry — adzuna and remotive.
        # SOURCES order is: adzuna, arbeitnow, himalayas, remoteok, usajobs, the_muse, remotive.
        providers = _providers({
            "adzuna":    {"enabled": True, "app_id": "id", "app_key": "key"},
            "remotive":  {"enabled": True},
        })
        config = {**_BASE_CONFIG, "adzuna_app_id": "id", "adzuna_app_key": "key"}
        result = make_enabled_sources(providers, config)

        source_names = [s.SOURCE for s in result]
        # adzuna must come before remotive since SOURCES is ordered that way.
        assert source_names.index("adzuna") < source_names.index("remotive")


class TestMakeEnabledSourcesWithFakeRegistry:
    """Isolates make_enabled_sources from the real SOURCES dict via monkeypatching."""

    def test_keyless_source_enabled_is_included(self, monkeypatch):
        fake_sources = {"_keyless_test": _KeylessSource}
        monkeypatch.setattr("job_sources.SOURCES", fake_sources)

        providers = _providers({"_keyless_test": {"enabled": True}})
        result = make_enabled_sources(providers, _BASE_CONFIG)

        assert len(result) == 1
        assert isinstance(result[0], _KeylessSource)

    def test_keyed_source_enabled_with_creds_is_included(self, monkeypatch):
        fake_sources = {"_keyed_test": _KeyedSource}
        monkeypatch.setattr("job_sources.SOURCES", fake_sources)

        providers = _providers({"_keyed_test": {"enabled": True, "api_key": "secret"}})
        result = make_enabled_sources(providers, _BASE_CONFIG)

        assert len(result) == 1
        assert isinstance(result[0], _KeyedSource)

    def test_keyed_source_enabled_missing_creds_is_skipped(self, monkeypatch, caplog):
        fake_sources = {"_keyed_test": _KeyedSource}
        monkeypatch.setattr("job_sources.SOURCES", fake_sources)

        providers = _providers({"_keyed_test": {"enabled": True, "api_key": ""}})
        with caplog.at_level(logging.WARNING, logger="ingest.sources"):
            result = make_enabled_sources(providers, _BASE_CONFIG)

        assert result == []
        assert any("missing required credentials" in rec.message for rec in caplog.records)

    def test_disabled_source_excluded_regardless_of_credentials(self, monkeypatch):
        fake_sources = {"_keyed_test": _KeyedSource}
        monkeypatch.setattr("job_sources.SOURCES", fake_sources)

        providers = _providers({"_keyed_test": {"enabled": False, "api_key": "secret"}})
        result = make_enabled_sources(providers, _BASE_CONFIG)

        assert result == []

    def test_source_not_in_providers_defaults_to_enabled(self, monkeypatch):
        """A keyless source with no entry in providers_data is enabled by default."""
        fake_sources = {"_keyless_test": _KeylessSource}
        monkeypatch.setattr("job_sources.SOURCES", fake_sources)

        # No entry for _keyless_test in providers_data — missing key defaults to True.
        result = make_enabled_sources(_providers({}), _BASE_CONFIG)
        assert len(result) == 1
        assert isinstance(result[0], _KeylessSource)
