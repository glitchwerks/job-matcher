"""
tests/test_job_sources.py — Unit tests for the job_sources package.

Covers:
  - JobSource ABC contract (cannot instantiate without implementing all methods)
  - AdzunaClient.normalise() canonical schema output
  - AdzunaClient.total_pages() reads from config
  - AdzunaClient.fetch_page() HTTP success, 429 rate-limit, and error paths
  - SOURCES registry contents
  - make_source() factory — happy path and unknown source error
"""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from job_sources import SOURCES, JobSource, make_source
from job_sources.base import JobSource as JobSourceBase

# Resolve AdzunaClient from the plugin registry so we get the exact same class
# object that SOURCES["adzuna"] holds — necessary for identity checks below.
AdzunaClient = SOURCES["adzuna"]


# ---------------------------------------------------------------------------
# JobSource ABC
# ---------------------------------------------------------------------------

class TestJobSourceABC:
    def test_cannot_instantiate_abstract_class(self):
        """JobSource cannot be instantiated directly — all methods are abstract."""
        with pytest.raises(TypeError):
            JobSourceBase()  # type: ignore[abstract]

    def test_concrete_subclass_must_implement_all_methods(self):
        """A subclass missing any abstract method cannot be instantiated."""
        class Incomplete(JobSourceBase):
            def fetch_page(self, page):
                return []
            # total_pages and normalise intentionally missing

        with pytest.raises(TypeError):
            Incomplete()  # type: ignore[abstract]

    def test_complete_subclass_can_be_instantiated(self):
        """A subclass that implements all three methods can be instantiated."""
        class Complete(JobSourceBase):
            def fetch_page(self, page):
                return []
            def total_pages(self):
                return 1
            def normalise(self, raw):
                return {}

        # Should not raise.
        instance = Complete()
        assert isinstance(instance, JobSourceBase)


# ---------------------------------------------------------------------------
# AdzunaClient.normalise()
# ---------------------------------------------------------------------------

_ADZUNA_CONFIG = {
    "adzuna_app_id": "test-id",
    "adzuna_app_key": "test-key",
    "search": {
        "country": "us",
        "what": "software engineer",
        "results_per_page": 10,
        "max_pages": 3,
    },
    "job_source": "adzuna",
}

_RAW_ADZUNA_LISTING = {
    "id": "12345",
    "title": "Senior Python Engineer",
    "company": {"display_name": "Acme Corp"},
    "location": {"display_name": "New York, NY"},
    "salary_min": 120000,
    "salary_max": 160000,
    "salary_is_predicted": "0",
    "contract_type": "permanent",
    "contract_time": "full_time",
    "description": "We need a Python expert.",
    "redirect_url": "https://www.adzuna.com/details/12345",
    "created": "2026-01-15T09:00:00Z",
}


class TestAdzunaClientNormalise:
    def _client(self) -> AdzunaClient:
        return AdzunaClient(
            app_id=_ADZUNA_CONFIG["adzuna_app_id"],
            app_key=_ADZUNA_CONFIG["adzuna_app_key"],
            config=_ADZUNA_CONFIG,
        )

    def test_normalise_maps_canonical_fields(self):
        """normalise() maps all Adzuna raw fields to the canonical schema."""
        client = self._client()
        result = client.normalise(_RAW_ADZUNA_LISTING)

        assert result["source"] == "adzuna"
        assert result["source_id"] == "12345"
        assert result["title"] == "Senior Python Engineer"
        assert result["company"] == "Acme Corp"
        assert result["location"] == "New York, NY"
        assert result["salary_min"] == 120000
        assert result["salary_max"] == 160000
        assert result["salary_is_predicted"] == 0
        assert result["contract_type"] == "permanent"
        assert result["contract_time"] == "full_time"
        assert result["description"] == "We need a Python expert."
        assert result["redirect_url"] == "https://www.adzuna.com/details/12345"
        assert result["created_at"] == "2026-01-15T09:00:00Z"

    def test_normalise_no_adzuna_id_key(self):
        """normalise() output must not contain an 'adzuna_id' key."""
        client = self._client()
        result = client.normalise(_RAW_ADZUNA_LISTING)
        assert "adzuna_id" not in result

    def test_normalise_source_is_adzuna_string(self):
        """normalise() always sets source='adzuna'."""
        client = self._client()
        result = client.normalise({"id": "99", "title": "Dev"})
        assert result["source"] == "adzuna"

    def test_normalise_salary_is_predicted_coerces_string(self):
        """normalise() converts salary_is_predicted '1' (string) to int 1."""
        client = self._client()
        result = client.normalise({**_RAW_ADZUNA_LISTING, "salary_is_predicted": "1"})
        assert result["salary_is_predicted"] == 1

    def test_normalise_missing_optional_fields_default_to_empty_string(self):
        """Missing optional fields in a raw dict default to empty strings."""
        client = self._client()
        minimal = {"id": "42"}
        result = client.normalise(minimal)

        assert result["source_id"] == "42"
        assert result["title"] == ""
        assert result["company"] == ""
        assert result["description"] == ""
        assert result["redirect_url"] == ""
        assert result["created_at"] == ""

    def test_normalise_company_not_a_dict_returns_empty_string(self):
        """When company is not a dict (e.g. None), company defaults to ''."""
        client = self._client()
        result = client.normalise({**_RAW_ADZUNA_LISTING, "company": None})
        assert result["company"] == ""

    def test_normalise_location_not_a_dict_returns_empty_string(self):
        """When location is not a dict, location defaults to ''."""
        client = self._client()
        result = client.normalise({**_RAW_ADZUNA_LISTING, "location": "London"})
        assert result["location"] == ""

    def test_normalise_null_salary_fields_are_none(self):
        """When salary_min / salary_max are absent, they are None."""
        client = self._client()
        result = client.normalise({"id": "1"})
        assert result["salary_min"] is None
        assert result["salary_max"] is None


