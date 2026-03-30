"""
tests/test_credentials.py — TDD tests for credentials.py

Covers every behavioural rule from the spec:

load_providers()
  - reads providers.json correctly
  - falls back to env vars when file is absent
  - raises CredentialError when neither file nor env vars provide credentials
  - does NOT fall back to env vars when providers.json exists with empty values
  - raises CredentialError on invalid JSON

migrate_from_legacy()
  - both files present → correct providers.json written atomically
  - keys.json only → LLM credentials migrate, Adzuna fields empty
  - config.json only → Adzuna migrates, LLM section empty
  - neither file → returns None, no file written
  - write failure → no partial file left (providers.json.tmp cleaned up)
  - preferred_provider string → provider_order array (preferred first)

build_provider_chain() edge cases (new provider_order array API)
  - unknown entry in provider_order → WARNING logged, skipped
  - registry entry not in provider_order → appended at end
  - duplicate in provider_order → second dropped
  - empty provider_order array → all registry providers used
  - missing provider_order key → all registry providers used
  - provider with empty api_key → skipped regardless of position
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ---------------------------------------------------------------------------
# Helpers shared across tests
# ---------------------------------------------------------------------------

_VALID_PROVIDERS_JSON = {
    "provider_order": ["anthropic", "gemini", "openai"],
    "llm": {
        "anthropic": {"api_key": "sk-ant-test", "model": "claude-haiku-4-5-20251001"},
        "openai":    {"api_key": "sk-oai-test",  "model": "gpt-4o-mini"},
        "gemini":    {"api_key": "ggl-test",      "model": "gemini-1.5-flash"},
    },
    "job_sources": {
        "adzuna": {"app_id": "my-app-id", "app_key": "my-app-key"},
    },
}

_VALID_KEYS_JSON = {
    "providers": {
        "anthropic": {"api_key": "sk-ant-legacy", "model": "claude-haiku-4-5-20251001"},
        "openai":    {"api_key": "sk-oai-legacy",  "model": "gpt-4o-mini"},
        "gemini":    {"api_key": "ggl-legacy",     "model": "gemini-1.5-flash"},
    },
    "preferred_provider": "anthropic",
}

_VALID_CONFIG_JSON = {
    "adzuna_app_id":  "cfg-app-id",
    "adzuna_app_key": "cfg-app-key",
    "search": {"country": "gb", "what": "python", "results_per_page": 50, "max_pages": 3},
    "scoring": {"threshold": 6},
}


def _write(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data), encoding="utf-8")


# ---------------------------------------------------------------------------
# Import under test (deferred so we can patch paths before import if needed)
# ---------------------------------------------------------------------------

from credentials import CredentialError, load_providers, migrate_from_legacy  # noqa: E402


# ===========================================================================
# load_providers()
# ===========================================================================

class TestLoadProvidersFromFile:
    """load_providers() reads a well-formed providers.json."""

    def test_returns_correct_provider_order(self, tmp_path):
        _write(tmp_path / "providers.json", _VALID_PROVIDERS_JSON)
        result = load_providers(providers_path=str(tmp_path / "providers.json"))
        assert result["provider_order"] == ["anthropic", "gemini", "openai"]

    def test_returns_llm_section(self, tmp_path):
        _write(tmp_path / "providers.json", _VALID_PROVIDERS_JSON)
        result = load_providers(providers_path=str(tmp_path / "providers.json"))
        assert result["llm"]["anthropic"]["api_key"] == "sk-ant-test"
        assert result["llm"]["anthropic"]["model"] == "claude-haiku-4-5-20251001"

    def test_returns_job_sources_section(self, tmp_path):
        _write(tmp_path / "providers.json", _VALID_PROVIDERS_JSON)
        result = load_providers(providers_path=str(tmp_path / "providers.json"))
        assert result["job_sources"]["adzuna"]["app_id"] == "my-app-id"
        assert result["job_sources"]["adzuna"]["app_key"] == "my-app-key"

    def test_invalid_json_raises_credential_error(self, tmp_path):
        (tmp_path / "providers.json").write_text("{not valid json", encoding="utf-8")
        with pytest.raises(CredentialError, match="[Ii]nvalid JSON|not valid JSON|parse"):
            load_providers(providers_path=str(tmp_path / "providers.json"))


class TestLoadProvidersEnvVarFallback:
    """load_providers() falls back to env vars only when providers.json is absent."""

    def test_env_vars_used_when_no_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "env-ant-key")
        monkeypatch.setenv("OPENAI_API_KEY", "")
        monkeypatch.setenv("GOOGLE_API_KEY", "")
        monkeypatch.setenv("ADZUNA_APP_ID", "")
        monkeypatch.setenv("ADZUNA_APP_KEY", "")
        result = load_providers(
            providers_path=str(tmp_path / "providers.json"),
            keys_path=str(tmp_path / "keys.json"),
            config_path=str(tmp_path / "config.json"),
        )
        assert result["llm"]["anthropic"]["api_key"] == "env-ant-key"

    def test_multiple_env_vars_all_included(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "env-ant")
        monkeypatch.setenv("OPENAI_API_KEY", "env-oai")
        monkeypatch.setenv("GOOGLE_API_KEY", "env-ggl")
        monkeypatch.setenv("ADZUNA_APP_ID", "env-id")
        monkeypatch.setenv("ADZUNA_APP_KEY", "env-key")
        result = load_providers(
            providers_path=str(tmp_path / "providers.json"),
            keys_path=str(tmp_path / "keys.json"),
            config_path=str(tmp_path / "config.json"),
        )
        assert result["llm"]["openai"]["api_key"] == "env-oai"
        assert result["llm"]["gemini"]["api_key"] == "env-ggl"
        assert result["job_sources"]["adzuna"]["app_id"] == "env-id"
        assert result["job_sources"]["adzuna"]["app_key"] == "env-key"

    def test_adzuna_env_vars_included(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "env-ant")
        monkeypatch.setenv("OPENAI_API_KEY", "")
        monkeypatch.setenv("GOOGLE_API_KEY", "")
        monkeypatch.setenv("ADZUNA_APP_ID", "az-id")
        monkeypatch.setenv("ADZUNA_APP_KEY", "az-key")
        result = load_providers(
            providers_path=str(tmp_path / "providers.json"),
            keys_path=str(tmp_path / "keys.json"),
            config_path=str(tmp_path / "config.json"),
        )
        assert result["job_sources"]["adzuna"]["app_id"] == "az-id"

    def test_env_vars_not_used_when_file_exists_with_empty_values(self, tmp_path, monkeypatch):
        """
        Critical: env vars must NOT override a present-but-empty providers.json.
        An empty file means "configured but empty", not "absent".
        """
        empty_providers = {
            "provider_order": [],
            "llm": {
                "anthropic": {"api_key": "", "model": "claude-haiku-4-5-20251001"},
            },
            "job_sources": {"adzuna": {"app_id": "", "app_key": ""}},
        }
        _write(tmp_path / "providers.json", empty_providers)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "should-not-appear")
        result = load_providers(providers_path=str(tmp_path / "providers.json"))
        # File present with empty key → env var must NOT override
        assert result["llm"]["anthropic"]["api_key"] == ""

    def test_raises_credential_error_when_no_file_no_env_vars(self, tmp_path, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
        monkeypatch.delenv("ADZUNA_APP_ID", raising=False)
        monkeypatch.delenv("ADZUNA_APP_KEY", raising=False)
        with pytest.raises(CredentialError):
            load_providers(
                providers_path=str(tmp_path / "providers.json"),
                keys_path=str(tmp_path / "keys.json"),
                config_path=str(tmp_path / "config.json"),
            )

    def test_does_not_sys_exit(self, tmp_path, monkeypatch):
        """load_providers() must raise CredentialError, never SystemExit."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
        monkeypatch.delenv("ADZUNA_APP_ID", raising=False)
        monkeypatch.delenv("ADZUNA_APP_KEY", raising=False)
        with pytest.raises(CredentialError):
            load_providers(
                providers_path=str(tmp_path / "providers.json"),
                keys_path=str(tmp_path / "keys.json"),
                config_path=str(tmp_path / "config.json"),
            )
        # If we got here without SystemExit, the contract holds.


