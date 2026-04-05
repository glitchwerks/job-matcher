"""
tests/test_job_sources_the_muse.py — Unit tests for TheMuseClient.

Covers:
  - TheMuseClient.normalise() — canonical field mapping
  - TheMuseClient.normalise() — HTML stripping from description
  - TheMuseClient.normalise() — missing/optional field handling
  - TheMuseClient.total_pages() — reads page_count from API, caches result
  - TheMuseClient.fetch_page() — page 0, arbitrary page N, and empty page
  - TheMuseClient.fetch_page() — network error, non-200 status, invalid JSON
  - TheMuseClient — missing API key handled gracefully (no key in params)
  - SOURCES registry — "the_muse" registered as TheMuseClient
  - make_source() — returns TheMuseClient when job_source="the_muse"
"""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from job_sources import SOURCES, JobSource, make_source

TheMuseClient = SOURCES["the_muse"]


# ---------------------------------------------------------------------------
# Shared fixtures / helpers
# ---------------------------------------------------------------------------

_BASE_CONFIG: dict = {}  # minimal config with no the_muse block

_FULL_CONFIG: dict = {
    "the_muse": {
        "api_key": "test-key-123",
        "category": "Engineering",
        "results_per_page": 10,
    }
}

_RAW_LISTING: dict = {
    "id": 987654,
    "name": "Senior Python Developer",
    "company": {"name": "Acme Corp"},
    "locations": [{"name": "New York, NY"}],
    "levels": [{"name": "Senior Level"}],
    "type": "permanent",
    "contents": "<p>We need a <strong>Python</strong> expert.</p>",
    "refs": {"landing_page": "https://www.themuse.com/jobs/acme/senior-python-developer"},
    "publication_date": "2026-01-15T09:00:00Z",
}

_API_PAGE_0_RESPONSE: dict = {
    "results": [_RAW_LISTING],
    "page_count": 5,
    "page": 0,
    "total": 95,
}


def _make_client(config: dict | None = None) -> TheMuseClient:
    return TheMuseClient(config=config if config is not None else _BASE_CONFIG)


def _mock_response(status_code: int, json_data: dict) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data
    return resp


# ---------------------------------------------------------------------------
# TheMuseClient.normalise() — canonical field mapping
# ---------------------------------------------------------------------------

class TestTheMuseClientNormalise:
    def test_source_is_the_muse(self):
        """normalise() always sets source='the_muse'."""
        client = _make_client()
        result = client.normalise(_RAW_LISTING)
        assert result["source"] == "the_muse"

    def test_source_id_is_stringified_int(self):
        """normalise() converts the integer id to a string for source_id."""
        client = _make_client()
        result = client.normalise(_RAW_LISTING)
        assert result["source_id"] == "987654"

    def test_title_from_name_field(self):
        """normalise() maps 'name' → 'title'."""
        client = _make_client()
        result = client.normalise(_RAW_LISTING)
        assert result["title"] == "Senior Python Developer"

    def test_company_from_nested_company_name(self):
        """normalise() maps 'company.name' → 'company'."""
        client = _make_client()
        result = client.normalise(_RAW_LISTING)
        assert result["company"] == "Acme Corp"

    def test_location_from_first_locations_entry(self):
        """normalise() maps 'locations[0].name' → 'location'."""
        client = _make_client()
        result = client.normalise(_RAW_LISTING)
        assert result["location"] == "New York, NY"

    def test_salary_fields_are_none(self):
        """normalise() always returns None for salary_min and salary_max."""
        client = _make_client()
        result = client.normalise(_RAW_LISTING)
        assert result["salary_min"] is None
        assert result["salary_max"] is None

    def test_contract_type_is_none(self):
        """normalise() always returns None for contract_type (not in API)."""
        client = _make_client()
        result = client.normalise(_RAW_LISTING)
        assert result["contract_type"] is None

    def test_contract_time_from_type_field(self):
        """normalise() maps 'type' → 'contract_time'."""
        client = _make_client()
        result = client.normalise(_RAW_LISTING)
        assert result["contract_time"] == "permanent"

    def test_redirect_url_from_refs_landing_page(self):
        """normalise() maps 'refs.landing_page' → 'redirect_url'."""
        client = _make_client()
        result = client.normalise(_RAW_LISTING)
        assert result["redirect_url"] == "https://www.themuse.com/jobs/acme/senior-python-developer"

    def test_created_at_from_publication_date(self):
        """normalise() maps 'publication_date' → 'created_at'."""
        client = _make_client()
        result = client.normalise(_RAW_LISTING)
        assert result["created_at"] == "2026-01-15T09:00:00Z"

    def test_all_canonical_keys_present(self):
        """normalise() output contains every key in the canonical schema."""
        expected_keys = {
            "source", "source_id", "title", "company", "location",
            "salary_min", "salary_max", "salary_period", "contract_type", "contract_time",
            "description", "redirect_url", "created_at",
        }
        client = _make_client()
        result = client.normalise(_RAW_LISTING)
        assert expected_keys == set(result.keys())


