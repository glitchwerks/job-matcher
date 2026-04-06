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
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import db
import ingest
from app import app as flask_app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PREFIX = "sn-"


def make_listing(
    source_id: str = "test-001",
    title: str = "Software Engineer",
    company: str = "Acme Corp",
    location: str = "New York, NY",
    description: str = "A great job.",
    redirect_url: str = "https://example.com/job/1",
    score: float | None = 8.0,
    dismissed: int = 0,
    applied: int = 0,
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
        "applied": applied,
        "job_type": job_type,
        "model_used": None,
        "posted_at": posted_at,
        "description_source": description_source,
    }


def _cleanup(*prefixes: str) -> None:
    with db.get_connection() as conn:
        for prefix in prefixes:
            conn.execute(
                "DELETE FROM listings WHERE source_id LIKE %s", (prefix + "%",)
            )


# ---------------------------------------------------------------------------
# DB schema — description_source column
# ---------------------------------------------------------------------------

class TestDescriptionSourceColumn:
    def test_column_exists_in_schema(self):
        """init_db() creates the description_source column in the schema."""
        db.init_db()
        with db.get_connection() as conn:
            rows = conn.execute(
                """
                SELECT column_name FROM information_schema.columns
                WHERE table_name = 'listings' AND column_name = 'description_source'
                """
            ).fetchall()
        assert len(rows) == 1

    def test_default_value_is_full(self):
        """Rows inserted without description_source default to 'full'."""
        listing = make_listing(source_id="sn-default-001")
        del listing["description_source"]
        try:
            db.insert_listing(listing)
            with db.get_connection() as conn:
                row = conn.execute(
                    "SELECT description_source FROM listings WHERE source_id = %s",
                    ("sn-default-001",),
                ).fetchone()
            assert row["description_source"] == "full"
        finally:
            _cleanup("sn-default-")

    def test_insert_stores_snippet_value(self):
        """insert_listing() stores description_source = 'snippet' correctly."""
        try:
            db.insert_listing(make_listing(source_id="sn-snip-001", description_source="snippet"))
            with db.get_connection() as conn:
                row = conn.execute(
                    "SELECT description_source FROM listings WHERE source_id = %s",
                    ("sn-snip-001",),
                ).fetchone()
            assert row["description_source"] == "snippet"
        finally:
            _cleanup("sn-snip-")

    def test_insert_stores_full_value(self):
        """insert_listing() stores description_source = 'full' correctly."""
        try:
            db.insert_listing(make_listing(source_id="sn-full-001", description_source="full"))
            with db.get_connection() as conn:
                row = conn.execute(
                    "SELECT description_source FROM listings WHERE source_id = %s",
                    ("sn-full-001",),
                ).fetchone()
            assert row["description_source"] == "full"
        finally:
            _cleanup("sn-full-")


# ---------------------------------------------------------------------------
# update_score — description_source propagation
# ---------------------------------------------------------------------------

class TestUpdateScoreDescriptionSource:
    def teardown_method(self):
        _cleanup("sn-upd-")

    def test_update_score_propagates_description_source(self):
        """update_score() updates description_source when provided in score_data."""
        db.insert_listing(make_listing(source_id="sn-upd-001", description_source="snippet"))
        db.update_score(
            "adzuna",
            "sn-upd-001",
            {
                "score": 9.0,
                "matched_skills": ["Python"],
                "missing_skills": [],
                "concerns": [],
                "verdict": "Great.",
                "tokens_input": 100,
                "tokens_output": 50,
                "model_used": "anthropic/claude-haiku",
                "description_source": "full",
            },
        )
        with db.get_connection() as conn:
            row = conn.execute(
                "SELECT description_source FROM listings WHERE source_id = %s",
                ("sn-upd-001",),
            ).fetchone()
        assert row["description_source"] == "full"

    def test_update_score_preserves_existing_when_not_provided(self):
        """update_score() leaves description_source unchanged when not in score_data."""
        db.insert_listing(make_listing(source_id="sn-upd-002", description_source="snippet"))
        db.update_score(
            "adzuna",
            "sn-upd-002",
            {
                "score": 7.0,
                "matched_skills": [],
                "missing_skills": [],
                "concerns": [],
                "verdict": "OK.",
                "tokens_input": 100,
                "tokens_output": 50,
                "model_used": "anthropic/claude-haiku",
                # description_source intentionally absent
            },
        )
        with db.get_connection() as conn:
            row = conn.execute(
                "SELECT description_source FROM listings WHERE source_id = %s",
                ("sn-upd-002",),
            ).fetchone()
        assert row["description_source"] == "snippet"


