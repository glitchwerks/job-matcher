"""
tests/test_job_sources_usajobs.py — Unit tests for USAJobsClient.

Covers:
  - Constructor: valid config, missing config block, missing api_key, missing user_agent
  - fetch_page(): correct headers sent, success path, non-200, network error, bad JSON
  - total_pages(): success path, non-200, network error, bad JSON, missing key
  - normalise(): full descriptor, salary PA-only, non-PA salary, missing fields,
                 unparseable salary, missing remuneration list
  - SOURCES registry: usajobs registered
  - make_source() factory: usajobs happy path
"""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

import pytest
import requests as req_module

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from job_sources import SOURCES, JobSource, make_source
from job_sources.usajobs import USAJobsClient, _parse_float


# ---------------------------------------------------------------------------
# Fixtures / shared test data
# ---------------------------------------------------------------------------

_VALID_CONFIG = {
    "job_source": "usajobs",
    "usajobs": {
        "api_key": "test-api-key",
        "user_agent": "test@example.com",
        "keyword": "python developer",
        "results_per_page": 10,
    },
}

# A complete raw SearchResultItems entry as returned by the API.
_RAW_ITEM = {
    "MatchedObjectId": "ABC123",
    "MatchedObjectDescriptor": {
        "PositionTitle": "Software Engineer",
        "OrganizationName": "Dept of Testing",
        "PositionLocationDisplay": "Washington, DC",
        "PositionRemuneration": [
            {
                "MinimumRange": "85000.0",
                "MaximumRange": "130000.0",
                "RateIntervalCode": "PA",
            }
        ],
        "PositionOfferingType": [{"Name": "Permanent"}],
        "ScheduleTypeName": "Full-Time",
        "QualificationSummary": "Must have Python skills.",
        "PositionURI": "https://www.usajobs.gov/job/ABC123",
        "PublicationStartDate": "2026-03-01T00:00:00Z",
    },
}


def _make_client(config: dict | None = None) -> USAJobsClient:
    return USAJobsClient(config=config if config is not None else _VALID_CONFIG)


def _mock_response(status_code: int, json_data: dict) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data
    return resp


def _search_response(items: list[dict], number_of_pages: int = 3) -> dict:
    """Build a minimal USAJobs search API response envelope."""
    return {
        "SearchResult": {
            "SearchResultItems": items,
            "UserArea": {
                "NumberOfPages": number_of_pages,
                "TotalJobs": len(items) * number_of_pages,
            },
        }
    }


# ---------------------------------------------------------------------------
# Constructor validation
# ---------------------------------------------------------------------------

class TestUSAJobsClientInit:
    def test_valid_config_creates_instance(self):
        """USAJobsClient initialises without error given a valid config."""
        client = _make_client()
        assert isinstance(client, USAJobsClient)
        assert isinstance(client, JobSource)

    def test_missing_usajobs_block_raises_value_error(self):
        """ValueError is raised when 'usajobs' key is absent from config and no credentials given."""
        with pytest.raises(ValueError, match="api_key"):
            USAJobsClient(config={})

    def test_empty_usajobs_block_raises_value_error(self):
        """ValueError is raised when 'usajobs' dict is empty and no credentials given."""
        with pytest.raises(ValueError, match="api_key"):
            USAJobsClient(config={"usajobs": {}})

    def test_missing_api_key_raises_value_error(self):
        """ValueError is raised when api_key is absent."""
        config = {"usajobs": {"user_agent": "x@example.com"}}
        with pytest.raises(ValueError, match="api_key"):
            USAJobsClient(config=config)

    def test_missing_user_agent_raises_value_error(self):
        """ValueError is raised when user_agent is absent."""
        config = {"usajobs": {"api_key": "k"}}
        with pytest.raises(ValueError, match="user_agent"):
            USAJobsClient(config=config)

    def test_keyword_defaults_to_software_engineer(self):
        """keyword defaults to 'software engineer' when not specified."""
        config = {"usajobs": {"api_key": "k", "user_agent": "u@example.com"}}
        client = USAJobsClient(config=config)
        assert client._keyword == "software engineer"

    def test_results_per_page_defaults_to_25(self):
        """results_per_page defaults to 25 when not specified."""
        config = {"usajobs": {"api_key": "k", "user_agent": "u@example.com"}}
        client = USAJobsClient(config=config)
        assert client._results_per_page == 25

    def test_custom_keyword_and_results_per_page(self):
        """Custom keyword and results_per_page are read from config."""
        client = _make_client()
        assert client._keyword == "python developer"
        assert client._results_per_page == 10