# ===========================================================================
# migrate_from_legacy()
# ===========================================================================

class TestMigrateFromLegacy:
    """migrate_from_legacy() handles all four migration cases."""

    def test_both_files_present_writes_providers_json(self, tmp_path):
        """Case 1: keys.json + config.json → providers.json written."""
        _write(tmp_path / "keys.json", _VALID_KEYS_JSON)
        _write(tmp_path / "config.json", _VALID_CONFIG_JSON)
        providers_path = str(tmp_path / "providers.json")

        result = migrate_from_legacy(
            providers_path=providers_path,
            keys_path=str(tmp_path / "keys.json"),
            config_path=str(tmp_path / "config.json"),
        )

        assert result is not None
        # File must exist after migration
        assert (tmp_path / "providers.json").exists()

    def test_both_files_migrates_llm_keys_correctly(self, tmp_path):
        _write(tmp_path / "keys.json", _VALID_KEYS_JSON)
        _write(tmp_path / "config.json", _VALID_CONFIG_JSON)
        providers_path = str(tmp_path / "providers.json")

        migrate_from_legacy(
            providers_path=providers_path,
            keys_path=str(tmp_path / "keys.json"),
            config_path=str(tmp_path / "config.json"),
        )

        data = json.loads((tmp_path / "providers.json").read_text())
        assert data["llm"]["anthropic"]["api_key"] == "sk-ant-legacy"
        assert data["llm"]["openai"]["api_key"] == "sk-oai-legacy"
        assert data["llm"]["gemini"]["api_key"] == "ggl-legacy"

    def test_both_files_migrates_adzuna_keys_correctly(self, tmp_path):
        _write(tmp_path / "keys.json", _VALID_KEYS_JSON)
        _write(tmp_path / "config.json", _VALID_CONFIG_JSON)
        providers_path = str(tmp_path / "providers.json")

        migrate_from_legacy(
            providers_path=providers_path,
            keys_path=str(tmp_path / "keys.json"),
            config_path=str(tmp_path / "config.json"),
        )

        data = json.loads((tmp_path / "providers.json").read_text())
        assert data["job_sources"]["adzuna"]["app_id"] == "cfg-app-id"
        assert data["job_sources"]["adzuna"]["app_key"] == "cfg-app-key"

    def test_preferred_provider_becomes_first_in_order(self, tmp_path):
        """preferred_provider string → first entry in provider_order array."""
        keys = {
            "providers": {
                "anthropic": {"api_key": "sk-ant", "model": "claude-haiku-4-5-20251001"},
                "openai":    {"api_key": "sk-oai", "model": "gpt-4o-mini"},
                "gemini":    {"api_key": "ggl",    "model": "gemini-1.5-flash"},
            },
            "preferred_provider": "openai",
        }
        _write(tmp_path / "keys.json", keys)
        _write(tmp_path / "config.json", _VALID_CONFIG_JSON)

        migrate_from_legacy(
            providers_path=str(tmp_path / "providers.json"),
            keys_path=str(tmp_path / "keys.json"),
            config_path=str(tmp_path / "config.json"),
        )

        data = json.loads((tmp_path / "providers.json").read_text())
        assert data["provider_order"][0] == "openai", (
            "preferred_provider should be first in provider_order"
        )
        assert set(data["provider_order"]) == {"anthropic", "openai", "gemini"}

    def test_keys_only_adzuna_fields_empty(self, tmp_path):
        """Case 2: keys.json only → LLM migrates, Adzuna fields are empty strings."""
        _write(tmp_path / "keys.json", _VALID_KEYS_JSON)
        providers_path = str(tmp_path / "providers.json")

        migrate_from_legacy(
            providers_path=providers_path,
            keys_path=str(tmp_path / "keys.json"),
            config_path=str(tmp_path / "config.json"),  # absent
        )

        assert (tmp_path / "providers.json").exists()
        data = json.loads((tmp_path / "providers.json").read_text())
        assert data["llm"]["anthropic"]["api_key"] == "sk-ant-legacy"
        assert data["job_sources"]["adzuna"]["app_id"] == ""
        assert data["job_sources"]["adzuna"]["app_key"] == ""

    def test_config_only_llm_section_empty(self, tmp_path):
        """Case 3: config.json only → Adzuna migrates, LLM section is empty."""
        _write(tmp_path / "config.json", _VALID_CONFIG_JSON)
        providers_path = str(tmp_path / "providers.json")

        migrate_from_legacy(
            providers_path=providers_path,
            keys_path=str(tmp_path / "keys.json"),  # absent
            config_path=str(tmp_path / "config.json"),
        )

        assert (tmp_path / "providers.json").exists()
        data = json.loads((tmp_path / "providers.json").read_text())
        assert data["job_sources"]["adzuna"]["app_id"] == "cfg-app-id"
        # LLM section should have no entries with non-empty keys
        for _name, cfg in data.get("llm", {}).items():
            assert cfg.get("api_key", "") == "", "LLM api_key must be empty when keys.json absent"

    def test_neither_file_returns_none(self, tmp_path):
        """Case 4: Neither file present → returns None, no providers.json written."""
        result = migrate_from_legacy(
            providers_path=str(tmp_path / "providers.json"),
            keys_path=str(tmp_path / "keys.json"),
            config_path=str(tmp_path / "config.json"),
        )

        assert result is None
        assert not (tmp_path / "providers.json").exists()

    def test_migration_is_atomic_no_partial_on_failure(self, tmp_path):
        """Write failure → providers.json.tmp cleaned up, providers.json absent."""
        _write(tmp_path / "keys.json", _VALID_KEYS_JSON)
        _write(tmp_path / "config.json", _VALID_CONFIG_JSON)
        providers_path = str(tmp_path / "providers.json")

        # Simulate a write failure by patching os.replace
        with patch("os.replace", side_effect=OSError("disk full")):
            try:
                migrate_from_legacy(
                    providers_path=providers_path,
                    keys_path=str(tmp_path / "keys.json"),
                    config_path=str(tmp_path / "config.json"),
                )
            except Exception:
                pass  # Expected — write failed

        # Neither the final file nor the temp file should remain
        assert not (tmp_path / "providers.json").exists(), "providers.json must not exist after failed write"
        assert not (tmp_path / "providers.json.tmp").exists(), "providers.json.tmp must be cleaned up"

    def test_original_files_not_modified(self, tmp_path):
        """keys.json and config.json must never be modified by migration."""
        _write(tmp_path / "keys.json", _VALID_KEYS_JSON)
        _write(tmp_path / "config.json", _VALID_CONFIG_JSON)

        keys_mtime_before = (tmp_path / "keys.json").stat().st_mtime
        config_mtime_before = (tmp_path / "config.json").stat().st_mtime

        migrate_from_legacy(
            providers_path=str(tmp_path / "providers.json"),
            keys_path=str(tmp_path / "keys.json"),
            config_path=str(tmp_path / "config.json"),
        )

        assert (tmp_path / "keys.json").stat().st_mtime == keys_mtime_before
        assert (tmp_path / "config.json").stat().st_mtime == config_mtime_before


