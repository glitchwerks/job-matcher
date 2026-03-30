"""
tests/test_db.py — Unit tests for db.py using a temporary SQLite file.

Each test class creates a fresh database in a NamedTemporaryFile so tests
are fully isolated from each other and from jobs.db in the project root.
"""

import os
import sys
import tempfile

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
    }


class TempDB:
    """Context manager that creates a temp SQLite file and removes it on exit."""

    def __enter__(self) -> str:
        self._fh = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._fh.close()
        self.path = self._fh.name
        db.init_db(self.path)
        return self.path

    def __exit__(self, *_):
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass


# ---------------------------------------------------------------------------
# init_db
# ---------------------------------------------------------------------------

class TestInitDb:
    def test_creates_listings_table(self):
        """init_db() creates the listings table in a fresh database."""
        with TempDB() as path:
            conn = db.get_connection(path)
            try:
                rows = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='listings'"
                ).fetchall()
                assert len(rows) == 1
            finally:
                conn.close()

    def test_idempotent(self):
        """Calling init_db() twice on the same file does not raise."""
        with TempDB() as path:
            db.init_db(path)  # second call
            # If we get here without exception, the test passes.


# ---------------------------------------------------------------------------
# listing_exists
# ---------------------------------------------------------------------------

class TestListingExists:
    def test_returns_false_for_unknown_id(self):
        with TempDB() as path:
            conn = db.get_connection(path)
            try:
                assert db.listing_exists(conn, "adzuna", "nonexistent-id") is False
            finally:
                conn.close()

    def test_returns_true_after_insert(self):
        with TempDB() as path:
            listing = make_listing(source_id="exists-001")
            db.insert_listing(listing, db_path=path)
            conn = db.get_connection(path)
            try:
                assert db.listing_exists(conn, "adzuna", "exists-001") is True
            finally:
                conn.close()

    def test_returns_false_for_different_id(self):
        """Existence check is specific to the queried (source, source_id) pair."""
        with TempDB() as path:
            db.insert_listing(make_listing(source_id="abc"), db_path=path)
            conn = db.get_connection(path)
            try:
                assert db.listing_exists(conn, "adzuna", "xyz") is False
            finally:
                conn.close()


# ---------------------------------------------------------------------------
# insert_listing + get_feed
# ---------------------------------------------------------------------------

class TestInsertAndFeed:
    def test_inserted_listing_appears_in_feed(self):
        """An inserted listing appears in get_feed() at the correct threshold."""
        with TempDB() as path:
            db.insert_listing(make_listing(source_id="feed-001", score=8.0), db_path=path)
            results = db.get_feed(threshold=7.0, db_path=path)
            ids = [r["source_id"] for r in results]
            assert "feed-001" in ids

    def test_listing_below_threshold_excluded(self):
        """Listings whose score is below the threshold do not appear in the feed."""
        with TempDB() as path:
            db.insert_listing(make_listing(source_id="low-score", score=4.0), db_path=path)
            results = db.get_feed(threshold=7.0, db_path=path)
            ids = [r["source_id"] for r in results]
            assert "low-score" not in ids

    def test_dismissed_listing_excluded_from_feed(self):
        """Dismissed listings (dismissed=1) do not appear in get_feed()."""
        with TempDB() as path:
            listing = make_listing(source_id="dismissed-001", score=9.0, dismissed=1)
            db.insert_listing(listing, db_path=path)
            results = db.get_feed(threshold=7.0, db_path=path)
            ids = [r["source_id"] for r in results]
            assert "dismissed-001" not in ids

    def test_applied_listing_excluded_from_feed(self):
        """Applied listings (applied=1) do not appear in get_feed()."""
        with TempDB() as path:
            listing = make_listing(source_id="applied-001", score=9.0, applied=1)
            db.insert_listing(listing, db_path=path)
            results = db.get_feed(threshold=7.0, db_path=path)
            ids = [r["source_id"] for r in results]
            assert "applied-001" not in ids

    def test_feed_ordered_by_score_descending(self):
        """Feed results are sorted highest score first."""
        with TempDB() as path:
            db.insert_listing(make_listing(source_id="score-7", score=7.0), db_path=path)
            db.insert_listing(make_listing(source_id="score-9", score=9.0), db_path=path)
            db.insert_listing(make_listing(source_id="score-8", score=8.0), db_path=path)
            results = db.get_feed(threshold=7.0, db_path=path)
            scores = [r["score"] for r in results]
            assert scores == sorted(scores, reverse=True)