# ---------------------------------------------------------------------------
# fetch_page() — headers, success, and error paths
# ---------------------------------------------------------------------------

class TestUSAJobsClientFetchPage:
    def test_sends_authorization_key_header(self):
        """fetch_page() sends Authorization-Key header from config."""
        client = _make_client()
        mock_resp = _mock_response(200, _search_response([_RAW_ITEM]))

        with patch("job_sources.usajobs.requests.get", return_value=mock_resp) as mock_get:
            client.fetch_page(1)

        _, kwargs = mock_get.call_args
        assert kwargs["headers"]["Authorization-Key"] == "test-api-key"

    def test_sends_user_agent_header(self):
        """fetch_page() sends User-Agent header from config."""
        client = _make_client()
        mock_resp = _mock_response(200, _search_response([_RAW_ITEM]))

        with patch("job_sources.usajobs.requests.get", return_value=mock_resp) as mock_get:
            client.fetch_page(1)

        _, kwargs = mock_get.call_args
        assert kwargs["headers"]["User-Agent"] == "test@example.com"

    def test_sends_correct_page_param(self):
        """fetch_page(n) passes n as the Page query parameter."""
        client = _make_client()
        mock_resp = _mock_response(200, _search_response([_RAW_ITEM]))

        with patch("job_sources.usajobs.requests.get", return_value=mock_resp) as mock_get:
            client.fetch_page(3)

        _, kwargs = mock_get.call_args
        assert kwargs["params"]["Page"] == 3

    def test_sends_keyword_param(self):
        """fetch_page() passes the configured keyword as Keyword param."""
        client = _make_client()
        mock_resp = _mock_response(200, _search_response([]))

        with patch("job_sources.usajobs.requests.get", return_value=mock_resp) as mock_get:
            client.fetch_page(1)

        _, kwargs = mock_get.call_args
        assert kwargs["params"]["Keyword"] == "python developer"

    def test_success_returns_raw_items(self):
        """A 200 response returns the raw SearchResultItems list."""
        client = _make_client()
        mock_resp = _mock_response(200, _search_response([_RAW_ITEM]))

        with patch("job_sources.usajobs.requests.get", return_value=mock_resp):
            results = client.fetch_page(1)

        assert len(results) == 1
        assert results[0]["MatchedObjectId"] == "ABC123"

    def test_empty_results_returns_empty_list(self):
        """A 200 response with no items returns an empty list."""
        client = _make_client()
        mock_resp = _mock_response(200, _search_response([]))

        with patch("job_sources.usajobs.requests.get", return_value=mock_resp):
            results = client.fetch_page(1)

        assert results == []

    def test_non_200_response_returns_empty_list(self):
        """A non-200 HTTP status returns an empty list."""
        client = _make_client()
        mock_resp = _mock_response(403, {})

        with patch("job_sources.usajobs.requests.get", return_value=mock_resp):
            results = client.fetch_page(1)

        assert results == []

    def test_network_error_returns_empty_list(self):
        """A RequestException returns an empty list."""
        client = _make_client()

        with patch(
            "job_sources.usajobs.requests.get",
            side_effect=req_module.RequestException("connection refused"),
        ):
            results = client.fetch_page(1)

        assert results == []

    def test_invalid_json_returns_empty_list(self):
        """A response that fails JSON parsing returns an empty list."""
        client = _make_client()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.side_effect = ValueError("not json")

        with patch("job_sources.usajobs.requests.get", return_value=mock_resp):
            results = client.fetch_page(1)

        assert results == []

    def test_missing_search_result_key_returns_empty_list(self):
        """A 200 response body without SearchResult returns an empty list."""
        client = _make_client()
        mock_resp = _mock_response(200, {"other": "data"})

        with patch("job_sources.usajobs.requests.get", return_value=mock_resp):
            results = client.fetch_page(1)

        assert results == []


