"""
tests/test_ingest_run.py — Integration tests for ingest.run().

These tests exercise the full orchestrator end-to-end without making any HTTP
requests, LLM API calls, or reading real config files from disk.  They use:

  - A minimal ``JobSource`` subclass that yields a fixed fixture dataset.
  - A real temp SQLite database via pytest's ``tmp_path`` fixture.
  - Patched ``scrape_description`` and ``score_listing_with_fallback`` at the
    ``ingest`` module level so no network or LLM calls are made.
  - Temp JSON files for config, profile, and keys so ``load_config()``,
    ``load_profile()``, and ``load_keys()`` succeed without touching the
    real project files.

The summary line printed by ``run()`` is validated against the same regex
that ``app.py``'s ``_INGEST_SUMMARY_RE`` uses, ensuring the two stay in sync.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from unittest.mock import patch, MagicMock

import pytest

# Ensure the project root is on the path regardless of how pytest is invoked.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import db
import ingest
from job_sources.base import JobSource


# ---------------------------------------------------------------------------
# The same regex used in app.py — kept here for cross-module validation.
# If app._INGEST_SUMMARY_RE ever changes this constant must be updated too.
# ---------------------------------------------------------------------------

_INGEST_SUMMARY_RE = re.compile(
    r"Run complete:\s*(\d+)\s*fetched\s*\|"   # group 1 = fetched
    r".*?(\d+)\s*pre-filtered\s*\|"           # group 2 = pre-filtered
    r".*?scored\s*\((\d+)\s*failed\)",         # group 3 = score-failed
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Fixture listings — realistic normalised dicts matching the canonical schema
# defined in job_sources/base.py.
# ---------------------------------------------------------------------------

_LISTING_1 = {
    "source": "mock",
    "source_id": "mock-001",
    "title": "Senior Python Engineer",
    "company": "Acme Corp",
    "location": "Remote",
    "salary_min": 90_000.0,
    "salary_max": 130_000.0,
    "salary_is_predicted": 0,
    "contract_type": "permanent",
    "contract_time": "full_time",
    "description": "We need a senior Python engineer to join our platform team.",
    "redirect_url": "https://example.com/jobs/mock-001",
    "created_at": "2026-03-30T10:00:00Z",
}

_LISTING_2 = {
    "source": "mock",
    "source_id": "mock-002",
    "title": "Backend Developer",
    "company": "Beta Ltd",
    "location": "London",
    "salary_min": 70_000.0,
    "salary_max": 100_000.0,
    "salary_is_predicted": 0,
    "contract_type": "permanent",
    "contract_time": "full_time",
    "description": "Backend role using Python and Django in a small team.",
    "redirect_url": "https://example.com/jobs/mock-002",
    "created_at": "2026-03-30T09:00:00Z",
}


# ---------------------------------------------------------------------------
# Mock JobSource
# ---------------------------------------------------------------------------

class MockJobSource(JobSource):
    """Minimal ``JobSource`` subclass that returns a fixed page of listings.

    ``pages()`` yields a single page containing the fixture listings so no
    HTTP requests are made during the test.
    """

    def __init__(self, listings: list[dict] | None = None, config: dict | None = None):
        self._listings = listings if listings is not None else [_LISTING_1, _LISTING_2]

    def fetch_page(self, page: int) -> list[dict]:
        """Return raw listings (already normalised for this mock)."""
        return self._listings if page == 1 else []

    def total_pages(self) -> int:
        return 1

    def normalise(self, raw: dict) -> dict:
        """Pass-through — the fixture data is already normalised."""
        return raw

    def pages(self):
        """Yield a single page of normalised listings."""
        yield self._listings


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_json(path: str, data: dict) -> None:
    """Write *data* as JSON to *path*."""
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh)


def _make_temp_config(tmp_path) -> str:
    """Write a minimal valid config.json to tmp_path and return its path."""
    cfg = {
        "adzuna_app_id": "test-app-id",
        "adzuna_app_key": "test-app-key",
        "search": {
            "country": "gb",
            "what": "python developer",
            "results_per_page": 10,
            "max_pages": 1,
        },
        "scoring": {
            "threshold": 6.0,
        },
    }
    path = str(tmp_path / "config.json")
    _write_json(path, cfg)
    return path


def _make_temp_profile(tmp_path) -> str:
    """Write a minimal valid profile.json to tmp_path and return its path."""
    profile = {
        "primary_skills": ["Python", "Django"],
        "anti_preferences": [],
        "seniority": "senior",
        "preferred_industries": ["tech"],
        "location_preference": "remote",
        "scoring_notes": "Prefer remote-first companies.",
    }
    path = str(tmp_path / "profile.json")
    _write_json(path, profile)
    return path


def _make_temp_keys(tmp_path) -> str:
    """Write a minimal valid keys.json to tmp_path and return its path."""
    keys = {
        "providers": {
            "anthropic": {
                "api_key": "sk-test-anthropic",
                "model": "claude-haiku-4-5-20251001",
            }
        },
        "preferred_provider": "anthropic",
    }
    path = str(tmp_path / "keys.json")
    _write_json(path, keys)
    return path


def _fixed_score_result() -> dict:
    """Return a realistic scoring result dict matching the expected schema."""
    return {
        "score": 8,
        "matched_skills": ["Python", "Django"],
        "missing_skills": [],
        "concerns": [],
        "verdict": "Strong match for a Python backend role.",
        "tokens_input": 250,
        "tokens_output": 80,
        "model_used": "anthropic/claude-haiku-4-5-20251001",
    }


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------

class TestIngestRunHappyPath:
    """Both listings score successfully and are persisted to the DB."""

    def test_inserted_row_count(self, tmp_path, capsys):
        """run() with two fixture listings inserts exactly two rows."""
        db_path = str(tmp_path / "jobs.db")
        config_path = _make_temp_config(tmp_path)
        profile_path = _make_temp_profile(tmp_path)
        keys_path = _make_temp_keys(tmp_path)

        db.init_db(db_path=db_path)

        mock_source = MockJobSource()

        with (
            patch("ingest.make_source", return_value=mock_source),
            patch("ingest.scrape_description", return_value=("Full job description text here.", True)),
            patch("ingest.score_listing_with_fallback", return_value=_fixed_score_result()),
            patch.object(ingest, "_DB_PATH", db_path),
        ):
            ingest.run(
                config_path=config_path,
                profile_path=profile_path,
                keys_path=keys_path,
            )

        conn = db.get_connection(db_path)
        try:
            count = conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
        finally:
            conn.close()

        assert count == 2, f"Expected 2 rows but got {count}"

    def test_get_feed_returns_inserted_listings(self, tmp_path, capsys):
        """db.get_feed() returns the two listings after run() completes."""
        db_path = str(tmp_path / "jobs.db")
        config_path = _make_temp_config(tmp_path)
        profile_path = _make_temp_profile(tmp_path)
        keys_path = _make_temp_keys(tmp_path)

        db.init_db(db_path=db_path)

        mock_source = MockJobSource()

        with (
            patch("ingest.make_source", return_value=mock_source),
            patch("ingest.scrape_description", return_value=("Full job description text here.", True)),
            patch("ingest.score_listing_with_fallback", return_value=_fixed_score_result()),
            patch.object(ingest, "_DB_PATH", db_path),
        ):
            ingest.run(
                config_path=config_path,
                profile_path=profile_path,
                keys_path=keys_path,
            )

        # get_feed() uses threshold=6.0; our score is 8 so both should appear.
        feed = db.get_feed(threshold=6.0, db_path=db_path)
        assert len(feed) == 2

        titles = {row["title"] for row in feed}
        assert "Senior Python Engineer" in titles
        assert "Backend Developer" in titles

    def test_summary_line_matches_app_regex(self, tmp_path, capsys):
        """The printed summary line matches ``app._INGEST_SUMMARY_RE``."""
        db_path = str(tmp_path / "jobs.db")
        config_path = _make_temp_config(tmp_path)
        profile_path = _make_temp_profile(tmp_path)
        keys_path = _make_temp_keys(tmp_path)

        db.init_db(db_path=db_path)

        mock_source = MockJobSource()

        with (
            patch("ingest.make_source", return_value=mock_source),
            patch("ingest.scrape_description", return_value=("Full job description text here.", True)),
            patch("ingest.score_listing_with_fallback", return_value=_fixed_score_result()),
            patch.object(ingest, "_DB_PATH", db_path),
        ):
            ingest.run(
                config_path=config_path,
                profile_path=profile_path,
                keys_path=keys_path,
            )

        captured = capsys.readouterr()
        stdout = captured.out

        m = _INGEST_SUMMARY_RE.search(stdout)
        assert m is not None, (
            f"Summary line did not match _INGEST_SUMMARY_RE.\n"
            f"Printed output:\n{stdout}"
        )

        fetched = int(m.group(1))
        pre_filtered = int(m.group(2))
        score_failed = int(m.group(3))

        assert fetched == 2, f"Expected 2 fetched, got {fetched}"
        assert pre_filtered == 0, f"Expected 0 pre-filtered, got {pre_filtered}"
        assert score_failed == 0, f"Expected 0 score-failed, got {score_failed}"

    def test_listings_stored_with_correct_fields(self, tmp_path):
        """Persisted listings have the expected source, score, and seen flag."""
        db_path = str(tmp_path / "jobs.db")
        config_path = _make_temp_config(tmp_path)
        profile_path = _make_temp_profile(tmp_path)
        keys_path = _make_temp_keys(tmp_path)

        db.init_db(db_path=db_path)

        mock_source = MockJobSource()

        with (
            patch("ingest.make_source", return_value=mock_source),
            patch("ingest.scrape_description", return_value=("Full job description text here.", True)),
            patch("ingest.score_listing_with_fallback", return_value=_fixed_score_result()),
            patch.object(ingest, "_DB_PATH", db_path),
        ):
            ingest.run(
                config_path=config_path,
                profile_path=profile_path,
                keys_path=keys_path,
            )

        conn = db.get_connection(db_path)
        try:
            rows = conn.execute(
                "SELECT source, source_id, score, seen FROM listings ORDER BY source_id"
            ).fetchall()
        finally:
            conn.close()

        assert len(rows) == 2

        for row in rows:
            assert row["source"] == "mock"
            assert row["score"] == 8
            assert row["seen"] == 1


class TestIngestRunScoringFailure:
    """Listings that fail scoring are inserted as unseen (seen=0), not dropped."""

    def test_score_failure_listing_is_inserted_unseen(self, tmp_path, capsys):
        """When score_listing_with_fallback returns None, the listing is stored with seen=0."""
        db_path = str(tmp_path / "jobs.db")
        config_path = _make_temp_config(tmp_path)
        profile_path = _make_temp_profile(tmp_path)
        keys_path = _make_temp_keys(tmp_path)

        db.init_db(db_path=db_path)

        # Single listing source.
        mock_source = MockJobSource(listings=[_LISTING_1])

        with (
            patch("ingest.make_source", return_value=mock_source),
            patch("ingest.scrape_description", return_value=("Full job description text here.", True)),
            patch("ingest.score_listing_with_fallback", return_value=None),
            patch.object(ingest, "_DB_PATH", db_path),
        ):
            ingest.run(
                config_path=config_path,
                profile_path=profile_path,
                keys_path=keys_path,
            )

        conn = db.get_connection(db_path)
        try:
            rows = conn.execute(
                "SELECT source_id, score, seen FROM listings"
            ).fetchall()
        finally:
            conn.close()

        # The listing must be stored even on score failure.
        assert len(rows) == 1, "Score-failed listing should still be inserted"
        row = rows[0]
        assert row["source_id"] == "mock-001"
        assert row["score"] is None, "Score should be NULL on failure"
        assert row["seen"] == 0, "seen should be 0 (unscored) on failure"

    def test_score_failure_not_in_feed(self, tmp_path):
        """A listing with NULL score does not appear in get_feed() (score >= threshold)."""
        db_path = str(tmp_path / "jobs.db")
        config_path = _make_temp_config(tmp_path)
        profile_path = _make_temp_profile(tmp_path)
        keys_path = _make_temp_keys(tmp_path)

        db.init_db(db_path=db_path)

        mock_source = MockJobSource(listings=[_LISTING_1])

        with (
            patch("ingest.make_source", return_value=mock_source),
            patch("ingest.scrape_description", return_value=("Full job description text here.", True)),
            patch("ingest.score_listing_with_fallback", return_value=None),
            patch.object(ingest, "_DB_PATH", db_path),
        ):
            ingest.run(
                config_path=config_path,
                profile_path=profile_path,
                keys_path=keys_path,
            )

        feed = db.get_feed(threshold=6.0, db_path=db_path)
        assert feed == [], "Score-failed listing must not appear in the feed"

    def test_score_failure_summary_reports_one_failed(self, tmp_path, capsys):
        """The summary line records score_failed=1 when one listing fails scoring."""
        db_path = str(tmp_path / "jobs.db")
        config_path = _make_temp_config(tmp_path)
        profile_path = _make_temp_profile(tmp_path)
        keys_path = _make_temp_keys(tmp_path)

        db.init_db(db_path=db_path)

        mock_source = MockJobSource(listings=[_LISTING_1])

        with (
            patch("ingest.make_source", return_value=mock_source),
            patch("ingest.scrape_description", return_value=("Full job description text here.", True)),
            patch("ingest.score_listing_with_fallback", return_value=None),
            patch.object(ingest, "_DB_PATH", db_path),
        ):
            ingest.run(
                config_path=config_path,
                profile_path=profile_path,
                keys_path=keys_path,
            )

        captured = capsys.readouterr()
        stdout = captured.out

        m = _INGEST_SUMMARY_RE.search(stdout)
        assert m is not None, (
            f"Summary line did not match _INGEST_SUMMARY_RE.\n"
            f"Printed output:\n{stdout}"
        )

        score_failed = int(m.group(3))
        assert score_failed == 1, f"Expected 1 score-failed, got {score_failed}"


class TestIngestRunDedup:
    """Listings already in the DB are skipped (dedup check)."""

    def test_duplicate_listing_not_inserted_twice(self, tmp_path, capsys):
        """Running run() twice with the same listings results in only 2 DB rows."""
        db_path = str(tmp_path / "jobs.db")
        config_path = _make_temp_config(tmp_path)
        profile_path = _make_temp_profile(tmp_path)
        keys_path = _make_temp_keys(tmp_path)

        db.init_db(db_path=db_path)

        # side_effect receives the same args as the patched function (config dict).
        def _make_mock_source(_config=None):
            return MockJobSource()

        with (
            patch("ingest.make_source", side_effect=_make_mock_source),
            patch("ingest.scrape_description", return_value=("Full job description text here.", True)),
            patch("ingest.score_listing_with_fallback", return_value=_fixed_score_result()),
            patch.object(ingest, "_DB_PATH", db_path),
        ):
            ingest.run(config_path=config_path, profile_path=profile_path, keys_path=keys_path)
            ingest.run(config_path=config_path, profile_path=profile_path, keys_path=keys_path)

        conn = db.get_connection(db_path)
        try:
            count = conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
        finally:
            conn.close()

        assert count == 2, f"Expected 2 rows (no dupes), got {count}"

    def test_duplicate_reflected_in_summary(self, tmp_path, capsys):
        """Second run reports 2 dupes skipped in its summary line."""
        db_path = str(tmp_path / "jobs.db")
        config_path = _make_temp_config(tmp_path)
        profile_path = _make_temp_profile(tmp_path)
        keys_path = _make_temp_keys(tmp_path)

        db.init_db(db_path=db_path)

        _dupe_summary_re = re.compile(
            r"Run complete:\s*(\d+)\s*fetched\s*\|"
            r".*?(\d+)\s*dupes skipped",
            re.IGNORECASE,
        )

        # side_effect receives the same args as the patched function (config dict).
        def _make_mock_source(_config=None):
            return MockJobSource()

        with (
            patch("ingest.make_source", side_effect=_make_mock_source),
            patch("ingest.scrape_description", return_value=("Full job description text here.", True)),
            patch("ingest.score_listing_with_fallback", return_value=_fixed_score_result()),
            patch.object(ingest, "_DB_PATH", db_path),
        ):
            ingest.run(config_path=config_path, profile_path=profile_path, keys_path=keys_path)
            capsys.readouterr()  # Discard first run output.
            ingest.run(config_path=config_path, profile_path=profile_path, keys_path=keys_path)

        captured = capsys.readouterr()
        stdout = captured.out

        m = _dupe_summary_re.search(stdout)
        assert m is not None, f"Expected dupe summary in output:\n{stdout}"

        dupes = int(m.group(2))
        assert dupes == 2, f"Expected 2 dupes skipped, got {dupes}"
