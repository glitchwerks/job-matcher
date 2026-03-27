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
    adzuna_id: str = "test-001",
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
) -> dict:
    """Return a complete listing dict suitable for db.insert_listing()."""
    return {
        "adzuna_id": adzuna_id,
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
            assert db.listing_exists("nonexistent-id", db_path=path) is False

    def test_returns_true_after_insert(self):
        with TempDB() as path:
            listing = make_listing(adzuna_id="exists-001")
            db.insert_listing(listing, db_path=path)
            assert db.listing_exists("exists-001", db_path=path) is True

    def test_returns_false_for_different_id(self):
        """Existence check is specific to the queried adzuna_id."""
        with TempDB() as path:
            db.insert_listing(make_listing(adzuna_id="abc"), db_path=path)
            assert db.listing_exists("xyz", db_path=path) is False


# ---------------------------------------------------------------------------
# insert_listing + get_feed
# ---------------------------------------------------------------------------

class TestInsertAndFeed:
    def test_inserted_listing_appears_in_feed(self):
        """An inserted listing appears in get_feed() at the correct threshold."""
        with TempDB() as path:
            db.insert_listing(make_listing(adzuna_id="feed-001", score=8.0), db_path=path)
            results = db.get_feed(threshold=7.0, db_path=path)
            ids = [r["adzuna_id"] for r in results]
            assert "feed-001" in ids

    def test_listing_below_threshold_excluded(self):
        """Listings whose score is below the threshold do not appear in the feed."""
        with TempDB() as path:
            db.insert_listing(make_listing(adzuna_id="low-score", score=4.0), db_path=path)
            results = db.get_feed(threshold=7.0, db_path=path)
            ids = [r["adzuna_id"] for r in results]
            assert "low-score" not in ids

    def test_dismissed_listing_excluded_from_feed(self):
        """Dismissed listings (dismissed=1) do not appear in get_feed()."""
        with TempDB() as path:
            listing = make_listing(adzuna_id="dismissed-001", score=9.0, dismissed=1)
            db.insert_listing(listing, db_path=path)
            results = db.get_feed(threshold=7.0, db_path=path)
            ids = [r["adzuna_id"] for r in results]
            assert "dismissed-001" not in ids

    def test_applied_listing_excluded_from_feed(self):
        """Applied listings (applied=1) do not appear in get_feed()."""
        with TempDB() as path:
            listing = make_listing(adzuna_id="applied-001", score=9.0, applied=1)
            db.insert_listing(listing, db_path=path)
            results = db.get_feed(threshold=7.0, db_path=path)
            ids = [r["adzuna_id"] for r in results]
            assert "applied-001" not in ids

    def test_feed_ordered_by_score_descending(self):
        """Feed results are sorted highest score first."""
        with TempDB() as path:
            db.insert_listing(make_listing(adzuna_id="score-7", score=7.0), db_path=path)
            db.insert_listing(make_listing(adzuna_id="score-9", score=9.0), db_path=path)
            db.insert_listing(make_listing(adzuna_id="score-8", score=8.0), db_path=path)
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
            db.insert_listing(make_listing(adzuna_id="py-001", title="Python Developer", score=8.0), db_path=path)
            db.insert_listing(make_listing(adzuna_id="java-001", title="Java Developer", score=8.0), db_path=path)
            results = db.get_feed(threshold=7.0, search="python", db_path=path)
            ids = [r["adzuna_id"] for r in results]
            assert "py-001" in ids
            assert "java-001" not in ids

    def test_search_filter_case_insensitive(self):
        """Search filter is case-insensitive."""
        with TempDB() as path:
            db.insert_listing(make_listing(adzuna_id="ci-001", title="PYTHON DEVELOPER", score=8.0), db_path=path)
            results = db.get_feed(threshold=7.0, search="python", db_path=path)
            assert any(r["adzuna_id"] == "ci-001" for r in results)

    def test_search_filter_by_company(self):
        """Search filter also matches on company name."""
        with TempDB() as path:
            db.insert_listing(
                make_listing(adzuna_id="co-001", title="Engineer", company="Acme Corp", score=8.0),
                db_path=path,
            )
            results = db.get_feed(threshold=7.0, search="acme", db_path=path)
            assert any(r["adzuna_id"] == "co-001" for r in results)

    def test_remote_only_filter(self):
        """remote_only=True returns only listings whose location contains 'remote'."""
        with TempDB() as path:
            db.insert_listing(
                make_listing(adzuna_id="remote-001", location="Remote, US", score=8.0),
                db_path=path,
            )
            db.insert_listing(
                make_listing(adzuna_id="office-001", location="New York, NY", score=8.0),
                db_path=path,
            )
            results = db.get_feed(threshold=7.0, remote_only=True, db_path=path)
            ids = [r["adzuna_id"] for r in results]
            assert "remote-001" in ids
            assert "office-001" not in ids

    def test_remote_only_case_insensitive(self):
        """remote_only filter matches 'REMOTE' in location regardless of case."""
        with TempDB() as path:
            db.insert_listing(
                make_listing(adzuna_id="remote-upper", location="REMOTE ONLY", score=8.0),
                db_path=path,
            )
            results = db.get_feed(threshold=7.0, remote_only=True, db_path=path)
            assert any(r["adzuna_id"] == "remote-upper" for r in results)


# ---------------------------------------------------------------------------
# Bookmarks
# ---------------------------------------------------------------------------

class TestBookmarks:
    def test_bookmarked_listing_appears_in_get_bookmarks(self):
        """set_bookmarked(id, 1) causes the listing to appear in get_bookmarks()."""
        with TempDB() as path:
            db.insert_listing(make_listing(adzuna_id="bm-001", score=8.0), db_path=path)
            conn = db.get_connection(path)
            try:
                row = conn.execute("SELECT id FROM listings WHERE adzuna_id = 'bm-001'").fetchone()
                listing_id = row["id"]
            finally:
                conn.close()

            db.set_bookmarked(listing_id, 1, db_path=path)
            bookmarks = db.get_bookmarks(db_path=path)
            ids = [r["adzuna_id"] for r in bookmarks]
            assert "bm-001" in ids

    def test_unbookmark_removes_from_bookmarks(self):
        """set_bookmarked(id, 0) removes a previously bookmarked listing."""
        with TempDB() as path:
            db.insert_listing(make_listing(adzuna_id="bm-002", score=8.0), db_path=path)
            conn = db.get_connection(path)
            try:
                row = conn.execute("SELECT id FROM listings WHERE adzuna_id = 'bm-002'").fetchone()
                listing_id = row["id"]
            finally:
                conn.close()

            db.set_bookmarked(listing_id, 1, db_path=path)
            db.set_bookmarked(listing_id, 0, db_path=path)
            bookmarks = db.get_bookmarks(db_path=path)
            ids = [r["adzuna_id"] for r in bookmarks]
            assert "bm-002" not in ids

    def test_non_bookmarked_listing_absent_from_bookmarks(self):
        """A listing that has never been bookmarked does not appear in get_bookmarks()."""
        with TempDB() as path:
            db.insert_listing(make_listing(adzuna_id="bm-003", score=8.0, bookmarked=0), db_path=path)
            bookmarks = db.get_bookmarks(db_path=path)
            ids = [r["adzuna_id"] for r in bookmarks]
            assert "bm-003" not in ids


# ---------------------------------------------------------------------------
# Dismissed
# ---------------------------------------------------------------------------

class TestDismissed:
    def test_dismissed_listing_disappears_from_feed(self):
        """After set_dismissed(id, 1) the listing no longer appears in get_feed()."""
        with TempDB() as path:
            db.insert_listing(make_listing(adzuna_id="dm-001", score=9.0), db_path=path)
            conn = db.get_connection(path)
            try:
                row = conn.execute("SELECT id FROM listings WHERE adzuna_id = 'dm-001'").fetchone()
                listing_id = row["id"]
            finally:
                conn.close()

            db.set_dismissed(listing_id, 1, db_path=path)
            feed = db.get_feed(threshold=7.0, db_path=path)
            ids = [r["adzuna_id"] for r in feed]
            assert "dm-001" not in ids


# ---------------------------------------------------------------------------
# Applied
# ---------------------------------------------------------------------------

class TestApplied:
    def test_applied_listing_appears_in_get_applied(self):
        """set_applied(id, 1) causes the listing to appear in get_applied()."""
        with TempDB() as path:
            db.insert_listing(make_listing(adzuna_id="ap-001", score=8.0), db_path=path)
            conn = db.get_connection(path)
            try:
                row = conn.execute("SELECT id FROM listings WHERE adzuna_id = 'ap-001'").fetchone()
                listing_id = row["id"]
            finally:
                conn.close()

            db.set_applied(listing_id, 1, db_path=path)
            applied = db.get_applied(db_path=path)
            ids = [r["adzuna_id"] for r in applied]
            assert "ap-001" in ids

    def test_unapplied_listing_absent_from_get_applied(self):
        """A listing with applied=0 does not appear in get_applied()."""
        with TempDB() as path:
            db.insert_listing(make_listing(adzuna_id="ap-002", score=8.0, applied=0), db_path=path)
            applied = db.get_applied(db_path=path)
            ids = [r["adzuna_id"] for r in applied]
            assert "ap-002" not in ids

    def test_unapply_removes_from_applied(self):
        """set_applied(id, 0) removes a listing from get_applied()."""
        with TempDB() as path:
            db.insert_listing(make_listing(adzuna_id="ap-003", score=8.0), db_path=path)
            conn = db.get_connection(path)
            try:
                row = conn.execute("SELECT id FROM listings WHERE adzuna_id = 'ap-003'").fetchone()
                listing_id = row["id"]
            finally:
                conn.close()

            db.set_applied(listing_id, 1, db_path=path)
            db.set_applied(listing_id, 0, db_path=path)
            applied = db.get_applied(db_path=path)
            ids = [r["adzuna_id"] for r in applied]
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
                make_listing(adzuna_id="json-001", matched_skills=skills),
                db_path=path,
            )
            feed = db.get_feed(threshold=7.0, db_path=path)
            row = next(r for r in feed if r["adzuna_id"] == "json-001")
            assert row["matched_skills"] == skills

    def test_empty_list_round_trips(self):
        """An empty list for concerns is stored and retrieved as an empty list."""
        with TempDB() as path:
            db.insert_listing(
                make_listing(adzuna_id="json-002", concerns=[]),
                db_path=path,
            )
            feed = db.get_feed(threshold=7.0, db_path=path)
            row = next(r for r in feed if r["adzuna_id"] == "json-002")
            assert row["concerns"] == []

    def test_none_skills_retrieved_as_empty_list(self):
        """When matched_skills is None on insert it is retrieved as an empty list."""
        with TempDB() as path:
            listing = make_listing(adzuna_id="json-003")
            listing["matched_skills"] = None
            db.insert_listing(listing, db_path=path)
            feed = db.get_feed(threshold=7.0, db_path=path)
            row = next(r for r in feed if r["adzuna_id"] == "json-003")
            assert row["matched_skills"] == []