# ---------------------------------------------------------------------------
# TheMuseClient.normalise() — HTML stripping
# ---------------------------------------------------------------------------

class TestTheMuseClientHTMLStrip:
    def test_html_tags_stripped_from_description(self):
        """normalise() strips HTML markup from the 'contents' field."""
        client = _make_client()
        result = client.normalise(_RAW_LISTING)
        assert "<p>" not in result["description"]
        assert "<strong>" not in result["description"]

    def test_plain_text_preserved_after_strip(self):
        """normalise() preserves meaningful text after stripping HTML."""
        client = _make_client()
        result = client.normalise(_RAW_LISTING)
        assert "Python" in result["description"]
        assert "expert" in result["description"]

    def test_none_contents_returns_empty_string(self):
        """normalise() returns '' for description when 'contents' is None."""
        client = _make_client()
        raw = {**_RAW_LISTING, "contents": None}
        result = client.normalise(raw)
        assert result["description"] == ""

    def test_empty_contents_returns_empty_string(self):
        """normalise() returns '' for description when 'contents' is empty."""
        client = _make_client()
        raw = {**_RAW_LISTING, "contents": ""}
        result = client.normalise(raw)
        assert result["description"] == ""

    def test_nested_html_stripped(self):
        """normalise() strips deeply nested HTML and returns joined text."""
        client = _make_client()
        html = "<div><ul><li>Python</li><li>Django</li></ul></div>"
        raw = {**_RAW_LISTING, "contents": html}
        result = client.normalise(raw)
        assert "Python" in result["description"]
        assert "Django" in result["description"]
        assert "<" not in result["description"]


# ---------------------------------------------------------------------------
# TheMuseClient.normalise() — optional / missing fields
# ---------------------------------------------------------------------------

class TestTheMuseClientNormaliseMissingFields:
    def test_missing_locations_returns_none_for_location(self):
        """normalise() returns None for location when 'locations' is absent."""
        client = _make_client()
        raw = {**_RAW_LISTING, "locations": []}
        result = client.normalise(raw)
        assert result["location"] is None

    def test_absent_locations_key_returns_none_for_location(self):
        """normalise() returns None for location when 'locations' key is missing."""
        client = _make_client()
        raw = {k: v for k, v in _RAW_LISTING.items() if k != "locations"}
        result = client.normalise(raw)
        assert result["location"] is None

    def test_missing_company_name_returns_empty_string(self):
        """normalise() returns '' for company when company.name is absent."""
        client = _make_client()
        raw = {**_RAW_LISTING, "company": {}}
        result = client.normalise(raw)
        assert result["company"] == ""

    def test_none_company_returns_empty_string(self):
        """normalise() returns '' for company when company is None."""
        client = _make_client()
        raw = {**_RAW_LISTING, "company": None}
        result = client.normalise(raw)
        assert result["company"] == ""

    def test_missing_type_returns_none_for_contract_time(self):
        """normalise() returns None for contract_time when 'type' is absent."""
        client = _make_client()
        raw = {k: v for k, v in _RAW_LISTING.items() if k != "type"}
        result = client.normalise(raw)
        assert result["contract_time"] is None

    def test_missing_publication_date_returns_none_for_created_at(self):
        """normalise() returns None for created_at when 'publication_date' is absent."""
        client = _make_client()
        raw = {k: v for k, v in _RAW_LISTING.items() if k != "publication_date"}
        result = client.normalise(raw)
        assert result["created_at"] is None

    def test_missing_refs_returns_empty_redirect_url(self):
        """normalise() returns '' for redirect_url when 'refs' is absent."""
        client = _make_client()
        raw = {k: v for k, v in _RAW_LISTING.items() if k != "refs"}
        result = client.normalise(raw)
        assert result["redirect_url"] == ""

    def test_minimal_raw_does_not_raise(self):
        """normalise() handles a nearly empty raw dict without raising."""
        client = _make_client()
        result = client.normalise({"id": 1})
        assert result["source"] == "the_muse"
        assert result["source_id"] == "1"


# ---------------------------------------------------------------------------
# TheMuseClient.total_pages()
# ---------------------------------------------------------------------------