# ---------------------------------------------------------------------------
# AdzunaClient.total_pages()
# ---------------------------------------------------------------------------

class TestAdzunaClientTotalPages:
    def test_total_pages_returns_configured_max(self):
        """total_pages() returns the max_pages value from the search config."""
        config = {**_ADZUNA_CONFIG, "search": {**_ADZUNA_CONFIG["search"], "max_pages": 7}}
        client = AdzunaClient(app_id="x", app_key="y", config=config)
        assert client.total_pages() == 7


# ---------------------------------------------------------------------------
# AdzunaClient.fetch_page()
# ---------------------------------------------------------------------------

class TestAdzunaClientFetchPage:
    def _client(self) -> AdzunaClient:
        return AdzunaClient(
            app_id="test-id",
            app_key="test-key",
            config=_ADZUNA_CONFIG,
        )

    def _mock_response(self, status_code: int, json_data: dict) -> MagicMock:
        resp = MagicMock()
        resp.status_code = status_code
        resp.json.return_value = json_data
        return resp

    def test_fetch_page_success_returns_normalised_listings(self):
        """A 200 response with results returns normalised listing dicts."""
        client = self._client()
        mock_resp = self._mock_response(200, {"results": [_RAW_ADZUNA_LISTING]})

        with patch("job_sources.adzuna.requests.get", return_value=mock_resp):
            results = client.fetch_page(1)

        assert len(results) == 1
        assert results[0]["source"] == "adzuna"
        assert results[0]["source_id"] == "12345"

    def test_fetch_page_empty_results_returns_empty_list(self):
        """A 200 response with no results returns an empty list."""
        client = self._client()
        mock_resp = self._mock_response(200, {"results": []})

        with patch("job_sources.adzuna.requests.get", return_value=mock_resp):
            results = client.fetch_page(1)

        assert results == []

    def test_fetch_page_non_200_returns_empty_list(self):
        """A non-200, non-429 response returns an empty list."""
        client = self._client()
        mock_resp = self._mock_response(500, {})

        with patch("job_sources.adzuna.requests.get", return_value=mock_resp):
            results = client.fetch_page(1)

        assert results == []

    def test_fetch_page_request_exception_returns_empty_list(self):
        """A network exception returns an empty list."""
        import requests

        client = self._client()

        with patch(
            "job_sources.adzuna.requests.get",
            side_effect=requests.RequestException("timeout"),
        ):
            results = client.fetch_page(1)

        assert results == []

    def test_fetch_page_invalid_json_returns_empty_list(self):
        """A response that cannot be parsed as JSON returns an empty list."""
        client = self._client()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.side_effect = ValueError("not json")

        with patch("job_sources.adzuna.requests.get", return_value=mock_resp):
            results = client.fetch_page(1)

        assert results == []

    def test_fetch_page_429_exhausted_returns_empty_list(self):
        """After four 429 responses (initial + 3 retries) returns an empty list."""
        client = self._client()
        mock_429 = self._mock_response(429, {})

        with patch("job_sources.adzuna.requests.get", return_value=mock_429), \
             patch("job_sources.adzuna.time.sleep"):  # skip actual sleep in tests
            results = client.fetch_page(1)

        assert results == []


# ---------------------------------------------------------------------------
# SOURCES registry
# ---------------------------------------------------------------------------

