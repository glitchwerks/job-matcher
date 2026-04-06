"""
tests/test_credential_source_bugs.py — Regression tests for issues #273, #274,
#282, #283, and #284.

Each class is named after the issue it covers and contains at least one test
that would have FAILED before the fix was applied, proving the bug is gone.
"""

from __future__ import annotations

import json
import logging
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app as app_module
from db import _lookup_pricing
from app import app as flask_app
from credentials import save_providers
from job_sources import SOURCES, make_enabled_sources, JobSource


# ===========================================================================
# Shared fixtures
# ===========================================================================


@pytest.fixture()
def tmp_providers_path(tmp_path, monkeypatch):
    """Isolated providers.json path; patch app._PROVIDERS_PATH."""
    path = str(tmp_path / "providers.json")
    monkeypatch.setattr(app_module, "_PROVIDERS_PATH", path)
    return path


@pytest.fixture()
def tmp_keys_path(tmp_path, monkeypatch):
    path = str(tmp_path / "keys.json")
    monkeypatch.setattr(app_module, "_KEYS_PATH", path)
    return path


@pytest.fixture()
def tmp_config_path(tmp_path, monkeypatch):
    path = str(tmp_path / "config.json")
    monkeypatch.setattr(app_module, "_CONFIG_PATH", path)
    return path


@pytest.fixture()
def client():
    flask_app.config["TESTING"] = True
    with flask_app.test_client() as c:
        yield c


def _write_providers(path: str, data: dict | None = None) -> None:
    if data is None:
        data = {
            "provider_order": ["anthropic"],
            "llm": {
                "anthropic": {"api_key": "sk-existing", "model": "claude-haiku-4-5-20251001"},
            },
            "job_sources": {
                "adzuna": {"app_id": "existing-id", "app_key": "existing-key"},
            },
        }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)


# ===========================================================================
# Issue #273 — ingest.py crashes with SystemExit when Adzuna credentials absent
# ===========================================================================


class TestIssue273IngestGracefulCredentialFailure:
    """run() must log a warning and return rather than raising SystemExit."""

    def test_run_returns_gracefully_when_no_credentials(self, tmp_path, monkeypatch):
        """run() must not raise SystemExit when credentials cannot be loaded.

        Before the fix, CredentialError was caught and re-raised as sys.exit(1),
        crashing the entire process.  After the fix it logs a warning and returns.
        """
        import ingest
        from credentials import CredentialError

        # Patch load_providers to simulate the no-credentials case.
        def _raise_credential_error(**kwargs):
            raise CredentialError("no credentials configured")

        monkeypatch.setattr("ingest.load_providers", _raise_credential_error)

        # Write minimal config/profile so the function gets to load_providers.
        cfg_path = str(tmp_path / "config.json")
        profile_path = str(tmp_path / "profile.json")
        with open(cfg_path, "w") as fh:
            json.dump({
                "search": {"country": "us", "what": "engineer",
                           "results_per_page": 10, "max_pages": 1},
                "scoring": {"threshold": 5},
            }, fh)
        with open(profile_path, "w") as fh:
            json.dump({"primary_skills": []}, fh)

        # Must not raise — before the fix this would SystemExit.
        ingest.run(
            config_path=cfg_path,
            profile_path=profile_path,
            providers_path=str(tmp_path / "providers.json"),
        )

    def test_run_logs_warning_when_no_credentials(self, tmp_path, monkeypatch, caplog):
        """A warning is logged when credentials are absent."""
        import ingest
        from credentials import CredentialError

        def _raise_credential_error(**kwargs):
            raise CredentialError("no credentials")

        monkeypatch.setattr("ingest.load_providers", _raise_credential_error)

        cfg_path = str(tmp_path / "config.json")
        profile_path = str(tmp_path / "profile.json")
        with open(cfg_path, "w") as fh:
            json.dump({
                "search": {"country": "us", "what": "engineer",
                           "results_per_page": 10, "max_pages": 1},
                "scoring": {"threshold": 5},
            }, fh)
        with open(profile_path, "w") as fh:
            json.dump({"primary_skills": []}, fh)

        with caplog.at_level(logging.WARNING, logger="ingest"):
            ingest.run(
                config_path=cfg_path,
                profile_path=profile_path,
                providers_path=str(tmp_path / "providers.json"),
            )

        assert any("credentials" in rec.message.lower() for rec in caplog.records)

    def test_rescore_returns_gracefully_when_no_credentials(self, tmp_path, monkeypatch):
        """rescore() must not raise SystemExit when credentials cannot be loaded."""
        import ingest
        from credentials import CredentialError

        def _raise_credential_error(**kwargs):
            raise CredentialError("no credentials")

        monkeypatch.setattr("ingest.load_providers", _raise_credential_error)

        cfg_path = str(tmp_path / "config.json")
        profile_path = str(tmp_path / "profile.json")
        with open(cfg_path, "w") as fh:
            json.dump({
                "search": {"country": "us", "what": "engineer",
                           "results_per_page": 10, "max_pages": 1},
                "scoring": {"threshold": 5},
            }, fh)
        with open(profile_path, "w") as fh:
            json.dump({"primary_skills": []}, fh)

        # Must not raise SystemExit.
        ingest.rescore(
            config_path=cfg_path,
            profile_path=profile_path,
            providers_path=str(tmp_path / "providers.json"),
        )


