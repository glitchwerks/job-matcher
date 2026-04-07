"""
tests/test_job_sources_jsearch.py — Unit tests for JSearchClient.

Covers:
  - Module-level helpers: _normalise_contract_time, _normalise_salary_period,
    _map_date_posted
  - JSearchClient constructor: credential resolution, ValueError on missing key
  - normalise(): all canonical fields, location assembly, salary passthrough,
    redirect_url fallback, skip_scrape=True, minimal dict no-raise
  - fetch_page(): 200 success, empty data, status != OK, non-200, 429 retry ×4,
    network exception, bad JSON, query construction, headers, date_posted
    inclusion/omission, num_pages=1, timeout=20
  - total_pages(): returns max_pages; no HTTP call made
  - pages(): yields 2 pages; stops early on empty page
  - SOURCES registry: "jsearch" registered as JSearchClient (JobSource subclass)
  - settings_schema(): display_name present; 1 field: api_key, password, required
"""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

import requests as _req

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from job_sources import SOURCES, JobSource
from job_sources._plugin_jsearch import (
    _CONTRACT_TIME_MAP,
    _SALARY_PERIOD_MAP,
    _map_date_posted,
    _normalise_contract_time,
    _normalise_salary_period,
)

JSearchClient = SOURCES["jsearch"]


# ---------------------------------------------------------------------------
# Helper factories
# ---------------------------------------------------------------------------

def _config(max_pages: int = 3, what: str = "python developer", where: str = "Miami, FL",
            max_days_old: int = 0, **kwargs) -> dict:
    """Return a minimal config dict with a search sub-dict."""
    search: dict = {
        "what": what,
        "where": where,
        "max_pages": max_pages,
        "max_days_old": max_days_old,
        **kwargs,
    }
    return {"search": search}


def _client(api_key: str = "test-rapidapi-key", **config_kwargs) -> JSearchClient:
    """Return a JSearchClient using credentials dict (the normal path)."""
    return JSearchClient(
        config=_config(**config_kwargs),
        credentials={"api_key": api_key},
    )


def _mock_response(status_code: int, json_data: dict) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data
    return resp


def _jsearch_envelope(jobs: list) -> dict:
    """Wrap a jobs list in a well-formed JSearch success envelope."""
    return {"status": "OK", "request_id": "test-req", "data": jobs}


# A complete representative JSearch data[] entry.
_RAW_JOB: dict = {
    "job_id": "abc123XYZ",
    "job_title": "Senior Python Engineer",
    "employer_name": "Acme Corp",
    "job_city": "Miami",
    "job_state": "FL",
    "job_country": "US",
    "job_location": "Miami, FL, US",
    "job_is_remote": False,
    "job_employment_type": "FULLTIME",
    "job_min_salary": 90000.0,
    "job_max_salary": 130000.0,
    "job_salary_period": "YEAR",
    "job_posted_at_datetime_utc": "2026-03-10T08:00:00.000Z",
    "job_apply_link": "https://apply.example.com/job/123",
    "job_google_link": "https://www.google.com/search?ibp=htl;jobs&q=python",
    "job_description": "We are looking for a Python engineer with Django experience.",
}


# ---------------------------------------------------------------------------
# _normalise_contract_time()
# ---------------------------------------------------------------------------