# ---------------------------------------------------------------------------
# get_feed search and remote_only filters
# ---------------------------------------------------------------------------

class TestFeedFilters:
    def test_search_filter_by_title(self):
        """get_feed(search=...) returns only listings whose title contains the term."""
        with TempDB() as path:
            db.insert_listing(make_listing(source_id="py-001", title="Python Developer", score=8.0), db_path=path)
            db.insert_listing(make_listing(source_id="java-001", title="Java Developer", score=8.0), db_path=path)
            results = db.get_feed(threshold=7.0, search="python", db_path=path)
            ids = [r["source_id"] for r in results]
            assert "py-001" in ids
            assert "java-001" not in ids

    def test_search_filter_case_insensitive(self):
        """Search filter is case-insensitive."""
        with TempDB() as path:
            db.insert_listing(make_listing(source_id="ci-001", title="PYTHON DEVELOPER", score=8.0), db_path=path)
            results = db.get_feed(threshold=7.0, search="python", db_path=path)
            assert any(r["source_id"] == "ci-001" for r in results)

    def test_search_filter_by_company(self):
        """Search filter also matches on company name."""
        with TempDB() as path:
            db.insert_listing(
                make_listing(source_id="co-001", title="Engineer", company="Acme Corp", score=8.0),
                db_path=path,
            )
            results = db.get_feed(threshold=7.0, search="acme", db_path=path)
            assert any(r["source_id"] == "co-001" for r in results)

    def test_remote_only_filter(self):
        """remote_only=True returns only listings whose location contains 'remote'."""
        with TempDB() as path:
            db.insert_listing(
                make_listing(source_id="remote-001", location="Remote, US", score=8.0),
                db_path=path,
            )
            db.insert_listing(
                make_listing(source_id="office-001", location="New York, NY", score=8.0),
                db_path=path,
            )
            results = db.get_feed(threshold=7.0, remote_only=True, db_path=path)
            ids = [r["source_id"] for r in results]
            assert "remote-001" in ids
            assert "office-001" not in ids

    def test_remote_only_case_insensitive(self):
        """remote_only filter matches 'REMOTE' in location regardless of case."""
        with TempDB() as path:
            db.insert_listing(
                make_listing(source_id="remote-upper", location="REMOTE ONLY", score=8.0),
                db_path=path,
            )
            results = db.get_feed(threshold=7.0, remote_only=True, db_path=path)
            assert any(r["source_id"] == "remote-upper" for r in results)


# ---------------------------------------------------------------------------
# Bookmarks
# ---------------------------------------------------------------------------

class TestBookmarks:
    def test_bookmarked_listing_appears_in_get_bookmarks(self):
        """set_bookmarked(id, 1) causes the listing to appear in get_bookmarks()."""
        with TempDB() as path:
            db.insert_listing(make_listing(source_id="bm-001", score=8.0), db_path=path)
            conn = db.get_connection(path)
            try:
                row = conn.execute("SELECT id FROM listings WHERE source_id = 'bm-001'").fetchone()
                listing_id = row["id"]
            finally:
                conn.close()

            db.set_bookmarked(listing_id, 1, db_path=path)
            bookmarks = db.get_bookmarks(db_path=path)
            ids = [r["source_id"] for r in bookmarks]
            assert "bm-001" in ids

    def test_unbookmark_removes_from_bookmarks(self):
        """set_bookmarked(id, 0) removes a previously bookmarked listing."""
        with TempDB() as path:
            db.insert_listing(make_listing(source_id="bm-002", score=8.0), db_path=path)
            conn = db.get_connection(path)
            try:
                row = conn.execute("SELECT id FROM listings WHERE source_id = 'bm-002'").fetchone()
                listing_id = row["id"]
            finally:
                conn.close()

            db.set_bookmarked(listing_id, 1, db_path=path)
            db.set_bookmarked(listing_id, 0, db_path=path)
            bookmarks = db.get_bookmarks(db_path=path)
            ids = [r["source_id"] for r in bookmarks]
            assert "bm-002" not in ids

    def test_non_bookmarked_listing_absent_from_bookmarks(self):
        """A listing that has never been bookmarked does not appear in get_bookmarks()."""
        with TempDB() as path:
            db.insert_listing(make_listing(source_id="bm-003", score=8.0, bookmarked=0), db_path=path)
            bookmarks = db.get_bookmarks(db_path=path)
            ids = [r["source_id"] for r in bookmarks]
            assert "bm-003" not in ids