# ===========================================================================
# Issue #274 — Keyed sources must NOT activate by default when no entry exists
# ===========================================================================


class _KeylessSource(JobSource):
    """Minimal keyless source — no required credentials."""
    SOURCE = "_keyless_274"

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
        return {"display_name": "Keyless 274", "fields": []}


class _KeyedSource(JobSource):
    """Minimal keyed source — requires 'api_key'."""
    SOURCE = "_keyed_274"

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
            "display_name": "Keyed 274",
            "fields": [
                {"name": "api_key", "label": "API Key", "type": "password", "required": True},
            ],
        }


_BASE_CONFIG = {
    "search": {"country": "us", "what": "engineer", "results_per_page": 10, "max_pages": 1},
}


def _providers(sources_cfg: dict) -> dict:
    return {"provider_order": [], "llm": {}, "job_sources": sources_cfg}


class TestIssue274KeyedSourceNotDefaultEnabled:
    """Keyed sources with no providers_data entry must NOT activate silently."""

    def test_keyed_source_not_in_providers_data_is_skipped(self, monkeypatch, caplog):
        """Before fix: keyed sources defaulted to enabled=True even without credentials.

        After fix: a keyed source with no entry in providers_data is skipped with
        a warning, not silently activated.
        """
        fake_sources = {"_keyed_274": _KeyedSource}
        monkeypatch.setattr("job_sources.SOURCES", fake_sources)

        # No entry for _keyed_274 in providers_data.
        with caplog.at_level(logging.WARNING, logger="ingest.sources"):
            result = make_enabled_sources(_providers({}), _BASE_CONFIG)

        # Must be skipped — no credentials.
        assert result == []

    def test_keyed_source_no_entry_emits_warning(self, monkeypatch, caplog):
        """A warning must be logged when a keyed source has no providers_data entry."""
        fake_sources = {"_keyed_274": _KeyedSource}
        monkeypatch.setattr("job_sources.SOURCES", fake_sources)

        with caplog.at_level(logging.WARNING, logger="ingest.sources"):
            make_enabled_sources(_providers({}), _BASE_CONFIG)

        assert any("_keyed_274" in rec.message for rec in caplog.records)

    def test_keyless_source_not_in_providers_data_is_still_enabled(self, monkeypatch):
        """Keyless sources with no entry must still default to enabled=True."""
        fake_sources = {"_keyless_274": _KeylessSource}
        monkeypatch.setattr("job_sources.SOURCES", fake_sources)

        result = make_enabled_sources(_providers({}), _BASE_CONFIG)

        assert len(result) == 1
        assert isinstance(result[0], _KeylessSource)

    def test_keyed_source_enabled_false_still_skipped(self, monkeypatch):
        """explicit enabled=False on keyed source must still skip it."""
        fake_sources = {"_keyed_274": _KeyedSource}
        monkeypatch.setattr("job_sources.SOURCES", fake_sources)

        providers = _providers({"_keyed_274": {"enabled": False, "api_key": "x"}})
        result = make_enabled_sources(providers, _BASE_CONFIG)

        assert result == []

    def test_keyed_source_enabled_true_with_credentials_is_included(self, monkeypatch):
        """Keyed source with enabled=True and all credentials must still be included."""
        fake_sources = {"_keyed_274": _KeyedSource}
        monkeypatch.setattr("job_sources.SOURCES", fake_sources)

        providers = _providers({"_keyed_274": {"enabled": True, "api_key": "secret"}})
        result = make_enabled_sources(providers, _BASE_CONFIG)

        assert len(result) == 1
        assert isinstance(result[0], _KeyedSource)

    def test_real_adzuna_not_in_providers_data_is_skipped(self, caplog):
        """Real AdzunaClient (keyed) must not activate if absent from providers_data."""
        AdzunaClient = SOURCES["adzuna"]

        with caplog.at_level(logging.WARNING, logger="ingest.sources"):
            result = make_enabled_sources(_providers({}), _BASE_CONFIG)

        types = [type(s) for s in result]
        assert AdzunaClient not in types


# ===========================================================================
# Issue #282 — _config_warnings() shows false "Adzuna not configured" warning
# ===========================================================================


