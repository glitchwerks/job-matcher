"""
tests/test_db.py — Integration tests for db.py against a live PostgreSQL database.

Requires DATABASE_URL to be set in the environment (see .env.example).
Each test class inserts rows using unique source_id prefixes and deletes
them in teardown so tests are isolated without needing separate databases.
"""

import datetime
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import db

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_listing(
    source_id: str = "test-001",
    title: str = "Software Engineer",
    company: str = "Acme Corp",
    location: str = "New York, NY",
    salary_min: float | None = 80_000,
    salary_max: float | None = 120_000,
    salary_is_predicted: int = 0,
    contract_type: str = "permanent",
    contract_time: str = "full_time",
    description: str = "A great job.",
    redirect_url: str = "https://example.com/job/1",
    created_at: str = "2026-01-01T00:00:00Z",
    fetched_at: str = "2026-01-02T00:00:00Z",
    score: float | None = 8.0,
    matched_skills: list | None = None,
    missing_skills: list | None = None,
    concerns: list | None = None,
    verdict: str | None = "Strong match.",
    bookmarked: int = 0,
    dismissed: int = 0,
    seen: int = 1,
    applied: int = 0,
    job_type: str | None = None,
    model_used: str | None = None,
    source: str = "adzuna",
    posted_at: str | None = None,
    description_source: str = "full",
) -> dict:
    """Return a complete listing dict suitable for db.insert_listing()."""
    return {
        "source": source,
        "source_id": source_id,
        "title": title,
        "company": company,
        "location": location,
        "salary_min": salary_min,
        "salary_max": salary_max,
        "salary_is_predicted": salary_is_predicted,
        "contract_type": contract_type,
        "contract_time": contract_time,
        "description": description,
        "redirect_url": redirect_url,
        "created_at": created_at,
        "fetched_at": fetched_at,
        "score": score,
        "matched_skills": matched_skills if matched_skills is not None else ["Python", "SQL"],
        "missing_skills": missing_skills if missing_skills is not None else ["Rust"],
        "concerns": concerns if concerns is not None else [],
        "verdict": verdict,
        "bookmarked": bookmarked,
        "dismissed": dismissed,
        "seen": seen,
        "applied": applied,
        "job_type": job_type,
        "model_used": model_used,
        "posted_at": posted_at,
        "description_source": description_source,
    }


def _cleanup(*prefixes: str) -> None:
    """Delete all listings whose source_id starts with any of the given prefixes."""
    with db.get_connection() as conn:
        for prefix in prefixes:
            conn.execute(
                "DELETE FROM listings WHERE source_id LIKE %s",
                (prefix + "%",),
            )


def _get_id_by_source_id(source_id: str) -> int:
    """Return the internal integer id for a listing by source_id."""
    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT id FROM listings WHERE source_id = %s", (source_id,)
        ).fetchone()
    assert row is not None, f"No listing found with source_id={source_id!r}"
    return row["id"]


# ---------------------------------------------------------------------------
# init_db
# ---------------------------------------------------------------------------

class TestInitDb:
    def test_creates_listings_table(self):
        """init_db() creates the listings table (idempotent — table already exists)."""
        # Just call it and check the table is queryable.
        db.init_db()
        with db.get_connection() as conn:
            rows = conn.execute(
                """
                SELECT table_name FROM information_schema.tables
                WHERE table_schema = 'public' AND table_name = 'listings'
                """
            ).fetchall()
        assert len(rows) == 1

    def test_idempotent(self):
        """Calling init_db() twice does not raise."""
        db.init_db()
        db.init_db()  # second call — should be a no-op

    def test_redirect_url_index_exists(self):
        """init_db() creates the idx_listings_redirect_url index."""
        db.init_db()
        with db.get_connection() as conn:
            rows = conn.execute(
                """
                SELECT indexname FROM pg_indexes
                WHERE tablename = 'listings'
                  AND indexname = 'idx_listings_redirect_url'
                """
            ).fetchall()
        assert len(rows) == 1

    def test_schema_has_model_used_column(self):
        """init_db() ensures model_used column exists in the schema."""
        db.init_db()
        with db.get_connection() as conn:
            rows = conn.execute(
                """
                SELECT column_name FROM information_schema.columns
                WHERE table_name = 'listings' AND column_name = 'model_used'
                """
            ).fetchall()
        assert len(rows) == 1

    def test_schema_has_posted_at_column(self):
        """init_db() ensures posted_at column exists in the schema."""
        db.init_db()
        with db.get_connection() as conn:
            rows = conn.execute(
                """
                SELECT column_name FROM information_schema.columns
                WHERE table_name = 'listings' AND column_name = 'posted_at'
                """
            ).fetchall()
        assert len(rows) == 1