class TestNormaliseContractTime:
    def test_fulltime_maps_to_full_time(self):
        assert _normalise_contract_time("FULLTIME") == "full_time"

    def test_parttime_maps_to_part_time(self):
        assert _normalise_contract_time("PARTTIME") == "part_time"

    def test_contractor_maps_to_contract(self):
        assert _normalise_contract_time("CONTRACTOR") == "contract"

    def test_intern_maps_to_intern(self):
        assert _normalise_contract_time("INTERN") == "intern"

    def test_case_insensitive_fulltime(self):
        """Lowercase input still resolves via upper() lookup."""
        assert _normalise_contract_time("fulltime") == "full_time"

    def test_case_insensitive_mixed(self):
        assert _normalise_contract_time("FullTime") == "full_time"

    def test_unknown_value_lowercased_passthrough(self):
        """Unknown values are lowercased and passed through."""
        assert _normalise_contract_time("SEASONAL") == "seasonal"

    def test_unknown_value_preserves_content(self):
        assert _normalise_contract_time("GIG_WORK") == "gig_work"

    def test_none_returns_none(self):
        assert _normalise_contract_time(None) is None

    def test_empty_string_returns_none(self):
        assert _normalise_contract_time("") is None

    def test_map_has_all_four_entries(self):
        """The CONTRACT_TIME_MAP covers all documented JSearch employment types."""
        assert set(_CONTRACT_TIME_MAP.keys()) == {"FULLTIME", "PARTTIME", "CONTRACTOR", "INTERN"}

    # ------------------------------------------------------------------
    # Hyphenated / spaced inputs (regression for #81)
    # ------------------------------------------------------------------

    def test_hyphenated_full_time_lowercase(self):
        """'full-time' (JSearch API value) normalises to 'full_time'."""
        assert _normalise_contract_time("full-time") == "full_time"

    def test_hyphenated_full_time_titlecase(self):
        """'Full-Time' normalises to 'full_time'."""
        assert _normalise_contract_time("Full-Time") == "full_time"

    def test_hyphenated_full_time_uppercase(self):
        """'FULL-TIME' normalises to 'full_time'."""
        assert _normalise_contract_time("FULL-TIME") == "full_time"

    def test_hyphenated_part_time(self):
        """'part-time' normalises to 'part_time'."""
        assert _normalise_contract_time("part-time") == "part_time"

    # ------------------------------------------------------------------
    # Regression: pre-existing canonical inputs still work
    # ------------------------------------------------------------------

    def test_canonical_fulltime_regression(self):
        """'FULLTIME' (no hyphen) still maps to 'full_time'."""
        assert _normalise_contract_time("FULLTIME") == "full_time"

    def test_canonical_parttime_regression(self):
        """'PARTTIME' (no hyphen) still maps to 'part_time'."""
        assert _normalise_contract_time("PARTTIME") == "part_time"

    def test_unknown_value_fallback_unchanged(self):
        """Unknown value 'temporary' is lowercased and passed through."""
        assert _normalise_contract_time("temporary") == "temporary"

    # ------------------------------------------------------------------
    # Space-separated inputs (regression for #83 review feedback)
    # ------------------------------------------------------------------

    def test_space_separated_full_time_uppercase(self):
        """'FULL TIME' (space instead of hyphen) normalises to 'full_time'."""
        assert _normalise_contract_time("FULL TIME") == "full_time"

    def test_space_separated_part_time_titlecase(self):
        """'Part Time' (titlecase with space) normalises to 'part_time'."""
        assert _normalise_contract_time("Part Time") == "part_time"

    # ------------------------------------------------------------------
    # Regression: map entries for CONTRACTOR and INTERN
    # ------------------------------------------------------------------

    def test_contractor_regression(self):
        """'CONTRACTOR' maps to 'contract' via _CONTRACT_TIME_MAP."""
        assert _normalise_contract_time("CONTRACTOR") == "contract"

    def test_intern_regression(self):
        """'INTERN' maps to 'intern' via _CONTRACT_TIME_MAP."""
        assert _normalise_contract_time("INTERN") == "intern"


# ---------------------------------------------------------------------------
# _normalise_salary_period()
# ---------------------------------------------------------------------------