class TestTheMuseClientTotalPages:
    def test_total_pages_returns_page_count_from_api(self):
        """total_pages() returns the page_count value from the API response."""
        client = _make_client()
        mock_resp = _mock_response(200, _API_PAGE_0_RESPONSE)

        with patch("job_sources._plugin_the_muse.requests.get", return_value=mock_resp):
            count = client.total_pages()

        assert count == 5

    def test_total_pages_queries_page_zero(self):
        """total_pages() fetches page=0 from the API."""
        client = _make_client()
        mock_resp = _mock_response(200, _API_PAGE_0_RESPONSE)

        with patch("job_sources._plugin_the_muse.requests.get", return_value=mock_resp) as mock_get:
            client.total_pages()

        _, kwargs = mock_get.call_args
        assert kwargs["params"]["page"] == 0

    def test_total_pages_caches_result(self):
        """total_pages() only calls the API once; subsequent calls use cache."""
        client = _make_client()
        mock_resp = _mock_response(200, _API_PAGE_0_RESPONSE)

        with patch("job_sources._plugin_the_muse.requests.get", return_value=mock_resp) as mock_get:
            client.total_pages()
            client.total_pages()

        assert mock_get.call_count == 1

    def test_total_pages_returns_zero_on_api_failure(self):
        """total_pages() returns 0 when the API request fails."""
        client = _make_client()
        mock_resp = _mock_response(500, {})

        with patch("job_sources._plugin_the_muse.requests.get", return_value=mock_resp):
            count = client.total_pages()

        assert count == 0


# ---------------------------------------------------------------------------
# TheMuseClient.fetch_page()
# ---------------------------------------------------------------------------

class TestTheMuseClientFetchPage:
    def test_fetch_page_1_returns_normalised_listings(self):
        """fetch_page(1) returns normalised listing dicts for first page."""
        client = _make_client()
        mock_resp = _mock_response(200, _API_PAGE_0_RESPONSE)

        with patch("job_sources._plugin_the_muse.requests.get", return_value=mock_resp) as mock_get:
            results = client.fetch_page(1)

        assert len(results) == 1
        assert results[0]["source"] == "the_muse"
        assert results[0]["source_id"] == "987654"
        _, kwargs = mock_get.call_args
        assert kwargs["params"]["page"] == 0

    def test_fetch_page_n_passes_correct_page_param(self):
        """fetch_page(3) passes page=2 to the API (3-1=2, converting 1-based to 0-based)."""
        client = _make_client()
        page_2_response = {**_API_PAGE_0_RESPONSE, "page": 2}
        mock_resp = _mock_response(200, page_2_response)

        with patch("job_sources._plugin_the_muse.requests.get", return_value=mock_resp) as mock_get:
            client.fetch_page(3)

        _, kwargs = mock_get.call_args
        assert kwargs["params"]["page"] == 2

    def test_fetch_page_empty_results_returns_empty_list(self):
        """fetch_page() returns [] when the API returns no results."""
        client = _make_client()
        mock_resp = _mock_response(200, {"results": [], "page_count": 5})

        with patch("job_sources._plugin_the_muse.requests.get", return_value=mock_resp):
            results = client.fetch_page(1)

        assert results == []

    def test_fetch_page_non_200_returns_empty_list(self):
        """fetch_page() returns [] on a non-200 HTTP status."""
        client = _make_client()
        mock_resp = _mock_response(403, {})

        with patch("job_sources._plugin_the_muse.requests.get", return_value=mock_resp):
            results = client.fetch_page(1)

        assert results == []

    def test_fetch_page_request_exception_returns_empty_list(self):
        """fetch_page() returns [] when a network exception is raised."""
        import requests as req

        client = _make_client()

        with patch(
            "job_sources._plugin_the_muse.requests.get",
            side_effect=req.RequestException("connection refused"),
        ):
            results = client.fetch_page(1)

        assert results == []

    def test_fetch_page_invalid_json_returns_empty_list(self):
        """fetch_page() returns [] when the response body is not valid JSON."""
        client = _make_client()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.side_effect = ValueError("not json")

        with patch("job_sources._plugin_the_muse.requests.get", return_value=mock_resp):
            results = client.fetch_page(1)

        assert results == []

    def test_fetch_page_multiple_results(self):
        """fetch_page() normalises each listing in a multi-result page."""
        client = _make_client()
        second_listing = {**_RAW_LISTING, "id": 111111, "name": "Junior Dev"}
        response = {**_API_PAGE_0_RESPONSE, "results": [_RAW_LISTING, second_listing]}
        mock_resp = _mock_response(200, response)

        with patch("job_sources._plugin_the_muse.requests.get", return_value=mock_resp):
            results = client.fetch_page(1)

        assert len(results) == 2
        assert results[1]["source_id"] == "111111"
        assert results[1]["title"] == "Junior Dev"


# ---------------------------------------------------------------------------
# TheMuseClient — API key handling
# ---------------------------------------------------------------------------

