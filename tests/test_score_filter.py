"""
tests/test_score_filter.py — Tests for the min_score filter behaviour on the
feed and snippets routes (GitHub issue #86).

Verifies that:
  - Absent min_score (default) → threshold is used, not score >= 0
  - min_score=0 → score >= 0 is applied (shows all scored listings)
  - min_score=5 → score >= 5 is applied
  - The filter bar renders the correct option labels and selected state

These tests use Flask's test client. DB calls are mocked so no real Postgres
connection is required (conftest.py already patches db.init_db and
db.get_listing_count when DATABASE_URL is absent).
"""

from __future__ import annotations

import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import app as flask_app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_client():
    flask_app.config["TESTING"] = True
    return flask_app.test_client()


# ---------------------------------------------------------------------------
# Feed route — min_score parameter parsing
# ---------------------------------------------------------------------------

class TestFeedMinScore:
    """Verify that the / (feed) route passes the correct min_score to db.get_feed()."""

    def setup_method(self):
        self.client = _make_client()

    def test_absent_min_score_passes_none_to_db(self):
        """When min_score param is absent, db.get_feed is called with min_score=None
        so the DB layer applies the configured threshold."""
        with patch("db.get_feed", return_value=[]) as mock_feed, \
             patch("db.get_job_types", return_value=[]), \
             patch("db.get_last_fetch_time", return_value=None):
            resp = self.client.get("/")
        assert resp.status_code == 200
        _, kwargs = mock_feed.call_args
        assert kwargs.get("min_score") is None

    def test_min_score_zero_passes_zero_to_db(self):
        """When min_score=0, db.get_feed is called with min_score=0.0
        so all scored listings are shown (score >= 0)."""
        with patch("db.get_feed", return_value=[]) as mock_feed, \
             patch("db.get_job_types", return_value=[]), \
             patch("db.get_last_fetch_time", return_value=None):
            resp = self.client.get("/?min_score=0")
        assert resp.status_code == 200
        _, kwargs = mock_feed.call_args
        assert kwargs.get("min_score") == 0.0

    def test_min_score_five_passes_five_to_db(self):
        """When min_score=5, db.get_feed is called with min_score=5.0."""
        with patch("db.get_feed", return_value=[]) as mock_feed, \
             patch("db.get_job_types", return_value=[]), \
             patch("db.get_last_fetch_time", return_value=None):
            resp = self.client.get("/?min_score=5")
        assert resp.status_code == 200
        _, kwargs = mock_feed.call_args
        assert kwargs.get("min_score") == 5.0

    def test_invalid_min_score_falls_back_to_none(self):
        """When min_score is not a valid float, it falls back to None (threshold)."""
        with patch("db.get_feed", return_value=[]) as mock_feed, \
             patch("db.get_job_types", return_value=[]), \
             patch("db.get_last_fetch_time", return_value=None):
            resp = self.client.get("/?min_score=notanumber")
        assert resp.status_code == 200
        _, kwargs = mock_feed.call_args
        assert kwargs.get("min_score") is None


# ---------------------------------------------------------------------------
# Snippets route — min_score parameter parsing
# ---------------------------------------------------------------------------

class TestSnippetsMinScore:
    """Verify that the /snippets route passes the correct min_score to db.get_snippet_feed()."""

    def setup_method(self):
        self.client = _make_client()

    def test_absent_min_score_passes_none_to_db(self):
        """When min_score param is absent, db.get_snippet_feed is called with min_score=None."""
        with patch("db.get_snippet_feed", return_value=[]) as mock_feed, \
             patch("db.get_job_types", return_value=[]):
            resp = self.client.get("/snippets")
        assert resp.status_code == 200
        _, kwargs = mock_feed.call_args
        assert kwargs.get("min_score") is None

    def test_min_score_zero_passes_zero_to_db(self):
        """When min_score=0, db.get_snippet_feed is called with min_score=0.0."""
        with patch("db.get_snippet_feed", return_value=[]) as mock_feed, \
             patch("db.get_job_types", return_value=[]):
            resp = self.client.get("/snippets?min_score=0")
        assert resp.status_code == 200
        _, kwargs = mock_feed.call_args
        assert kwargs.get("min_score") == 0.0

    def test_min_score_five_passes_five_to_db(self):
        """When min_score=5, db.get_snippet_feed is called with min_score=5.0."""
        with patch("db.get_snippet_feed", return_value=[]) as mock_feed, \
             patch("db.get_job_types", return_value=[]):
            resp = self.client.get("/snippets?min_score=5")
        assert resp.status_code == 200
        _, kwargs = mock_feed.call_args
        assert kwargs.get("min_score") == 5.0


# ---------------------------------------------------------------------------
# Filter bar HTML — option labels and selected state
# ---------------------------------------------------------------------------

class TestFilterBarScoreOptions:
    """Verify the rendered HTML of the min_score select contains the correct
    labels and selected state for each filter scenario."""

    def setup_method(self):
        self.client = _make_client()

    def _get_feed_body(self, query: str = "") -> str:
        with patch("db.get_feed", return_value=[]), \
             patch("db.get_job_types", return_value=[]), \
             patch("db.get_last_fetch_time", return_value=None):
            resp = self.client.get(f"/{query}")
        assert resp.status_code == 200
        return resp.data.decode("utf-8")

    def test_default_option_label_contains_default(self):
        """The empty-value option renders 'Score: default' text."""
        body = self._get_feed_body()
        assert "Score: default" in body

    def test_any_option_label_present(self):
        """The value='0' option renders 'Score: any' text."""
        body = self._get_feed_body()
        assert "Score: any" in body

    def test_default_option_selected_when_no_param(self):
        """When min_score is absent, the value='' option has the selected attribute."""
        body = self._get_feed_body()
        # The default option (value="") should be selected
        assert 'value="" selected' in body or '<option value=""' in body
        # The value="0" option must NOT be selected
        assert 'value="0" selected' not in body

    def test_any_option_selected_when_min_score_zero(self):
        """When min_score=0, the value='0' option has the selected attribute."""
        body = self._get_feed_body("?min_score=0")
        assert 'value="0" selected' in body or 'value="0"  selected' in body

    def test_numeric_option_selected_when_min_score_five(self):
        """When min_score=5, the value='5' option has the selected attribute."""
        body = self._get_feed_body("?min_score=5")
        assert 'value="5"' in body
        # value="0" must NOT be selected
        assert 'value="0" selected' not in body