# ---------------------------------------------------------------------------
# listing_exists
# ---------------------------------------------------------------------------

class TestListingExists:
    _PREFIX = "le-"

    def teardown_method(self):
        _cleanup(self._PREFIX)

    def test_returns_false_for_unknown_id(self):
        with db.get_connection() as conn:
            assert db.listing_exists(conn, "adzuna", "le-nonexistent") is False

    def test_returns_true_after_insert(self):
        db.insert_listing(make_listing(source_id="le-001"))
        with db.get_connection() as conn:
            assert db.listing_exists(conn, "adzuna", "le-001") is True

    def test_returns_false_for_different_id(self):
        db.insert_listing(make_listing(source_id="le-002"))
        with db.get_connection() as conn:
            assert db.listing_exists(conn, "adzuna", "le-xyz") is False


# ---------------------------------------------------------------------------
# insert_listing + get_feed
# ---------------------------------------------------------------------------

class TestInsertAndFeed:
    _PREFIX = "feed-"

    def teardown_method(self):
        _cleanup(self._PREFIX)

    def test_inserted_listing_appears_in_feed(self):
        db.insert_listing(make_listing(source_id="feed-001", score=8.0))
        results = db.get_feed(threshold=7.0)
        ids = [r["source_id"] for r in results]
        assert "feed-001" in ids

    def test_listing_below_threshold_excluded(self):
        db.insert_listing(make_listing(source_id="feed-002", score=4.0))
        results = db.get_feed(threshold=7.0)
        ids = [r["source_id"] for r in results]
        assert "feed-002" not in ids

    def test_dismissed_listing_excluded_from_feed(self):
        db.insert_listing(make_listing(source_id="feed-003", score=9.0, dismissed=1))
        results = db.get_feed(threshold=7.0)
        ids = [r["source_id"] for r in results]
        assert "feed-003" not in ids

    def test_applied_listing_excluded_from_feed(self):
        db.insert_listing(make_listing(source_id="feed-004", score=9.0, applied=1))
        results = db.get_feed(threshold=7.0)
        ids = [r["source_id"] for r in results]
        assert "feed-004" not in ids

    def test_feed_ordered_by_score_descending(self):
        db.insert_listing(make_listing(source_id="feed-sc7", score=7.0))
        db.insert_listing(make_listing(source_id="feed-sc9", score=9.0))
        db.insert_listing(make_listing(source_id="feed-sc8", score=8.0))
        results = db.get_feed(threshold=7.0)
        # Filter to just our test rows to avoid interference from other test data.
        our_results = [r for r in results if r["source_id"].startswith(self._PREFIX)]
        scores = [r["score"] for r in our_results]
        assert scores == sorted(scores, reverse=True)

    def test_insert_listing_with_minimal_fields(self):
        """insert_listing() succeeds with only required fields."""
        minimal = {
            "source": "remotive",
            "source_id": "feed-min-001",
            "title": "Test Job",
            "company": "Test Co",
            "location": "Remote",
            "description": "Test description",
            "redirect_url": "https://example.com/job/minimal",
            "created_at": "2026-01-01T00:00:00Z",
            "fetched_at": "2026-01-02T00:00:00Z",
            "score": 7.5,
            "matched_skills": ["Python"],
            "missing_skills": [],
            "concerns": [],
            "verdict": "Good match.",
        }
        db.insert_listing(minimal)

        with db.get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM listings WHERE source_id = %s", ("feed-min-001",)
            ).fetchone()

        assert row is not None, "Minimal listing was not inserted"
        assert row["salary_is_predicted"] is None
        assert row["source"] == "remotive"
        assert row["title"] == "Test Job"


