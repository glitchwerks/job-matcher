"""
tests/test_job_sources_jobicy.py — Unit tests for JobicyClient.

Covers:
  - normalise(): canonical field mapping, HTML stripping, salary from
    structured min/max fields, source_id always a string
  - fetch_page(): HTTP 200 success, non-200 error, network exception, bad JSON,
    empty jobs list
  - total_pages(): always returns 1
  - pages(): yields one list on success, yields nothing when fetch returns []
  - SOURCES registry: "jobicy" key maps to JobicyClient
  - settings_schema(): returns empty fields list (no credentials required)
"""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

import requests as _req

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from job_sources import SOURCES, JobSource
from job_sources.jobicy import JobicyClient
from job_sources.utils import strip_html as _strip_html


# ---------------------------------------------------------------------------
# Helper factories
# ---------------------------------------------------------------------------

def _client(config: dict | None = None) -> JobicyClient:
    """Return a JobicyClient with a minimal or provided config."""
    return JobicyClient(config=config or {})


def _mock_response(status_code: int, json_data: dict) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data
    return resp


# A complete raw Jobicy listing matching the documented API schema.
_RAW_JOB: dict = {
    "id": 12345,
    "url": "https://jobicy.com/jobs/12345-senior-python-developer",
    "jobTitle": "Senior Python Developer",
    "companyName": "Remote Corp",
    "jobGeo": "USA",
    "jobType": "full_time",
    "jobExcerpt": "<p>Short excerpt.</p>",
    "jobDescription": "<p>We need a <strong>Python</strong> expert with Django experience.</p>",
    "pubDate": "2026-03-15T09:00:00",
    "annualSalaryMin": 90000,
    "annualSalaryMax": 130000,
    "salaryCurrency": "USD",
}


# ---------------------------------------------------------------------------
# _strip_html helper
# ---------------------------------------------------------------------------

class TestStripHtml:
    def test_strips_paragraph_and_strong_tags(self):
        """Removes <p> and <strong> tags, returning plain text."""
        result = _strip_html("<p>We need a <strong>Python</strong> expert.</p>")
        assert "<p>" not in result
        assert "<strong>" not in result
        assert "Python" in result

    def test_plain_text_unchanged(self):
        """Plain text with no HTML is returned as-is (modulo strip)."""
        assert _strip_html("Hello world") == "Hello world"

    def test_empty_string_returns_empty(self):
        """Empty string input returns empty string."""
        assert _strip_html("") == ""

    def test_nested_html_flattened(self):
        """Nested tags like <ul><li> are stripped to plain text."""
        html = "<ul><li>Python</li><li>Django</li></ul>"
        result = _strip_html(html)
        assert "Python" in result
        assert "Django" in result
        assert "<li>" not in result


# ---------------------------------------------------------------------------
# JobicyClient.total_pages()
# ---------------------------------------------------------------------------

class TestJobicyTotalPages:
    def test_always_returns_1(self):
        """total_pages() always returns 1 — Jobicy API is single-page."""
        assert _client().total_pages() == 1

    def test_returns_1_regardless_of_config(self):
        """total_pages() returns 1 even when custom config is provided."""
        client = _client({"jobicy": {"tag": "devops", "geo": "uk", "count": 10}})
        assert client.total_pages() == 1


# ---------------------------------------------------------------------------
# JobicyClient.normalise()
# ---------------------------------------------------------------------------

