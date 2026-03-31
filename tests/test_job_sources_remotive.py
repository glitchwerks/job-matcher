"""
tests/test_job_sources_remotive.py — Unit tests for RemotiveClient.

Covers:
  - fetch_page(): success, empty results, HTTP error, request exception, bad JSON
  - normalise(): canonical field mapping
  - _parse_salary(): range, k-suffix, single value, empty string, unparseable
  - _strip_html(): HTML tags removed, plain text passed through
  - total_pages(): always returns 1
  - SOURCES registry contains "remotive"
  - make_source() factory for "remotive"
"""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from job_sources import SOURCES, JobSource, make_source
from job_sources.remotive import RemotiveClient, _parse_salary, _strip_html


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_client(config: dict | None = None) -> RemotiveClient:
    """Return a RemotiveClient with a minimal or provided config."""
    return RemotiveClient(config=config or {})


def _mock_response(status_code: int, json_data: dict) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data
    return resp


_RAW_JOB = {
    "id": 99001,
    "title": "Senior Python Developer",
    "company_name": "Remote Inc.",
    "candidate_required_location": "Worldwide",
    "salary": "$80,000 - $120,000",
    "job_type": "full_time",
    "description": "<p>We need a <strong>Python</strong> expert.</p>",
    "url": "https://remotive.com/job/99001",
    "publication_date": "2026-03-01T10:00:00",
}


# ---------------------------------------------------------------------------
# _parse_salary()
# ---------------------------------------------------------------------------

class TestParseSalary:
    def test_range_with_dollar_signs(self):
        """Parses '$80,000 - $120,000' into (80000.0, 120000.0)."""
        assert _parse_salary("$80,000 - $120,000") == (80000.0, 120000.0)

    def test_range_with_k_suffix(self):
        """Parses '80k - 120k' into (80000.0, 120000.0)."""
        assert _parse_salary("80k - 120k") == (80000.0, 120000.0)

    def test_single_k_suffix(self):
        """Parses '€50k' into (50000.0, 50000.0) — same min and max."""
        assert _parse_salary("€50k") == (50000.0, 50000.0)

    def test_uppercase_k_suffix(self):
        """Parses '100K' into (100000.0, 100000.0)."""
        assert _parse_salary("100K") == (100000.0, 100000.0)

    def test_single_plain_number(self):
        """Parses '90000' into (90000.0, 90000.0)."""
        assert _parse_salary("90000") == (90000.0, 90000.0)

    def test_empty_string_returns_none_none(self):
        """Empty string returns (None, None)."""
        assert _parse_salary("") == (None, None)

    def test_whitespace_only_returns_none_none(self):
        """Whitespace-only string returns (None, None)."""
        assert _parse_salary("   ") == (None, None)

    def test_no_numbers_returns_none_none(self):
        """A string with no numeric content returns (None, None)."""
        assert _parse_salary("competitive") == (None, None)

    def test_range_no_comma_separators(self):
        """Parses '100000 - 150000' (no commas) into (100000.0, 150000.0)."""
        assert _parse_salary("100000 - 150000") == (100000.0, 150000.0)

    def test_range_with_currency_symbol_and_k(self):
        """Parses '$80k - $100k' into (80000.0, 100000.0)."""
        assert _parse_salary("$80k - $100k") == (80000.0, 100000.0)


# ---------------------------------------------------------------------------
# _strip_html()
# ---------------------------------------------------------------------------

class TestStripHtml:
    def test_strips_paragraph_and_strong_tags(self):
        """Removes <p> and <strong> tags, returning plain text."""
        result = _strip_html("<p>We need a <strong>Python</strong> expert.</p>")
        assert "<p>" not in result
        assert "<strong>" not in result
        assert "Python" in result
        assert "expert" in result

    def test_plain_text_unchanged(self):
        """Plain text with no HTML is returned as-is (modulo strip)."""
        assert _strip_html("Hello world") == "Hello world"

    def test_empty_string_returns_empty(self):
        """Empty string input returns empty string."""
        assert _strip_html("") == ""

    def test_nested_html(self):
        """Handles nested tags like <ul><li>...</li></ul>."""
        html = "<ul><li>Python</li><li>Django</li></ul>"
        result = _strip_html(html)
        assert "Python" in result
        assert "Django" in result
        assert "<li>" not in result