# ---------------------------------------------------------------------------
# get_feed search and remote_only filters
# ---------------------------------------------------------------------------

class TestFeedFilters:
    _PREFIX = "ff-"

    def teardown_method(self):
        _cleanup(self._PREFIX)

    def test_search_filter_by_title(self):
        db.insert_listing(make_listing(source_id="ff-py-001", title="Python Developer", score=8.0))
        db.insert_listing(make_listing(source_id="ff-java-001", title="Java Developer", score=8.0))
        results = db.get_feed(threshold=7.0, search="python")
        ids = [r["source_id"] for r in results]
        assert "ff-py-001" in ids
        assert "ff-java-001" not in ids

    def test_search_filter_case_insensitive(self):
        db.insert_listing(make_listing(source_id="ff-ci-001", title="PYTHON DEVELOPER", score=8.0))
        results = db.get_feed(threshold=7.0, search="python")
        assert any(r["source_id"] == "ff-ci-001" for r in results)

    def test_search_filter_by_company(self):
        db.insert_listing(
            make_listing(source_id="ff-co-001", title="Engineer", company="Acme Corp", score=8.0)
        )
        results = db.get_feed(threshold=7.0, search="acme")
        assert any(r["source_id"] == "ff-co-001" for r in results)

    def test_remote_only_filter(self):
        db.insert_listing(
            make_listing(source_id="ff-rem-001", location="Remote, US", score=8.0)
        )
        db.insert_listing(
            make_listing(source_id="ff-off-001", location="New York, NY", score=8.0)
        )
        results = db.get_feed(threshold=7.0, remote_only=True)
        ids = [r["source_id"] for r in results]
        assert "ff-rem-001" in ids
        assert "ff-off-001" not in ids

    def test_remote_only_case_insensitive(self):
        db.insert_listing(
            make_listing(source_id="ff-rem-up", location="REMOTE ONLY", score=8.0)
        )
        results = db.get_feed(threshold=7.0, remote_only=True)
        assert any(r["source_id"] == "ff-rem-up" for r in results)


# ---------------------------------------------------------------------------
# Bookmarks
# ---------------------------------------------------------------------------

class TestBookmarks:
    _PREFIX = "bm-"

    def teardown_method(self):
        _cleanup(self._PREFIX)

    def test_bookmarked_listing_appears_in_get_bookmarks(self):
        db.insert_listing(make_listing(source_id="bm-001", score=8.0))
        listing_id = _get_id_by_source_id("bm-001")
        db.set_bookmarked(listing_id, 1)
        bookmarks = db.get_bookmarks()
        ids = [r["source_id"] for r in bookmarks]
        assert "bm-001" in ids

    def test_unbookmark_removes_from_bookmarks(self):
        db.insert_listing(make_listing(source_id="bm-002", score=8.0))
        listing_id = _get_id_by_source_id("bm-002")
        db.set_bookmarked(listing_id, 1)
        db.set_bookmarked(listing_id, 0)
        bookmarks = db.get_bookmarks()
        ids = [r["source_id"] for r in bookmarks]
        assert "bm-002" not in ids

    def test_non_bookmarked_listing_absent_from_bookmarks(self):
        db.insert_listing(make_listing(source_id="bm-003", score=8.0, bookmarked=0))
        bookmarks = db.get_bookmarks()
        ids = [r["source_id"] for r in bookmarks]
        assert "bm-003" not in ids


# ---------------------------------------------------------------------------
# Dismissed
# ---------------------------------------------------------------------------

class TestDismissed:
    _PREFIX = "dm-"

    def teardown_method(self):
        _cleanup(self._PREFIX)

    def test_dismissed_listing_disappears_from_feed(self):
        db.insert_listing(make_listing(source_id="dm-001", score=9.0))
        listing_id = _get_id_by_source_id("dm-001")
        db.set_dismissed(listing_id, 1)
        feed = db.get_feed(threshold=7.0)
        ids = [r["source_id"] for r in feed]
        assert "dm-001" not in ids


