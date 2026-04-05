"""
tests/test_job_sources_himalayas.py — Unit tests for HimalayasClient.

Covers:
  - fetch_page(): offset calculation, success, empty results, HTTP error,
    network exception, invalid JSON
  - total_pages(): uses cached total, fetches when uncached, ceil division,
    handles API failure gracefully
  - normalise(): all canonical field mappings
    - locationRestrictions: join, empty → "Worldwide"
    - createdAt: ISO string pass-through, Unix ms int conversion, None
    - jobType: all known mappings, unknown value lowercased, None
    - description: HTML stripped, plain text unchanged, empty
    - salary_min / salary_max: present, null
    - contract_type: always None
  - SOURCES registry contains "himalayas"
  - make_source() returns HimalayasClient when job_source="himalayas"
"""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from job_sources import SOURCES, make_source
from job_sources._plugin_himalayas import _parse_created_at, _strip_html, _map_job_type

# Resolve HimalayasClient from the plugin registry so identity checks pass.
HimalayasClient = SOURCES["himalayas"]


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

_BASE_CONFIG: dict = {}  # Himalayas needs no API keys

_HIMALAYAS_CONFIG_WITH_LIMIT: dict = {"himalayas": {"limit": 10}}

_RAW_JOB: dict = {
    "guid": "https://himalayas.app/companies/remote-co/jobs/abc123",
    "title": "Senior Python Engineer",
    "companyName": "Remote Co",
    "locationRestrictions": ["USA", "Canada"],
    "minSalary": 120000,
    "maxSalary": 160000,
    "employmentType": "FULL_TIME",
    "description": "<p>We need a <strong>Python</strong> expert.</p>",
    "applicationLink": "https://himalayas.app/jobs/abc123/apply",
    "pubDate": "2026-01-15T09:00:00Z",
}


def _mock_response(status_code: int, json_data: dict) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data
    resp.raise_for_status = MagicMock()
    if status_code >= 400:
        resp.raise_for_status.side_effect = Exception(f"HTTP {status_code}")
    return resp


# ---------------------------------------------------------------------------
# _strip_html helper
# ---------------------------------------------------------------------------

class TestStripHtml:
    def test_strips_simple_tags(self):
        assert _strip_html("<p>Hello world</p>") == "Hello world"

    def test_strips_nested_tags(self):
        result = _strip_html("<p>We need a <strong>Python</strong> expert.</p>")
        assert "Python" in result
        assert "<" not in result

    def test_plain_text_unchanged(self):
        plain = "No HTML here, just plain text."
        assert _strip_html(plain) == plain

    def test_markdown_text_unchanged(self):
        md = "## Heading\n- bullet\n**bold**"
        assert _strip_html(md) == md

    def test_empty_string(self):
        assert _strip_html("") == ""


# ---------------------------------------------------------------------------
# _parse_created_at helper
# ---------------------------------------------------------------------------

class TestParseCreatedAt:
    def test_none_returns_none(self):
        assert _parse_created_at(None) is None

    def test_iso_string_passed_through(self):
        iso = "2026-01-15T09:00:00Z"
        assert _parse_created_at(iso) == iso

    def test_unix_ms_int_converted_to_iso(self):
        # 1_700_000_000_000 ms = 2023-11-14T22:13:20Z
        result = _parse_created_at(1_700_000_000_000)
        assert result == "2023-11-14T22:13:20Z"

    def test_zero_unix_ms_int(self):
        result = _parse_created_at(0)
        assert result == "1970-01-01T00:00:00Z"

    def test_unix_ms_int_arbitrary(self):
        # 1_704_067_200_000 ms = 2024-01-01T00:00:00Z
        result = _parse_created_at(1_704_067_200_000)
        assert result == "2024-01-01T00:00:00Z"


# ---------------------------------------------------------------------------
# _map_job_type helper
# ---------------------------------------------------------------------------