# ---------------------------------------------------------------------------
# Dismissed
# ---------------------------------------------------------------------------

class TestDismissed:
    def test_dismissed_listing_disappears_from_feed(self):
        """After set_dismissed(id, 1) the listing no longer appears in get_feed()."""
        with TempDB() as path:
            db.insert_listing(make_listing(source_id="dm-001", score=9.0), db_path=path)
            conn = db.get_connection(path)
            try:
                row = conn.execute("SELECT id FROM listings WHERE source_id = 'dm-001'").fetchone()
                listing_id = row["id"]
            finally:
                conn.close()

            db.set_dismissed(listing_id, 1, db_path=path)
            feed = db.get_feed(threshold=7.0, db_path=path)
            ids = [r["source_id"] for r in feed]
            assert "dm-001" not in ids


# ---------------------------------------------------------------------------
# Applied
# ---------------------------------------------------------------------------

class TestApplied:
    def test_applied_listing_appears_in_get_applied(self):
        """set_applied(id, 1) causes the listing to appear in get_applied()."""
        with TempDB() as path:
            db.insert_listing(make_listing(source_id="ap-001", score=8.0), db_path=path)
            conn = db.get_connection(path)
            try:
                row = conn.execute("SELECT id FROM listings WHERE source_id = 'ap-001'").fetchone()
                listing_id = row["id"]
            finally:
                conn.close()

            db.set_applied(listing_id, 1, db_path=path)
            applied = db.get_applied(db_path=path)
            ids = [r["source_id"] for r in applied]
            assert "ap-001" in ids

    def test_unapplied_listing_absent_from_get_applied(self):
        """A listing with applied=0 does not appear in get_applied()."""
        with TempDB() as path:
            db.insert_listing(make_listing(source_id="ap-002", score=8.0, applied=0), db_path=path)
            applied = db.get_applied(db_path=path)
            ids = [r["source_id"] for r in applied]
            assert "ap-002" not in ids

    def test_unapply_removes_from_applied(self):
        """set_applied(id, 0) removes a listing from get_applied()."""
        with TempDB() as path:
            db.insert_listing(make_listing(source_id="ap-003", score=8.0), db_path=path)
            conn = db.get_connection(path)
            try:
                row = conn.execute("SELECT id FROM listings WHERE source_id = 'ap-003'").fetchone()
                listing_id = row["id"]
            finally:
                conn.close()

            db.set_applied(listing_id, 1, db_path=path)
            db.set_applied(listing_id, 0, db_path=path)
            applied = db.get_applied(db_path=path)
            ids = [r["source_id"] for r in applied]
            assert "ap-003" not in ids


# ---------------------------------------------------------------------------
# JSON array columns
# ---------------------------------------------------------------------------

class TestJsonColumns:
    def test_matched_skills_round_trips_as_list(self):
        """matched_skills stored as a Python list is retrieved as a Python list."""
        with TempDB() as path:
            skills = ["Python", "FastAPI", "PostgreSQL"]
            db.insert_listing(
                make_listing(source_id="json-001", matched_skills=skills),
                db_path=path,
            )
            feed = db.get_feed(threshold=7.0, db_path=path)
            row = next(r for r in feed if r["source_id"] == "json-001")
            assert row["matched_skills"] == skills

    def test_empty_list_round_trips(self):
        """An empty list for concerns is stored and retrieved as an empty list."""
        with TempDB() as path:
            db.insert_listing(
                make_listing(source_id="json-002", concerns=[]),
                db_path=path,
            )
            feed = db.get_feed(threshold=7.0, db_path=path)
            row = next(r for r in feed if r["source_id"] == "json-002")
            assert row["concerns"] == []

    def test_none_skills_retrieved_as_empty_list(self):
        """When matched_skills is None on insert it is retrieved as an empty list."""
        with TempDB() as path:
            listing = make_listing(source_id="json-003")
            listing["matched_skills"] = None
            db.insert_listing(listing, db_path=path)
            feed = db.get_feed(threshold=7.0, db_path=path)
            row = next(r for r in feed if r["source_id"] == "json-003")
            assert row["matched_skills"] == []


