"""
tests/test_ingest_error_handling.py — Unit tests for error handling in ingest.run() and
ingest.rescore() (issue #241).

These tests verify that:
  - Missing/invalid config.json is logged via logger.error before the process exits.
  - Missing/invalid profile.json is logged via logger.error before the process exits.
  - db.init_db() failure is caught, logged, and exits with code 1.
  - db.create_ingest_run() failure is caught and logged as a warning; the run continues.
  - A finish_ingest_run() failure inside the outer except handler does not mask the
    original pipeline exception.
  - rescore() load_profile() failure is logged before re-raising.
  - rescore() db.get_all_scored() failure is caught, logged, and exits with code 1.

No live database required — all DB and credential calls are mocked out.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from unittest.mock import MagicMock, patch, call

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import ingest  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_json(path, data):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh)


def _make_config(tmp_path):
    cfg = {
        "adzuna_app_id": "x",
        "adzuna_app_key": "y",
        "search": {
            "country": "gb",
            "what": "python developer",
            "results_per_page": 10,
            "max_pages": 1,
        },
        "scoring": {"threshold": 6.0},
    }
    p = str(tmp_path / "config.json")
    _write_json(p, cfg)
    return p


def _make_profile(tmp_path):
    profile = {
        "primary_skills": ["Python"],
        "anti_preferences": [],
        "seniority": "senior",
        "preferred_industries": [],
        "location": {"geocode_fallback": "pass"},
        "scoring_notes": "",
    }
    p = str(tmp_path / "profile.json")
    _write_json(p, profile)
    return p


def _make_providers(tmp_path):
    providers = {
        "providers": {
            "anthropic": {
                "api_key": "sk-test",
                "model": "claude-haiku-4-5-20251001",
            }
        },
        "preferred_provider": "anthropic",
    }
    p = str(tmp_path / "providers.json")
    _write_json(p, providers)
    return p


# ---------------------------------------------------------------------------
# Gap 1 — load_config() raises SystemExit → must appear in log
# ---------------------------------------------------------------------------

class TestMissingConfigIsLogged:
    """load_config() raises SystemExit when config.json is missing; run() must
    log the error before re-raising so it appears in the log file."""

    def test_missing_config_logs_error_and_exits(self, tmp_path, caplog):
        missing_config = str(tmp_path / "no_config.json")
        profile_path = _make_profile(tmp_path)

        with caplog.at_level(logging.ERROR, logger="ingest"):
            with pytest.raises(SystemExit):
                ingest.run(
                    config_path=missing_config,
                    profile_path=profile_path,
                )

        assert any(
            "Startup error" in r.message or "not found" in r.message.lower()
            for r in caplog.records
            if r.levelno >= logging.ERROR
        ), f"Expected an ERROR log for missing config, got:\n{caplog.text}"

    def test_invalid_config_json_logs_error_and_exits(self, tmp_path, caplog):
        bad_config = str(tmp_path / "bad_config.json")
        with open(bad_config, "w") as fh:
            fh.write("{ not valid json }")
        profile_path = _make_profile(tmp_path)

        with caplog.at_level(logging.ERROR, logger="ingest"):
            with pytest.raises(SystemExit):
                ingest.run(
                    config_path=bad_config,
                    profile_path=profile_path,
                )

        assert any(
            "Startup error" in r.message
            for r in caplog.records
            if r.levelno >= logging.ERROR
        ), f"Expected an ERROR log for bad config JSON, got:\n{caplog.text}"


# ---------------------------------------------------------------------------
# Gap 1 — load_profile() raises SystemExit → must appear in log
# ---------------------------------------------------------------------------

class TestMissingProfileIsLogged:
    """load_profile() raises SystemExit when profile.json is missing; run() must
    log the error before re-raising so it appears in the log file."""

    def test_missing_profile_logs_error_and_exits(self, tmp_path, caplog):
        config_path = _make_config(tmp_path)
        missing_profile = str(tmp_path / "no_profile.json")

        with caplog.at_level(logging.ERROR, logger="ingest"):
            with pytest.raises(SystemExit):
                ingest.run(
                    config_path=config_path,
                    profile_path=missing_profile,
                )

        assert any(
            "Startup error" in r.message
            for r in caplog.records
            if r.levelno >= logging.ERROR
        ), f"Expected an ERROR log for missing profile, got:\n{caplog.text}"

    def test_invalid_profile_json_logs_error_and_exits(self, tmp_path, caplog):
        config_path = _make_config(tmp_path)
        bad_profile = str(tmp_path / "bad_profile.json")
        with open(bad_profile, "w") as fh:
            fh.write("not json at all")

        with caplog.at_level(logging.ERROR, logger="ingest"):
            with pytest.raises(SystemExit):
                ingest.run(
                    config_path=config_path,
                    profile_path=bad_profile,
                )

        assert any(
            "Startup error" in r.message
            for r in caplog.records
            if r.levelno >= logging.ERROR
        ), f"Expected an ERROR log for bad profile JSON, got:\n{caplog.text}"


# ---------------------------------------------------------------------------
# Gap 2 — db.init_db() failure is caught and logged
# ---------------------------------------------------------------------------

class TestInitDbFailureIsLogged:
    """When db.init_db() raises (e.g. DB unreachable), run() must log the error
    and call sys.exit(1) rather than crashing with an unhandled traceback."""

    def test_init_db_failure_logs_error_and_calls_sys_exit(self, tmp_path, caplog):
        config_path = _make_config(tmp_path)
        profile_path = _make_profile(tmp_path)

        import psycopg2
        db_error = psycopg2.OperationalError("could not connect to server")

        with (
            patch("ingest.db.init_db", side_effect=db_error),
            caplog.at_level(logging.ERROR, logger="ingest"),
            pytest.raises(SystemExit) as exc_info,
        ):
            ingest.run(
                config_path=config_path,
                profile_path=profile_path,
            )

        assert exc_info.value.code == 1
        assert any(
            "Database initialisation failed" in r.message
            for r in caplog.records
            if r.levelno >= logging.ERROR
        ), f"Expected 'Database initialisation failed' ERROR log, got:\n{caplog.text}"


# ---------------------------------------------------------------------------
# Gap 3 — db.create_ingest_run() failure is a warning, run continues
# ---------------------------------------------------------------------------

class TestCreateIngestRunFailureContinues:
    """When db.create_ingest_run() raises, run() must log a warning and continue
    the pipeline (run_id becomes None and no admin-UI tracking occurs)."""

    def test_create_ingest_run_failure_is_warning_not_fatal(self, tmp_path, caplog):
        config_path = _make_config(tmp_path)
        profile_path = _make_profile(tmp_path)

        import psycopg2

        with (
            patch("ingest.db.init_db"),
            patch("ingest.db.create_ingest_run", side_effect=psycopg2.OperationalError("table missing")),
            # Prevent the pipeline from getting further — no sources configured.
            patch("ingest.load_providers", side_effect=ingest.CredentialError("no creds")),
            caplog.at_level(logging.WARNING, logger="ingest"),
        ):
            # CredentialError path returns cleanly, so no exception expected.
            ingest.run(
                config_path=config_path,
                profile_path=profile_path,
            )

        assert any(
            "Could not create ingest_runs record" in r.message
            for r in caplog.records
            if r.levelno >= logging.WARNING
        ), f"Expected 'Could not create ingest_runs record' WARNING, got:\n{caplog.text}"


# ---------------------------------------------------------------------------
# Gap 4 — finish_ingest_run() failure inside outer except does not mask error
# ---------------------------------------------------------------------------

class TestFinishIngestRunDoesNotMaskOriginalError:
    """If finish_ingest_run() itself raises inside the outer except handler, the
    original pipeline exception must still propagate — not be replaced by the
    DB error."""

    def test_original_exception_propagates_when_finish_fails(self, tmp_path, caplog):
        config_path = _make_config(tmp_path)
        profile_path = _make_profile(tmp_path)

        original_error = RuntimeError("simulated pipeline failure")
        db_error = Exception("DB write failed")

        with (
            patch("ingest.db.init_db"),
            patch("ingest.db.create_ingest_run", return_value=42),
            # Trigger a pipeline failure by making load_providers raise something unexpected.
            patch("ingest.load_providers", side_effect=original_error),
            patch("ingest.db.finish_ingest_run", side_effect=db_error),
            caplog.at_level(logging.WARNING, logger="ingest"),
        ):
            with pytest.raises(RuntimeError, match="simulated pipeline failure"):
                ingest.run(
                    config_path=config_path,
                    profile_path=profile_path,
                )

        # finish_ingest_run failed, so we expect the "could not record" warning.
        assert any(
            "Could not record run failure" in r.message
            for r in caplog.records
            if r.levelno >= logging.WARNING
        ), f"Expected 'Could not record run failure' WARNING, got:\n{caplog.text}"


# ---------------------------------------------------------------------------
# Gap 5 — rescore() load_profile() failure is logged
# ---------------------------------------------------------------------------

class TestRescoreLoadProfileIsLogged:
    """rescore() must log a startup error when load_profile() raises SystemExit."""

    def test_rescore_missing_profile_logs_error_and_exits(self, tmp_path, caplog):
        missing_profile = str(tmp_path / "no_profile.json")

        with caplog.at_level(logging.ERROR, logger="ingest"):
            with pytest.raises(SystemExit):
                ingest.rescore(profile_path=missing_profile)

        assert any(
            "Startup error" in r.message
            for r in caplog.records
            if r.levelno >= logging.ERROR
        ), f"Expected 'Startup error' ERROR log in rescore, got:\n{caplog.text}"


# ---------------------------------------------------------------------------
# Gap 5 — rescore() db.get_all_scored() failure is caught and logged
# ---------------------------------------------------------------------------

class TestRescoreDbFailureIsLogged:
    """When db.get_all_scored() raises in rescore(), it must be caught, logged,
    and exit with code 1 rather than crashing with an unhandled traceback."""

    def test_get_all_scored_failure_logs_error_and_calls_sys_exit(self, tmp_path, caplog):
        profile_path = _make_profile(tmp_path)
        providers_path = _make_providers(tmp_path)

        import psycopg2

        with (
            patch("ingest.load_providers", return_value={}),
            patch("ingest.build_provider_chain", return_value=[]),
            patch("ingest.db.get_all_scored", side_effect=psycopg2.OperationalError("DB down")),
            caplog.at_level(logging.ERROR, logger="ingest"),
            pytest.raises(SystemExit) as exc_info,
        ):
            ingest.rescore(
                profile_path=profile_path,
                providers_path=providers_path,
            )

        assert exc_info.value.code == 1
        assert any(
            "Could not fetch listings from database" in r.message
            for r in caplog.records
            if r.levelno >= logging.ERROR
        ), f"Expected 'Could not fetch listings from database' ERROR log, got:\n{caplog.text}"