class TestTheMuseClientAPIKey:
    def test_api_key_included_in_params_when_present(self):
        """When api_key is in config, it is passed as a query parameter."""
        client = _make_client(_FULL_CONFIG)
        mock_resp = _mock_response(200, _API_PAGE_0_RESPONSE)

        with patch("job_sources._plugin_the_muse.requests.get", return_value=mock_resp) as mock_get:
            client.fetch_page(1)

        _, kwargs = mock_get.call_args
        assert kwargs["params"]["api_key"] == "test-key-123"

    def test_api_key_absent_from_params_when_not_configured(self):
        """When no api_key is in config, api_key is not in the request params."""
        client = _make_client(_BASE_CONFIG)
        mock_resp = _mock_response(200, _API_PAGE_0_RESPONSE)

        with patch("job_sources._plugin_the_muse.requests.get", return_value=mock_resp) as mock_get:
            client.fetch_page(1)

        _, kwargs = mock_get.call_args
        assert "api_key" not in kwargs["params"]

    def test_category_uses_default_when_not_configured(self):
        """When no category is set, the default 'Software Engineer' is used."""
        client = _make_client(_BASE_CONFIG)
        mock_resp = _mock_response(200, _API_PAGE_0_RESPONSE)

        with patch("job_sources._plugin_the_muse.requests.get", return_value=mock_resp) as mock_get:
            client.fetch_page(1)

        _, kwargs = mock_get.call_args
        assert kwargs["params"]["category"] == "Software Engineer"

    def test_category_overridden_by_config(self):
        """When category is in config, it overrides the default."""
        client = _make_client(_FULL_CONFIG)
        mock_resp = _mock_response(200, _API_PAGE_0_RESPONSE)

        with patch("job_sources._plugin_the_muse.requests.get", return_value=mock_resp) as mock_get:
            client.fetch_page(1)

        _, kwargs = mock_get.call_args
        assert kwargs["params"]["category"] == "Engineering"

    def test_missing_the_muse_block_in_config_does_not_raise(self):
        """Constructing TheMuseClient with no 'the_muse' key in config is fine."""
        client = TheMuseClient(config={})  # no 'the_muse' key at all
        assert client._api_key is None
        assert client._category == "Software Engineer"


# ---------------------------------------------------------------------------
# SOURCES registry
# ---------------------------------------------------------------------------

class TestSourcesRegistry:
    def test_the_muse_registered_in_sources(self):
        """SOURCES contains 'the_muse' mapped to TheMuseClient."""
        assert "the_muse" in SOURCES
        assert SOURCES["the_muse"] is TheMuseClient

    def test_the_muse_is_job_source_subclass(self):
        """TheMuseClient is a proper JobSource subclass."""
        assert issubclass(TheMuseClient, JobSource)


# ---------------------------------------------------------------------------
# make_source() factory
# ---------------------------------------------------------------------------

class TestMakeSourceTheMuse:
    def test_make_source_returns_the_muse_client(self):
        """make_source() returns a TheMuseClient when job_source='the_muse'."""
        config = {"job_source": "the_muse"}
        source = make_source(config)
        assert isinstance(source, TheMuseClient)

    def test_make_source_the_muse_is_job_source_instance(self):
        """make_source() with the_muse returns a JobSource instance."""
        config = {"job_source": "the_muse"}
        source = make_source(config)
        assert isinstance(source, JobSource)


# ---------------------------------------------------------------------------
# TheMuseClient.pages()
# ---------------------------------------------------------------------------

class TestTheMuseClientPages:
    def test_pages_yields_all_pages(self):
        """pages() yields one list per page for all pages returned by total_pages()."""
        client = _make_client()
        page_results = [
            [{"source": "the_muse", "source_id": "1"}],
            [{"source": "the_muse", "source_id": "2"}],
            [{"source": "the_muse", "source_id": "3"}],
        ]

        with patch.object(client, "total_pages", return_value=3):
            with patch.object(client, "fetch_page", side_effect=page_results) as mock_fetch:
                results = list(client.pages())

        assert len(results) == 3
        assert results[0] == page_results[0]
        assert results[1] == page_results[1]
        assert results[2] == page_results[2]
        mock_fetch.assert_any_call(1)
        mock_fetch.assert_any_call(2)
        mock_fetch.assert_any_call(3)

    def test_pages_stops_early_on_empty_page(self):
        """pages() stops iterating when a page returns an empty list."""
        client = _make_client()
        page_results = [
            [{"source": "the_muse", "source_id": "1"}],
            [],  # page 2 is empty — should stop here
        ]

        with patch.object(client, "total_pages", return_value=5):
            with patch.object(client, "fetch_page", side_effect=page_results):
                results = list(client.pages())

        assert len(results) == 1
        assert results[0] == page_results[0]