# ---------------------------------------------------------------------------
# model_used column
# ---------------------------------------------------------------------------

class TestModelUsed:
    def test_model_used_stored_and_retrieved_via_insert(self):
        """model_used set in the listing dict is persisted and readable."""
        with TempDB() as path:
            db.insert_listing(
                make_listing(source_id="mu-001", score=8.0, model_used="claude-haiku-4-5"),
                db_path=path,
            )
            conn = db.get_connection(path)
            try:
                row = conn.execute(
                    "SELECT model_used FROM listings WHERE source_id = 'mu-001'"
                ).fetchone()
                assert row["model_used"] == "claude-haiku-4-5"
            finally:
                conn.close()

    def test_model_used_defaults_to_null_when_absent(self):
        """If model_used is not supplied to insert_listing(), it is stored as NULL."""
        with TempDB() as path:
            # make_listing() passes model_used=None by default.
            db.insert_listing(make_listing(source_id="mu-002", score=8.0), db_path=path)
            conn = db.get_connection(path)
            try:
                row = conn.execute(
                    "SELECT model_used FROM listings WHERE source_id = 'mu-002'"
                ).fetchone()
                assert row["model_used"] is None
            finally:
                conn.close()

    def test_update_score_writes_model_used(self):
        """update_score() persists model_used from score_data."""
        with TempDB() as path:
            db.insert_listing(make_listing(source_id="mu-003", score=None, seen=0), db_path=path)
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
                db_path=path,
            )
            conn = db.get_connection(path)
            try:
                row = conn.execute(
                    "SELECT model_used, seen FROM listings WHERE source_id = 'mu-003'"
                ).fetchone()
                assert row["model_used"] == "claude-haiku-4-5"
                assert row["seen"] == 1
            finally:
                conn.close()

    def test_update_score_model_used_none_when_absent(self):
        """update_score() stores NULL for model_used when not present in score_data."""
        with TempDB() as path:
            db.insert_listing(make_listing(source_id="mu-004", score=None, seen=0), db_path=path)
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
                db_path=path,
            )
            conn = db.get_connection(path)
            try:
                row = conn.execute(
                    "SELECT model_used FROM listings WHERE source_id = 'mu-004'"
                ).fetchone()
                assert row["model_used"] is None
            finally:
                conn.close()

    def test_column_exists_in_schema(self):
        """init_db() creates the model_used column (verifiable via PRAGMA)."""
        with TempDB() as path:
            conn = db.get_connection(path)
            try:
                cols = conn.execute("PRAGMA table_info(listings)").fetchall()
                col_names = [c["name"] for c in cols]
                assert "model_used" in col_names
            finally:
                conn.close()

    def test_migration_on_existing_db_without_model_used(self):
        """init_db() on a legacy adzuna_id-based table adds model_used via migration.

        Simulates a pre-migration database that has ``adzuna_id`` but no
        ``model_used`` column, then verifies that init_db() migrates the
        schema to include ``model_used`` (via the Path B table-copy migration).
        """
        with TempDB() as path:
            # Drop the freshly created table and replace it with a minimal
            # legacy schema that has adzuna_id and no model_used.
            conn = db.get_connection(path)
            try:
                conn.execute("DROP TABLE listings")
                conn.execute("""
                    CREATE TABLE listings (
                        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                        adzuna_id           TEXT UNIQUE NOT NULL,
                        title               TEXT,
                        score               REAL,
                        seen                INTEGER DEFAULT 0
                    )
                """)
                conn.execute(
                    "INSERT INTO listings (adzuna_id, title, score, seen) "
                    "VALUES ('legacy-mu-001', 'Old Job', 8.0, 1)"
                )
                conn.commit()
            finally:
                conn.close()

            # Run init_db() — Path B migration copies to canonical schema.
            db.init_db(path)

            conn = db.get_connection(path)
            try:
                cols = conn.execute("PRAGMA table_info(listings)").fetchall()
                col_names = [c["name"] for c in cols]
                assert "model_used" in col_names
                assert "source_id" in col_names
                # adzuna_id must no longer appear after the migration.
                assert "adzuna_id" not in col_names
            finally:
                conn.close()