class TestJobicyNormalise:
    def test_maps_all_canonical_fields(self):
        """normalise() maps every Jobicy field to the canonical schema."""
        client = _client()
        result = client.normalise(_RAW_JOB)

        assert result["source"] == "jobicy"
        assert result["source_id"] == "12345"
        assert result["title"] == "Senior Python Developer"
        assert result["company"] == "Remote Corp"
        assert result["location"] == "USA"
        assert result["salary_min"] == 90000.0
        assert result["salary_max"] == 130000.0
        assert result["salary_period"] == "annual"
        assert result["contract_type"] is None
        assert result["contract_time"] == "full_time"
        assert result["redirect_url"] == "https://jobicy.com/jobs/12345-senior-python-developer"
        assert result["created_at"] == "2026-03-15T09:00:00"

    def test_source_id_is_string_for_integer_id(self):
        """normalise() converts integer id to string for source_id."""
        client = _client()
        result = client.normalise({**_RAW_JOB, "id": 99})
        assert result["source_id"] == "99"
        assert isinstance(result["source_id"], str)

    def test_html_stripped_from_description(self):
        """normalise() strips HTML tags from jobDescription."""
        client = _client()
        result = client.normalise(_RAW_JOB)
        assert "<p>" not in result["description"]
        assert "<strong>" not in result["description"]
        assert "Python" in result["description"]

    def test_salary_period_annual_when_min_present(self):
        """salary_period is 'annual' when annualSalaryMin is set."""
        client = _client()
        result = client.normalise({**_RAW_JOB, "annualSalaryMin": 80000, "annualSalaryMax": None})
        assert result["salary_period"] == "annual"
        assert result["salary_min"] == 80000.0
        assert result["salary_max"] is None

    def test_salary_period_annual_when_max_present(self):
        """salary_period is 'annual' when annualSalaryMax is set."""
        client = _client()
        result = client.normalise({**_RAW_JOB, "annualSalaryMin": None, "annualSalaryMax": 120000})
        assert result["salary_period"] == "annual"
        assert result["salary_min"] is None
        assert result["salary_max"] == 120000.0

    def test_salary_period_none_when_both_absent(self):
        """salary_period is None when both salary fields are absent."""
        client = _client()
        raw = {k: v for k, v in _RAW_JOB.items()
               if k not in ("annualSalaryMin", "annualSalaryMax")}
        result = client.normalise(raw)
        assert result["salary_period"] is None
        assert result["salary_min"] is None
        assert result["salary_max"] is None

    def test_salary_period_none_when_both_null(self):
        """salary_period is None when both salary fields are null."""
        client = _client()
        result = client.normalise({**_RAW_JOB, "annualSalaryMin": None, "annualSalaryMax": None})
        assert result["salary_period"] is None
        assert result["salary_min"] is None
        assert result["salary_max"] is None

    def test_contract_type_always_none(self):
        """contract_type is always None — Jobicy has no equivalent field."""
        client = _client()
        result = client.normalise(_RAW_JOB)
        assert result["contract_type"] is None

    def test_source_is_jobicy_string(self):
        """source field is always the literal string 'jobicy'."""
        client = _client()
        assert client.normalise({"id": 1})["source"] == "jobicy"

    def test_minimal_raw_dict_does_not_raise(self):
        """normalise() handles a minimal dict without crashing."""
        client = _client()
        result = client.normalise({"id": 1})
        assert result["source"] == "jobicy"
        assert result["source_id"] == "1"
        assert result["title"] == ""
        assert result["company"] == ""
        assert result["description"] == ""

    def test_result_contains_all_canonical_keys(self):
        """normalise() output contains all required canonical schema keys."""
        required_keys = {
            "source", "source_id", "title", "company", "location",
            "salary_min", "salary_max", "salary_period", "contract_type", "contract_time",
            "description", "redirect_url", "created_at",
        }
        client = _client()
        result = client.normalise(_RAW_JOB)
        assert required_keys.issubset(result.keys())


# ---------------------------------------------------------------------------
# JobicyClient.fetch_page()
# ---------------------------------------------------------------------------