# ---------------------------------------------------------------------------
# Applied
# ---------------------------------------------------------------------------

class TestApplied:
    _PREFIX = "ap-"

    def teardown_method(self):
        _cleanup(self._PREFIX)

    def test_applied_listing_appears_in_get_applied(self):
        db.insert_listing(make_listing(source_id="ap-001", score=8.0))
        listing_id = _get_id_by_source_id("ap-001")
        db.set_applied(listing_id, 1)
        applied = db.get_applied()
        ids = [r["source_id"] for r in applied]
        assert "ap-001" in ids

    def test_unapplied_listing_absent_from_get_applied(self):
        db.insert_listing(make_listing(source_id="ap-002", score=8.0, applied=0))
        applied = db.get_applied()
        ids = [r["source_id"] for r in applied]
        assert "ap-002" not in ids

    def test_unapply_removes_from_applied(self):
        db.insert_listing(make_listing(source_id="ap-003", score=8.0))
        listing_id = _get_id_by_source_id("ap-003")
        db.set_applied(listing_id, 1)
        db.set_applied(listing_id, 0)
        applied = db.get_applied()
        ids = [r["source_id"] for r in applied]
        assert "ap-003" not in ids


# ---------------------------------------------------------------------------
# JSON array columns
# ---------------------------------------------------------------------------

class TestJsonColumns:
    _PREFIX = "json-"

    def teardown_method(self):
        _cleanup(self._PREFIX)

    def test_matched_skills_round_trips_as_list(self):
        skills = ["Python", "FastAPI", "PostgreSQL"]
        db.insert_listing(make_listing(source_id="json-001", matched_skills=skills))
        feed = db.get_feed(threshold=7.0)
        row = next(r for r in feed if r["source_id"] == "json-001")
        assert row["matched_skills"] == skills

    def test_empty_list_round_trips(self):
        db.insert_listing(make_listing(source_id="json-002", concerns=[]))
        feed = db.get_feed(threshold=7.0)
        row = next(r for r in feed if r["source_id"] == "json-002")
        assert row["concerns"] == []

    def test_none_skills_retrieved_as_empty_list(self):
        listing = make_listing(source_id="json-003")
        listing["matched_skills"] = None
        db.insert_listing(listing)
        feed = db.get_feed(threshold=7.0)
        row = next(r for r in feed if r["source_id"] == "json-003")
        assert row["matched_skills"] == []


# ---------------------------------------------------------------------------
# model_used column
# ---------------------------------------------------------------------------

class TestModelUsed:
    _PREFIX = "mu-"

    def teardown_method(self):
        _cleanup(self._PREFIX)

    def test_model_used_stored_and_retrieved_via_insert(self):
        db.insert_listing(
            make_listing(source_id="mu-001", score=8.0, model_used="claude-haiku-4-5")
        )
        with db.get_connection() as conn:
            row = conn.execute(
                "SELECT model_used FROM listings WHERE source_id = %s", ("mu-001",)
            ).fetchone()
        assert row["model_used"] == "claude-haiku-4-5"

    def test_model_used_defaults_to_null_when_absent(self):
        db.insert_listing(make_listing(source_id="mu-002", score=8.0))
        with db.get_connection() as conn:
            row = conn.execute(
                "SELECT model_used FROM listings WHERE source_id = %s", ("mu-002",)
            ).fetchone()
        assert row["model_used"] is None

    def test_update_score_writes_model_used(self):
        db.insert_listing(make_listing(source_id="mu-003", score=None, seen=0))
        db.update_score(
            "adzuna",
            "mu-003",
            {
                "score": 7.5,
                "matched_skills": ["Python"],
                "missing_skills": [],
                "concerns": [],
                "verdict": "Good fit.",
                "tokens_input": 100,
                "tokens_output": 50,
                "model_used": "claude-haiku-4-5",
            },
        )
        with db.get_connection() as conn:
            row = conn.execute(
                "SELECT model_used, seen FROM listings WHERE source_id = %s", ("mu-003",)
            ).fetchone()
        assert row["model_used"] == "claude-haiku-4-5"
        assert row["seen"] == 1

    def test_update_score_model_used_none_when_absent(self):
        db.insert_listing(make_listing(source_id="mu-004", score=None, seen=0))
        db.update_score(
            "adzuna",
            "mu-004",
            {
                "score": 6.0,
                "matched_skills": [],
                "missing_skills": ["Go"],
                "concerns": ["contract only"],
                "verdict": "Weak fit.",
                "tokens_input": 80,
                "tokens_output": 40,
                # model_used intentionally omitted
            },
        )
        with db.get_connection() as conn:
            row = conn.execute(
                "SELECT model_used FROM listings WHERE source_id = %s", ("mu-004",)
            ).fetchone()
        assert row["model_used"] is None