class TestNormaliseSalaryPeriod:
    def test_year_maps_to_annual(self):
        assert _normalise_salary_period("YEAR") == "annual"

    def test_day_maps_to_daily(self):
        assert _normalise_salary_period("DAY") == "daily"

    def test_hour_maps_to_hourly(self):
        assert _normalise_salary_period("HOUR") == "hourly"

    def test_month_passes_through(self):
        assert _normalise_salary_period("MONTH") == "month"

    def test_week_passes_through(self):
        assert _normalise_salary_period("WEEK") == "week"

    def test_case_insensitive(self):
        assert _normalise_salary_period("year") == "annual"

    def test_unknown_returns_none(self):
        assert _normalise_salary_period("BIWEEKLY") is None

    def test_none_returns_none(self):
        assert _normalise_salary_period(None) is None

    def test_empty_string_returns_none(self):
        assert _normalise_salary_period("") is None

    def test_map_has_five_entries(self):
        assert set(_SALARY_PERIOD_MAP.keys()) == {"YEAR", "DAY", "HOUR", "MONTH", "WEEK"}


# ---------------------------------------------------------------------------
# _map_date_posted()
# ---------------------------------------------------------------------------

class TestMapDatePosted:
    def test_zero_returns_none(self):
        """0 means no filter — omit the parameter."""
        assert _map_date_posted(0) is None

    def test_one_returns_today(self):
        assert _map_date_posted(1) == "today"

    def test_two_returns_3days(self):
        assert _map_date_posted(2) == "3days"

    def test_three_returns_3days(self):
        assert _map_date_posted(3) == "3days"

    def test_four_returns_week(self):
        assert _map_date_posted(4) == "week"

    def test_seven_returns_week(self):
        assert _map_date_posted(7) == "week"

    def test_eight_returns_month(self):
        assert _map_date_posted(8) == "month"

    def test_thirty_returns_month(self):
        assert _map_date_posted(30) == "month"


# ---------------------------------------------------------------------------
# JSearchClient constructor
# ---------------------------------------------------------------------------

class TestJSearchClientConstructor:
    def test_raises_when_api_key_absent_from_both(self):
        """ValueError raised when api_key is absent from credentials and config."""
        try:
            JSearchClient(config=_config(), credentials={})
            assert False, "Expected ValueError"
        except ValueError as exc:
            assert "api_key" in str(exc).lower()

    def test_raises_when_api_key_empty_string(self):
        """ValueError raised when api_key resolves to an empty string."""
        try:
            JSearchClient(config=_config(), credentials={"api_key": ""})
            assert False, "Expected ValueError"
        except ValueError as exc:
            assert "api_key" in str(exc).lower()

    def test_succeeds_with_credentials_api_key(self):
        """Constructor succeeds when api_key provided via credentials."""
        client = JSearchClient(config=_config(), credentials={"api_key": "my-key"})
        assert client is not None
        assert client._api_key == "my-key"

    def test_legacy_fallback_to_config_jsearch(self):
        """api_key from config['jsearch'] is accepted when credentials is absent."""
        config = {**_config(), "jsearch": {"api_key": "legacy-key"}}
        client = JSearchClient(config=config, credentials=None)
        assert client._api_key == "legacy-key"

    def test_credentials_takes_precedence_over_config_jsearch(self):
        """credentials api_key wins over config['jsearch']['api_key']."""
        config = {**_config(), "jsearch": {"api_key": "legacy-key"}}
        client = JSearchClient(config=config, credentials={"api_key": "creds-key"})
        assert client._api_key == "creds-key"

    def test_empty_credentials_falls_back_to_config_jsearch(self):
        """credentials={"api_key": ""} falls back to config['jsearch']."""
        config = {**_config(), "jsearch": {"api_key": "legacy-key"}}
        client = JSearchClient(config=config, credentials={"api_key": ""})
        assert client._api_key == "legacy-key"


# ---------------------------------------------------------------------------
# JSearchClient.normalise()
# ---------------------------------------------------------------------------