class TestIssue282ConfigWarningsFalsePositive:
    """_config_warnings() must not warn about Adzuna when it is not explicitly enabled."""

    def test_no_warning_when_adzuna_not_in_providers_data(
        self, tmp_providers_path, tmp_keys_path, tmp_config_path, monkeypatch
    ):
        """No Adzuna entry at all → no warning (source is not expected to run)."""
        _write_providers(tmp_providers_path, data={
            "provider_order": [],
            "llm": {},
            "job_sources": {},  # no adzuna entry
        })
        monkeypatch.setattr(app_module, "_PROVIDERS_PATH", tmp_providers_path)

        from app import _config_warnings
        warnings = _config_warnings()

        assert warnings == [], f"Expected no warnings, got: {warnings}"

    def test_no_warning_when_adzuna_explicitly_disabled(
        self, tmp_providers_path, tmp_keys_path, tmp_config_path, monkeypatch
    ):
        """enabled=False with missing credentials → no warning."""
        _write_providers(tmp_providers_path, data={
            "provider_order": [],
            "llm": {},
            "job_sources": {
                "adzuna": {"enabled": False, "app_id": "", "app_key": ""},
            },
        })
        monkeypatch.setattr(app_module, "_PROVIDERS_PATH", tmp_providers_path)

        from app import _config_warnings
        warnings = _config_warnings()

        assert warnings == []

    def test_warning_shown_when_adzuna_enabled_but_unconfigured(
        self, tmp_providers_path, tmp_keys_path, tmp_config_path, monkeypatch
    ):
        """enabled=True with blank credentials → warning IS shown."""
        _write_providers(tmp_providers_path, data={
            "provider_order": [],
            "llm": {},
            "job_sources": {
                "adzuna": {"enabled": True, "app_id": "", "app_key": ""},
            },
        })
        monkeypatch.setattr(app_module, "_PROVIDERS_PATH", tmp_providers_path)

        from app import _config_warnings
        warnings = _config_warnings()

        assert len(warnings) == 1
        assert "Adzuna" in warnings[0]

    def test_no_warning_when_adzuna_enabled_and_configured(
        self, tmp_providers_path, tmp_keys_path, tmp_config_path, monkeypatch
    ):
        """enabled=True with valid credentials → no warning."""
        _write_providers(tmp_providers_path, data={
            "provider_order": [],
            "llm": {},
            "job_sources": {
                "adzuna": {"enabled": True, "app_id": "real-id", "app_key": "real-key"},
            },
        })
        monkeypatch.setattr(app_module, "_PROVIDERS_PATH", tmp_providers_path)

        from app import _config_warnings
        warnings = _config_warnings()

        assert warnings == []


# ===========================================================================
# Issue #283 — get_usage_stats() applies flat Haiku pricing to all providers
#
# The pricing logic lives in db._lookup_pricing() (pure Python, no DB needed).
# Tests assert the correct rates are returned for each known model.
# ===========================================================================


class TestIssue283PerModelPricing:
    """_lookup_pricing() must use per-model pricing, not flat Haiku rates."""

    def _cost(self, model_used: str, tokens_in: int, tokens_out: int) -> float | None:
        """Compute expected cost using _lookup_pricing directly."""
        pricing = _lookup_pricing(model_used)
        if pricing is None:
            return None
        in_rate, out_rate = pricing
        return tokens_in / 1_000_000 * in_rate + tokens_out / 1_000_000 * out_rate

    def test_anthropic_haiku_uses_correct_rates(self):
        """Haiku tokens are priced at $0.80/$4.00 per million (not flat default)."""
        cost = self._cost("anthropic/claude-haiku-4-5-20251001", 1_000_000, 1_000_000)
        # $0.80/M input + $4.00/M output = $4.80
        assert cost is not None
        assert abs(cost - 4.80) < 0.001

    def test_openai_gpt4o_mini_uses_correct_rates(self):
        """gpt-4o-mini tokens are priced at $0.15/$0.60 per million."""
        cost = self._cost("openai/gpt-4o-mini", 1_000_000, 1_000_000)
        # $0.15/M input + $0.60/M output = $0.75
        assert cost is not None
        assert abs(cost - 0.75) < 0.001

    def test_gemini_flash_uses_correct_rates(self):
        """gemini-1.5-flash tokens are priced at $0.075/$0.30 per million."""
        cost = self._cost("gemini/gemini-1.5-flash", 1_000_000, 1_000_000)
        # $0.075/M input + $0.30/M output = $0.375
        assert cost is not None
        assert abs(cost - 0.375) < 0.001

    def test_unknown_model_returns_none(self):
        """An unknown model_used value must return None."""
        assert _lookup_pricing("unknown-provider/unknown-model-xyz") is None

    def test_null_model_used_returns_none(self):
        """NULL model_used must return None."""
        assert _lookup_pricing(None) is None

    def test_gemini_costs_differ_from_haiku(self):
        """Gemini flash ($0.075/$0.30) is cheaper than Haiku ($0.80/$4.00).

        Before the fix, Gemini was priced at Haiku rates — costs would be equal.
        """
        cost_gemini = self._cost("gemini/gemini-1.5-flash", 1_000_000, 1_000_000)
        cost_haiku = self._cost("anthropic/claude-haiku-4-5-20251001", 1_000_000, 1_000_000)
        assert cost_gemini is not None
        assert cost_haiku is not None
        assert cost_gemini != cost_haiku
        assert cost_gemini < cost_haiku

    def test_lookup_pricing_unknown_provider_returns_none(self):
        """A future-provider/new-model string must return None."""
        assert _lookup_pricing("future-provider/new-model") is None