# ---------------------------------------------------------------------------
# RemotiveClient.total_pages()
# ---------------------------------------------------------------------------

class TestRemotiveTotalPages:
    def test_always_returns_1(self):
        """total_pages() always returns 1 — Remotive API is single-page."""
        assert _make_client().total_pages() == 1

    def test_returns_1_regardless_of_config(self):
        """total_pages() returns 1 even when custom config is provided."""
        client = _make_client({"remotive": {"category": "devops", "limit": 50}})
        assert client.total_pages() == 1


# ---------------------------------------------------------------------------
# RemotiveClient.normalise()
# ---------------------------------------------------------------------------

class TestRemotiveNormalise:
    def test_maps_all_canonical_fields(self):
        """normalise() maps every Remotive field to the canonical schema."""
        client = _make_client()
        result = client.normalise(_RAW_JOB)

        assert result["source"] == "remotive"
        assert result["source_id"] == "99001"
        assert result["title"] == "Senior Python Developer"
        assert result["company"] == "Remote Inc."
        assert result["location"] == "Worldwide"
        assert result["salary_min"] == 80000.0
        assert result["salary_max"] == 120000.0
        assert result["contract_type"] is None
        assert result["contract_time"] == "full_time"
        assert result["salary_period"] is None  # Remotive API doesn't expose pay period
        assert result["redirect_url"] == "https://remotive.com/job/99001"
        assert result["created_at"] == "2026-03-01T10:00:00"

    def test_html_stripped_from_description(self):
        """normalise() strips HTML tags from the description field."""
        client = _make_client()
        result = client.normalise(_RAW_JOB)
        assert "<p>" not in result["description"]
        assert "<strong>" not in result["description"]
        assert "Python" in result["description"]

    def test_source_id_is_string_for_int_id(self):
        """normalise() converts integer id to string for source_id."""
        client = _make_client()
        result = client.normalise({**_RAW_JOB, "id": 42})
        assert result["source_id"] == "42"
        assert isinstance(result["source_id"], str)

    def test_contract_type_always_none(self):
        """contract_type is always None — Remotive has no equivalent field."""
        client = _make_client()
        result = client.normalise(_RAW_JOB)
        assert result["contract_type"] is None

    def test_empty_salary_gives_none_none(self):
        """Empty salary field maps to salary_min=None, salary_max=None."""
        client = _make_client()
        result = client.normalise({**_RAW_JOB, "salary": ""})
        assert result["salary_min"] is None
        assert result["salary_max"] is None

    def test_missing_salary_key_gives_none_none(self):
        """Absent salary key maps to salary_min=None, salary_max=None."""
        client = _make_client()
        job = {k: v for k, v in _RAW_JOB.items() if k != "salary"}
        result = client.normalise(job)
        assert result["salary_min"] is None
        assert result["salary_max"] is None

    def test_minimal_raw_dict_does_not_raise(self):
        """normalise() handles a minimal dict without crashing."""
        client = _make_client()
        result = client.normalise({"id": 1})
        assert result["source"] == "remotive"
        assert result["source_id"] == "1"
        assert result["title"] == ""
        assert result["company"] == ""
        assert result["description"] == ""

    def test_source_is_remotive_string(self):
        """source field is always the literal string 'remotive'."""
        client = _make_client()
        assert client.normalise({"id": 1})["source"] == "remotive"


# ---------------------------------------------------------------------------
# RemotiveClient.fetch_page()
# ---------------------------------------------------------------------------