# ---------------------------------------------------------------------------
# Cross-source dedup
# ---------------------------------------------------------------------------

class TestCrossSourceDedup:
    def test_listing_exists_by_source_and_id(self):
        """listing_exists() returns True for the correct (source, source_id) pair
        and False when the source differs (same source_id, different source is not a dupe)."""
        with TempDB() as path:
            db.insert_listing(
                make_listing(source_id="cs-001", source="adzuna"),
                db_path=path,
            )
            conn = db.get_connection(path)
            try:
                assert db.listing_exists(conn, "adzuna", "cs-001") is True
                # Same source_id but a different source — not a dupe.
                assert db.listing_exists(conn, "remotive", "cs-001") is False
            finally:
                conn.close()

    def test_listing_exists_by_url(self):
        """listing_exists_by_url() returns True for a URL that is already stored
        and False for a URL that has not been seen."""
        with TempDB() as path:
            db.insert_listing(
                make_listing(
                    source_id="url-001",
                    redirect_url="https://example.com/job/unique-url",
                ),
                db_path=path,
            )
            conn = db.get_connection(path)
            try:
                assert db.listing_exists_by_url(conn, "https://example.com/job/unique-url") is True
                assert db.listing_exists_by_url(conn, "https://example.com/job/other-url") is False
            finally:
                conn.close()

    def test_cross_source_url_dedup(self):
        """A listing stored under 'adzuna' is caught as a dupe by URL when a
        second source posts the same redirect_url."""
        with TempDB() as path:
            db.insert_listing(
                make_listing(
                    source_id="xsd-001",
                    source="adzuna",
                    redirect_url="https://example.com/job/shared-url",
                ),
                db_path=path,
            )
            conn = db.get_connection(path)
            try:
                # listing_exists() returns False for a different source ...
                assert db.listing_exists(conn, "remotive", "rem-999") is False
                # ... but listing_exists_by_url() catches the shared URL.
                assert db.listing_exists_by_url(conn, "https://example.com/job/shared-url") is True
            finally:
                conn.close()

    def test_source_id_unique_per_source(self):
        """Two listings that share the same source_id but differ in source can
        both be inserted without violating the unique index on (source, source_id)."""
        with TempDB() as path:
            db.insert_listing(
                make_listing(
                    source_id="job-42",
                    source="adzuna",
                    redirect_url="https://adzuna.com/job/42",
                ),
                db_path=path,
            )
            # Different source, same source_id — should succeed without raising.
            db.insert_listing(
                make_listing(
                    source_id="job-42",
                    source="remotive",
                    redirect_url="https://remotive.com/job/42",
                ),
                db_path=path,
            )

            conn = db.get_connection(path)
            try:
                count = conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
                assert count == 2
            finally:
                conn.close()


# ---------------------------------------------------------------------------
# Migration backfill
# ---------------------------------------------------------------------------

class TestMigrationBackfill:
    def test_backfill_sets_source_and_source_id_for_existing_rows(self):
        """init_db() migrates a legacy adzuna_id-based table: renames the column
        to source_id and backfills source='adzuna' for all existing rows."""
        with TempDB() as path:
            # Simulate a pre-migration database that has adzuna_id but no
            # source or source_id columns (Path B in init_db).
            conn = db.get_connection(path)
            try:
                conn.execute("DROP TABLE listings")
                conn.execute("""
                    CREATE TABLE listings (
                        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                        adzuna_id           TEXT UNIQUE NOT NULL,
                        title               TEXT,
                        score               REAL,
                        seen                INTEGER DEFAULT 0,
                        redirect_url        TEXT,
                        fetched_at          TEXT,
                        created_at          TEXT,
                        company             TEXT,
                        location            TEXT,
                        salary_min          INTEGER,
                        salary_max          INTEGER,
                        salary_is_predicted INTEGER,
                        contract_type       TEXT,
                        contract_time       TEXT,
                        description         TEXT,
                        matched_skills      TEXT,
                        missing_skills      TEXT,
                        concerns            TEXT,
                        verdict             TEXT,
                        bookmarked          INTEGER DEFAULT 0,
                        dismissed           INTEGER DEFAULT 0,
                        applied             INTEGER DEFAULT 0,
                        job_type            TEXT,
                        model_used          TEXT,
                        tokens_input        INTEGER,
                        tokens_output       INTEGER
                    )
                """)
                conn.execute(
                    "INSERT INTO listings (adzuna_id, title, score, seen) "
                    "VALUES ('legacy-001', 'Old Job', 7.0, 1)"
                )
                conn.commit()
            finally:
                conn.close()

            # Run init_db() — Path B migration copies adzuna_id → source_id.
            db.init_db(path)

            conn = db.get_connection(path)
            try:
                row = conn.execute(
                    "SELECT source, source_id FROM listings WHERE source_id = 'legacy-001'"
                ).fetchone()
                assert row["source"] == "adzuna"
                assert row["source_id"] == "legacy-001"
            finally:
                conn.close()