# ---------------------------------------------------------------------------
# total_pages()
# ---------------------------------------------------------------------------

class TestUSAJobsClientTotalPages:
    def test_returns_number_of_pages_from_api(self):
        """total_pages() returns NumberOfPages from the API response."""
        client = _make_client()
        mock_resp = _mock_response(200, _search_response([], number_of_pages=7))

        with patch("job_sources.usajobs.requests.get", return_value=mock_resp):
            pages = client.total_pages()

        assert pages == 7

    def test_sends_correct_headers_for_total_pages(self):
        """total_pages() also sends Authorization-Key and User-Agent headers."""
        client = _make_client()
        mock_resp = _mock_response(200, _search_response([], number_of_pages=1))

        with patch("job_sources.usajobs.requests.get", return_value=mock_resp) as mock_get:
            client.total_pages()

        _, kwargs = mock_get.call_args
        assert kwargs["headers"]["Authorization-Key"] == "test-api-key"
        assert kwargs["headers"]["User-Agent"] == "test@example.com"

    def test_non_200_raises_runtime_error(self):
        """total_pages() raises RuntimeError on non-200 HTTP status."""
        client = _make_client()
        mock_resp = _mock_response(401, {})

        with patch("job_sources.usajobs.requests.get", return_value=mock_resp):
            with pytest.raises(RuntimeError, match="HTTP 401"):
                client.total_pages()

    def test_network_error_raises_runtime_error(self):
        """total_pages() raises RuntimeError on network failure."""
        client = _make_client()

        with patch(
            "job_sources.usajobs.requests.get",
            side_effect=req_module.RequestException("timeout"),
        ):
            with pytest.raises(RuntimeError, match="request failed"):
                client.total_pages()

    def test_invalid_json_raises_runtime_error(self):
        """total_pages() raises RuntimeError when response is not valid JSON."""
        client = _make_client()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.side_effect = ValueError("bad json")

        with patch("job_sources.usajobs.requests.get", return_value=mock_resp):
            with pytest.raises(RuntimeError, match="not valid JSON"):
                client.total_pages()

    def test_missing_number_of_pages_key_raises_runtime_error(self):
        """total_pages() raises RuntimeError when NumberOfPages is absent."""
        client = _make_client()
        bad_body = {"SearchResult": {"UserArea": {}}}
        mock_resp = _mock_response(200, bad_body)

        with patch("job_sources.usajobs.requests.get", return_value=mock_resp):
            with pytest.raises(RuntimeError, match="NumberOfPages"):
                client.total_pages()


# ---------------------------------------------------------------------------
# normalise()
# ---------------------------------------------------------------------------