# ---------------------------------------------------------------------------
# model_used column
# ---------------------------------------------------------------------------

class TestModelUsed:
    def test_model_used_stored_and_retrieved_via_insert(self):
        """model_used set in the listing dict is persisted and readable."""
        with TempDB() as path:
            db.insert_listing(
                make_listing(adzuna_id="mu-001", score=8.0, model_used="claude-haiku-4-5"),
                db_path=path,
            )
            conn = db.get_connection(path)
            try:
                row = conn.execute(
                    "SELECT model_used FROM listings WHERE adzuna_id = 'mu-001'"
                ).fetchone()
                assert row["model_used"] == "claude-haiku-4-5"
            finally:
                conn.close()

    def test_model_used_defaults_to_null_when_absent(self):
        """If model_used is not supplied to insert_listing(), it is stored as NULL."""
        with TempDB() as path:
            # make_listing() passes model_used=None by default.
            db.insert_listing(make_listing(adzuna_id="mu-002", score=8.0), db_path=path)
            conn = db.get_connection(path)
            try:
                row = conn.execute(
                    "SELECT model_used FROM listings WHERE adzuna_id = 'mu-002'"
                ).fetchone()
                assert row["model_used"] is None
            finally:
                conn.close()

    def test_update_score_writes_model_used(self):
        """update_score() persists model_used from score_data."""
        with TempDB() as path:
            db.insert_listing(make_listing(adzuna_id="mu-003", score=None, seen=0), db_path=path)
            db.update_score(
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
                    "SELECT model_used, seen FROM listings WHERE adzuna_id = 'mu-003'"
                ).fetchone()
                assert row["model_used"] == "claude-haiku-4-5"
                assert row["seen"] == 1
            finally:
                conn.close()

    def test_update_score_model_used_none_when_absent(self):
        """update_score() stores NULL for model_used when not present in score_data."""
        with TempDB() as path:
            db.insert_listing(make_listing(adzuna_id="mu-004", score=None, seen=0), db_path=path)
            db.update_score(
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
                    "SELECT model_used FROM listings WHERE adzuna_id = 'mu-004'"
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

    def test_migration_on_existing_db_without_column(self):
        """init_db() adds model_used to a database that was created without it."""
        with TempDB() as path:
            # Manually drop the column by recreating the table without it, then
            # run init_db() again to trigger the migration path.
            conn = db.get_connection(path)
            try:
                conn.execute("ALTER TABLE listings RENAME TO listings_old")
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
                    "SELECT adzuna_id, title, score, seen FROM listings_old"
                )
                conn.execute("DROP TABLE listings_old")
                conn.commit()
            finally:
                conn.close()

            # Now run init_db() — the migration loop should add model_used.
            db.init_db(path)

            conn = db.get_connection(path)
            try:
                cols = conn.execute("PRAGMA table_info(listings)").fetchall()
                col_names = [c["name"] for c in cols]
                assert "model_used" in col_names
            finally:
                conn.close()