class TestJobicyFetchPage:
    def test_success_returns_normalised_listings(self):
        """A 200 response returns a list of normalised listing dicts."""
        client = _client()
        mock_resp = _mock_response(200, {"jobs": [_RAW_JOB]})

        with patch("job_sources.jobicy.requests.get", return_value=mock_resp):
            results = client.fetch_page(1)

        assert len(results) == 1
        assert results[0]["source"] == "jobicy"
        assert results[0]["source_id"] == "12345"

    def test_empty_jobs_returns_empty_list(self):
        """A 200 response with an empty jobs list returns []."""
        client = _client()
        mock_resp = _mock_response(200, {"jobs": []})

        with patch("job_sources.jobicy.requests.get", return_value=mock_resp):
            assert client.fetch_page(1) == []

    def test_missing_jobs_key_returns_empty_list(self):
        """A 200 response without a 'jobs' key returns []."""
        client = _client()
        mock_resp = _mock_response(200, {})

        with patch("job_sources.jobicy.requests.get", return_value=mock_resp):
            assert client.fetch_page(1) == []

    def test_non_200_returns_empty_list(self):
        """A non-200 HTTP status returns []."""
        client = _client()
        mock_resp = _mock_response(503, {})

        with patch("job_sources.jobicy.requests.get", return_value=mock_resp):
            assert client.fetch_page(1) == []

    def test_request_exception_returns_empty_list(self):
        """A network exception returns []."""
        client = _client()

        with patch(
            "job_sources.jobicy.requests.get",
            side_effect=_req.RequestException("timeout"),
        ):
            assert client.fetch_page(1) == []

    def test_invalid_json_returns_empty_list(self):
        """A non-JSON response body returns []."""
        client = _client()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.side_effect = ValueError("not json")

        with patch("job_sources.jobicy.requests.get", return_value=mock_resp):
            assert client.fetch_page(1) == []

    def test_passes_configured_params(self):
        """fetch_page() sends tag, geo, and count from config to the API."""
        client = _client({"jobicy": {"tag": "devops", "geo": "uk", "count": 25}})
        mock_resp = _mock_response(200, {"jobs": []})

        with patch("job_sources.jobicy.requests.get", return_value=mock_resp) as mock_get:
            client.fetch_page(1)

        _, kwargs = mock_get.call_args
        params = kwargs["params"]
        assert params["tag"] == "devops"
        assert params["geo"] == "uk"
        assert params["count"] == 25

    def test_uses_default_params_when_no_jobicy_config(self):
        """fetch_page() uses default tag/geo/count when config has no 'jobicy' key."""
        client = _client({})
        mock_resp = _mock_response(200, {"jobs": []})

        with patch("job_sources.jobicy.requests.get", return_value=mock_resp) as mock_get:
            client.fetch_page(1)

        _, kwargs = mock_get.call_args
        params = kwargs["params"]
        assert params["tag"] == "software engineer"
        assert params["geo"] == "usa"
        assert params["count"] == 50


# ---------------------------------------------------------------------------
# JobicyClient.pages()
# ---------------------------------------------------------------------------

class TestJobicyPages:
    def test_yields_one_page_on_success(self):
        """pages() yields exactly one list when fetch_page returns results."""
        client = _client()
        mock_resp = _mock_response(200, {"jobs": [_RAW_JOB]})

        with patch("job_sources.jobicy.requests.get", return_value=mock_resp):
            pages = list(client.pages())

        assert len(pages) == 1
        assert len(pages[0]) == 1
        assert pages[0][0]["source_id"] == "12345"

    def test_yields_nothing_when_fetch_empty(self):
        """pages() yields nothing when the fetch returns no results."""
        client = _client()
        mock_resp = _mock_response(200, {"jobs": []})

        with patch("job_sources.jobicy.requests.get", return_value=mock_resp):
            pages = list(client.pages())

        assert pages == []


# ---------------------------------------------------------------------------
# SOURCES registry and settings_schema()
# ---------------------------------------------------------------------------

class TestSourcesRegistry:
    def test_jobicy_registered(self):
        """SOURCES contains 'jobicy' mapped to JobicyClient."""
        assert "jobicy" in SOURCES
        assert SOURCES["jobicy"] is JobicyClient

    def test_jobicy_is_job_source_subclass(self):
        """JobicyClient is a subclass of JobSource."""
        assert issubclass(JobicyClient, JobSource)


class TestJobicySettingsSchema:
    def test_has_display_name(self):
        """settings_schema() returns a display_name string."""
        schema = JobicyClient.settings_schema()
        assert isinstance(schema["display_name"], str)
        assert schema["display_name"]

    def test_fields_is_empty_list(self):
        """settings_schema() returns an empty fields list (no credentials needed)."""
        schema = JobicyClient.settings_schema()
        assert schema["fields"] == []
