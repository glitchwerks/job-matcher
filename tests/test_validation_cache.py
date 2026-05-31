"""tests/test_validation_cache.py — Unit tests for the mtime-keyed cache
on :func:`services.provider_schemas._get_search_validation_issues`.

Covered cases
-------------
* Cold call: first call reads from disk (validate_search_config invoked once).
* Warm call: second call with unchanged mtimes hits the cache (no second read).
* Cold-after-mutation: bumping a file's mtime busts the cache and triggers
  a fresh read.

No live database required — DB and credential calls are mocked out.
All tests use tmp_path for isolation and reset the lru_cache between runs.
"""

from __future__ import annotations

import json
import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import services.provider_schemas as _schemas_mod
from ingest import ValidationIssue
from services.provider_schemas import _get_search_validation_issues


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_providers(path: str) -> None:
    """Write a minimal valid providers.json to *path*."""
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"provider_order": [], "llm": {}, "job_sources": {}}, fh)


def _write_config(path: str) -> None:
    """Write a minimal valid config.json to *path*."""
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(
            {
                "search": {
                    "country": "us",
                    "what": "developer",
                    "where": "Miami",
                    "results_per_page": 50,
                    "max_pages": 3,
                },
                "scoring": {"threshold": 7.0},
            },
            fh,
        )


# ---------------------------------------------------------------------------
# Cache tests
# ---------------------------------------------------------------------------


class TestValidationIssuesCache:
    """Mtime-keyed cache on _get_search_validation_issues.

    Each test resets the lru_cache via _cached_validation.cache_clear() so
    tests do not interfere with each other.
    """

    @pytest.fixture(autouse=True)
    def _clear_cache(self):
        """Clear the lru_cache before and after every test in this class."""
        _schemas_mod._cached_validation.cache_clear()
        yield
        _schemas_mod._cached_validation.cache_clear()

    def test_cold_call_reads_from_disk(self, tmp_path):
        """First call invokes validate_search_config exactly once."""
        providers_path = str(tmp_path / "providers.json")
        config_path = str(tmp_path / "config.json")
        _write_providers(providers_path)
        _write_config(config_path)

        fake_issue = ValidationIssue(
            source_key="adzuna", missing_fields=["country"]
        )

        with patch.object(
            _schemas_mod, "validate_search_config", return_value=[fake_issue]
        ) as mock_validate:
            result = _get_search_validation_issues(
                providers_path=providers_path,
                config_path=config_path,
            )

        assert mock_validate.call_count == 1, (
            "Expected validate_search_config to be called once on a cold call"
        )
        assert result == [fake_issue]

    def test_warm_call_does_not_re_read(self, tmp_path):
        """Second call with unchanged file mtimes returns cached result."""
        providers_path = str(tmp_path / "providers.json")
        config_path = str(tmp_path / "config.json")
        _write_providers(providers_path)
        _write_config(config_path)

        fake_issue = ValidationIssue(
            source_key="adzuna", missing_fields=["country"]
        )

        with patch.object(
            _schemas_mod, "validate_search_config", return_value=[fake_issue]
        ) as mock_validate:
            result_first = _get_search_validation_issues(
                providers_path=providers_path,
                config_path=config_path,
            )
            result_second = _get_search_validation_issues(
                providers_path=providers_path,
                config_path=config_path,
            )

        assert mock_validate.call_count == 1, (
            "Expected validate_search_config to be called only once across "
            "two calls with unchanged file mtimes (cache miss prevented second read)"
        )
        assert result_first == result_second

    def test_cold_after_mtime_mutation_re_reads(self, tmp_path):
        """Bumping a config file's mtime busts the cache and triggers a re-read."""
        providers_path = str(tmp_path / "providers.json")
        config_path = str(tmp_path / "config.json")
        _write_providers(providers_path)
        _write_config(config_path)

        first_issue = ValidationIssue(
            source_key="adzuna", missing_fields=["country"]
        )
        second_issue = ValidationIssue(
            source_key="adzuna", missing_fields=["what"]
        )

        with patch.object(
            _schemas_mod,
            "validate_search_config",
            side_effect=[[first_issue], [second_issue]],
        ) as mock_validate:
            result_first = _get_search_validation_issues(
                providers_path=providers_path,
                config_path=config_path,
            )

            # Advance the config file's mtime by 1 second to simulate a
            # /settings POST rewrite or out-of-process edit.
            stat = os.stat(config_path)
            new_atime = stat.st_atime + 1.0
            new_mtime = stat.st_mtime + 1.0
            os.utime(config_path, (new_atime, new_mtime))

            result_second = _get_search_validation_issues(
                providers_path=providers_path,
                config_path=config_path,
            )

        assert mock_validate.call_count == 2, (
            "Expected validate_search_config to be called twice: once for the "
            "cold call, once after the mtime bump busted the cache"
        )
        assert result_first == [first_issue]
        assert result_second == [second_issue]