class TestMapJobType:
    def test_full_time(self):
        assert _map_job_type("FULL_TIME") == "full_time"

    def test_part_time(self):
        assert _map_job_type("PART_TIME") == "part_time"

    def test_contract(self):
        assert _map_job_type("CONTRACT") == "contract"

    def test_freelance(self):
        assert _map_job_type("FREELANCE") == "freelance"

    def test_internship(self):
        assert _map_job_type("INTERNSHIP") == "internship"

    def test_unknown_value_lowercased(self):
        assert _map_job_type("TEMPORARY") == "temporary"

    def test_none_returns_none(self):
        assert _map_job_type(None) is None

    def test_empty_string_returns_none(self):
        assert _map_job_type("") is None


# ---------------------------------------------------------------------------
# HimalayasClient.normalise()
# ---------------------------------------------------------------------------

class TestHimalayasClientNormalise:
    def _client(self) -> HimalayasClient:
        return HimalayasClient(config=_BASE_CONFIG)

    def test_normalise_source_is_himalayas(self):
        client = self._client()
        result = client.normalise(_RAW_JOB)
        assert result["source"] == "himalayas"

    def test_normalise_maps_all_canonical_fields(self):
        client = self._client()
        result = client.normalise(_RAW_JOB)

        assert result["source_id"] == "https://himalayas.app/companies/remote-co/jobs/abc123"
        assert result["title"] == "Senior Python Engineer"
        assert result["company"] == "Remote Co"
        assert result["location"] == "USA, Canada"
        assert result["salary_min"] == 120000
        assert result["salary_max"] == 160000
        assert result["contract_type"] is None
        assert result["contract_time"] == "full_time"
        assert result["redirect_url"] == "https://himalayas.app/jobs/abc123/apply"
        assert result["created_at"] == "2026-01-15T09:00:00Z"

    def test_normalise_html_description_stripped(self):
        client = self._client()
        result = client.normalise(_RAW_JOB)
        assert "<" not in result["description"]
        assert "Python" in result["description"]

    def test_normalise_plain_text_description_unchanged(self):
        client = self._client()
        raw = {**_RAW_JOB, "description": "No HTML here."}
        result = client.normalise(raw)
        assert result["description"] == "No HTML here."

    def test_normalise_empty_location_restrictions_gives_worldwide(self):
        client = self._client()
        raw = {**_RAW_JOB, "locationRestrictions": []}
        result = client.normalise(raw)
        assert result["location"] == "Worldwide"

    def test_normalise_null_location_restrictions_gives_worldwide(self):
        client = self._client()
        raw = {**_RAW_JOB, "locationRestrictions": None}
        result = client.normalise(raw)
        assert result["location"] == "Worldwide"

    def test_normalise_missing_location_restrictions_gives_worldwide(self):
        client = self._client()
        raw = {k: v for k, v in _RAW_JOB.items() if k != "locationRestrictions"}
        result = client.normalise(raw)
        assert result["location"] == "Worldwide"

    def test_normalise_single_location_restriction(self):
        client = self._client()
        raw = {**_RAW_JOB, "locationRestrictions": ["Europe"]}
        result = client.normalise(raw)
        assert result["location"] == "Europe"

    def test_normalise_multiple_location_restrictions_joined(self):
        client = self._client()
        raw = {**_RAW_JOB, "locationRestrictions": ["USA", "Canada", "UK"]}
        result = client.normalise(raw)
        assert result["location"] == "USA, Canada, UK"

    def test_normalise_created_at_unix_ms_int(self):
        client = self._client()
        raw = {**_RAW_JOB, "pubDate": 1_700_000_000_000}
        result = client.normalise(raw)
        assert result["created_at"] == "2023-11-14T22:13:20Z"

    def test_normalise_created_at_none(self):
        client = self._client()
        raw = {**_RAW_JOB, "pubDate": None}
        result = client.normalise(raw)
        assert result["created_at"] is None

    def test_normalise_created_at_missing_is_none(self):
        client = self._client()
        raw = {k: v for k, v in _RAW_JOB.items() if k != "pubDate"}
        result = client.normalise(raw)
        assert result["created_at"] is None

    def test_normalise_null_salary_fields_are_none(self):
        client = self._client()
        raw = {**_RAW_JOB, "minSalary": None, "maxSalary": None}
        result = client.normalise(raw)
        assert result["salary_min"] is None
        assert result["salary_max"] is None

    def test_normalise_missing_salary_fields_are_none(self):
        client = self._client()
        raw = {k: v for k, v in _RAW_JOB.items() if k not in ("minSalary", "maxSalary")}
        result = client.normalise(raw)
        assert result["salary_min"] is None
        assert result["salary_max"] is None

    def test_normalise_contract_type_always_none(self):
        """contract_type is always None — Himalayas does not surface this field."""
        client = self._client()
        result = client.normalise(_RAW_JOB)
        assert result["contract_type"] is None

    def test_normalise_unknown_job_type_lowercased(self):
        client = self._client()
        raw = {**_RAW_JOB, "employmentType": "TEMPORARY"}
        result = client.normalise(raw)
        assert result["contract_time"] == "temporary"

    def test_normalise_null_job_type_is_none(self):
        client = self._client()
        raw = {**_RAW_JOB, "employmentType": None}
        result = client.normalise(raw)
        assert result["contract_time"] is None

    def test_normalise_part_time_job_type(self):
        client = self._client()
        raw = {**_RAW_JOB, "employmentType": "PART_TIME"}
        result = client.normalise(raw)
        assert result["contract_time"] == "part_time"

    def test_normalise_empty_description(self):
        client = self._client()
        raw = {**_RAW_JOB, "description": ""}
        result = client.normalise(raw)
        assert result["description"] == ""

    def test_normalise_null_description(self):
        client = self._client()
        raw = {**_RAW_JOB, "description": None}
        result = client.normalise(raw)
        assert result["description"] == ""

    def test_normalise_canonical_keys_present(self):
        """normalise() output must contain all canonical schema keys."""
        expected_keys = {
            "source", "source_id", "title", "company", "location",
            "salary_min", "salary_max", "salary_period", "contract_type", "contract_time",
            "description", "redirect_url", "created_at",
        }
        client = self._client()
        result = client.normalise(_RAW_JOB)
        assert set(result.keys()) == expected_keys

    def test_normalise_salary_period_is_none(self):
        """salary_period is always None — Himalayas API does not expose pay period."""
        client = self._client()
        result = client.normalise(_RAW_JOB)
        assert result["salary_period"] is None