# ---------------------------------------------------------------------------
# Cross-source dedup
# ---------------------------------------------------------------------------

class TestCrossSourceDedup:
    _PREFIX = "cs-"

    def teardown_method(self):
        _cleanup(self._PREFIX, "url-", "xsd-", "job-")

    def test_listing_exists_by_source_and_id(self):
        db.insert_listing(make_listing(source_id="cs-001", source="adzuna"))
        with db.get_connection() as conn:
            assert db.listing_exists(conn, "adzuna", "cs-001") is True
            # Same source_id but a different source — not a dupe.
            assert db.listing_exists(conn, "remotive", "cs-001") is False

    def test_listing_exists_by_url(self):
        db.insert_listing(
            make_listing(
                source_id="url-001",
                redirect_url="https://example.com/job/unique-url",
            )
        )
        with db.get_connection() as conn:
            assert db.listing_exists_by_url(conn, "https://example.com/job/unique-url") is True
            assert db.listing_exists_by_url(conn, "https://example.com/job/other-url") is False

    def test_cross_source_url_dedup(self):
        db.insert_listing(
            make_listing(
                source_id="xsd-001",
                source="adzuna",
                redirect_url="https://example.com/job/shared-url",
            )
        )
        with db.get_connection() as conn:
            assert db.listing_exists(conn, "remotive", "xsd-rem-999") is False
            assert db.listing_exists_by_url(conn, "https://example.com/job/shared-url") is True

    def test_source_id_unique_per_source(self):
        db.insert_listing(
            make_listing(
                source_id="job-42",
                source="adzuna",
                redirect_url="https://adzuna.com/job/42",
            )
        )
        # Different source, same source_id — should succeed without raising.
        db.insert_listing(
            make_listing(
                source_id="job-42",
                source="remotive",
                redirect_url="https://remotive.com/job/42",
            )
        )
        with db.get_connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM listings WHERE source_id = 'job-42'"
            ).fetchone()
        assert row["cnt"] == 2


# ---------------------------------------------------------------------------
# get_last_fetch_time
# ---------------------------------------------------------------------------

class TestGetLastFetchTime:
    _PREFIX = "lft-"

    def teardown_method(self):
        _cleanup(self._PREFIX)

    def test_returns_datetime_for_single_listing(self):
        db.insert_listing(
            make_listing(source_id="lft-001", fetched_at="2026-01-15T10:00:00Z")
        )
        result = db.get_last_fetch_time()
        assert isinstance(result, datetime.datetime)

    def test_returns_most_recent_when_multiple_listings(self):
        db.insert_listing(make_listing(source_id="lft-a1", fetched_at="2026-01-10T08:00:00Z"))
        db.insert_listing(make_listing(source_id="lft-a2", fetched_at="2026-03-20T14:30:00Z"))
        db.insert_listing(make_listing(source_id="lft-a3", fetched_at="2026-02-05T00:00:00Z"))
        result = db.get_last_fetch_time()
        # The result should be >= 2026-03-20 (the maximum we inserted).
        assert isinstance(result, datetime.datetime)

    def test_handles_fetched_at_without_trailing_z(self):
        db.insert_listing(
            make_listing(source_id="lft-002", fetched_at="2026-06-01T12:00:00")
        )
        result = db.get_last_fetch_time()
        assert isinstance(result, datetime.datetime)


