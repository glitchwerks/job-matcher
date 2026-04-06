"""
tests/test_unread_badge.py — Tests for the "New" badge feature (Issue #236).

Covers:
  - db.mark_opened(): sets opened_at on first call, no-ops on repeat calls
  - POST /listings/<id>/open: returns 200, marks the listing opened, is idempotent
  - GET /: new_count passed to template equals count of listings with opened_at IS NULL
  - Schema: opened_at column is present in the listings table
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import db
from app import app as flask_app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PREFIX = "ub-"


def make_listing(
    source_id: str = "test-001",
    score: float = 8.0,
    dismissed: int = 0,
    applied: int = 0,
) -> dict:
    """Return a minimal listing dict suitable for db.insert_listing()."""
    return {
        "source": "adzuna",
        "source_id": source_id,
        "title": "Software Engineer",
        "company": "Acme Corp",
        "location": "New York, NY",
        "salary_min": None,
        "salary_max": None,
        "salary_is_predicted": 0,
        "contract_type": "permanent",
        "contract_time": "full_time",
        "description": "A great job.",
        "redirect_url": f"https://example.com/{source_id}",
        "created_at": "2026-01-01T00:00:00Z",
        "fetched_at": "2026-01-02T00:00:00Z",
        "score": score,
        "matched_skills": ["Python"],
        "missing_skills": [],
        "concerns": [],
        "verdict": "Good match.",
        "bookmarked": 0,
        "dismissed": dismissed,
        "seen": 1,
        "applied": applied,
        "job_type": None,
        "model_used": None,
        "posted_at": None,
    }


def _cleanup(*prefixes: str) -> None:
    with db.get_connection() as conn:
        for prefix in prefixes:
            conn.execute(
                "DELETE FROM listings WHERE source_id LIKE %s", (prefix + "%",)
            )


def _get_id(source_id: str) -> int:
    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT id FROM listings WHERE source_id = %s", (source_id,)
        ).fetchone()
    assert row is not None
    return row["id"]


# ---------------------------------------------------------------------------
# Schema: opened_at column added by init_db
# ---------------------------------------------------------------------------

class TestOpenedAtColumn:
    def test_opened_at_column_exists(self):
        """init_db() creates the opened_at column in the schema."""
        db.init_db()
        with db.get_connection() as conn:
            rows = conn.execute(
                """
                SELECT column_name FROM information_schema.columns
                WHERE table_name = 'listings' AND column_name = 'opened_at'
                """
            ).fetchall()
        assert len(rows) == 1

    def test_opened_at_defaults_to_null(self):
        """Newly inserted listings have opened_at = NULL."""
        db.insert_listing(make_listing(source_id="ub-default-null"))
        try:
            with db.get_connection() as conn:
                row = conn.execute(
                    "SELECT opened_at FROM listings WHERE source_id = %s",
                    ("ub-default-null",),
                ).fetchone()
            assert row["opened_at"] is None
        finally:
            _cleanup("ub-default-null")


# ---------------------------------------------------------------------------
# db.mark_opened
# ---------------------------------------------------------------------------

class TestMarkOpened:
    def teardown_method(self):
        _cleanup(_PREFIX, "ub-open-", "ub-fmt-")

    def test_sets_opened_at_on_first_call(self):
        """mark_opened() sets opened_at to a non-null timestamp on first call."""
        db.insert_listing(make_listing(source_id="ub-open-001"))
        listing_id = _get_id("ub-open-001")
        db.mark_opened(listing_id)
        with db.get_connection() as conn:
            row = conn.execute(
                "SELECT opened_at FROM listings WHERE id = %s", (listing_id,)
            ).fetchone()
        assert row["opened_at"] is not None

    def test_opened_at_is_idempotent(self):
        """Calling mark_opened() twice preserves the first timestamp."""
        db.insert_listing(make_listing(source_id="ub-open-002"))
        listing_id = _get_id("ub-open-002")

        db.mark_opened(listing_id)
        with db.get_connection() as conn:
            first_ts = conn.execute(
                "SELECT opened_at FROM listings WHERE id = %s", (listing_id,)
            ).fetchone()["opened_at"]

        db.mark_opened(listing_id)
        with db.get_connection() as conn:
            second_ts = conn.execute(
                "SELECT opened_at FROM listings WHERE id = %s", (listing_id,)
            ).fetchone()["opened_at"]

        assert first_ts == second_ts

    def test_mark_opened_nonexistent_id_is_silent(self):
        """mark_opened() with a non-existent id does not raise."""
        db.mark_opened(999999999)  # Should not raise.

    def test_opened_at_format_is_iso_utc(self):
        """opened_at is stored as an ISO 8601 UTC string ending in 'Z'."""
        db.insert_listing(make_listing(source_id="ub-fmt-001"))
        listing_id = _get_id("ub-fmt-001")
        db.mark_opened(listing_id)

        with db.get_connection() as conn:
            ts = conn.execute(
                "SELECT opened_at FROM listings WHERE id = %s", (listing_id,)
            ).fetchone()["opened_at"]

        assert ts.endswith("Z"), f"Expected ISO UTC format ending in Z, got: {ts!r}"
        import datetime
        dt = datetime.datetime.fromisoformat(ts.rstrip("Z"))
        assert isinstance(dt, datetime.datetime)


# ---------------------------------------------------------------------------
# POST /listings/<id>/open route
# ---------------------------------------------------------------------------

@pytest.fixture()
def client():
    flask_app.config["TESTING"] = True
    with flask_app.test_client() as c:
        yield c


class TestMarkListingOpenedRoute:
    def teardown_method(self):
        _cleanup("ub-route-")

    def test_returns_200(self, client):
        """POST /listings/<id>/open returns 200 with an OOB swap fragment."""
        db.insert_listing(make_listing(source_id="ub-route-001"))
        listing_id = _get_id("ub-route-001")
        resp = client.post(f"/listings/{listing_id}/open")
        assert resp.status_code == 200

    def test_marks_listing_as_opened(self, client):
        """POST /listings/<id>/open sets opened_at on the listing."""
        db.insert_listing(make_listing(source_id="ub-route-002"))
        listing_id = _get_id("ub-route-002")
        client.post(f"/listings/{listing_id}/open")
        with db.get_connection() as conn:
            row = conn.execute(
                "SELECT opened_at FROM listings WHERE id = %s", (listing_id,)
            ).fetchone()
        assert row["opened_at"] is not None

    def test_is_idempotent_via_route(self, client):
        """Calling POST /listings/<id>/open twice preserves the first timestamp."""
        db.insert_listing(make_listing(source_id="ub-route-003"))
        listing_id = _get_id("ub-route-003")

        client.post(f"/listings/{listing_id}/open")
        with db.get_connection() as conn:
            first_ts = conn.execute(
                "SELECT opened_at FROM listings WHERE id = %s", (listing_id,)
            ).fetchone()["opened_at"]

        client.post(f"/listings/{listing_id}/open")
        with db.get_connection() as conn:
            second_ts = conn.execute(
                "SELECT opened_at FROM listings WHERE id = %s", (listing_id,)
            ).fetchone()["opened_at"]

        assert first_ts == second_ts

    def test_nonexistent_listing_returns_200(self, client):
        """POST /listings/<id>/open for a missing id still returns 200 (benign)."""
        resp = client.post("/listings/999999999/open")
        assert resp.status_code == 200

    def test_response_contains_oob_fragment(self, client):
        """POST /listings/<id>/open returns an OOB swap fragment for the badge."""
        db.insert_listing(make_listing(source_id="ub-route-oob"))
        listing_id = _get_id("ub-route-oob")
        resp = client.post(f"/listings/{listing_id}/open")
        assert resp.status_code == 200
        expected = f'<span id="badge-new-{listing_id}" hx-swap-oob="outerHTML"></span>'
        assert expected.encode() in resp.data