class TestRemotiveFetchPage:
    def test_fetch_page_success_returns_normalised_listings(self):
        """A 200 response returns a list of normalised listing dicts."""
        client = _make_client()
        mock_resp = _mock_response(200, {"jobs": [_RAW_JOB], "job-count": 1})

        with patch("job_sources.remotive.requests.get", return_value=mock_resp):
            results = client.fetch_page(1)

        assert len(results) == 1
        assert results[0]["source"] == "remotive"
        assert results[0]["source_id"] == "99001"

    def test_fetch_page_empty_jobs_returns_empty_list(self):
        """A 200 response with empty jobs list returns []."""
        client = _make_client()
        mock_resp = _mock_response(200, {"jobs": [], "job-count": 0})

        with patch("job_sources.remotive.requests.get", return_value=mock_resp):
            results = client.fetch_page(1)

        assert results == []

    def test_fetch_page_non_200_returns_empty_list(self):
        """An HTTP error response returns []."""
        client = _make_client()
        mock_resp = _mock_response(503, {})

        with patch("job_sources.remotive.requests.get", return_value=mock_resp):
            results = client.fetch_page(1)

        assert results == []

    def test_fetch_page_request_exception_returns_empty_list(self):
        """A network exception returns []."""
        import requests as req_module

        client = _make_client()

        with patch(
            "job_sources.remotive.requests.get",
            side_effect=req_module.RequestException("timeout"),
        ):
            results = client.fetch_page(1)

        assert results == []

    def test_fetch_page_invalid_json_returns_empty_list(self):
        """A non-JSON response returns []."""
        client = _make_client()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.side_effect = ValueError("not json")

        with patch("job_sources.remotive.requests.get", return_value=mock_resp):
            results = client.fetch_page(1)

        assert results == []

    def test_fetch_page_uses_configured_category_and_limit(self):
        """fetch_page() passes category and limit from config to the API."""
        client = _make_client({"remotive": {"category": "devops", "limit": 25}})
        mock_resp = _mock_response(200, {"jobs": [], "job-count": 0})

        with patch("job_sources.remotive.requests.get", return_value=mock_resp) as mock_get:
            client.fetch_page(1)

        call_kwargs = mock_get.call_args
        params = call_kwargs[1]["params"] if "params" in call_kwargs[1] else call_kwargs[0][1]
        assert params["category"] == "devops"
        assert params["limit"] == 25

    def test_fetch_page_defaults_when_no_remotive_config(self):
        """fetch_page() uses default category and limit when config has no 'remotive' key."""
        client = _make_client({})
        mock_resp = _mock_response(200, {"jobs": [], "job-count": 0})

        with patch("job_sources.remotive.requests.get", return_value=mock_resp) as mock_get:
            client.fetch_page(1)

        call_kwargs = mock_get.call_args
        params = call_kwargs[1]["params"] if "params" in call_kwargs[1] else call_kwargs[0][1]
        assert params["category"] == "software-dev"
        assert params["limit"] == 100


# ---------------------------------------------------------------------------
# SOURCES registry
# ---------------------------------------------------------------------------

class TestSourcesRegistry:
    def test_remotive_registered(self):
        """SOURCES contains 'remotive' mapped to RemotiveClient."""
        assert "remotive" in SOURCES
        assert SOURCES["remotive"] is RemotiveClient

    def test_remotive_is_job_source_subclass(self):
        """RemotiveClient is a subclass of JobSource."""
        assert issubclass(RemotiveClient, JobSource)


# ---------------------------------------------------------------------------
# make_source() factory
# ---------------------------------------------------------------------------

class TestMakeSourceRemotive:
    def test_returns_remotive_client(self):
        """make_source() returns a RemotiveClient when job_source='remotive'."""
        config = {"job_source": "remotive"}
        source = make_source(config)
        assert isinstance(source, RemotiveClient)

    def test_returned_instance_is_job_source(self):
        """make_source() for remotive returns a JobSource instance."""
        config = {"job_source": "remotive"}
        source = make_source(config)
        assert isinstance(source, JobSource)