# ===========================================================================
# Issue #284 — save_providers() silently ignores blank strings
# ===========================================================================


class TestIssue284SaveProvidersBlankStringClears:
    """save_providers() must allow blank strings to clear stored credentials."""

    def test_blank_string_clears_api_key(self, tmp_path):
        """Before fix: blank string was silently dropped, leaving old value.

        After fix: blank string overwrites the existing credential with ''.
        """
        path = str(tmp_path / "providers.json")
        _write_providers(path, data={
            "provider_order": ["anthropic"],
            "llm": {"anthropic": {"api_key": "sk-to-be-cleared", "model": "claude-haiku-4-5-20251001"}},
            "job_sources": {},
        })

        save_providers({"llm": {"anthropic": {"api_key": ""}}}, providers_path=path)

        with open(path, encoding="utf-8") as fh:
            saved = json.load(fh)
        # Must be cleared, not the original value.
        assert saved["llm"]["anthropic"]["api_key"] == ""

    def test_blank_string_clears_job_source_credential(self, tmp_path):
        """Clearing a job source credential must persist the blank string."""
        path = str(tmp_path / "providers.json")
        _write_providers(path, data={
            "provider_order": [],
            "llm": {},
            "job_sources": {"adzuna": {"app_id": "real-id", "app_key": "real-key"}},
        })

        save_providers({"job_sources": {"adzuna": {"app_id": ""}}}, providers_path=path)

        with open(path, encoding="utf-8") as fh:
            saved = json.load(fh)
        assert saved["job_sources"]["adzuna"]["app_id"] == ""
        # app_key was not mentioned — must remain unchanged.
        assert saved["job_sources"]["adzuna"]["app_key"] == "real-key"

    def test_absent_key_not_touched(self, tmp_path):
        """A key completely absent from updates must remain in the file unchanged."""
        path = str(tmp_path / "providers.json")
        _write_providers(path, data={
            "provider_order": [],
            "llm": {"openai": {"api_key": "sk-openai", "model": "gpt-4o-mini"}},
            "job_sources": {},
        })

        # Only update anthropic — openai must be untouched.
        save_providers(
            {"llm": {"anthropic": {"api_key": "sk-ant", "model": "claude-haiku-4-5-20251001"}}},
            providers_path=path,
        )

        with open(path, encoding="utf-8") as fh:
            saved = json.load(fh)
        assert saved["llm"]["openai"]["api_key"] == "sk-openai"
        assert saved["llm"]["anthropic"]["api_key"] == "sk-ant"

    def test_settings_post_blank_field_clears_credential(
        self, client, tmp_providers_path, tmp_keys_path, tmp_config_path
    ):
        """POST /settings with a blank credential field must clear it in providers.json."""
        _write_providers(tmp_providers_path, data={
            "provider_order": ["anthropic"],
            "llm": {"anthropic": {"api_key": "sk-old", "model": "claude-haiku-4-5-20251001"}},
            "job_sources": {},
        })

        client.post("/settings", data={
            "anthropic__api_key": "",   # blank → clear
            "anthropic__model": "claude-haiku-4-5-20251001",
            "tab": "llm",
        })

        with open(tmp_providers_path, encoding="utf-8") as fh:
            saved = json.load(fh)
        assert saved["llm"]["anthropic"]["api_key"] == ""

    def test_boolean_false_still_persisted(self, tmp_path):
        """Boolean False values must still be written (they are not blank strings)."""
        path = str(tmp_path / "providers.json")
        _write_providers(path, data={
            "provider_order": [],
            "llm": {},
            "job_sources": {"adzuna": {"enabled": True, "app_id": "id", "app_key": "key"}},
        })

        save_providers({"job_sources": {"adzuna": {"enabled": False}}}, providers_path=path)

        with open(path, encoding="utf-8") as fh:
            saved = json.load(fh)
        assert saved["job_sources"]["adzuna"]["enabled"] is False