class TestJSearchNormalise:
    def test_all_canonical_keys_present(self):
        """normalise() output contains all required canonical schema keys."""
        required_keys = {
            "source", "source_id", "title", "company", "location",
            "salary_min", "salary_max", "salary_period", "contract_type",
            "contract_time", "description", "redirect_url", "created_at",
        }
        client = _client()
        result = client.normalise(_RAW_JOB)
        assert required_keys.issubset(result.keys())

    def test_source_is_jsearch(self):
        assert _client().normalise(_RAW_JOB)["source"] == "jsearch"

    def test_source_id_is_string(self):
        result = _client().normalise(_RAW_JOB)
        assert isinstance(result["source_id"], str)
        assert result["source_id"] == "abc123XYZ"

    def test_title_mapped_correctly(self):
        assert _client().normalise(_RAW_JOB)["title"] == "Senior Python Engineer"

    def test_company_mapped_correctly(self):
        assert _client().normalise(_RAW_JOB)["company"] == "Acme Corp"

    def test_location_assembled_from_city_state_country(self):
        """Location is assembled from job_city, job_state, job_country."""
        result = _client().normalise(_RAW_JOB)
        assert result["location"] == "Miami, FL, US"

    def test_location_fallback_to_job_location(self):
        """When structured parts are absent, location falls back to job_location."""
        raw = {
            **_RAW_JOB,
            "job_city": "",
            "job_state": "",
            "job_country": "",
            "job_location": "Remote, Worldwide",
        }
        result = _client().normalise(raw)
        assert result["location"] == "Remote, Worldwide"

    def test_location_empty_when_all_absent(self):
        """Location is empty string when all location fields are absent."""
        raw = {"job_id": "1"}
        result = _client().normalise(raw)
        assert result["location"] == ""

    def test_location_partial_city_and_country(self):
        """Missing state still assembles correctly without double commas."""
        raw = {**_RAW_JOB, "job_state": ""}
        result = _client().normalise(raw)
        assert result["location"] == "Miami, US"

    def test_salary_min_passthrough(self):
        assert _client().normalise(_RAW_JOB)["salary_min"] == 90000.0

    def test_salary_max_passthrough(self):
        assert _client().normalise(_RAW_JOB)["salary_max"] == 130000.0

    def test_salary_none_when_absent(self):
        raw = {"job_id": "1"}
        result = _client().normalise(raw)
        assert result["salary_min"] is None
        assert result["salary_max"] is None

    def test_salary_period_year_maps_to_annual(self):
        assert _client().normalise(_RAW_JOB)["salary_period"] == "annual"

    def test_salary_period_none_when_absent(self):
        raw = {"job_id": "1"}
        assert _client().normalise(raw)["salary_period"] is None

    def test_contract_type_always_none(self):
        """JSearch does not expose permanent/contract distinction."""
        assert _client().normalise(_RAW_JOB)["contract_type"] is None

    def test_contract_time_fulltime_maps_correctly(self):
        assert _client().normalise(_RAW_JOB)["contract_time"] == "full_time"

    def test_contract_time_none_when_absent(self):
        raw = {"job_id": "1"}
        assert _client().normalise(raw)["contract_time"] is None

    def test_description_is_job_description(self):
        result = _client().normalise(_RAW_JOB)
        assert result["description"] == "We are looking for a Python engineer with Django experience."

    def test_redirect_url_uses_apply_link(self):
        """redirect_url uses job_apply_link when present."""
        assert _client().normalise(_RAW_JOB)["redirect_url"] == "https://apply.example.com/job/123"

    def test_redirect_url_falls_back_to_google_link(self):
        """redirect_url falls back to job_google_link when apply_link absent."""
        raw = {**_RAW_JOB, "job_apply_link": None}
        result = _client().normalise(raw)
        assert result["redirect_url"] == "https://www.google.com/search?ibp=htl;jobs&q=python"

    def test_redirect_url_empty_when_both_absent(self):
        raw = {"job_id": "1"}
        assert _client().normalise(raw)["redirect_url"] == ""

    def test_redirect_url_empty_string_apply_link_falls_back(self):
        """Empty string job_apply_link is falsy; falls back to google_link."""
        raw = {**_RAW_JOB, "job_apply_link": ""}
        result = _client().normalise(raw)
        assert result["redirect_url"] == "https://www.google.com/search?ibp=htl;jobs&q=python"

    def test_created_at_passthrough(self):
        result = _client().normalise(_RAW_JOB)
        assert result["created_at"] == "2026-03-10T08:00:00.000Z"

    def test_skip_scrape_is_true(self):
        """skip_scrape is always True — full description is in the API response."""
        assert _client().normalise(_RAW_JOB)["skip_scrape"] is True

    def test_minimal_raw_dict_does_not_raise(self):
        """normalise() handles a minimal dict without raising."""
        result = _client().normalise({"job_id": "1"})
        assert result["source"] == "jsearch"
        assert result["source_id"] == "1"
        assert result["title"] == ""
        assert result["description"] == ""