# ---------------------------------------------------------------------------
# HimalayasClient.fetch_page()
# ---------------------------------------------------------------------------

class TestHimalayasClientFetchPage:
    def _client(self, config: dict | None = None) -> HimalayasClient:
        return HimalayasClient(config=config or _BASE_CONFIG)

    def test_fetch_page_uses_correct_offset(self):
        """fetch_page(page) passes offset=(page-1)*limit to the API."""
        client = HimalayasClient(config=_HIMALAYAS_CONFIG_WITH_LIMIT)
        mock_resp = _mock_response(200, {"jobs": [], "total": 0})

        with patch("job_sources._plugin_himalayas.requests.get", return_value=mock_resp) as mock_get:
            client.fetch_page(3)

        call_kwargs = mock_get.call_args
        params = call_kwargs[1]["params"] if "params" in call_kwargs[1] else call_kwargs[0][1]
        assert params["offset"] == 20  # page=3, limit=10 → offset=20
        assert params["limit"] == 10

    def test_fetch_page_1_uses_offset_0(self):
        """fetch_page(1) sends offset=0."""
        client = HimalayasClient(config=_HIMALAYAS_CONFIG_WITH_LIMIT)
        mock_resp = _mock_response(200, {"jobs": [_RAW_JOB], "total": 1})

        with patch("job_sources._plugin_himalayas.requests.get", return_value=mock_resp) as mock_get:
            client.fetch_page(1)

        params = mock_get.call_args[1]["params"]
        assert params["offset"] == 0

    def test_fetch_page_success_returns_normalised_listings(self):
        """A 200 response returns normalised listing dicts."""
        client = self._client()
        mock_resp = _mock_response(200, {"jobs": [_RAW_JOB], "total": 1})

        with patch("job_sources._plugin_himalayas.requests.get", return_value=mock_resp):
            results = client.fetch_page(1)

        assert len(results) == 1
        assert results[0]["source"] == "himalayas"
        assert results[0]["source_id"] == "https://himalayas.app/companies/remote-co/jobs/abc123"

    def test_fetch_page_caches_total(self):
        """fetch_page() stores the total so total_pages() can use it."""
        client = self._client()
        mock_resp = _mock_response(200, {"jobs": [_RAW_JOB], "total": 250})

        with patch("job_sources._plugin_himalayas.requests.get", return_value=mock_resp):
            client.fetch_page(1)

        assert client._total == 250

    def test_fetch_page_empty_jobs_returns_empty_list(self):
        """A 200 response with no jobs returns an empty list."""
        client = self._client()
        mock_resp = _mock_response(200, {"jobs": [], "total": 0})

        with patch("job_sources._plugin_himalayas.requests.get", return_value=mock_resp):
            results = client.fetch_page(1)

        assert results == []

    def test_fetch_page_non_200_returns_empty_list(self):
        """A non-200 HTTP response returns an empty list."""
        client = self._client()
        mock_resp = _mock_response(500, {})

        with patch("job_sources._plugin_himalayas.requests.get", return_value=mock_resp):
            results = client.fetch_page(1)

        assert results == []

    def test_fetch_page_request_exception_returns_empty_list(self):
        """A network exception returns an empty list."""
        import requests as req_lib

        client = self._client()

        with patch(
            "job_sources._plugin_himalayas.requests.get",
            side_effect=req_lib.RequestException("timeout"),
        ):
            results = client.fetch_page(1)

        assert results == []

    def test_fetch_page_invalid_json_returns_empty_list(self):
        """A response that cannot be parsed as JSON returns an empty list."""
        client = self._client()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.side_effect = ValueError("not json")

        with patch("job_sources._plugin_himalayas.requests.get", return_value=mock_resp):
            results = client.fetch_page(1)

        assert results == []

    def test_fetch_page_multiple_jobs(self):
        """Multiple jobs in the response are all normalised."""
        client = self._client()
        raw2 = {
            **_RAW_JOB,
            "guid": "https://himalayas.app/companies/remote-co/jobs/xyz789",
            "title": "Junior Engineer",
        }
        mock_resp = _mock_response(200, {"jobs": [_RAW_JOB, raw2], "total": 2})

        with patch("job_sources._plugin_himalayas.requests.get", return_value=mock_resp):
            results = client.fetch_page(1)

        assert len(results) == 2
        assert {r["source_id"] for r in results} == {
            "https://himalayas.app/companies/remote-co/jobs/abc123",
            "https://himalayas.app/companies/remote-co/jobs/xyz789",
        }


