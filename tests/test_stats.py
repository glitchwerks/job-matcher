"""
tests/test_stats.py — Regression tests for the /stats route NULL cost handling.

Verifies that when get_usage_stats() returns None for estimated_cost_usd or
per-date cost_usd (which happens when any listing has a NULL model_used), the
template renders an em dash (—) instead of crashing with a TypeError.

These tests use Flask's built-in test client. DB calls are fully mocked so no
real Postgres connection is required.

Regression for: GitHub issue #97 / PR #103
"""

from __future__ import annotations

import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import app as flask_app


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def client():
    flask_app.config["TESTING"] = True
    with flask_app.test_client() as c:
        yield c


def _make_stats(**overrides) -> dict:
    """Return a well-formed get_usage_stats() payload with sensible defaults."""
    base = {
        "total_scored": 10,
        "total_tokens_input": 5000,
        "total_tokens_output": 1200,
        "estimated_cost_usd": 0.0042,
        "by_date": [
            {
                "date": "2026-04-08",
                "scored": 5,
                "tokens_input": 2500,
                "tokens_output": 600,
                "cost_usd": 0.0021,
            }
        ],
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestStatsRoute:
    """Route-level regression tests for GET /stats."""

    def test_stats_renders_with_all_costs_known(self, client):
        """When all costs are non-None floats the page renders 200 and shows a '$' sign."""
        stats_data = _make_stats(
            estimated_cost_usd=0.0042,
            by_date=[
                {
                    "date": "2026-04-08",
                    "scored": 5,
                    "tokens_input": 2500,
                    "tokens_output": 600,
                    "cost_usd": 0.0021,
                }
            ],
        )
        with patch("db.get_usage_stats", return_value=stats_data), \
             patch("web.feed._config_warnings", return_value=[]):
            resp = client.get("/stats")

        assert resp.status_code == 200
        body = resp.data.decode()
        assert "$" in body

    def test_stats_renders_with_null_costs(self, client):
        """When estimated_cost_usd and per-date cost_usd are None the page renders 200.

        This is the core regression: before the fix, Jinja2 would raise a
        TypeError when trying to format None as a float.  After the fix the
        template guards with ``{% if ... is not none %}`` and renders an em
        dash (—) via ``&mdash;`` instead.
        """
        stats_data = _make_stats(
            estimated_cost_usd=None,
            by_date=[
                {
                    "date": "2026-04-08",
                    "scored": 5,
                    "tokens_input": 2500,
                    "tokens_output": 600,
                    "cost_usd": None,
                }
            ],
        )
        with patch("db.get_usage_stats", return_value=stats_data), \
             patch("web.feed._config_warnings", return_value=[]):
            resp = client.get("/stats")

        assert resp.status_code == 200
        # The template renders &mdash; which the browser decodes to —.
        # Flask returns the raw HTML so we check for the entity or the literal char.
        body = resp.data.decode()
        assert "&mdash;" in body or "\u2014" in body

    def test_stats_renders_with_empty_data(self, client):
        """When there are zero scored listings and no by_date rows the page renders 200."""
        stats_data = {
            "total_scored": 0,
            "total_tokens_input": 0,
            "total_tokens_output": 0,
            "estimated_cost_usd": None,
            "by_date": [],
        }
        with patch("db.get_usage_stats", return_value=stats_data), \
             patch("web.feed._config_warnings", return_value=[]):
            resp = client.get("/stats")

        assert resp.status_code == 200

    def test_stats_renders_with_mixed_null_and_known_costs(self, client):
        """Some by_date rows have cost_usd=None while others have float values.

        This exercises the per-row guard inside the template loop — each row
        must render independently without any single None causing a crash.
        """
        stats_data = _make_stats(
            estimated_cost_usd=None,
            by_date=[
                {
                    "date": "2026-04-07",
                    "scored": 3,
                    "tokens_input": 1500,
                    "tokens_output": 400,
                    "cost_usd": None,
                },
                {
                    "date": "2026-04-08",
                    "scored": 5,
                    "tokens_input": 2500,
                    "tokens_output": 600,
                    "cost_usd": 0.0021,
                },
            ],
        )
        with patch("db.get_usage_stats", return_value=stats_data), \
             patch("web.feed._config_warnings", return_value=[]):
            resp = client.get("/stats")

        assert resp.status_code == 200
        body = resp.data.decode()
        # The row with a known cost should show a dollar amount.
        assert "$" in body
        # The row with None cost should show an em dash.
        assert "&mdash;" in body or "\u2014" in body