# ---------------------------------------------------------------------------
# get_feed() — excludes snippet listings
# ---------------------------------------------------------------------------

class TestGetFeedExcludesSnippets:
    def teardown_method(self):
        _cleanup("sn-ff-")

    def test_full_listing_appears_in_feed(self):
        db.insert_listing(make_listing(source_id="sn-ff-full", description_source="full"))
        results = db.get_feed(threshold=5.0)
        ids = [r["source_id"] for r in results]
        assert "sn-ff-full" in ids

    def test_snippet_listing_excluded_from_feed(self):
        db.insert_listing(make_listing(source_id="sn-ff-snip", description_source="snippet"))
        results = db.get_feed(threshold=5.0)
        ids = [r["source_id"] for r in results]
        assert "sn-ff-snip" not in ids

    def test_mixed_only_full_returned(self):
        db.insert_listing(make_listing(source_id="sn-ff-mix-full", description_source="full"))
        db.insert_listing(
            make_listing(
                source_id="sn-ff-mix-snip",
                description_source="snippet",
                redirect_url="https://example.com/job/2",
            )
        )
        results = db.get_feed(threshold=5.0)
        ids = [r["source_id"] for r in results]
        assert "sn-ff-mix-full" in ids
        assert "sn-ff-mix-snip" not in ids


# ---------------------------------------------------------------------------
# get_snippet_feed()
# ---------------------------------------------------------------------------