# ---------------------------------------------------------------------------
# HimalayasClient.total_pages()
# ---------------------------------------------------------------------------

class TestHimalayasClientTotalPages:
    def _client(self, config: dict | None = None) -> HimalayasClient:
        return HimalayasClient(config=config or _BASE_CONFIG)

    def test_total_pages_uses_cached_total(self):
        """total_pages() does not make an HTTP call if total is already cached."""
        client = HimalayasClient(config=_HIMALAYAS_CONFIG_WITH_LIMIT)  # limit=10
        client._total = 25  # 25 / 10 = 3 (ceil)

        with patch("job_sources._plugin_himalayas.requests.get") as mock_get:
            pages = client.total_pages()

        mock_get.assert_not_called()
        assert pages == 3

    def test_total_pages_fetches_when_uncached(self):
        """total_pages() makes one HTTP request when total is not cached."""
        client = HimalayasClient(config=_HIMALAYAS_CONFIG_WITH_LIMIT)  # limit=10
        mock_resp = _mock_response(200, {"jobs": [], "total": 47})

        with patch("job_sources._plugin_himalayas.requests.get", return_value=mock_resp):
            pages = client.total_pages()

        assert pages == 5  # ceil(47 / 10)

    def test_total_pages_ceil_division(self):
        """total_pages() rounds up (ceil) when total is not evenly divisible."""
        client = HimalayasClient(config=_HIMALAYAS_CONFIG_WITH_LIMIT)  # limit=10
        client._total = 101  # 101 / 10 = 10.1 → ceil = 11

        assert client.total_pages() == 11

    def test_total_pages_exact_division(self):
        """total_pages() returns exact quotient when evenly divisible."""
        client = HimalayasClient(config=_HIMALAYAS_CONFIG_WITH_LIMIT)  # limit=10
        client._total = 100  # 100 / 10 = 10

        assert client.total_pages() == 10

    def test_total_pages_zero_total_returns_1(self):
        """total_pages() returns 1 when total is 0 to avoid divide-by-zero issues."""
        client = self._client()
        mock_resp = _mock_response(200, {"jobs": [], "total": 0})

        with patch("job_sources._plugin_himalayas.requests.get", return_value=mock_resp):
            pages = client.total_pages()

        assert pages == 1

    def test_total_pages_api_failure_returns_1(self):
        """total_pages() returns 1 when the API call fails."""
        import requests as req_lib

        client = self._client()

        with patch(
            "job_sources._plugin_himalayas.requests.get",
            side_effect=req_lib.RequestException("network error"),
        ):
            pages = client.total_pages()

        assert pages == 1

    def test_total_pages_default_limit_is_100(self):
        """With no config, limit defaults to 100."""
        client = HimalayasClient(config={})
        client._total = 250  # 250 / 100 = 2.5 → ceil = 3

        assert client.total_pages() == 3