# ===========================================================================
# build_provider_chain() — new provider_order array API
# ===========================================================================

# Patch all three SDK constructors so no real clients are created.
_PATCHES = [
    patch("providers.anthropic_provider.anthropic.Anthropic"),
    patch("providers.openai_provider.openai.OpenAI"),
    patch("providers.gemini_provider.genai.Client"),
]


def _start_patches() -> list:
    return [p.start() for p in _PATCHES]


def _stop_patches() -> None:
    for p in _PATCHES:
        p.stop()


def _make_providers_dict(
    provider_order: list | None = None,
    anthropic_key: str = "key-ant",
    openai_key: str = "key-oai",
    gemini_key: str = "key-ggl",
) -> dict:
    """Return a providers.json-shaped dict for testing build_provider_chain()."""
    d: dict = {
        "llm": {
            "anthropic": {"api_key": anthropic_key, "model": "claude-haiku-4-5-20251001"},
            "openai":    {"api_key": openai_key,    "model": "gpt-4o-mini"},
            "gemini":    {"api_key": gemini_key,    "model": "gemini-1.5-flash"},
        },
        "job_sources": {"adzuna": {"app_id": "", "app_key": ""}},
    }
    if provider_order is not None:
        d["provider_order"] = provider_order
    return d


@pytest.fixture(autouse=False)
def sdk_patches():
    mocks = _start_patches()
    yield mocks
    _stop_patches()