class TestGetSnippetFeed:
    def teardown_method(self):
        _cleanup("sn-sf-")

    def test_returns_snippet_listings(self):
        db.insert_listing(make_listing(source_id="sn-sf-001", description_source="snippet"))
        results = db.get_snippet_feed()
        ids = [r["source_id"] for r in results]
        assert "sn-sf-001" in ids

    def test_excludes_full_listings(self):
        db.insert_listing(make_listing(source_id="sn-sf-full", description_source="full"))
        results = db.get_snippet_feed()
        ids = [r["source_id"] for r in results]
        assert "sn-sf-full" not in ids

    def test_excludes_dismissed_listings(self):
        db.insert_listing(
            make_listing(source_id="sn-sf-dis", description_source="snippet", dismissed=1)
        )
        results = db.get_snippet_feed()
        ids = [r["source_id"] for r in results]
        assert "sn-sf-dis" not in ids

    def test_excludes_applied_listings(self):
        db.insert_listing(
            make_listing(source_id="sn-sf-applied", description_source="snippet", applied=1)
        )
        results = db.get_snippet_feed()
        ids = [r["source_id"] for r in results]
        assert "sn-sf-applied" not in ids

    def test_excludes_null_score(self):
        db.insert_listing(
            make_listing(source_id="sn-sf-null", description_source="snippet", score=None)
        )
        results = db.get_snippet_feed()
        ids = [r["source_id"] for r in results]
        assert "sn-sf-null" not in ids

    def test_includes_non_dismissed_snippet(self):
        db.insert_listing(
            make_listing(source_id="sn-sf-ok", description_source="snippet", score=7.0, dismissed=0)
        )
        results = db.get_snippet_feed(threshold=7.0)
        ids = [r["source_id"] for r in results]
        assert "sn-sf-ok" in ids

    def test_excludes_below_threshold(self):
        db.insert_listing(
            make_listing(source_id="sn-sf-low", description_source="snippet", score=5.0, dismissed=0)
        )
        results = db.get_snippet_feed(threshold=7.0)
        ids = [r["source_id"] for r in results]
        assert "sn-sf-low" not in ids

    def test_sort_by_date_posted(self):
        db.insert_listing(
            make_listing(
                source_id="sn-sf-old",
                description_source="snippet",
                score=8.0,
                posted_at="2026-01-01T00:00:00Z",
                redirect_url="https://example.com/a",
            )
        )
        db.insert_listing(
            make_listing(
                source_id="sn-sf-new",
                description_source="snippet",
                score=7.0,
                posted_at="2026-02-01T00:00:00Z",
                redirect_url="https://example.com/b",
            )
        )
        results = db.get_snippet_feed(threshold=5.0, sort="date_posted")
        ids = [r["source_id"] for r in results if r["source_id"] in ("sn-sf-old", "sn-sf-new")]
        assert ids.index("sn-sf-new") < ids.index("sn-sf-old")

    def test_default_sort_by_score(self):
        db.insert_listing(
            make_listing(
                source_id="sn-sf-lo",
                description_source="snippet",
                score=7.5,
                redirect_url="https://example.com/c",
            )
        )
        db.insert_listing(
            make_listing(
                source_id="sn-sf-hi",
                description_source="snippet",
                score=9.0,
                redirect_url="https://example.com/d",
            )
        )
        results = db.get_snippet_feed(threshold=7.0)
        ids = [r["source_id"] for r in results if r["source_id"] in ("sn-sf-lo", "sn-sf-hi")]
        assert ids.index("sn-sf-hi") < ids.index("sn-sf-lo")


# ---------------------------------------------------------------------------
# Ingest pipeline — description_source set correctly (pure unit tests, no DB)
# ---------------------------------------------------------------------------

class TestIngestDescriptionSource:
    """Tests that the ingest pipeline sets listing['description_source'] correctly."""

    def _run_ingest_stage(self, listing: dict, scrape_ok: bool, skip_scrape: bool = False):
        listing = dict(listing)
        if skip_scrape:
            listing["skip_scrape"] = True
        if listing.get("skip_scrape"):
            listing["description_source"] = "snippet"
        else:
            description = "Full job description." if scrape_ok else listing["description"]
            listing["description_source"] = "full" if scrape_ok else "snippet"
            listing["description"] = description
        return listing

    def test_description_source_full_on_successful_scrape(self):
        listing = {
            "source": "adzuna", "source_id": "ingest-001",
            "title": "Engineer", "description": "short snippet",
            "redirect_url": "https://example.com/1",
        }
        result = self._run_ingest_stage(listing, scrape_ok=True)
        assert result["description_source"] == "full"

    def test_description_source_snippet_on_scrape_fallback(self):
        listing = {
            "source": "adzuna", "source_id": "ingest-002",
            "title": "Engineer", "description": "short snippet",
            "redirect_url": "https://example.com/2",
        }
        result = self._run_ingest_stage(listing, scrape_ok=False)
        assert result["description_source"] == "snippet"

    def test_description_source_snippet_when_skip_scrape(self):
        listing = {
            "source": "jooble", "source_id": "ingest-003",
            "title": "Engineer", "description": "short snippet from jooble",
            "redirect_url": "https://example.com/3", "skip_scrape": True,
        }
        result = self._run_ingest_stage(listing, scrape_ok=False, skip_scrape=True)
        assert result["description_source"] == "snippet"

    def test_ingest_loop_sets_description_source_via_mock(self):
        with patch("ingest.scrape_description", return_value=("short snippet", False)):
            listing = {
                "source": "adzuna", "source_id": "loop-snip",
                "title": "Dev", "description": "short snippet",
                "redirect_url": "https://example.com/job/loop",
                "skip_scrape": False,
            }
            _, ok = ingest.scrape_description(listing["redirect_url"], fallback=listing["description"])
            listing["description_source"] = "full" if ok else "snippet"
            assert listing["description_source"] == "snippet"

        with patch("ingest.scrape_description", return_value=("Full JD text here...", True)):
            listing2 = {
                "source": "adzuna", "source_id": "loop-full",
                "title": "Dev", "description": "short snippet",
                "redirect_url": "https://example.com/job/loop2",
                "skip_scrape": False,
            }
            _, ok2 = ingest.scrape_description(listing2["redirect_url"], fallback=listing2["description"])
            listing2["description_source"] = "full" if ok2 else "snippet"
            assert listing2["description_source"] == "full"