class TestUSAJobsClientNormalise:
    def _client(self) -> USAJobsClient:
        return _make_client()

    def test_normalise_maps_all_canonical_fields(self):
        """normalise() maps all fields from a complete MatchedObjectDescriptor."""
        client = self._client()
        result = client.normalise(_RAW_ITEM)

        assert result["source"] == "usajobs"
        assert result["source_id"] == "ABC123"
        assert result["title"] == "Software Engineer"
        assert result["company"] == "Dept of Testing"
        assert result["location"] == "Washington, DC"
        assert result["salary_min"] == 85000.0
        assert result["salary_max"] == 130000.0
        assert result["contract_type"] == "Permanent"
        assert result["contract_time"] == "Full-Time"
        assert result["description"] == "Must have Python skills."
        assert result["redirect_url"] == "https://www.usajobs.gov/job/ABC123"
        assert result["created_at"] == "2026-03-01T00:00:00Z"

    def test_normalise_source_always_usajobs(self):
        """normalise() always sets source='usajobs'."""
        client = self._client()
        result = client.normalise({"MatchedObjectId": "X"})
        assert result["source"] == "usajobs"

    def test_normalise_salary_not_mapped_when_rate_is_not_pa(self):
        """Salary fields are None when RateIntervalCode is not 'PA'."""
        client = self._client()
        item = {
            **_RAW_ITEM,
            "MatchedObjectDescriptor": {
                **_RAW_ITEM["MatchedObjectDescriptor"],
                "PositionRemuneration": [
                    {
                        "MinimumRange": "30.0",
                        "MaximumRange": "50.0",
                        "RateIntervalCode": "PH",  # per hour, not per annum
                    }
                ],
            },
        }
        result = client.normalise(item)
        assert result["salary_min"] is None
        assert result["salary_max"] is None

    def test_normalise_salary_mapped_when_rate_is_pa(self):
        """Salary fields are float when RateIntervalCode == 'PA'."""
        client = self._client()
        result = client.normalise(_RAW_ITEM)
        assert isinstance(result["salary_min"], float)
        assert isinstance(result["salary_max"], float)

    def test_normalise_salary_none_when_remuneration_absent(self):
        """Salary fields are None when PositionRemuneration list is absent."""
        client = self._client()
        item = {
            **_RAW_ITEM,
            "MatchedObjectDescriptor": {
                **_RAW_ITEM["MatchedObjectDescriptor"],
                "PositionRemuneration": [],
            },
        }
        result = client.normalise(item)
        assert result["salary_min"] is None
        assert result["salary_max"] is None

    def test_normalise_salary_none_when_unparseable(self):
        """Salary fields are None when MinimumRange / MaximumRange are non-numeric."""
        client = self._client()
        item = {
            **_RAW_ITEM,
            "MatchedObjectDescriptor": {
                **_RAW_ITEM["MatchedObjectDescriptor"],
                "PositionRemuneration": [
                    {
                        "MinimumRange": "N/A",
                        "MaximumRange": "",
                        "RateIntervalCode": "PA",
                    }
                ],
            },
        }
        result = client.normalise(item)
        assert result["salary_min"] is None
        assert result["salary_max"] is None

    def test_normalise_contract_type_none_when_offering_type_absent(self):
        """contract_type is None when PositionOfferingType is empty."""
        client = self._client()
        item = {
            **_RAW_ITEM,
            "MatchedObjectDescriptor": {
                **_RAW_ITEM["MatchedObjectDescriptor"],
                "PositionOfferingType": [],
            },
        }
        result = client.normalise(item)
        assert result["contract_type"] is None

    def test_normalise_minimal_raw_item(self):
        """normalise() handles a minimal raw item with no descriptor gracefully."""
        client = self._client()
        result = client.normalise({"MatchedObjectId": "MIN1"})

        assert result["source_id"] == "MIN1"
        assert result["title"] == ""
        assert result["company"] == ""
        assert result["location"] == ""
        assert result["salary_min"] is None
        assert result["salary_max"] is None
        assert result["contract_type"] is None
        assert result["contract_time"] is None
        assert result["description"] == ""
        assert result["redirect_url"] == ""
        assert result["created_at"] == ""

    def test_normalise_returns_all_canonical_keys(self):
        """normalise() always returns exactly the canonical schema keys."""
        canonical_keys = {
            "source", "source_id", "title", "company", "location",
            "salary_min", "salary_max", "salary_period", "contract_type", "contract_time",
            "description", "redirect_url", "created_at",
        }
        client = self._client()
        result = client.normalise(_RAW_ITEM)
        assert set(result.keys()) == canonical_keys


# ---------------------------------------------------------------------------
# _parse_float helper
# ---------------------------------------------------------------------------

class TestParseFloat:
    def test_parses_string_float(self):
        assert _parse_float("75000.0") == 75000.0

    def test_parses_integer_string(self):
        assert _parse_float("100000") == 100000.0

    def test_parses_actual_float(self):
        assert _parse_float(50000.0) == 50000.0

    def test_none_returns_none(self):
        assert _parse_float(None) is None

    def test_empty_string_returns_none(self):
        assert _parse_float("") is None

    def test_non_numeric_string_returns_none(self):
        assert _parse_float("N/A") is None

    def test_list_returns_none(self):
        assert _parse_float([1, 2]) is None