class TestSourcesRegistry:
    def test_adzuna_registered(self):
        """SOURCES contains 'adzuna' mapped to AdzunaClient."""
        assert "adzuna" in SOURCES
        assert SOURCES["adzuna"] is AdzunaClient

    def test_all_values_are_job_source_subclasses(self):
        """Every value in SOURCES is a subclass of JobSource."""
        for name, cls in SOURCES.items():
            assert issubclass(cls, JobSource), f"{name!r} is not a JobSource subclass"


# ---------------------------------------------------------------------------
# make_source() factory
# ---------------------------------------------------------------------------

class TestMakeSource:
    def test_returns_adzuna_client_by_default(self):
        """make_source() with no 'job_source' key defaults to AdzunaClient."""
        config = {
            "adzuna_app_id": "id",
            "adzuna_app_key": "key",
            "search": _ADZUNA_CONFIG["search"],
        }
        source = make_source(config)
        assert isinstance(source, AdzunaClient)

    def test_returns_adzuna_client_when_explicitly_set(self):
        """make_source() returns AdzunaClient when job_source='adzuna'."""
        config = {
            "adzuna_app_id": "id",
            "adzuna_app_key": "key",
            "search": _ADZUNA_CONFIG["search"],
            "job_source": "adzuna",
        }
        source = make_source(config)
        assert isinstance(source, AdzunaClient)

    def test_raises_for_unknown_source(self):
        """make_source() raises ValueError for an unregistered source name."""
        config = {
            "job_source": "nonexistent_source",
            "search": _ADZUNA_CONFIG["search"],
        }
        with pytest.raises(ValueError, match="Unknown job source"):
            make_source(config)

    def test_returned_source_is_job_source_instance(self):
        """make_source() always returns a JobSource instance."""
        config = {
            "adzuna_app_id": "id",
            "adzuna_app_key": "key",
            "search": _ADZUNA_CONFIG["search"],
            "job_source": "adzuna",
        }
        source = make_source(config)
        assert isinstance(source, JobSource)


# ---------------------------------------------------------------------------
# AdzunaClient — credential precedence
# ---------------------------------------------------------------------------

class TestAdzunaClientCredentialPrecedence:
    """Verify the three-way credential resolution: credentials > config > empty."""

    _BASE_CONFIG = {
        "search": {
            "country": "us",
            "what": "software engineer",
            "results_per_page": 10,
            "max_pages": 2,
        },
    }

    def test_credentials_dict_used_when_provided(self):
        """When credentials dict has app_id/app_key, they are used directly."""
        client = AdzunaClient(
            config=self._BASE_CONFIG,
            credentials={"app_id": "creds-id", "app_key": "creds-key"},
        )
        assert client._app_id == "creds-id"
        assert client._app_key == "creds-key"

    def test_credentials_dict_takes_precedence_over_config(self):
        """credentials dict values win over top-level config keys."""
        config = {
            **self._BASE_CONFIG,
            "adzuna_app_id": "config-id",
            "adzuna_app_key": "config-key",
        }
        client = AdzunaClient(
            config=config,
            credentials={"app_id": "creds-id", "app_key": "creds-key"},
        )
        assert client._app_id == "creds-id"
        assert client._app_key == "creds-key"

    def test_fallback_to_config_when_credentials_empty(self):
        """When credentials is {} (or absent), values come from config top-level keys."""
        config = {
            **self._BASE_CONFIG,
            "adzuna_app_id": "config-id",
            "adzuna_app_key": "config-key",
        }
        client = AdzunaClient(config=config, credentials={})
        assert client._app_id == "config-id"
        assert client._app_key == "config-key"

    def test_both_empty_gives_empty_strings(self):
        """When neither credentials nor config has values, fields are empty strings.

        AdzunaClient does not raise on missing credentials — it leaves them
        as empty strings and lets the API call fail at runtime.  This matches
        the legacy behaviour where app_id/app_key defaulted to ''.
        """
        client = AdzunaClient(config=self._BASE_CONFIG, credentials={})
        assert client._app_id == ""
        assert client._app_key == ""

    def test_empty_string_in_credentials_falls_back_to_config(self):
        """credentials={"app_id": ""} is treated as absent and falls back to config.

        This is the 'empty string in credentials -> falls back' case: the ``or``
        chain evaluates "" as falsy and continues to the config lookup.
        """
        config = {
            **self._BASE_CONFIG,
            "adzuna_app_id": "config-id",
            "adzuna_app_key": "config-key",
        }
        client = AdzunaClient(
            config=config,
            credentials={"app_id": "", "app_key": ""},
        )
        assert client._app_id == "config-id"
        assert client._app_key == "config-key"