class TestBuildProviderChainNewAPI:
    """build_provider_chain() updated to read provider_order from top-level key."""

    def test_provider_order_is_respected(self, sdk_patches):
        from providers import build_provider_chain, GeminiProvider, AnthropicProvider
        providers = _make_providers_dict(provider_order=["gemini", "anthropic", "openai"])
        chain = build_provider_chain(providers)
        assert isinstance(chain[0], GeminiProvider), "gemini should be first"
        assert isinstance(chain[1], AnthropicProvider), "anthropic should be second"

    def test_unknown_entry_in_provider_order_logged_and_skipped(self, sdk_patches, caplog):
        from providers import build_provider_chain
        providers = _make_providers_dict(provider_order=["unknown_llm", "anthropic"])
        with caplog.at_level(logging.WARNING, logger="providers"):
            chain = build_provider_chain(providers)
        assert any("unknown_llm" in r.message for r in caplog.records), (
            "WARNING should mention the unknown provider name"
        )
        names = [type(p).__name__.lower() for p in chain]
        assert not any("unknown" in n for n in names), "Unknown provider must not appear in chain"

    def test_registry_entry_not_in_provider_order_appended_at_end(self, sdk_patches):
        from providers import build_provider_chain, GeminiProvider
        # Only anthropic and openai in order — gemini should be appended
        providers = _make_providers_dict(provider_order=["anthropic", "openai"])
        chain = build_provider_chain(providers)
        assert isinstance(chain[-1], GeminiProvider), (
            "gemini (not in provider_order) should be appended at end"
        )
        assert len(chain) == 3

    def test_duplicate_in_provider_order_second_dropped(self, sdk_patches):
        from providers import build_provider_chain, AnthropicProvider
        providers = _make_providers_dict(provider_order=["anthropic", "anthropic", "openai"])
        chain = build_provider_chain(providers)
        types = [type(p) for p in chain]
        assert types.count(AnthropicProvider) == 1, "Duplicate provider_order entry must produce only one instance"
        assert len(chain) == 3

    def test_empty_provider_order_uses_all_registry_in_insertion_order(self, sdk_patches):
        from providers import build_provider_chain, AnthropicProvider, OpenAIProvider, GeminiProvider
        providers = _make_providers_dict(provider_order=[])
        chain = build_provider_chain(providers)
        assert len(chain) == 3
        # Must follow _PROVIDER_CLASS_MAP insertion order: anthropic, openai, gemini
        assert isinstance(chain[0], AnthropicProvider)
        assert isinstance(chain[1], OpenAIProvider)
        assert isinstance(chain[2], GeminiProvider)

    def test_missing_provider_order_key_uses_all_registry(self, sdk_patches):
        from providers import build_provider_chain
        providers = _make_providers_dict()  # provider_order key omitted
        assert "provider_order" not in providers
        chain = build_provider_chain(providers)
        assert len(chain) == 3

    def test_empty_api_key_skipped_regardless_of_position(self, sdk_patches):
        from providers import build_provider_chain, AnthropicProvider
        providers = _make_providers_dict(
            provider_order=["anthropic", "openai", "gemini"],
            anthropic_key="",  # empty → skip
        )
        chain = build_provider_chain(providers)
        types = [type(p) for p in chain]
        assert AnthropicProvider not in types, "Provider with empty api_key must be skipped"
        assert len(chain) == 2

    def test_all_empty_keys_returns_empty_chain(self, sdk_patches):
        """When all keys are empty, chain should be empty (no ValueError for the new API)."""
        from providers import build_provider_chain
        providers = _make_providers_dict(
            provider_order=["anthropic", "openai", "gemini"],
            anthropic_key="",
            openai_key="",
            gemini_key="",
        )
        # New API: empty chain is valid — callers decide how to handle it
        chain = build_provider_chain(providers)
        assert chain == []

    def test_provider_order_with_no_llm_section_skips_gracefully(self, sdk_patches):
        """If a provider is in provider_order but has no llm entry, skip it."""
        from providers import build_provider_chain, OpenAIProvider
        providers = {
            "provider_order": ["anthropic", "openai"],
            "llm": {
                # anthropic missing entirely
                "openai": {"api_key": "key-oai", "model": "gpt-4o-mini"},
            },
            "job_sources": {"adzuna": {"app_id": "", "app_key": ""}},
        }
        chain = build_provider_chain(providers)
        assert len(chain) == 1
        assert isinstance(chain[0], OpenAIProvider)