# ---------------------------------------------------------------------------
# SOURCES registry
# ---------------------------------------------------------------------------

class TestSourcesRegistry:
    def test_usajobs_registered(self):
        """SOURCES contains 'usajobs' mapped to USAJobsClient."""
        assert "usajobs" in SOURCES
        assert SOURCES["usajobs"] is USAJobsClient

    def test_usajobs_is_job_source_subclass(self):
        """USAJobsClient is a subclass of JobSource."""
        assert issubclass(USAJobsClient, JobSource)


# ---------------------------------------------------------------------------
# make_source() factory
# ---------------------------------------------------------------------------

class TestMakeSourceUSAJobs:
    def test_make_source_returns_usajobs_client(self):
        """make_source() returns a USAJobsClient when job_source='usajobs'."""
        source = make_source(_VALID_CONFIG)
        assert isinstance(source, USAJobsClient)

    def test_make_source_usajobs_is_job_source_instance(self):
        """make_source() with usajobs returns a JobSource instance."""
        source = make_source(_VALID_CONFIG)
        assert isinstance(source, JobSource)

    def test_make_source_usajobs_missing_config_block_raises(self):
        """make_source() raises ValueError when usajobs config block is absent."""
        config = {"job_source": "usajobs"}
        with pytest.raises(ValueError):
            make_source(config)


# ---------------------------------------------------------------------------
# USAJobsClient — credential precedence
# ---------------------------------------------------------------------------

class TestUSAJobsClientCredentialPrecedence:
    """Verify the three-way credential resolution: credentials > config > ValueError."""

    _BASE_CONFIG: dict = {}

    def test_credentials_dict_used_when_provided(self):
        """When credentials dict has api_key/user_agent, they are used directly."""
        client = USAJobsClient(
            config=self._BASE_CONFIG,
            credentials={"api_key": "creds-key", "user_agent": "creds@example.com"},
        )
        assert client._api_key == "creds-key"
        assert client._user_agent == "creds@example.com"

    def test_credentials_dict_takes_precedence_over_config(self):
        """credentials values win over config['usajobs'] values."""
        config = {"usajobs": {"api_key": "config-key", "user_agent": "config@example.com"}}
        client = USAJobsClient(
            config=config,
            credentials={"api_key": "creds-key", "user_agent": "creds@example.com"},
        )
        assert client._api_key == "creds-key"
        assert client._user_agent == "creds@example.com"

    def test_fallback_to_config_when_credentials_empty(self):
        """When credentials is {}, values come from config['usajobs']."""
        config = {"usajobs": {"api_key": "config-key", "user_agent": "config@example.com"}}
        client = USAJobsClient(config=config, credentials={})
        assert client._api_key == "config-key"
        assert client._user_agent == "config@example.com"

    def test_both_empty_raises_value_error_for_api_key(self):
        """When neither credentials nor config has api_key, raises ValueError."""
        with pytest.raises(ValueError, match="api_key"):
            USAJobsClient(config={}, credentials={})

    def test_both_empty_raises_value_error_for_user_agent(self):
        """When api_key is provided but user_agent is absent, raises ValueError."""
        with pytest.raises(ValueError, match="user_agent"):
            USAJobsClient(
                config={},
                credentials={"api_key": "some-key"},
            )

    def test_empty_string_in_credentials_falls_back_to_config(self):
        """credentials={"api_key": "", "user_agent": ""} falls back to config.

        The ``or`` chain evaluates "" as falsy, so the config lookup is used.
        """
        config = {"usajobs": {"api_key": "config-key", "user_agent": "config@example.com"}}
        client = USAJobsClient(
            config=config,
            credentials={"api_key": "", "user_agent": ""},
        )
        assert client._api_key == "config-key"
        assert client._user_agent == "config@example.com"