# ---------------------------------------------------------------------------
# posted_at column
# ---------------------------------------------------------------------------

class TestPostedAt:
    _PREFIX = "pa-"

    def teardown_method(self):
        _cleanup(self._PREFIX, "sc-", "null-", "real-")

    def test_posted_at_stored_and_retrieved(self):
        db.insert_listing(
            make_listing(source_id="pa-001", score=8.0, posted_at="2026-03-01T09:00:00Z")
        )
        feed = db.get_feed(threshold=7.0)
        row = next(r for r in feed if r["source_id"] == "pa-001")
        assert row["posted_at"] == "2026-03-01T09:00:00Z"

    def test_posted_at_null_when_not_supplied(self):
        listing = make_listing(source_id="pa-002", score=8.0)
        listing.pop("posted_at", None)
        db.insert_listing(listing)
        with db.get_connection() as conn:
            row = conn.execute(
                "SELECT posted_at FROM listings WHERE source_id = %s", ("pa-002",)
            ).fetchone()
        assert row["posted_at"] is None

    def test_sort_date_posted_orders_newest_first(self):
        db.insert_listing(
            make_listing(source_id="pa-old", score=9.0, posted_at="2026-01-01T00:00:00Z")
        )
        db.insert_listing(
            make_listing(source_id="pa-new", score=7.0, posted_at="2026-03-20T00:00:00Z")
        )
        db.insert_listing(
            make_listing(source_id="pa-mid", score=8.0, posted_at="2026-02-15T00:00:00Z")
        )
        results = db.get_feed(threshold=7.0, sort="date_posted")
        pa_results = [r for r in results if r["source_id"] in ("pa-old", "pa-new", "pa-mid")]
        ids = [r["source_id"] for r in pa_results]
        assert ids == ["pa-new", "pa-mid", "pa-old"]

    def test_sort_default_still_orders_by_score(self):
        db.insert_listing(
            make_listing(source_id="sc-low", score=7.5, posted_at="2026-03-25T00:00:00Z")
        )
        db.insert_listing(
            make_listing(source_id="sc-high", score=9.5, posted_at="2026-01-01T00:00:00Z")
        )
        results = db.get_feed(threshold=7.0)
        sc_results = [r for r in results if r["source_id"] in ("sc-low", "sc-high")]
        assert sc_results[0]["source_id"] == "sc-high"

    def test_null_posted_at_does_not_crash_sort(self):
        db.insert_listing(
            make_listing(source_id="null-pa", score=8.0, posted_at=None)
        )
        db.insert_listing(
            make_listing(source_id="real-pa", score=7.5, posted_at="2026-03-01T00:00:00Z")
        )
        results = db.get_feed(threshold=7.0, sort="date_posted")
        ids = [r["source_id"] for r in results]
        assert "null-pa" in ids
        assert "real-pa" in ids


# ---------------------------------------------------------------------------
# Atomic toggle helpers
# ---------------------------------------------------------------------------

class TestToggleBookmarked:
    _PREFIX = "tb-"

    def teardown_method(self):
        _cleanup(self._PREFIX)

    def _insert_and_get_id(self, source_id: str, bookmarked: int = 0) -> int:
        db.insert_listing(make_listing(source_id=source_id, bookmarked=bookmarked))
        return _get_id_by_source_id(source_id)

    def test_toggle_bookmarked_flips_zero_to_one(self):
        listing_id = self._insert_and_get_id("tb-001", bookmarked=0)
        result = db.toggle_bookmarked(listing_id)
        assert result is not None
        assert result["bookmarked"] == 1

    def test_toggle_bookmarked_flips_one_to_zero(self):
        listing_id = self._insert_and_get_id("tb-002", bookmarked=1)
        result = db.toggle_bookmarked(listing_id)
        assert result is not None
        assert result["bookmarked"] == 0

    def test_toggle_bookmarked_twice_returns_to_original(self):
        listing_id = self._insert_and_get_id("tb-003", bookmarked=0)
        db.toggle_bookmarked(listing_id)
        result = db.toggle_bookmarked(listing_id)
        assert result is not None
        assert result["bookmarked"] == 0

    def test_toggle_bookmarked_returns_none_for_missing_id(self):
        result = db.toggle_bookmarked(999999999)
        assert result is None

    def test_toggle_bookmarked_returns_full_listing_dict(self):
        listing_id = self._insert_and_get_id("tb-004", bookmarked=0)
        result = db.toggle_bookmarked(listing_id)
        assert result is not None
        assert result["source_id"] == "tb-004"
        assert "score" in result
        assert isinstance(result["matched_skills"], list)


