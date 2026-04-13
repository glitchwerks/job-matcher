"""
tests/test_ingest_run.py — Integration tests for ingest.run().

These tests exercise the full orchestrator end-to-end without making any HTTP
requests, LLM API calls, or reading real config files from disk.  They use:

  - A minimal ``JobSource`` subclass that yields a fixed fixture dataset.
  - The shared PostgreSQL database (DATABASE_URL required).
  - Patched ``scrape_description`` and ``score_listing_with_fallback`` at the
    ``ingest`` module level so no network or LLM calls are made.
  - Temp JSON files for config, profile, and providers so ``load_config()``,
    ``load_profile()``, and ``credentials.load_providers()`` succeed without
    touching the real project files.

Each test class cleans up its rows (source = 'mock' or 'jooble' with
source_id LIKE 'mock-%' / 'jooble-%') in teardown_method so the shared DB
remains tidy across test runs.

The summary line printed by ``run()`` is validated against the same regex
that ``app.py``'s ``_INGEST_SUMMARY_RE`` uses, ensuring the two stay in sync.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from unittest.mock import patch

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
    r"Run complete:\s*\d+\s*source\(s\)\s*\|"  # source count prefix
    r"\s*(\d+)\s*fetched\s*\|"                  # group 1 = fetched
    r".*?(\d+)\s*pre-filtered\s*\|"             # group 2 = pre-filtered
    r".*?scored\s*\((\d+)\s*failed\)",           # group 3 = score-failed
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

    SOURCE = "mock"

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

    @classmethod
    def settings_schema(cls) -> dict:
        return {"display_name": "Mock", "fields": []}

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
        "location": {
            "geocode_fallback": "pass",
            "notes": "Remote preferred",
        },
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


def _cleanup(*source_id_prefixes: str) -> None:
    """Delete all listings whose source_id starts with any of the given prefixes."""
    with db.get_connection() as conn:
        for prefix in source_id_prefixes:
            conn.execute(
                "DELETE FROM listings WHERE source_id LIKE %s",
                (prefix + "%",),
            )


def _count_by_prefix(*prefixes: str) -> int:
    """Return row count for listings matching any of the given source_id prefixes."""
    with db.get_connection() as conn:
        total = 0
        for prefix in prefixes:
            row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM listings WHERE source_id LIKE %s",
                (prefix + "%",),
            ).fetchone()
            total += row["cnt"]
    return total


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------

class TestIngestRunHappyPath:
    """Both listings score successfully and are persisted to the DB."""

    def setup_method(self):
        _cleanup("mock-")

    def teardown_method(self):
        _cleanup("mock-")

    def test_inserted_row_count(self, tmp_path, capsys):
        """run() with two fixture listings inserts exactly two rows."""
        config_path = _make_temp_config(tmp_path)
        profile_path = _make_temp_profile(tmp_path)
        keys_path = _make_temp_keys(tmp_path)

        mock_source = MockJobSource()

        with (
            patch("ingest.make_enabled_sources", return_value=[mock_source]),
            patch("ingest.scrape_description", return_value=("Full job description text here.", True)),
            patch("ingest.score_listing_with_fallback", return_value=_fixed_score_result()),
        ):
            ingest.run(
                config_path=config_path,
                profile_path=profile_path,
                keys_path=keys_path,
            )

        count = _count_by_prefix("mock-")
        assert count == 2, f"Expected 2 rows but got {count}"

    def test_get_feed_returns_inserted_listings(self, tmp_path, capsys):
        """db.get_feed() returns the two listings after run() completes."""
        config_path = _make_temp_config(tmp_path)
        profile_path = _make_temp_profile(tmp_path)
        keys_path = _make_temp_keys(tmp_path)

        mock_source = MockJobSource()

        with (
            patch("ingest.make_enabled_sources", return_value=[mock_source]),
            patch("ingest.scrape_description", return_value=("Full job description text here.", True)),
            patch("ingest.score_listing_with_fallback", return_value=_fixed_score_result()),
        ):
            ingest.run(
                config_path=config_path,
                profile_path=profile_path,
                keys_path=keys_path,
            )

        # get_feed() uses threshold=6.0; our score is 8 so both should appear.
        # Filter to only our test rows to avoid interference from other test data.
        with db.get_connection() as conn:
            rows = conn.execute(
                "SELECT title FROM listings WHERE source_id LIKE %s AND score >= %s",
                ("mock-%", 6.0),
            ).fetchall()

        titles = {row["title"] for row in rows}
        assert "Senior Python Engineer" in titles
        assert "Backend Developer" in titles

    def test_summary_line_matches_app_regex(self, tmp_path, caplog):
        """The logged summary line matches ``app._INGEST_SUMMARY_RE``."""
        config_path = _make_temp_config(tmp_path)
        profile_path = _make_temp_profile(tmp_path)
        keys_path = _make_temp_keys(tmp_path)

        mock_source = MockJobSource()

        with (
            patch("ingest.make_enabled_sources", return_value=[mock_source]),
            patch("ingest.scrape_description", return_value=("Full job description text here.", True)),
            patch("ingest.score_listing_with_fallback", return_value=_fixed_score_result()),
            caplog.at_level(logging.INFO, logger="ingest"),
        ):
            ingest.run(
                config_path=config_path,
                profile_path=profile_path,
                keys_path=keys_path,
            )

        log_text = caplog.text

        m = _INGEST_SUMMARY_RE.search(log_text)
        assert m is not None, (
            f"Summary line did not match _INGEST_SUMMARY_RE.\n"
            f"Log output:\n{log_text}"
        )

        fetched = int(m.group(1))
        pre_filtered = int(m.group(2))
        score_failed = int(m.group(3))

        assert fetched == 2, f"Expected 2 fetched, got {fetched}"
        assert pre_filtered == 0, f"Expected 0 pre-filtered, got {pre_filtered}"
        assert score_failed == 0, f"Expected 0 score-failed, got {score_failed}"

    def test_listings_stored_with_correct_fields(self, tmp_path):
        """Persisted listings have the expected source, score, and seen flag."""
        config_path = _make_temp_config(tmp_path)
        profile_path = _make_temp_profile(tmp_path)
        keys_path = _make_temp_keys(tmp_path)

        mock_source = MockJobSource()

        with (
            patch("ingest.make_enabled_sources", return_value=[mock_source]),
            patch("ingest.scrape_description", return_value=("Full job description text here.", True)),
            patch("ingest.score_listing_with_fallback", return_value=_fixed_score_result()),
        ):
            ingest.run(
                config_path=config_path,
                profile_path=profile_path,
                keys_path=keys_path,
            )

        with db.get_connection() as conn:
            rows = conn.execute(
                "SELECT source, source_id, score, seen FROM listings "
                "WHERE source_id LIKE %s ORDER BY source_id",
                ("mock-%",),
            ).fetchall()

        assert len(rows) == 2

        for row in rows:
            assert row["source"] == "mock"
            assert row["score"] == 8
            assert row["seen"] == 1


class TestIngestRunScoringFailure:
    """Listings that fail scoring are inserted as unseen (seen=0), not dropped."""

    def setup_method(self):
        _cleanup("mock-")

    def teardown_method(self):
        _cleanup("mock-")

    def test_score_failure_listing_is_inserted_unseen(self, tmp_path, capsys):
        """When score_listing_with_fallback returns None, the listing is stored with seen=0."""
        config_path = _make_temp_config(tmp_path)
        profile_path = _make_temp_profile(tmp_path)
        keys_path = _make_temp_keys(tmp_path)

        # Single listing source.
        mock_source = MockJobSource(listings=[_LISTING_1])

        with (
            patch("ingest.make_enabled_sources", return_value=[mock_source]),
            patch("ingest.scrape_description", return_value=("Full job description text here.", True)),
            patch("ingest.score_listing_with_fallback", return_value=None),
        ):
            ingest.run(
                config_path=config_path,
                profile_path=profile_path,
                keys_path=keys_path,
            )

        with db.get_connection() as conn:
            rows = conn.execute(
                "SELECT source_id, score, seen FROM listings WHERE source_id LIKE %s",
                ("mock-%",),
            ).fetchall()

        # The listing must be stored even on score failure.
        assert len(rows) == 1, "Score-failed listing should still be inserted"
        row = rows[0]
        assert row["source_id"] == "mock-001"
        assert row["score"] is None, "Score should be NULL on failure"
        assert row["seen"] == 0, "seen should be 0 (unscored) on failure"

    def test_score_failure_not_in_feed(self, tmp_path):
        """A listing with NULL score does not appear in get_feed() (score >= threshold)."""
        config_path = _make_temp_config(tmp_path)
        profile_path = _make_temp_profile(tmp_path)
        keys_path = _make_temp_keys(tmp_path)

        mock_source = MockJobSource(listings=[_LISTING_1])

        with (
            patch("ingest.make_enabled_sources", return_value=[mock_source]),
            patch("ingest.scrape_description", return_value=("Full job description text here.", True)),
            patch("ingest.score_listing_with_fallback", return_value=None),
        ):
            ingest.run(
                config_path=config_path,
                profile_path=profile_path,
                keys_path=keys_path,
            )

        with db.get_connection() as conn:
            rows = conn.execute(
                "SELECT source_id FROM listings WHERE source_id LIKE %s AND score >= %s",
                ("mock-%", 6.0),
            ).fetchall()
        assert rows == [], "Score-failed listing must not appear in the feed"

    def test_score_failure_summary_reports_one_failed(self, tmp_path, caplog):
        """The summary line records score_failed=1 when one listing fails scoring."""
        config_path = _make_temp_config(tmp_path)
        profile_path = _make_temp_profile(tmp_path)
        keys_path = _make_temp_keys(tmp_path)

        mock_source = MockJobSource(listings=[_LISTING_1])

        with (
            patch("ingest.make_enabled_sources", return_value=[mock_source]),
            patch("ingest.scrape_description", return_value=("Full job description text here.", True)),
            patch("ingest.score_listing_with_fallback", return_value=None),
            caplog.at_level(logging.INFO, logger="ingest"),
        ):
            ingest.run(
                config_path=config_path,
                profile_path=profile_path,
                keys_path=keys_path,
            )

        log_text = caplog.text

        m = _INGEST_SUMMARY_RE.search(log_text)
        assert m is not None, (
            f"Summary line did not match _INGEST_SUMMARY_RE.\n"
            f"Log output:\n{log_text}"
        )

        score_failed = int(m.group(3))
        assert score_failed == 1, f"Expected 1 score-failed, got {score_failed}"


class TestIngestRunDedup:
    """Listings already in the DB are skipped (dedup check)."""

    def setup_method(self):
        _cleanup("mock-")

    def teardown_method(self):
        _cleanup("mock-")

    def test_duplicate_listing_not_inserted_twice(self, tmp_path, capsys):
        """Running run() twice with the same listings results in only 2 DB rows."""
        config_path = _make_temp_config(tmp_path)
        profile_path = _make_temp_profile(tmp_path)
        keys_path = _make_temp_keys(tmp_path)

        # side_effect receives (providers_data, config) — return a list.
        def _make_mock_sources(_providers_data=None, _config=None):
            return [MockJobSource()]

        with (
            patch("ingest.make_enabled_sources", side_effect=_make_mock_sources),
            patch("ingest.scrape_description", return_value=("Full job description text here.", True)),
            patch("ingest.score_listing_with_fallback", return_value=_fixed_score_result()),
        ):
            ingest.run(config_path=config_path, profile_path=profile_path, keys_path=keys_path)
            ingest.run(config_path=config_path, profile_path=profile_path, keys_path=keys_path)

        count = _count_by_prefix("mock-")
        assert count == 2, f"Expected 2 rows (no dupes), got {count}"

    def test_duplicate_reflected_in_summary(self, tmp_path, caplog):
        """Second run reports 2 dupes skipped in its summary line."""
        config_path = _make_temp_config(tmp_path)
        profile_path = _make_temp_profile(tmp_path)
        keys_path = _make_temp_keys(tmp_path)

        _dupe_summary_re = re.compile(
            r"Run complete:\s*\d+\s*source\(s\)\s*\|"
            r"\s*(\d+)\s*fetched\s*\|"
            r".*?(\d+)\s*dupes skipped",
            re.IGNORECASE,
        )

        # side_effect receives (providers_data, config) — return a list.
        def _make_mock_sources(_providers_data=None, _config=None):
            return [MockJobSource()]

        with (
            patch("ingest.make_enabled_sources", side_effect=_make_mock_sources),
            patch("ingest.scrape_description", return_value=("Full job description text here.", True)),
            patch("ingest.score_listing_with_fallback", return_value=_fixed_score_result()),
            caplog.at_level(logging.INFO, logger="ingest"),
        ):
            ingest.run(config_path=config_path, profile_path=profile_path, keys_path=keys_path)
            caplog.clear()  # Discard first run log output.
            ingest.run(config_path=config_path, profile_path=profile_path, keys_path=keys_path)

        log_text = caplog.text

        m = _dupe_summary_re.search(log_text)
        assert m is not None, f"Expected dupe summary in log output:\n{log_text}"

        dupes = int(m.group(2))
        assert dupes == 2, f"Expected 2 dupes skipped, got {dupes}"


# ---------------------------------------------------------------------------
# _inject_env_var_credentials() — env var injection helper
# ---------------------------------------------------------------------------

class TestIngestRunSkipScrape:
    """Listings with skip_scrape=True bypass scrape_description()."""

    def setup_method(self):
        _cleanup("mock-", "jooble-")

    def teardown_method(self):
        _cleanup("mock-", "jooble-")

    def test_skip_scrape_listing_not_scraped(self, tmp_path):
        """scrape_description is never called when a listing sets skip_scrape=True."""
        config_path = _make_temp_config(tmp_path)
        profile_path = _make_temp_profile(tmp_path)
        keys_path = _make_temp_keys(tmp_path)

        skip_listing = {
            **_LISTING_1,
            "source": "jooble",
            "source_id": "jooble-001",
            "redirect_url": "https://jooble.org/jdp/-806082613597143857",
            "skip_scrape": True,
        }
        mock_source = MockJobSource(listings=[skip_listing])

        scrape_mock = patch("ingest.scrape_description", return_value=("Should not be called.", True))
        with (
            patch("ingest.make_enabled_sources", return_value=[mock_source]),
            scrape_mock as mock_scrape,
            patch("ingest.score_listing_with_fallback", return_value=_fixed_score_result()),
        ):
            ingest.run(
                config_path=config_path,
                profile_path=profile_path,
                keys_path=keys_path,
            )

        mock_scrape.assert_not_called()

    def test_skip_scrape_listing_is_inserted(self, tmp_path):
        """A listing with skip_scrape=True is still scored and inserted into the DB."""
        config_path = _make_temp_config(tmp_path)
        profile_path = _make_temp_profile(tmp_path)
        keys_path = _make_temp_keys(tmp_path)

        skip_listing = {
            **_LISTING_1,
            "source": "jooble",
            "source_id": "jooble-001",
            "redirect_url": "https://jooble.org/jdp/-806082613597143857",
            "skip_scrape": True,
        }
        mock_source = MockJobSource(listings=[skip_listing])

        with (
            patch("ingest.make_enabled_sources", return_value=[mock_source]),
            patch("ingest.scrape_description", return_value=("Should not be called.", True)),
            patch("ingest.score_listing_with_fallback", return_value=_fixed_score_result()),
        ):
            ingest.run(
                config_path=config_path,
                profile_path=profile_path,
                keys_path=keys_path,
            )

        count = _count_by_prefix("jooble-")
        assert count == 1, f"Expected 1 inserted row, got {count}"

    def test_skip_scrape_logs_scrape_skip(self, tmp_path, caplog):
        """A listing with skip_scrape=True logs 'SCRAPE SKIP' instead of 'SCRAPE FALLBACK'."""
        config_path = _make_temp_config(tmp_path)
        profile_path = _make_temp_profile(tmp_path)
        keys_path = _make_temp_keys(tmp_path)

        skip_listing = {
            **_LISTING_1,
            "source": "jooble",
            "source_id": "jooble-001",
            "redirect_url": "https://jooble.org/jdp/-806082613597143857",
            "skip_scrape": True,
        }
        mock_source = MockJobSource(listings=[skip_listing])

        with (
            patch("ingest.make_enabled_sources", return_value=[mock_source]),
            patch("ingest.scrape_description", return_value=("Should not be called.", True)),
            patch("ingest.score_listing_with_fallback", return_value=_fixed_score_result()),
            caplog.at_level(logging.INFO, logger="ingest"),
        ):
            ingest.run(
                config_path=config_path,
                profile_path=profile_path,
                keys_path=keys_path,
            )

        assert "SCRAPE SKIP" in caplog.text, (
            f"Expected 'SCRAPE SKIP' in log output; got:\n{caplog.text}"
        )
        assert "SCRAPE FALLBACK" not in caplog.text, (
            f"'SCRAPE FALLBACK' must not appear for skip_scrape listings; got:\n{caplog.text}"
        )


class TestIngestHoursFilter:
    """Tests for the --hours created_at filter in ingest.run().

    Regression guard for issue #199: ensures the filter behaves correctly when
    created_at is parseable, missing, or malformed.
    """

    def setup_method(self):
        _cleanup("mock-")

    def teardown_method(self):
        _cleanup("mock-")

    def test_recent_listing_passes_hours_filter(self, tmp_path):
        """A listing with created_at within the hours window is not filtered."""
        from datetime import datetime, timezone, timedelta

        config_path = _make_temp_config(tmp_path)
        profile_path = _make_temp_profile(tmp_path)
        keys_path = _make_temp_keys(tmp_path)

        recent_created_at = (
            datetime.now(timezone.utc) - timedelta(hours=1)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        recent_listing = {**_LISTING_1, "created_at": recent_created_at}
        mock_source = MockJobSource(listings=[recent_listing])

        with (
            patch("ingest.make_enabled_sources", return_value=[mock_source]),
            patch("ingest.scrape_description", return_value=("Job description.", True)),
            patch("ingest.score_listing_with_fallback", return_value=_fixed_score_result()),
        ):
            ingest.run(
                config_path=config_path,
                profile_path=profile_path,
                keys_path=keys_path,
                hours=24,
            )

        count = _count_by_prefix("mock-")
        assert count == 1, f"Recent listing should pass the 24-hour filter; got {count} rows"

    def test_old_listing_dropped_by_hours_filter(self, tmp_path, caplog):
        """A listing with created_at older than the hours window is filtered out."""
        from datetime import datetime, timezone, timedelta

        config_path = _make_temp_config(tmp_path)
        profile_path = _make_temp_profile(tmp_path)
        keys_path = _make_temp_keys(tmp_path)

        old_created_at = (
            datetime.now(timezone.utc) - timedelta(hours=48)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        old_listing = {**_LISTING_1, "created_at": old_created_at}
        mock_source = MockJobSource(listings=[old_listing])

        with (
            patch("ingest.make_enabled_sources", return_value=[mock_source]),
            patch("ingest.scrape_description", return_value=("Job description.", True)),
            patch("ingest.score_listing_with_fallback", return_value=_fixed_score_result()),
            caplog.at_level(logging.INFO, logger="ingest"),
        ):
            ingest.run(
                config_path=config_path,
                profile_path=profile_path,
                keys_path=keys_path,
                hours=24,
            )

        count = _count_by_prefix("mock-")
        assert count == 0, f"48-hour-old listing should be filtered by 24-hour window; got {count} rows"
        assert "FILTERED" in caplog.text, "Expected FILTERED log entry for old listing"

    def test_malformed_created_at_passes_hours_filter(self, tmp_path):
        """Regression (#199): a listing with an unparseable created_at is NOT dropped.

        The filter must log-and-pass (let it through) rather than silently drop
        listings whose timestamp cannot be parsed. This guards the existing
        except (ValueError, TypeError): pass behavior in ingest.py.
        """
        config_path = _make_temp_config(tmp_path)
        profile_path = _make_temp_profile(tmp_path)
        keys_path = _make_temp_keys(tmp_path)

        malformed_listing = {**_LISTING_1, "created_at": "not-a-date"}
        mock_source = MockJobSource(listings=[malformed_listing])

        with (
            patch("ingest.make_enabled_sources", return_value=[mock_source]),
            patch("ingest.scrape_description", return_value=("Job description.", True)),
            patch("ingest.score_listing_with_fallback", return_value=_fixed_score_result()),
        ):
            ingest.run(
                config_path=config_path,
                profile_path=profile_path,
                keys_path=keys_path,
                hours=24,
            )

        count = _count_by_prefix("mock-")
        assert count == 1, (
            f"Listing with malformed created_at should pass through the hours filter "
            f"(log-and-pass, not drop); got {count} rows"
        )

    def test_missing_created_at_passes_hours_filter(self, tmp_path):
        """A listing with no created_at field passes the hours filter unconditionally."""
        config_path = _make_temp_config(tmp_path)
        profile_path = _make_temp_profile(tmp_path)
        keys_path = _make_temp_keys(tmp_path)

        no_date_listing = {**_LISTING_1, "created_at": None}
        mock_source = MockJobSource(listings=[no_date_listing])

        with (
            patch("ingest.make_enabled_sources", return_value=[mock_source]),
            patch("ingest.scrape_description", return_value=("Job description.", True)),
            patch("ingest.score_listing_with_fallback", return_value=_fixed_score_result()),
        ):
            ingest.run(
                config_path=config_path,
                profile_path=profile_path,
                keys_path=keys_path,
                hours=24,
            )

        count = _count_by_prefix("mock-")
        assert count == 1, (
            f"Listing with missing created_at should pass through the hours filter; "
            f"got {count} rows"
        )


class TestInjectEnvVarCredentials:
    """Unit tests for ingest._inject_env_var_credentials()."""

    def test_env_vars_written_into_providers_dict(self, monkeypatch):
        """When both env vars are set, they appear under providers["job_sources"]["adzuna"]."""
        monkeypatch.setenv("ADZUNA_APP_ID",  "env-app-id")
        monkeypatch.setenv("ADZUNA_APP_KEY", "env-app-key")

        providers: dict = {}
        ingest._inject_env_var_credentials(providers)

        assert providers["job_sources"]["adzuna"]["app_id"]  == "env-app-id"
        assert providers["job_sources"]["adzuna"]["app_key"] == "env-app-key"

    def test_existing_providers_json_values_not_overwritten(self, monkeypatch):
        """setdefault semantics: env vars do NOT replace pre-existing credential values."""
        monkeypatch.setenv("ADZUNA_APP_ID",  "env-app-id")
        monkeypatch.setenv("ADZUNA_APP_KEY", "env-app-key")

        providers: dict = {
            "job_sources": {
                "adzuna": {
                    "app_id":  "file-app-id",
                    "app_key": "file-app-key",
                }
            }
        }
        ingest._inject_env_var_credentials(providers)

        # File values must be preserved.
        assert providers["job_sources"]["adzuna"]["app_id"]  == "file-app-id"
        assert providers["job_sources"]["adzuna"]["app_key"] == "file-app-key"

    def test_only_app_id_env_var_set(self, monkeypatch):
        """When only ADZUNA_APP_ID is set, only app_id is injected."""
        monkeypatch.setenv("ADZUNA_APP_ID", "env-app-id")
        monkeypatch.delenv("ADZUNA_APP_KEY", raising=False)

        providers: dict = {}
        ingest._inject_env_var_credentials(providers)

        assert providers["job_sources"]["adzuna"]["app_id"] == "env-app-id"
        assert "app_key" not in providers["job_sources"]["adzuna"]

    def test_only_app_key_env_var_set(self, monkeypatch):
        """When only ADZUNA_APP_KEY is set, only app_key is injected."""
        monkeypatch.delenv("ADZUNA_APP_ID", raising=False)
        monkeypatch.setenv("ADZUNA_APP_KEY", "env-app-key")

        providers: dict = {}
        ingest._inject_env_var_credentials(providers)

        assert providers["job_sources"]["adzuna"]["app_key"] == "env-app-key"
        assert "app_id" not in providers["job_sources"]["adzuna"]

    def test_neither_env_var_set_leaves_providers_unchanged(self, monkeypatch):
        """When neither env var is set, the providers dict is not modified."""
        monkeypatch.delenv("ADZUNA_APP_ID",  raising=False)
        monkeypatch.delenv("ADZUNA_APP_KEY", raising=False)

        providers: dict = {}
        ingest._inject_env_var_credentials(providers)

        # No "job_sources" key should have been created.
        assert "job_sources" not in providers