# ---------------------------------------------------------------------------
# JSearchClient.fetch_page()
# ---------------------------------------------------------------------------

class TestJSearchClientFetchPage:
    def test_200_success_returns_normalised_list(self):
        """A 200 response with data returns a list of normalised dicts."""
        client = _client()
        mock_resp = _mock_response(200, _jsearch_envelope([_RAW_JOB]))

        with patch("job_sources._plugin_jsearch.requests.get", return_value=mock_resp):
            results = client.fetch_page(1)

        assert len(results) == 1
        assert results[0]["source"] == "jsearch"
        assert results[0]["title"] == "Senior Python Engineer"

    def test_200_empty_data_returns_empty_list(self):
        """A 200 response with an empty data array returns []."""
        client = _client()
        mock_resp = _mock_response(200, _jsearch_envelope([]))

        with patch("job_sources._plugin_jsearch.requests.get", return_value=mock_resp):
            assert client.fetch_page(1) == []

    def test_200_status_not_ok_returns_empty_list(self):
        """A 200 response with status != 'OK' returns [] (RapidAPI error envelope)."""
        client = _client()
        mock_resp = _mock_response(200, {"status": "ERROR", "message": "quota exceeded"})

        with patch("job_sources._plugin_jsearch.requests.get", return_value=mock_resp):
            assert client.fetch_page(1) == []

    def test_200_missing_data_key_returns_empty_list(self):
        """A 200 response with status=OK but no 'data' key returns []."""
        client = _client()
        mock_resp = _mock_response(200, {"status": "OK", "request_id": "x"})

        with patch("job_sources._plugin_jsearch.requests.get", return_value=mock_resp):
            assert client.fetch_page(1) == []

    def test_non_200_non_429_returns_empty_list(self):
        """A 500 response returns []."""
        client = _client()
        mock_resp = _mock_response(500, {})

        with patch("job_sources._plugin_jsearch.requests.get", return_value=mock_resp):
            assert client.fetch_page(1) == []

    def test_429_exhausted_returns_empty_list(self):
        """After 4 total attempts of 429 responses, returns [] without raising."""
        client = _client()
        mock_resp = _mock_response(429, {})

        with patch("job_sources._plugin_jsearch.requests.get", return_value=mock_resp) as mock_get, \
             patch("job_sources._plugin_jsearch.time.sleep"):
            result = client.fetch_page(1)

        assert result == []
        assert mock_get.call_count == 4  # 1 initial + 3 retries

    def test_429_exhausted_sleep_called_with_backoff_delays(self):
        """time.sleep is called with the correct backoff delays on 429."""
        client = _client()
        mock_resp = _mock_response(429, {})

        with patch("job_sources._plugin_jsearch.requests.get", return_value=mock_resp), \
             patch("job_sources._plugin_jsearch.time.sleep") as mock_sleep:
            client.fetch_page(1)

        sleep_calls = [c.args[0] for c in mock_sleep.call_args_list]
        assert sleep_calls == [2, 4, 8]

    def test_network_exception_returns_empty_list(self):
        """A requests.RequestException returns []."""
        client = _client()

        with patch(
            "job_sources._plugin_jsearch.requests.get",
            side_effect=_req.RequestException("connection refused"),
        ):
            assert client.fetch_page(1) == []

    def test_bad_json_returns_empty_list(self):
        """A non-JSON response body returns []."""
        client = _client()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.side_effect = ValueError("not json")

        with patch("job_sources._plugin_jsearch.requests.get", return_value=mock_resp):
            assert client.fetch_page(1) == []

    def test_query_includes_where_when_set(self):
        """Query param is 'what in where' when where is non-empty."""
        client = _client(what="python developer", where="Miami, FL")
        mock_resp = _mock_response(200, _jsearch_envelope([]))

        with patch("job_sources._plugin_jsearch.requests.get", return_value=mock_resp) as mock_get:
            client.fetch_page(1)

        params = mock_get.call_args.kwargs["params"]
        assert params["query"] == "python developer in Miami, FL"

    def test_query_omits_where_when_empty(self):
        """Query param is just 'what' when where is empty."""
        client = _client(what="python developer", where="")
        mock_resp = _mock_response(200, _jsearch_envelope([]))

        with patch("job_sources._plugin_jsearch.requests.get", return_value=mock_resp) as mock_get:
            client.fetch_page(1)

        params = mock_get.call_args.kwargs["params"]
        assert params["query"] == "python developer"

    def test_correct_api_key_header(self):
        """X-RapidAPI-Key header contains the configured api_key."""
        client = _client(api_key="my-secret-key")
        mock_resp = _mock_response(200, _jsearch_envelope([]))

        with patch("job_sources._plugin_jsearch.requests.get", return_value=mock_resp) as mock_get:
            client.fetch_page(1)

        headers = mock_get.call_args.kwargs["headers"]
        assert headers["X-RapidAPI-Key"] == "my-secret-key"

    def test_correct_rapid_api_host_header(self):
        """X-RapidAPI-Host header is always 'jsearch.p.rapidapi.com'."""
        client = _client()
        mock_resp = _mock_response(200, _jsearch_envelope([]))

        with patch("job_sources._plugin_jsearch.requests.get", return_value=mock_resp) as mock_get:
            client.fetch_page(1)

        headers = mock_get.call_args.kwargs["headers"]
        assert headers["X-RapidAPI-Host"] == "jsearch.p.rapidapi.com"

    def test_date_posted_included_when_max_days_old_7(self):
        """date_posted='week' is added when max_days_old=7."""
        client = _client(max_days_old=7)
        mock_resp = _mock_response(200, _jsearch_envelope([]))

        with patch("job_sources._plugin_jsearch.requests.get", return_value=mock_resp) as mock_get:
            client.fetch_page(1)

        params = mock_get.call_args.kwargs["params"]
        assert params.get("date_posted") == "week"

    def test_date_posted_absent_when_max_days_old_0(self):
        """date_posted param is omitted when max_days_old=0."""
        client = _client(max_days_old=0)
        mock_resp = _mock_response(200, _jsearch_envelope([]))

        with patch("job_sources._plugin_jsearch.requests.get", return_value=mock_resp) as mock_get:
            client.fetch_page(1)

        params = mock_get.call_args.kwargs["params"]
        assert "date_posted" not in params

    def test_num_pages_always_1(self):
        """num_pages=1 is always included in params."""
        client = _client()
        mock_resp = _mock_response(200, _jsearch_envelope([]))

        with patch("job_sources._plugin_jsearch.requests.get", return_value=mock_resp) as mock_get:
            client.fetch_page(1)

        params = mock_get.call_args.kwargs["params"]
        assert params["num_pages"] == 1

    def test_timeout_is_20_seconds(self):
        """requests.get is called with timeout=20."""
        client = _client()
        mock_resp = _mock_response(200, _jsearch_envelope([]))

        with patch("job_sources._plugin_jsearch.requests.get", return_value=mock_resp) as mock_get:
            client.fetch_page(1)

        assert mock_get.call_args.kwargs["timeout"] == 20

    def test_page_number_in_params(self):
        """The page number is passed as a query param."""
        client = _client()
        mock_resp = _mock_response(200, _jsearch_envelope([]))

        with patch("job_sources._plugin_jsearch.requests.get", return_value=mock_resp) as mock_get:
            client.fetch_page(3)

        params = mock_get.call_args.kwargs["params"]
        assert params["page"] == 3