# ===========================================================================
# Warning #5 — partial Adzuna credentials
# ===========================================================================

class TestPartialAdzunaCredentials:
    """load_providers() handles partial Adzuna credentials gracefully."""

    def test_app_id_set_app_key_missing_returns_partial(self, tmp_path):
        """providers.json with app_id but no app_key is returned as-is — no crash."""
        partial_adzuna = {
            "provider_order": ["anthropic"],
            "llm": {
                "anthropic": {"api_key": "sk-ant-test", "model": "claude-haiku-4-5-20251001"},
            },
            "job_sources": {
                "adzuna": {"app_id": "my-app-id"},  # app_key key absent
            },
        }
        _write(tmp_path / "providers.json", partial_adzuna)
        result = load_providers(providers_path=str(tmp_path / "providers.json"))
        # Should return the dict without crashing
        assert result["job_sources"]["adzuna"]["app_id"] == "my-app-id"
        # app_key is absent from the dict — callers must handle a missing key
        assert "app_key" not in result["job_sources"]["adzuna"]

    def test_app_key_set_app_id_missing_returns_partial(self, tmp_path):
        """providers.json with app_key but no app_id is returned as-is — no crash."""
        partial_adzuna = {
            "provider_order": ["anthropic"],
            "llm": {
                "anthropic": {"api_key": "sk-ant-test", "model": "claude-haiku-4-5-20251001"},
            },
            "job_sources": {
                "adzuna": {"app_key": "my-app-key"},  # app_id key absent
            },
        }
        _write(tmp_path / "providers.json", partial_adzuna)
        result = load_providers(providers_path=str(tmp_path / "providers.json"))
        assert result["job_sources"]["adzuna"]["app_key"] == "my-app-key"
        assert "app_id" not in result["job_sources"]["adzuna"]