# ---------------------------------------------------------------------------
# get_last_fetch_time
# ---------------------------------------------------------------------------

class TestGetLastFetchTime:
    def test_returns_none_when_table_is_empty(self):
        """get_last_fetch_time() returns None when no listings exist."""
        with TempDB() as path:
            result = db.get_last_fetch_time(db_path=path)
            assert result is None

    def test_returns_datetime_for_single_listing(self):
        """get_last_fetch_time() returns a datetime when one listing exists."""
        import datetime
        with TempDB() as path:
            db.insert_listing(
                make_listing(fetched_at="2026-01-15T10:00:00Z"),
                db_path=path,
            )
            result = db.get_last_fetch_time(db_path=path)
            assert isinstance(result, datetime.datetime)
            assert result.year == 2026
            assert result.month == 1
            assert result.day == 15

    def test_returns_most_recent_when_multiple_listings(self):
        """get_last_fetch_time() returns the MAX fetched_at across all listings."""
        import datetime
        with TempDB() as path:
            db.insert_listing(
                make_listing(source_id="a1", fetched_at="2026-01-10T08:00:00Z"),
                db_path=path,
            )
            db.insert_listing(
                make_listing(source_id="a2", fetched_at="2026-03-20T14:30:00Z"),
                db_path=path,
            )
            db.insert_listing(
                make_listing(source_id="a3", fetched_at="2026-02-05T00:00:00Z"),
                db_path=path,
            )
            result = db.get_last_fetch_time(db_path=path)
            assert isinstance(result, datetime.datetime)
            # 2026-03-20 is the latest
            assert result.month == 3
            assert result.day == 20

    def test_handles_fetched_at_without_trailing_z(self):
        """get_last_fetch_time() parses ISO strings that lack a trailing 'Z'."""
        import datetime
        with TempDB() as path:
            db.insert_listing(
                make_listing(fetched_at="2026-06-01T12:00:00"),
                db_path=path,
            )
            result = db.get_last_fetch_time(db_path=path)
            assert isinstance(result, datetime.datetime)
            assert result.year == 2026
            assert result.month == 6


# ---------------------------------------------------------------------------
# posted_at column — migration, storage, and sort
# ---------------------------------------------------------------------------