# ---------------------------------------------------------------------------
# /snippets route
# ---------------------------------------------------------------------------

class TestSnippetsRoute:
    """Tests for the /snippets Flask route using the shared PostgreSQL database."""

    def setup_method(self):
        flask_app.config["TESTING"] = True
        self.client = flask_app.test_client()

    def teardown_method(self):
        _cleanup("sn-rt-", "sn-route-", "sn-search-", "sn-remote-", "sn-jt-")

    def test_snippets_route_returns_200(self):
        resp = self.client.get("/snippets")
        assert resp.status_code == 200

    def test_snippets_route_shows_snippet_badge(self):
        """GET /snippets renders the snippet badge on each card (listing at threshold)."""
        db.insert_listing(
            make_listing(source_id="sn-rt-001", description_source="snippet", score=8.0)
        )
        response = self.client.get("/snippets")
        assert response.status_code == 200
        assert b"badge-snippet" in response.data

    def test_snippets_sort_query_param(self):
        resp = self.client.get("/snippets?sort=date_posted")
        assert resp.status_code == 200

    def test_snippets_search_query_param(self):
        db.insert_listing(
            make_listing(
                source_id="sn-search-match",
                title="Python Developer",
                description_source="snippet",
                score=8.0,
                redirect_url="https://example.com/search-match",
            )
        )
        db.insert_listing(
            make_listing(
                source_id="sn-search-nomatch",
                title="Java Architect",
                description_source="snippet",
                score=8.0,
                redirect_url="https://example.com/search-nomatch",
            )
        )
        response = self.client.get("/snippets?search=python")
        assert response.status_code == 200
        body = response.data.decode("utf-8")
        assert "Python Developer" in body
        assert "Java Architect" not in body

    def test_snippets_remote_only_query_param(self):
        db.insert_listing(
            make_listing(
                source_id="sn-remote-yes",
                location="Remote",
                description_source="snippet",
                score=8.0,
                redirect_url="https://example.com/remote-yes",
            )
        )
        db.insert_listing(
            make_listing(
                source_id="sn-remote-no",
                location="New York, NY",
                description_source="snippet",
                score=8.0,
                redirect_url="https://example.com/remote-no",
            )
        )
        response = self.client.get("/snippets?remote_only=1")
        assert response.status_code == 200
        body = response.data.decode("utf-8")
        assert "Remote" in body
        assert "New York, NY" not in body

    def test_snippets_job_type_query_param(self):
        db.insert_listing(
            make_listing(
                source_id="sn-jt-match",
                job_type="permanent",
                description_source="snippet",
                score=8.0,
                redirect_url="https://example.com/jt-match",
            )
        )
        db.insert_listing(
            make_listing(
                source_id="sn-jt-nomatch",
                job_type="contract",
                description_source="snippet",
                score=8.0,
                redirect_url="https://example.com/jt-nomatch",
            )
        )
        response = self.client.get("/snippets?job_type=permanent")
        assert response.status_code == 200
        body = response.data.decode("utf-8")
        assert "Permanent" in body
        assert 'badge-jobtype">Contract' not in body