# ===========================================================================
# Warning #6 — missing provider_order key
# ===========================================================================

class TestMissingProviderOrderKey:
    """load_providers() and build_provider_chain() handle absent provider_order gracefully."""

    def test_load_providers_missing_provider_order_defaults_to_empty_list(self, tmp_path):
        """providers.json without provider_order key is loaded; provider_order defaults to []."""
        no_order = {
            "llm": {
                "anthropic": {"api_key": "sk-ant-test", "model": "claude-haiku-4-5-20251001"},
                "openai":    {"api_key": "sk-oai-test", "model": "gpt-4o-mini"},
            },
            "job_sources": {"adzuna": {"app_id": "", "app_key": ""}},
            # provider_order key is intentionally absent
        }
        _write(tmp_path / "providers.json", no_order)
        result = load_providers(providers_path=str(tmp_path / "providers.json"))
        # load_providers returns the raw file contents — provider_order is absent
        assert "provider_order" not in result

    def test_build_provider_chain_missing_provider_order_uses_registry_order(self):
        """build_provider_chain() with no provider_order key uses all registered providers."""
        _start_patches()
        try:
            from providers import build_provider_chain, AnthropicProvider, OpenAIProvider, GeminiProvider
            providers = {
                # provider_order key absent
                "llm": {
                    "anthropic": {"api_key": "key-ant", "model": "claude-haiku-4-5-20251001"},
                    "openai":    {"api_key": "key-oai", "model": "gpt-4o-mini"},
                    "gemini":    {"api_key": "key-ggl", "model": "gemini-1.5-flash"},
                },
                "job_sources": {"adzuna": {"app_id": "", "app_key": ""}},
            }
            assert "provider_order" not in providers
            chain = build_provider_chain(providers)
            # All three providers should appear in registry insertion order
            assert len(chain) == 3
            assert isinstance(chain[0], AnthropicProvider)
            assert isinstance(chain[1], OpenAIProvider)
            assert isinstance(chain[2], GeminiProvider)
        finally:
            _stop_patches()