class TestGetSearchValidationIssuesSafetyNet:
    """_get_search_validation_issues() must not propagate any exception.

    The function is called from GET /settings and GET /api/ingest/preflight.
    Any uncaught exception from _cached_validation would produce an HTTP 500.
    This class verifies the outer wrapper catches unexpected errors gracefully.

    Regression test for issue #757: test_settings_page_renders failed with
    HTTP 500 when _cached_validation was called without a tmp_config_path
    fixture isolating _CONFIG_PATH, leaving an opportunity for an unexpected
    exception (e.g. filelock.Timeout) to escape to the Flask route.
    """

    @pytest.fixture(autouse=True)
    def _clear_cache(self):
        _schemas_mod._cached_validation.cache_clear()
        yield
        _schemas_mod._cached_validation.cache_clear()

    def test_returns_empty_list_when_cached_validation_raises(self, tmp_path):
        """Any exception from _cached_validation is caught; empty list returned."""
        providers_path = str(tmp_path / "providers.json")
        config_path = str(tmp_path / "config.json")
        _write_providers(providers_path)
        _write_config(config_path)

        def _exploding_validate(*_args, **_kwargs):
            raise RuntimeError("simulated filelock.Timeout or other I/O error")

        with patch.object(
            _schemas_mod, "_cached_validation", side_effect=_exploding_validate
        ):
            result = _get_search_validation_issues(
                providers_path=providers_path,
                config_path=config_path,
            )

        assert result == [], (
            "_get_search_validation_issues must return [] when _cached_validation raises, "
            "never propagate the exception to the Flask route"
        )

    def test_settings_page_renders_when_config_path_not_isolated(self, tmp_path, monkeypatch):
        """GET /settings returns 200 even when _CONFIG_PATH is not overridden in a test.

        This is the regression scenario from issue #757: TestSettingsPageRenders
        used tmp_providers_path and tmp_keys_path but lacked tmp_config_path.
        The real _CONFIG_PATH (which may or may not exist) was used alongside
        the temp providers path, creating a mixed-path cache key.  Any
        exception from that combination must not bubble up as a 500.
        """
        import web.settings as _settings_module
        import services.profile_store as _profile_store_module
        from app import app as flask_app

        flask_app.config["TESTING"] = True
        providers_path = str(tmp_path / "providers.json")
        keys_path = str(tmp_path / "keys.json")
        # Intentionally do NOT create a config.json at config_path.
        missing_config = str(tmp_path / "config.json")

        monkeypatch.setattr(_profile_store_module, "_PROVIDERS_PATH", providers_path)
        monkeypatch.setattr(_profile_store_module, "_KEYS_PATH", keys_path)
        monkeypatch.setattr(_profile_store_module, "_CONFIG_PATH", missing_config)
        monkeypatch.setattr(_settings_module, "_PROVIDERS_PATH", providers_path)
        monkeypatch.setattr(_settings_module, "_KEYS_PATH", keys_path)
        monkeypatch.setattr(_settings_module, "_CONFIG_PATH", missing_config)

        with flask_app.test_client() as c:
            resp = c.get("/settings")

        assert resp.status_code == 200, (
            "GET /settings must return 200 even when config.json is absent and "
            "the config path is not isolated by tmp_config_path"
        )