class TestToggleApplied:
    _PREFIX = "ta-"

    def teardown_method(self):
        _cleanup(self._PREFIX)

    def _insert_and_get_id(self, source_id: str, applied: int = 0) -> int:
        db.insert_listing(make_listing(source_id=source_id, applied=applied))
        return _get_id_by_source_id(source_id)

    def test_toggle_applied_flips_zero_to_one(self):
        listing_id = self._insert_and_get_id("ta-001", applied=0)
        result = db.toggle_applied(listing_id)
        assert result is not None
        assert result["applied"] == 1

    def test_toggle_applied_flips_one_to_zero(self):
        listing_id = self._insert_and_get_id("ta-002", applied=1)
        result = db.toggle_applied(listing_id)
        assert result is not None
        assert result["applied"] == 0

    def test_toggle_applied_twice_returns_to_original(self):
        listing_id = self._insert_and_get_id("ta-003", applied=0)
        db.toggle_applied(listing_id)
        result = db.toggle_applied(listing_id)
        assert result is not None
        assert result["applied"] == 0

    def test_toggle_applied_returns_none_for_missing_id(self):
        result = db.toggle_applied(999999999)
        assert result is None

    def test_toggle_applied_returns_full_listing_dict(self):
        listing_id = self._insert_and_get_id("ta-004", applied=0)
        result = db.toggle_applied(listing_id)
        assert result is not None
        assert result["source_id"] == "ta-004"
        assert "score" in result
        assert isinstance(result["matched_skills"], list)


# ---------------------------------------------------------------------------
# source field present in all read helpers
# ---------------------------------------------------------------------------

class TestSourceFieldInReadHelpers:
    _PREFIX = "src-"

    def teardown_method(self):
        _cleanup(self._PREFIX)

    def test_get_feed_includes_source(self):
        db.insert_listing(
            make_listing(source="himalayas", source_id="src-f-001", score=9.0)
        )
        results = db.get_feed(threshold=7.0)
        our = next(r for r in results if r["source_id"] == "src-f-001")
        assert "source" in our
        assert our["source"] == "himalayas"

    def test_get_bookmarks_includes_source(self):
        db.insert_listing(
            make_listing(source="jobicy", source_id="src-b-001", bookmarked=1)
        )
        bookmarks = db.get_bookmarks()
        our = next(r for r in bookmarks if r["source_id"] == "src-b-001")
        assert "source" in our
        assert our["source"] == "jobicy"

    def test_get_applied_includes_source(self):
        db.insert_listing(
            make_listing(source="jooble", source_id="src-a-001", applied=1)
        )
        applied = db.get_applied()
        our = next(r for r in applied if r["source_id"] == "src-a-001")
        assert "source" in our
        assert our["source"] == "jooble"

    def test_get_listing_by_id_includes_source(self):
        db.insert_listing(
            make_listing(source="arbeitnow", source_id="src-i-001")
        )
        listing_id = _get_id_by_source_id("src-i-001")
        result = db.get_listing_by_id(listing_id)
        assert result is not None
        assert "source" in result
        assert result["source"] == "arbeitnow"

    def test_source_defaults_to_adzuna(self):
        listing = make_listing(source_id="src-def-001")
        listing["source"] = "adzuna"
        db.insert_listing(listing)
        results = db.get_feed(threshold=7.0)
        our = next(r for r in results if r["source_id"] == "src-def-001")
        assert our["source"] == "adzuna"