class TestPostedAt:
    def test_column_exists_in_schema(self):
        """init_db() creates the posted_at column (verifiable via PRAGMA)."""
        with TempDB() as path:
            conn = db.get_connection(path)
            try:
                cols = conn.execute("PRAGMA table_info(listings)").fetchall()
                col_names = [c["name"] for c in cols]
                assert "posted_at" in col_names
            finally:
                conn.close()

    def test_posted_at_stored_and_retrieved(self):
        """posted_at set on insert is present in the row returned by get_feed()."""
        with TempDB() as path:
            db.insert_listing(
                make_listing(source_id="pa-001", score=8.0, posted_at="2026-03-01T09:00:00Z"),
                db_path=path,
            )
            feed = db.get_feed(threshold=7.0, db_path=path)
            row = next(r for r in feed if r["source_id"] == "pa-001")
            assert row["posted_at"] == "2026-03-01T09:00:00Z"

    def test_posted_at_null_when_not_supplied(self):
        """When posted_at is not in the listing dict, it is stored as NULL."""
        with TempDB() as path:
            listing = make_listing(source_id="pa-002", score=8.0)
            # Ensure the key is absent (not just None) to test the setdefault path.
            listing.pop("posted_at", None)
            db.insert_listing(listing, db_path=path)
            conn = db.get_connection(path)
            try:
                row = conn.execute(
                    "SELECT posted_at FROM listings WHERE source_id = 'pa-002'"
                ).fetchone()
                assert row["posted_at"] is None
            finally:
                conn.close()

    def test_sort_date_posted_orders_newest_first(self):
        """get_feed(sort='date_posted') returns listings ordered by posted_at DESC."""
        with TempDB() as path:
            db.insert_listing(
                make_listing(source_id="pa-old", score=9.0, posted_at="2026-01-01T00:00:00Z"),
                db_path=path,
            )
            db.insert_listing(
                make_listing(source_id="pa-new", score=7.0,
                             posted_at="2026-03-20T00:00:00Z"),
                db_path=path,
            )
            db.insert_listing(
                make_listing(source_id="pa-mid", score=8.0,
                             posted_at="2026-02-15T00:00:00Z"),
                db_path=path,
            )
            results = db.get_feed(threshold=7.0, sort="date_posted", db_path=path)
            ids = [r["source_id"] for r in results]
            assert ids == ["pa-new", "pa-mid", "pa-old"]

    def test_sort_default_still_orders_by_score(self):
        """get_feed() without sort param still orders by score DESC."""
        with TempDB() as path:
            db.insert_listing(
                make_listing(source_id="sc-low", score=7.5, posted_at="2026-03-25T00:00:00Z"),
                db_path=path,
            )
            db.insert_listing(
                make_listing(source_id="sc-high", score=9.5,
                             posted_at="2026-01-01T00:00:00Z"),
                db_path=path,
            )
            results = db.get_feed(threshold=7.0, db_path=path)
            ids = [r["source_id"] for r in results]
            # sc-high has the higher score so should appear first despite older posted_at
            assert ids[0] == "sc-high"

    def test_migration_adds_posted_at_to_existing_db(self):
        """init_db() adds posted_at to a database that was created without it."""
        with TempDB() as path:
            # Simulate a pre-migration database (legacy adzuna_id schema, no posted_at).
            conn = db.get_connection(path)
            try:
                conn.execute("DROP TABLE listings")
                conn.execute("""
                    CREATE TABLE listings (
                        id        INTEGER PRIMARY KEY AUTOINCREMENT,
                        adzuna_id TEXT UNIQUE NOT NULL,
                        title     TEXT,
                        score     REAL,
                        seen      INTEGER DEFAULT 0
                    )
                """)
                conn.execute(
                    "INSERT INTO listings (adzuna_id, title, score, seen) "
                    "VALUES ('legacy-pa-001', 'Old Job', 8.0, 1)"
                )
                conn.commit()
            finally:
                conn.close()

            db.init_db(path)

            conn = db.get_connection(path)
            try:
                cols = conn.execute("PRAGMA table_info(listings)").fetchall()
                col_names = [c["name"] for c in cols]
                assert "posted_at" in col_names
            finally:
                conn.close()

    def test_null_posted_at_does_not_crash_sort(self):
        """get_feed(sort='date_posted') works even when some rows have NULL posted_at.

        SQLite sorts NULLs before non-NULL values in ASC order (i.e. last in DESC).
        The important thing is no exception is raised and non-NULL rows sort first.
        """
        with TempDB() as path:
            db.insert_listing(
                make_listing(source_id="null-pa", score=8.0, posted_at=None),
                db_path=path,
            )
            db.insert_listing(
                make_listing(source_id="real-pa", score=7.5,
                             posted_at="2026-03-01T00:00:00Z"),
                db_path=path,
            )
            results = db.get_feed(threshold=7.0, sort="date_posted", db_path=path)
            ids = [r["source_id"] for r in results]
            # real-pa has a non-NULL posted_at so should sort first (DESC puts NULLs last)
            assert ids[0] == "real-pa"
            assert "null-pa" in ids


# ---------------------------------------------------------------------------
# Issue #132 — redirect_url index
# ---------------------------------------------------------------------------