# ---------------------------------------------------------------------------
# JSearchClient.total_pages()
# ---------------------------------------------------------------------------

class TestJSearchClientTotalPages:
    def test_returns_max_pages_from_config(self):
        """total_pages() returns search.max_pages from config."""
        client = _client(max_pages=3)
        assert client.total_pages() == 3

    def test_returns_different_max_pages(self):
        """total_pages() reflects whatever max_pages is configured."""
        client = _client(max_pages=5)
        assert client.total_pages() == 5

    def test_no_http_call_made(self):
        """total_pages() makes no HTTP requests."""
        client = _client(max_pages=3)

        with patch("job_sources._plugin_jsearch.requests.get") as mock_get:
            client.total_pages()

        assert mock_get.call_count == 0


# ---------------------------------------------------------------------------
# JSearchClient.pages()
# ---------------------------------------------------------------------------

class TestJSearchClientPages:
    def test_yields_two_pages_of_results(self):
        """pages() yields one list per page for a 2-page scenario."""
        client = _client(max_pages=2)

        page1_resp = _mock_response(200, _jsearch_envelope([_RAW_JOB]))
        page2_resp = _mock_response(200, _jsearch_envelope([
            {**_RAW_JOB, "job_id": "xyz999", "job_title": "DevOps Engineer"},
        ]))

        with patch(
            "job_sources._plugin_jsearch.requests.get",
            side_effect=[page1_resp, page2_resp],
        ):
            pages = list(client.pages())

        assert len(pages) == 2
        assert pages[0][0]["source_id"] == "abc123XYZ"
        assert pages[1][0]["source_id"] == "xyz999"
        assert pages[1][0]["title"] == "DevOps Engineer"

    def test_stops_early_when_page_returns_empty(self):
        """pages() stops iteration when a page returns no results."""
        client = _client(max_pages=3)

        page1_resp = _mock_response(200, _jsearch_envelope([_RAW_JOB]))
        empty_resp = _mock_response(200, _jsearch_envelope([]))

        with patch(
            "job_sources._plugin_jsearch.requests.get",
            side_effect=[page1_resp, empty_resp],
        ):
            pages = list(client.pages())

        assert len(pages) == 1
        assert pages[0][0]["title"] == "Senior Python Engineer"


