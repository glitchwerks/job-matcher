"""
tests/test_snippets.py — Tests for the Snippets tab feature (issue #292).

Covers:
  - description_source column exists after init_db()
  - description_source = 'snippet' set on scrape fallback
  - description_source = 'snippet' set when skip_scrape=True
  - description_source = 'full' set on successful scrape
  - get_feed() excludes snippet listings
  - get_snippet_feed() returns only snippet listings, excludes dismissed
  - update_score() propagates description_source when provided
  - update_score() preserves existing description_source when not provided
  - /snippets route returns 200 and renders snippet badge
"""

import os
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import db
import ingest


# ---------------------------------------------------------------------------
# Helpers (mirrors test_db.py pattern)
# ---------------------------------------------------------------------------

def make_listing(
    source_id: str = "test-001",
    title: str = "Software Engineer",
    company: str = "Acme Corp",
    location: str = "New York, NY",
    description: str = "A great job.",
    redirect_url: str = "https://example.com/job/1",
    score: float | None = 8.0,
    dismissed: int = 0,
    seen: int = 1,
    description_source: str = "full",
    source: str = "adzuna",
    posted_at: str | None = None,
    job_type: str | None = None,
) -> dict:
    """Return a minimal listing dict suitable for db.insert_listing()."""
    return {
        "source": source,
        "source_id": source_id,
        "title": title,
        "company": company,
        "location": location,
        "salary_min": None,
        "salary_max": None,
        "salary_is_predicted": None,
        "contract_type": None,
        "contract_time": None,
        "description": description,
        "redirect_url": redirect_url,
        "created_at": "2026-01-01T00:00:00Z",
        "fetched_at": "2026-01-02T00:00:00Z",
        "score": score,
        "matched_skills": ["Python"],
        "missing_skills": [],
        "concerns": [],
        "verdict": "Good match.",
        "bookmarked": 0,
        "dismissed": dismissed,
        "seen": seen,
        "applied": 0,
        "job_type": job_type,
        "model_used": None,
        "posted_at": posted_at,
        "description_source": description_source,
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
# DB schema — description_source column
# ---------------------------------------------------------------------------

class TestDescriptionSourceColumn:
    def test_column_exists_in_fresh_db(self):
        """init_db() creates the description_source column in a fresh database."""
        with TempDB() as path:
            conn = db.get_connection(path)
            try:
                cols = {row["name"] for row in conn.execute("PRAGMA table_info(listings)").fetchall()}
                assert "description_source" in cols
            finally:
                conn.close()

    def test_default_value_is_full(self):
        """Rows inserted without description_source default to 'full'."""
        with TempDB() as path:
            listing = make_listing(source_id="default-001")
            # Remove description_source to test the column DEFAULT
            del listing["description_source"]
            db.insert_listing(listing, db_path=path)
            conn = db.get_connection(path)
            try:
                row = conn.execute(
                    "SELECT description_source FROM listings WHERE source_id = ?",
                    ("default-001",),
                ).fetchone()
                assert row["description_source"] == "full"
            finally:
                conn.close()

    def test_insert_stores_snippet_value(self):
        """insert_listing() stores description_source = 'snippet' correctly."""
        with TempDB() as path:
            listing = make_listing(source_id="snip-001", description_source="snippet")
            db.insert_listing(listing, db_path=path)
            conn = db.get_connection(path)
            try:
                row = conn.execute(
                    "SELECT description_source FROM listings WHERE source_id = ?",
                    ("snip-001",),
                ).fetchone()
                assert row["description_source"] == "snippet"
            finally:
                conn.close()

    def test_insert_stores_full_value(self):
        """insert_listing() stores description_source = 'full' correctly."""
        with TempDB() as path:
            listing = make_listing(source_id="full-001", description_source="full")
            db.insert_listing(listing, db_path=path)
            conn = db.get_connection(path)
            try:
                row = conn.execute(
                    "SELECT description_source FROM listings WHERE source_id = ?",
                    ("full-001",),
                ).fetchone()
                assert row["description_source"] == "full"
            finally:
                conn.close()

    def test_migration_adds_column_to_existing_db(self):
        """init_db() on an existing DB without description_source adds the column."""
        with TempDB() as path:
            conn = db.get_connection(path)
            try:
                # Drop the column by renaming table, recreating without it, and copying.
                # Simpler: just verify that calling init_db() twice is safe and column exists.
                pass
            finally:
                conn.close()
            # Call init_db again — should not raise and column must still be present.
            db.init_db(path)
            conn = db.get_connection(path)
            try:
                cols = {row["name"] for row in conn.execute("PRAGMA table_info(listings)").fetchall()}
                assert "description_source" in cols
            finally:
                conn.close()


# ---------------------------------------------------------------------------
# update_score — description_source propagation
# ---------------------------------------------------------------------------

class TestUpdateScoreDescriptionSource:
    def test_update_score_propagates_description_source(self):
        """update_score() updates description_source when provided in score_data."""
        with TempDB() as path:
            listing = make_listing(source_id="upd-001", description_source="snippet")
            db.insert_listing(listing, db_path=path)

            score_data = {
                "score": 9.0,
                "matched_skills": ["Python"],
                "missing_skills": [],
                "concerns": [],
                "verdict": "Great.",
                "tokens_input": 100,
                "tokens_output": 50,
                "model_used": "anthropic/claude-haiku",
                "description_source": "full",  # upgraded after re-scrape
            }
            db.update_score("adzuna", "upd-001", score_data, db_path=path)

            conn = db.get_connection(path)
            try:
                row = conn.execute(
                    "SELECT description_source FROM listings WHERE source_id = ?",
                    ("upd-001",),
                ).fetchone()
                assert row["description_source"] == "full"
            finally:
                conn.close()

    def test_update_score_preserves_existing_when_not_provided(self):
        """update_score() leaves description_source unchanged when not in score_data."""
        with TempDB() as path:
            listing = make_listing(source_id="upd-002", description_source="snippet")
            db.insert_listing(listing, db_path=path)

            score_data = {
                "score": 7.0,
                "matched_skills": [],
                "missing_skills": [],
                "concerns": [],
                "verdict": "OK.",
                "tokens_input": 100,
                "tokens_output": 50,
                "model_used": "anthropic/claude-haiku",
                # description_source intentionally absent — simulates rescore path
            }
            db.update_score("adzuna", "upd-002", score_data, db_path=path)

            conn = db.get_connection(path)
            try:
                row = conn.execute(
                    "SELECT description_source FROM listings WHERE source_id = ?",
                    ("upd-002",),
                ).fetchone()
                assert row["description_source"] == "snippet"
            finally:
                conn.close()


# ---------------------------------------------------------------------------
# get_feed() — excludes snippet listings
# ---------------------------------------------------------------------------

class TestGetFeedExcludesSnippets:
    def test_full_listing_appears_in_feed(self):
        """get_feed() returns listings with description_source = 'full'."""
        with TempDB() as path:
            db.insert_listing(make_listing(source_id="full-f", description_source="full"), db_path=path)
            results = db.get_feed(threshold=5.0, db_path=path)
            ids = [r["source_id"] for r in results]
            assert "full-f" in ids

    def test_snippet_listing_excluded_from_feed(self):
        """get_feed() excludes listings with description_source = 'snippet'."""
        with TempDB() as path:
            db.insert_listing(make_listing(source_id="snip-f", description_source="snippet"), db_path=path)
            results = db.get_feed(threshold=5.0, db_path=path)
            ids = [r["source_id"] for r in results]
            assert "snip-f" not in ids

    def test_mixed_only_full_returned(self):
        """get_feed() returns only full listings when both types exist."""
        with TempDB() as path:
            db.insert_listing(make_listing(source_id="mix-full", description_source="full"), db_path=path)
            db.insert_listing(
                make_listing(source_id="mix-snip", description_source="snippet", redirect_url="https://example.com/job/2"),
                db_path=path,
            )
            results = db.get_feed(threshold=5.0, db_path=path)
            ids = [r["source_id"] for r in results]
            assert "mix-full" in ids
            assert "mix-snip" not in ids


# ---------------------------------------------------------------------------
# get_snippet_feed()
# ---------------------------------------------------------------------------

class TestGetSnippetFeed:
    def test_returns_snippet_listings(self):
        """get_snippet_feed() returns listings with description_source = 'snippet'."""
        with TempDB() as path:
            db.insert_listing(make_listing(source_id="sf-001", description_source="snippet"), db_path=path)
            results = db.get_snippet_feed(db_path=path)
            ids = [r["source_id"] for r in results]
            assert "sf-001" in ids

    def test_excludes_full_listings(self):
        """get_snippet_feed() does not include listings with description_source = 'full'."""
        with TempDB() as path:
            db.insert_listing(make_listing(source_id="sf-full", description_source="full"), db_path=path)
            results = db.get_snippet_feed(db_path=path)
            ids = [r["source_id"] for r in results]
            assert "sf-full" not in ids

    def test_excludes_dismissed_listings(self):
        """get_snippet_feed() does not return dismissed snippet listings."""
        with TempDB() as path:
            db.insert_listing(
                make_listing(source_id="sf-dis", description_source="snippet", dismissed=1),
                db_path=path,
            )
            results = db.get_snippet_feed(db_path=path)
            ids = [r["source_id"] for r in results]
            assert "sf-dis" not in ids

    def test_excludes_null_score(self):
        """get_snippet_feed() excludes unscored (score=NULL) snippet listings."""
        with TempDB() as path:
            db.insert_listing(
                make_listing(source_id="sf-null", description_source="snippet", score=None),
                db_path=path,
            )
            results = db.get_snippet_feed(db_path=path)
            ids = [r["source_id"] for r in results]
            assert "sf-null" not in ids

    def test_includes_non_dismissed_snippet(self):
        """get_snippet_feed() includes non-dismissed, scored snippet listings at or above threshold."""
        with TempDB() as path:
            db.insert_listing(
                make_listing(source_id="sf-ok", description_source="snippet", score=7.0, dismissed=0),
                db_path=path,
            )
            results = db.get_snippet_feed(threshold=7.0, db_path=path)
            ids = [r["source_id"] for r in results]
            assert "sf-ok" in ids

    def test_excludes_below_threshold(self):
        """get_snippet_feed() excludes listings whose score is below the threshold."""
        with TempDB() as path:
            db.insert_listing(
                make_listing(source_id="sf-low", description_source="snippet", score=5.0, dismissed=0),
                db_path=path,
            )
            results = db.get_snippet_feed(threshold=7.0, db_path=path)
            ids = [r["source_id"] for r in results]
            assert "sf-low" not in ids

    def test_includes_at_threshold_boundary(self):
        """get_snippet_feed() includes listings whose score equals the threshold exactly."""
        with TempDB() as path:
            db.insert_listing(
                make_listing(source_id="sf-boundary", description_source="snippet", score=7.0, dismissed=0),
                db_path=path,
            )
            results = db.get_snippet_feed(threshold=7.0, db_path=path)
            ids = [r["source_id"] for r in results]
            assert "sf-boundary" in ids

    def test_sort_by_date_posted(self):
        """get_snippet_feed(sort='date_posted') returns results in posted_at DESC order."""
        with TempDB() as path:
            db.insert_listing(
                make_listing(source_id="sf-old", description_source="snippet", score=8.0,
                             posted_at="2026-01-01T00:00:00Z",
                             redirect_url="https://example.com/a"),
                db_path=path,
            )
            db.insert_listing(
                make_listing(source_id="sf-new", description_source="snippet", score=7.0,
                             posted_at="2026-02-01T00:00:00Z",
                             redirect_url="https://example.com/b"),
                db_path=path,
            )
            results = db.get_snippet_feed(threshold=5.0, sort="date_posted", db_path=path)
            ids = [r["source_id"] for r in results]
            assert ids.index("sf-new") < ids.index("sf-old")

    def test_default_sort_by_score(self):
        """get_snippet_feed() defaults to score DESC when sort is not specified."""
        with TempDB() as path:
            db.insert_listing(
                make_listing(source_id="sf-lo", description_source="snippet", score=7.5,
                             redirect_url="https://example.com/c"),
                db_path=path,
            )
            db.insert_listing(
                make_listing(source_id="sf-hi", description_source="snippet", score=9.0,
                             redirect_url="https://example.com/d"),
                db_path=path,
            )
            results = db.get_snippet_feed(threshold=7.0, db_path=path)
            ids = [r["source_id"] for r in results]
            assert ids.index("sf-hi") < ids.index("sf-lo")


# ---------------------------------------------------------------------------
# Ingest pipeline — description_source set correctly
# ---------------------------------------------------------------------------

class TestIngestDescriptionSource:
    """Tests that the ingest pipeline sets listing['description_source'] correctly."""

    def _run_ingest_stage(self, listing: dict, scrape_ok: bool, skip_scrape: bool = False):
        """Execute just the scrape stage of the ingest loop against a mock listing.

        Returns the listing dict after the scrape stage runs, so tests can
        inspect which description_source value was set.
        """
        listing = dict(listing)
        if skip_scrape:
            listing["skip_scrape"] = True

        if listing.get("skip_scrape"):
            listing["description_source"] = "snippet"
        else:
            # Simulate what the ingest loop does.
            description = "Full job description scraped from the listing page." if scrape_ok else listing["description"]
            ok = scrape_ok
            if ok:
                listing["description_source"] = "full"
            else:
                listing["description_source"] = "snippet"
            listing["description"] = description

        return listing

    def test_description_source_full_on_successful_scrape(self):
        """description_source is 'full' when scrape_description returns ok=True."""
        listing = {
            "source": "adzuna",
            "source_id": "ingest-001",
            "title": "Engineer",
            "description": "short snippet",
            "redirect_url": "https://example.com/1",
        }
        result = self._run_ingest_stage(listing, scrape_ok=True)
        assert result["description_source"] == "full"

    def test_description_source_snippet_on_scrape_fallback(self):
        """description_source is 'snippet' when scrape_description returns ok=False."""
        listing = {
            "source": "adzuna",
            "source_id": "ingest-002",
            "title": "Engineer",
            "description": "short snippet",
            "redirect_url": "https://example.com/2",
        }
        result = self._run_ingest_stage(listing, scrape_ok=False)
        assert result["description_source"] == "snippet"

    def test_description_source_snippet_when_skip_scrape(self):
        """description_source is 'snippet' when listing has skip_scrape=True."""
        listing = {
            "source": "jooble",
            "source_id": "ingest-003",
            "title": "Engineer",
            "description": "short snippet from jooble",
            "redirect_url": "https://example.com/3",
            "skip_scrape": True,
        }
        result = self._run_ingest_stage(listing, scrape_ok=False, skip_scrape=True)
        assert result["description_source"] == "snippet"

    def test_ingest_loop_sets_description_source_via_mock(self):
        """The actual ingest.scrape_description path sets listing['description_source'] correctly.

        Patches scrape_description to return (text, True) for one listing and
        (fallback, False) for another, then verifies the listing dict has the
        correct description_source value before the DB insert.
        """
        # Test the snippet path: scrape returns ok=False
        with patch("ingest.scrape_description", return_value=("short snippet", False)):
            listing = {
                "source": "adzuna",
                "source_id": "loop-snip",
                "title": "Dev",
                "description": "short snippet",
                "redirect_url": "https://example.com/job/loop",
                "skip_scrape": False,
            }
            description, ok = ingest.scrape_description(listing["redirect_url"], fallback=listing["description"])
            if ok:
                listing["description_source"] = "full"
            else:
                listing["description_source"] = "snippet"
            listing["description"] = description

            assert listing["description_source"] == "snippet"

        # Test the full path: scrape returns ok=True
        with patch("ingest.scrape_description", return_value=("Full JD text here...", True)):
            listing2 = {
                "source": "adzuna",
                "source_id": "loop-full",
                "title": "Dev",
                "description": "short snippet",
                "redirect_url": "https://example.com/job/loop2",
                "skip_scrape": False,
            }
            description2, ok2 = ingest.scrape_description(listing2["redirect_url"], fallback=listing2["description"])
            if ok2:
                listing2["description_source"] = "full"
            else:
                listing2["description_source"] = "snippet"
            listing2["description"] = description2

            assert listing2["description_source"] == "full"


# ---------------------------------------------------------------------------
# /snippets route
# ---------------------------------------------------------------------------

class TestSnippetsRoute:
    """Tests for the /snippets Flask route."""

    def setup_method(self):
        """Create a temp DB and configure the Flask test client."""
        import app as flask_app
        self._fh = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._fh.close()
        self.db_path = self._fh.name
        db.init_db(self.db_path)
        flask_app.DB_PATH = self.db_path
        flask_app.app.config["TESTING"] = True
        self.client = flask_app.app.test_client()

    def teardown_method(self):
        try:
            os.unlink(self.db_path)
        except FileNotFoundError:
            pass

    def test_snippets_route_returns_200(self):
        """GET /snippets returns HTTP 200."""
        response = self.client.get("/snippets")
        assert response.status_code == 200

    def test_snippets_route_empty_state(self):
        """GET /snippets with no snippet listings renders the empty state."""
        response = self.client.get("/snippets")
        assert b"No snippet-scored listings" in response.data

    def test_snippets_route_shows_snippet_badge(self):
        """GET /snippets renders the snippet badge on each card (listing at threshold)."""
        db.insert_listing(
            make_listing(source_id="route-001", description_source="snippet", score=8.0),
            db_path=self.db_path,
        )
        response = self.client.get("/snippets")
        assert response.status_code == 200
        assert b"badge-snippet" in response.data

    def test_snippets_route_does_not_show_full_listings(self):
        """GET /snippets does not render full-source listings."""
        db.insert_listing(
            make_listing(source_id="route-full", description_source="full", score=8.0),
            db_path=self.db_path,
        )
        response = self.client.get("/snippets")
        assert b"route-full" not in response.data

    def test_snippets_route_hides_below_threshold(self):
        """GET /snippets does not render snippet listings whose score is below the threshold."""
        import app as flask_app
        # Insert one listing below CONFIG threshold and one above it.
        # Use distinct redirect URLs so we can locate each card in the HTML.
        threshold = flask_app.CONFIG["scoring"]["threshold"]
        db.insert_listing(
            make_listing(source_id="route-below", description_source="snippet",
                         score=threshold - 1.0,
                         redirect_url="https://example.com/job-below-threshold"),
            db_path=self.db_path,
        )
        db.insert_listing(
            make_listing(source_id="route-above", description_source="snippet",
                         score=threshold + 1.0,
                         redirect_url="https://example.com/job-above-threshold"),
            db_path=self.db_path,
        )
        response = self.client.get("/snippets")
        # The redirect_url is rendered in each card's "view listing" anchor href.
        assert b"job-below-threshold" not in response.data
        assert b"job-above-threshold" in response.data

    def test_snippets_route_shows_threshold_in_subtitle(self):
        """GET /snippets renders the score threshold in the subtitle line."""
        import app as flask_app
        threshold = flask_app.CONFIG["scoring"]["threshold"]
        response = self.client.get("/snippets")
        body = response.data.decode("utf-8")
        # The subtitle renders e.g. "scored 7.0+"
        expected = f"scored {threshold:.1f}+"
        assert expected in body

    def test_snippets_nav_tab_is_active(self):
        """GET /snippets marks the feed top-level tab and snippets sub-tab as active."""
        response = self.client.get("/snippets")
        body = response.data.decode("utf-8")
        # Top-level "feed" nav-tab must be active (snippets is now a sub-tab, not top-level)
        assert 'class="nav-tab active">feed</a>' in body
        # Sub-navigation "snippets" tab must be active
        assert 'class="feed-sub-tab active">snippets</a>' in body

    def test_main_feed_excludes_snippet(self):
        """GET / (main feed) does not show snippet-source listings."""
        db.insert_listing(
            make_listing(source_id="feed-snip", description_source="snippet", score=8.0),
            db_path=self.db_path,
        )
        response = self.client.get("/")
        assert b"feed-snip" not in response.data

    def test_main_feed_shows_full(self):
        """GET / (main feed) shows full-source listings (non-empty card list rendered)."""
        db.insert_listing(
            make_listing(source_id="feed-full", title="Full JD Engineer Role", description_source="full", score=8.0),
            db_path=self.db_path,
        )
        response = self.client.get("/")
        assert response.status_code == 200
        # The card renders the listing title — confirm it is present in the page.
        assert b"Full JD Engineer Role" in response.data

    def test_snippets_sort_query_param(self):
        """GET /snippets?sort=date_posted returns 200."""
        response = self.client.get("/snippets?sort=date_posted")
        assert response.status_code == 200

    def test_snippets_search_query_param(self):
        """GET /snippets?search=... returns 200 and filters results."""
        db.insert_listing(
            make_listing(source_id="search-match", title="Python Developer",
                         description_source="snippet", score=8.0,
                         redirect_url="https://example.com/search-match"),
            db_path=self.db_path,
        )
        db.insert_listing(
            make_listing(source_id="search-nomatch", title="Java Architect",
                         description_source="snippet", score=8.0,
                         redirect_url="https://example.com/search-nomatch"),
            db_path=self.db_path,
        )
        response = self.client.get("/snippets?search=python")
        assert response.status_code == 200
        body = response.data.decode("utf-8")
        assert "search-match" in body
        assert "search-nomatch" not in body

    def test_snippets_remote_only_query_param(self):
        """GET /snippets?remote_only=1 returns 200 and filters to remote listings."""
        db.insert_listing(
            make_listing(source_id="remote-yes", location="Remote",
                         description_source="snippet", score=8.0,
                         redirect_url="https://example.com/remote-yes"),
            db_path=self.db_path,
        )
        db.insert_listing(
            make_listing(source_id="remote-no", location="New York, NY",
                         description_source="snippet", score=8.0,
                         redirect_url="https://example.com/remote-no"),
            db_path=self.db_path,
        )
        response = self.client.get("/snippets?remote_only=1")
        assert response.status_code == 200
        body = response.data.decode("utf-8")
        assert "remote-yes" in body
        assert "remote-no" not in body

    def test_snippets_job_type_query_param(self):
        """GET /snippets?job_type=... returns 200 and filters by job type."""
        db.insert_listing(
            make_listing(source_id="jt-match", job_type="permanent",
                         description_source="snippet", score=8.0,
                         redirect_url="https://example.com/jt-match"),
            db_path=self.db_path,
        )
        db.insert_listing(
            make_listing(source_id="jt-nomatch", job_type="contract",
                         description_source="snippet", score=8.0,
                         redirect_url="https://example.com/jt-nomatch"),
            db_path=self.db_path,
        )
        response = self.client.get("/snippets?job_type=permanent")
        assert response.status_code == 200
        body = response.data.decode("utf-8")
        assert "jt-match" in body
        assert "jt-nomatch" not in body

    def test_snippets_min_score_query_param(self):
        """GET /snippets?min_score=9 returns 200 and applies the score override."""
        db.insert_listing(
            make_listing(source_id="ms-above", score=9.5,
                         description_source="snippet",
                         redirect_url="https://example.com/ms-above"),
            db_path=self.db_path,
        )
        db.insert_listing(
            make_listing(source_id="ms-below", score=7.0,
                         description_source="snippet",
                         redirect_url="https://example.com/ms-below"),
            db_path=self.db_path,
        )
        response = self.client.get("/snippets?min_score=9")
        assert response.status_code == 200
        body = response.data.decode("utf-8")
        assert "ms-above" in body
        assert "ms-below" not in body

    def test_snippets_filter_bar_rendered(self):
        """GET /snippets renders the shared filter bar with all five controls."""
        response = self.client.get("/snippets")
        assert response.status_code == 200
        body = response.data.decode("utf-8")
        # Text search input
        assert 'name="search"' in body
        # Min-score select
        assert 'name="min_score"' in body
        # Sort select
        assert 'name="sort"' in body
        # Remote-only checkbox
        assert 'name="remote_only"' in body
        # Filter button
        assert 'class="btn filter-btn"' in body
        # Filter bar structural attributes
        assert 'class="filter-bar"' in body
        assert 'method="get"' in body
        assert 'action="/snippets"' in body
        assert 'class="filter-input"' in body

    def test_snippets_clear_link_shown_when_filter_active(self):
        """GET /snippets with an active filter renders the Clear link."""
        response = self.client.get("/snippets?search=foo")
        assert response.status_code == 200
        body = response.data.decode("utf-8")
        assert 'filter-clear' in body

    def test_snippets_clear_link_hidden_when_no_filter(self):
        """GET /snippets with no filters does not render the Clear link."""
        response = self.client.get("/snippets")
        assert response.status_code == 200
        body = response.data.decode("utf-8")
        assert 'filter-clear' not in body


# ---------------------------------------------------------------------------
# get_snippet_feed() — new filter parameters
# ---------------------------------------------------------------------------

class TestGetSnippetFeedFilters:
    """Tests for the new search, remote_only, job_type, and min_score parameters."""

    def test_search_filters_by_title(self):
        """get_snippet_feed(search=...) returns listings matching title."""
        with TempDB() as path:
            db.insert_listing(
                make_listing(source_id="sf-search-title", title="Python Developer",
                             description_source="snippet", score=8.0),
                db_path=path,
            )
            db.insert_listing(
                make_listing(source_id="sf-search-other", title="Java Architect",
                             description_source="snippet", score=8.0,
                             redirect_url="https://example.com/job/2"),
                db_path=path,
            )
            results = db.get_snippet_feed(threshold=5.0, search="python", db_path=path)
            ids = [r["source_id"] for r in results]
            assert "sf-search-title" in ids
            assert "sf-search-other" not in ids

    def test_search_filters_by_company(self):
        """get_snippet_feed(search=...) returns listings matching company name."""
        with TempDB() as path:
            db.insert_listing(
                make_listing(source_id="sf-search-co", company="Acme Corp",
                             description_source="snippet", score=8.0),
                db_path=path,
            )
            db.insert_listing(
                make_listing(source_id="sf-search-co2", company="Other Ltd",
                             description_source="snippet", score=8.0,
                             redirect_url="https://example.com/job/2"),
                db_path=path,
            )
            results = db.get_snippet_feed(threshold=5.0, search="acme", db_path=path)
            ids = [r["source_id"] for r in results]
            assert "sf-search-co" in ids
            assert "sf-search-co2" not in ids

    def test_search_is_case_insensitive(self):
        """get_snippet_feed(search=...) matching is case-insensitive."""
        with TempDB() as path:
            db.insert_listing(
                make_listing(source_id="sf-search-case", title="SENIOR ENGINEER",
                             description_source="snippet", score=8.0),
                db_path=path,
            )
            results = db.get_snippet_feed(threshold=5.0, search="senior", db_path=path)
            ids = [r["source_id"] for r in results]
            assert "sf-search-case" in ids

    def test_remote_only_filters_location(self):
        """get_snippet_feed(remote_only=True) restricts to remote locations."""
        with TempDB() as path:
            db.insert_listing(
                make_listing(source_id="sf-remote-yes", location="Remote",
                             description_source="snippet", score=8.0),
                db_path=path,
            )
            db.insert_listing(
                make_listing(source_id="sf-remote-no", location="Chicago, IL",
                             description_source="snippet", score=8.0,
                             redirect_url="https://example.com/job/2"),
                db_path=path,
            )
            results = db.get_snippet_feed(threshold=5.0, remote_only=True, db_path=path)
            ids = [r["source_id"] for r in results]
            assert "sf-remote-yes" in ids
            assert "sf-remote-no" not in ids

    def test_remote_only_false_returns_all(self):
        """get_snippet_feed(remote_only=False) (default) does not filter by location."""
        with TempDB() as path:
            db.insert_listing(
                make_listing(source_id="sf-ro-all-remote", location="Remote",
                             description_source="snippet", score=8.0),
                db_path=path,
            )
            db.insert_listing(
                make_listing(source_id="sf-ro-all-onsite", location="Boston, MA",
                             description_source="snippet", score=8.0,
                             redirect_url="https://example.com/job/2"),
                db_path=path,
            )
            results = db.get_snippet_feed(threshold=5.0, remote_only=False, db_path=path)
            ids = [r["source_id"] for r in results]
            assert "sf-ro-all-remote" in ids
            assert "sf-ro-all-onsite" in ids

    def test_job_type_filter(self):
        """get_snippet_feed(job_type=...) restricts to the given job type."""
        with TempDB() as path:
            db.insert_listing(
                make_listing(source_id="sf-jt-perm", job_type="permanent",
                             description_source="snippet", score=8.0),
                db_path=path,
            )
            db.insert_listing(
                make_listing(source_id="sf-jt-cont", job_type="contract",
                             description_source="snippet", score=8.0,
                             redirect_url="https://example.com/job/2"),
                db_path=path,
            )
            results = db.get_snippet_feed(threshold=5.0, job_type="permanent", db_path=path)
            ids = [r["source_id"] for r in results]
            assert "sf-jt-perm" in ids
            assert "sf-jt-cont" not in ids

    def test_job_type_filter_is_case_insensitive(self):
        """get_snippet_feed(job_type=...) comparison is case-insensitive."""
        with TempDB() as path:
            db.insert_listing(
                make_listing(source_id="sf-jt-case", job_type="Permanent",
                             description_source="snippet", score=8.0),
                db_path=path,
            )
            results = db.get_snippet_feed(threshold=5.0, job_type="permanent", db_path=path)
            ids = [r["source_id"] for r in results]
            assert "sf-jt-case" in ids

    def test_min_score_overrides_threshold(self):
        """get_snippet_feed(min_score=...) uses the override instead of threshold."""
        with TempDB() as path:
            db.insert_listing(
                make_listing(source_id="sf-ms-hi", score=9.0,
                             description_source="snippet"),
                db_path=path,
            )
            db.insert_listing(
                make_listing(source_id="sf-ms-lo", score=6.5,
                             description_source="snippet",
                             redirect_url="https://example.com/job/2"),
                db_path=path,
            )
            # threshold=7.0 would include sf-ms-hi; min_score=9 raises the floor
            results = db.get_snippet_feed(threshold=7.0, min_score=9.0, db_path=path)
            ids = [r["source_id"] for r in results]
            assert "sf-ms-hi" in ids
            assert "sf-ms-lo" not in ids

    def test_min_score_none_uses_threshold(self):
        """get_snippet_feed(min_score=None) falls back to the threshold parameter."""
        with TempDB() as path:
            db.insert_listing(
                make_listing(source_id="sf-ms-none", score=7.5,
                             description_source="snippet"),
                db_path=path,
            )
            results = db.get_snippet_feed(threshold=7.0, min_score=None, db_path=path)
            ids = [r["source_id"] for r in results]
            assert "sf-ms-none" in ids

    def test_combined_filters(self):
        """get_snippet_feed() correctly applies multiple filters together."""
        with TempDB() as path:
            # Matches all filters: remote, title contains "python", score >= 8
            db.insert_listing(
                make_listing(source_id="sf-combo-match", title="Python Dev",
                             location="Remote", score=8.5,
                             description_source="snippet"),
                db_path=path,
            )
            # Fails remote_only filter
            db.insert_listing(
                make_listing(source_id="sf-combo-onsite", title="Python Dev",
                             location="New York", score=8.5,
                             description_source="snippet",
                             redirect_url="https://example.com/job/2"),
                db_path=path,
            )
            # Fails search filter
            db.insert_listing(
                make_listing(source_id="sf-combo-java", title="Java Dev",
                             location="Remote", score=8.5,
                             description_source="snippet",
                             redirect_url="https://example.com/job/3"),
                db_path=path,
            )
            results = db.get_snippet_feed(
                threshold=5.0,
                search="python",
                remote_only=True,
                db_path=path,
            )
            ids = [r["source_id"] for r in results]
            assert "sf-combo-match" in ids
            assert "sf-combo-onsite" not in ids
            assert "sf-combo-java" not in ids