class TestRedirectUrlIndex:
    def test_index_exists_after_init_db(self):
        """init_db() creates idx_listings_redirect_url on the listings table."""
        with TempDB() as path:
            conn = db.get_connection(path)
            try:
                row = conn.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='index' AND name='idx_listings_redirect_url'"
                ).fetchone()
                assert row is not None, "idx_listings_redirect_url index was not created"
            finally:
                conn.close()

    def test_index_idempotent(self):
        """Calling init_db() twice does not raise due to the redirect_url index."""
        with TempDB() as path:
            db.init_db(path)  # second call — IF NOT EXISTS makes this safe


# ---------------------------------------------------------------------------
# Issue #134 — atomic bookmark/apply toggles
# ---------------------------------------------------------------------------

class TestAtomicToggles:
    def _get_listing_id(self, path: str, source_id: str) -> int:
        conn = db.get_connection(path)
        try:
            row = conn.execute(
                "SELECT id FROM listings WHERE source_id = ?", (source_id,)
            ).fetchone()
            return row["id"]
        finally:
            conn.close()

    def test_toggle_bookmarked_flips_from_zero_to_one(self):
        """toggle_bookmarked() sets bookmarked=1 when starting from 0."""
        with TempDB() as path:
            db.insert_listing(make_listing(source_id="tb-001", bookmarked=0), db_path=path)
            lid = self._get_listing_id(path, "tb-001")
            result = db.toggle_bookmarked(lid, db_path=path)
            assert result is not None
            assert result["bookmarked"] == 1

    def test_toggle_bookmarked_flips_from_one_to_zero(self):
        """toggle_bookmarked() sets bookmarked=0 when starting from 1."""
        with TempDB() as path:
            db.insert_listing(make_listing(source_id="tb-002", bookmarked=1), db_path=path)
            lid = self._get_listing_id(path, "tb-002")
            result = db.toggle_bookmarked(lid, db_path=path)
            assert result is not None
            assert result["bookmarked"] == 0

    def test_toggle_bookmarked_twice_returns_to_original(self):
        """Two rapid toggle_bookmarked() calls produce a net no-op (each flip is independent)."""
        with TempDB() as path:
            db.insert_listing(make_listing(source_id="tb-003", bookmarked=0), db_path=path)
            lid = self._get_listing_id(path, "tb-003")
            db.toggle_bookmarked(lid, db_path=path)   # 0 → 1
            result = db.toggle_bookmarked(lid, db_path=path)  # 1 → 0
            assert result is not None
            assert result["bookmarked"] == 0

    def test_toggle_bookmarked_returns_none_for_missing_id(self):
        """toggle_bookmarked() returns None when the listing id does not exist."""
        with TempDB() as path:
            result = db.toggle_bookmarked(99999, db_path=path)
            assert result is None

    def test_toggle_applied_flips_from_zero_to_one(self):
        """toggle_applied() sets applied=1 when starting from 0."""
        with TempDB() as path:
            db.insert_listing(make_listing(source_id="ta-001", applied=0), db_path=path)
            lid = self._get_listing_id(path, "ta-001")
            result = db.toggle_applied(lid, db_path=path)
            assert result is not None
            assert result["applied"] == 1

    def test_toggle_applied_flips_from_one_to_zero(self):
        """toggle_applied() sets applied=0 when starting from 1."""
        with TempDB() as path:
            db.insert_listing(make_listing(source_id="ta-002", applied=1), db_path=path)
            lid = self._get_listing_id(path, "ta-002")
            result = db.toggle_applied(lid, db_path=path)
            assert result is not None
            assert result["applied"] == 0

    def test_toggle_applied_twice_returns_to_original(self):
        """Two rapid toggle_applied() calls produce a net no-op (each flip is independent)."""
        with TempDB() as path:
            db.insert_listing(make_listing(source_id="ta-003", applied=0), db_path=path)
            lid = self._get_listing_id(path, "ta-003")
            db.toggle_applied(lid, db_path=path)   # 0 → 1
            result = db.toggle_applied(lid, db_path=path)  # 1 → 0
            assert result is not None
            assert result["applied"] == 0

    def test_toggle_applied_returns_none_for_missing_id(self):
        """toggle_applied() returns None when the listing id does not exist."""
        with TempDB() as path:
            result = db.toggle_applied(99999, db_path=path)
            assert result is None