# ---------------------------------------------------------------------------
# Config parsing
# ---------------------------------------------------------------------------

class TestHimalayasClientConfig:
    def test_custom_limit_from_config(self):
        """limit is read from config['himalayas']['limit']."""
        client = HimalayasClient(config={"himalayas": {"limit": 50}})
        assert client._limit == 50

    def test_default_limit_when_no_himalayas_key(self):
        """limit defaults to 100 when config has no 'himalayas' key."""
        client = HimalayasClient(config={})
        assert client._limit == 100

    def test_default_limit_when_himalayas_key_empty(self):
        """limit defaults to 100 when config['himalayas'] has no 'limit' key."""
        client = HimalayasClient(config={"himalayas": {}})
        assert client._limit == 100


# ---------------------------------------------------------------------------
# SOURCES registry
# ---------------------------------------------------------------------------

class TestSourcesRegistry:
    def test_himalayas_registered(self):
        """SOURCES contains 'himalayas' mapped to HimalayasClient."""
        assert "himalayas" in SOURCES
        assert SOURCES["himalayas"] is HimalayasClient

    def test_himalayas_is_job_source_subclass(self):
        """HimalayasClient is a subclass of JobSource."""
        from job_sources import JobSource
        assert issubclass(HimalayasClient, JobSource)


# ---------------------------------------------------------------------------
# make_source() factory
# ---------------------------------------------------------------------------

class TestMakeSourceHimalayas:
    def test_returns_himalayas_client_when_configured(self):
        """make_source() returns HimalayasClient when job_source='himalayas'."""
        config = {"job_source": "himalayas"}
        source = make_source(config)
        assert isinstance(source, HimalayasClient)

    def test_himalayas_client_from_factory_is_job_source(self):
        """make_source() result is a JobSource instance."""
        from job_sources import JobSource
        config = {"job_source": "himalayas"}
        source = make_source(config)
        assert isinstance(source, JobSource)


# ---------------------------------------------------------------------------
# Issue #175 / #289: empty/missing redirect_url — listing must be skipped
# ---------------------------------------------------------------------------

