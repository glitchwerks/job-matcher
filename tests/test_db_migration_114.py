"""Tests for the #114 migration: reclassify JSearch snippet listings as 'full'."""

import pytest

import db


@pytest.fixture(autouse=True)
def _clean_listings():
    """Truncate listings before and after each test for isolation."""
    with db.get_connection() as conn:
        conn.execute("TRUNCATE TABLE listings")
    yield
    with db.get_connection() as conn:
        conn.execute("TRUNCATE TABLE listings")


def _insert_listing(source, source_id, description, description_source):
    """Insert a minimal listing row via db.insert_listing() for testing."""
    db.insert_listing({
        "source": source,
        "source_id": source_id,
        "title": "Test Job",
        "company": "Test Co",
        "location": "NYC",
        "salary_min": None,
        "salary_max": None,
        "salary_is_predicted": None,
        "contract_type": None,
        "contract_time": None,
        "description": description,
        "redirect_url": "https://example.com/job/1",
        "created_at": "2026-01-01T00:00:00Z",
        "fetched_at": "2026-01-02T00:00:00Z",
        "score": 5.0,
        "matched_skills": [],
        "missing_skills": [],
        "concerns": [],
        "verdict": "Test",
        "bookmarked": 0,
        "dismissed": 0,
        "seen": 1,
        "applied": 0,
        "job_type": None,
        "model_used": None,
        "posted_at": None,
        "description_source": description_source,
    })


def _get_description_source(source, source_id):
    """Read description_source for a specific listing."""
    with db.get_connection() as conn:
        cur = conn.execute(
            "SELECT description_source FROM listings WHERE source=%s AND source_id=%s",
            (source, source_id),
        )
        row = cur.fetchone()
        return row["description_source"] if row else None


def _run_migration():
    """Run only the JSearch reclassification migration SQL."""
    with db.get_connection() as conn:
        cur = conn.execute(
            """UPDATE listings
               SET description_source = 'full'
               WHERE source = 'jsearch'
                 AND LENGTH(description) >= 100
                 AND description_source = 'snippet'"""
        )
        return cur.rowcount


class TestJSearchMigration:
    """Verify the #114 migration reclassifies JSearch snippets correctly."""

    def test_reclassifies_jsearch_with_long_description(self):
        _insert_listing("jsearch", "m1", "A" * 150, "snippet")
        count = _run_migration()
        assert count == 1
        assert _get_description_source("jsearch", "m1") == "full"

    def test_does_not_reclassify_jsearch_with_short_description(self):
        _insert_listing("jsearch", "m2", "Short", "snippet")
        count = _run_migration()
        assert count == 0
        assert _get_description_source("jsearch", "m2") == "snippet"

    def test_does_not_reclassify_other_sources(self):
        _insert_listing("jooble", "m3", "A" * 200, "snippet")
        count = _run_migration()
        assert count == 0
        assert _get_description_source("jooble", "m3") == "snippet"

    def test_does_not_reclassify_already_full(self):
        _insert_listing("jsearch", "m4", "A" * 150, "full")
        count = _run_migration()
        assert count == 0
        assert _get_description_source("jsearch", "m4") == "full"