# ---------------------------------------------------------------------------
# SOURCES registry
# ---------------------------------------------------------------------------

class TestSourcesRegistry:
    def test_jsearch_in_sources(self):
        """SOURCES contains the 'jsearch' key."""
        assert "jsearch" in SOURCES

    def test_sources_jsearch_is_jsearch_client(self):
        """SOURCES['jsearch'] resolves to JSearchClient."""
        assert SOURCES["jsearch"] is JSearchClient

    def test_jsearch_client_is_job_source_subclass(self):
        """JSearchClient is a subclass of JobSource."""
        assert issubclass(JSearchClient, JobSource)


# ---------------------------------------------------------------------------
# settings_schema()
# ---------------------------------------------------------------------------

class TestJSearchSettingsSchema:
    def test_has_display_name_string(self):
        """settings_schema() returns a non-empty display_name string."""
        schema = JSearchClient.settings_schema()
        assert isinstance(schema["display_name"], str)
        assert schema["display_name"]

    def test_fields_has_exactly_one_entry(self):
        """settings_schema() fields list has exactly one entry."""
        schema = JSearchClient.settings_schema()
        assert len(schema["fields"]) == 1

    def test_api_key_field_name(self):
        """The single field has name='api_key'."""
        field = JSearchClient.settings_schema()["fields"][0]
        assert field["name"] == "api_key"

    def test_api_key_field_type_is_password(self):
        """The api_key field type is 'password'."""
        field = JSearchClient.settings_schema()["fields"][0]
        assert field["type"] == "password"

    def test_api_key_field_is_required(self):
        """The api_key field is marked required=True."""
        field = JSearchClient.settings_schema()["fields"][0]
        assert field["required"] is True