class TestHimalayasMissingRedirectUrl:
    """fetch_page() must skip listings with no usable URL instead of yielding them.

    As of the API schema change tracked in issue #289, redirect_url is mapped
    solely from ``applicationLink``; the old ``applicationUrl`` field and slug
    fallback no longer exist.
    """

    def _client(self) -> HimalayasClient:
        return HimalayasClient(config=_BASE_CONFIG)

    # -- regression test for issue #289 ------------------------------------

    def test_normalise_uses_application_link(self):
        """Regression (#289): normalise() reads redirect_url from applicationLink."""
        client = self._client()
        result = client.normalise(_RAW_JOB)
        assert result["redirect_url"] == "https://himalayas.app/jobs/abc123/apply"

    def test_normalise_redirect_url_empty_when_application_link_missing(self):
        """normalise() yields empty redirect_url when applicationLink is absent."""
        client = self._client()
        raw = {k: v for k, v in _RAW_JOB.items() if k != "applicationLink"}
        result = client.normalise(raw)
        assert result["redirect_url"] == ""

    def test_normalise_redirect_url_empty_when_application_link_null(self):
        """normalise() yields empty redirect_url when applicationLink is null."""
        client = self._client()
        raw = {**_RAW_JOB, "applicationLink": None}
        result = client.normalise(raw)
        assert result["redirect_url"] == ""

    def test_normalise_redirect_url_empty_when_application_link_empty_string(self):
        """normalise() yields empty redirect_url when applicationLink is an empty string."""
        client = self._client()
        raw = {**_RAW_JOB, "applicationLink": ""}
        result = client.normalise(raw)
        assert result["redirect_url"] == ""

    def test_fetch_page_skips_listing_with_empty_redirect_url(self):
        """fetch_page() does not yield a listing whose applicationLink is missing."""
        client = self._client()
        raw_no_url = {k: v for k, v in _RAW_JOB.items() if k != "applicationLink"}
        mock_resp = _mock_response(200, {"jobs": [raw_no_url], "total": 1})

        with patch("job_sources._plugin_himalayas.requests.get", return_value=mock_resp):
            results = client.fetch_page(1)

        assert results == []

    def test_fetch_page_skips_listing_with_null_redirect_url(self):
        """fetch_page() does not yield a listing whose applicationLink is null."""
        client = self._client()
        raw_null_url = {**_RAW_JOB, "applicationLink": None}
        mock_resp = _mock_response(200, {"jobs": [raw_null_url], "total": 1})

        with patch("job_sources._plugin_himalayas.requests.get", return_value=mock_resp):
            results = client.fetch_page(1)

        assert results == []

    def test_fetch_page_keeps_valid_listings_alongside_skipped_ones(self):
        """fetch_page() yields only the listings that have a usable URL."""
        client = self._client()
        raw_no_url = {k: v for k, v in _RAW_JOB.items() if k != "applicationLink"}
        raw_no_url["guid"] = "https://himalayas.app/companies/remote-co/jobs/no-url-id"
        valid_raw = {
            **_RAW_JOB,
            "guid": "https://himalayas.app/companies/remote-co/jobs/valid-id",
        }
        mock_resp = _mock_response(200, {"jobs": [raw_no_url, valid_raw], "total": 2})

        with patch("job_sources._plugin_himalayas.requests.get", return_value=mock_resp):
            results = client.fetch_page(1)

        assert len(results) == 1
        assert results[0]["source_id"] == "https://himalayas.app/companies/remote-co/jobs/valid-id"

    def test_fetch_page_skipped_listing_emits_warning(self, caplog):
        """fetch_page() logs a warning when it skips a listing with no URL."""
        import logging

        client = self._client()
        raw_no_url = {**_RAW_JOB, "applicationLink": None, "title": "Skipped Job"}
        mock_resp = _mock_response(200, {"jobs": [raw_no_url], "total": 1})

        with patch("job_sources._plugin_himalayas.requests.get", return_value=mock_resp):
            with caplog.at_level(logging.WARNING, logger="ingest.himalayas"):
                client.fetch_page(1)

        assert any("Skipped Job" in record.message for record in caplog.records)
