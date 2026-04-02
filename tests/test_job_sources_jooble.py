"""
tests/test_job_sources_jooble.py — Unit tests for JoobleClient.

Covers:
  - normalise(): canonical field mapping, HTML stripping from snippet,
    salary parsing from free-text, contract_time normalisation, source_id
    always a string
  - fetch_page(): HTTP 200 success, non-200 error, network exception, bad JSON,
    empty jobs list
  - total_pages(): correct page count from totalCount, capped at max_pages,
    fallback to 1 on error or zero count
  - pages(): iterates pages, stops early on empty result
  - Constructor: raises ValueError when api_key absent
  - SOURCES registry: "jooble" key maps to JoobleClient
  - settings_schema(): returns required api_key field
"""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

import requests as _req

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from job_sources import SOURCES, JobSource
from job_sources.jooble import (
    JoobleClient,
    _CONTRACT_TIME_MAP,
    _normalise_contract_time,
)
from job_sources.utils import parse_salary as _parse_salary, strip_html as _strip_html


# ---------------------------------------------------------------------------
# Helper factories
# ---------------------------------------------------------------------------

def _config(api_key: str = "test-key", **kwargs) -> dict:
    """Return a config dict with a jooble section."""
    jooble_cfg: dict = {"api_key": api_key, **kwargs}
    return {"jooble": jooble_cfg}


def _client(**kwargs) -> JoobleClient:
    """Return a JoobleClient with a test API key and optional overrides."""
    return JoobleClient(config=_config(**kwargs))


def _mock_response(status_code: int, json_data: dict) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data
    resp.raise_for_status = MagicMock()
    if status_code >= 400:
        resp.raise_for_status.side_effect = _req.HTTPError(response=resp)
    return resp


# A complete raw Jooble listing matching the documented API schema.
_RAW_JOB: dict = {
    "title": "Senior Python Developer",
    "location": "New York, NY",
    "snippet": "<b>Looking for</b> a <em>Python</em> engineer with Django experience.",
    "salary": "$90,000 - $130,000",
    "source": "LinkedIn",
    "type": "Full-time",
    "link": "https://jooble.org/desc/987654321",
    "updated": "2026-03-10T08:00:00",
    "id": "987654321",
}


# ---------------------------------------------------------------------------
# _parse_salary()
# ---------------------------------------------------------------------------

class TestParseSalary:
    def test_range_with_dollar_signs(self):
        """Parses '$90,000 - $130,000' into (90000.0, 130000.0)."""
        assert _parse_salary("$90,000 - $130,000") == (90000.0, 130000.0)

    def test_range_with_k_suffix(self):
        """Parses '90k - 130k' into (90000.0, 130000.0)."""
        assert _parse_salary("90k - 130k") == (90000.0, 130000.0)

    def test_single_value(self):
        """Parses '80000' into (80000.0, 80000.0) — same min and max."""
        assert _parse_salary("80000") == (80000.0, 80000.0)

    def test_empty_string_returns_none_none(self):
        """Empty string returns (None, None)."""
        assert _parse_salary("") == (None, None)

    def test_whitespace_only_returns_none_none(self):
        """Whitespace-only string returns (None, None)."""
        assert _parse_salary("   ") == (None, None)

    def test_no_numbers_returns_none_none(self):
        """A string with no numeric content returns (None, None)."""
        assert _parse_salary("competitive") == (None, None)

    def test_uppercase_k_suffix(self):
        """Parses '100K-150K' into (100000.0, 150000.0)."""
        assert _parse_salary("100K-150K") == (100000.0, 150000.0)


# ---------------------------------------------------------------------------
# _strip_html()
# ---------------------------------------------------------------------------

class TestStripHtml:
    def test_strips_bold_and_em_tags(self):
        """Removes <b> and <em> tags, returning plain text."""
        result = _strip_html("<b>Looking for</b> a <em>Python</em> engineer.")
        assert "<b>" not in result
        assert "<em>" not in result
        assert "Python" in result
        assert "engineer" in result

    def test_plain_text_unchanged(self):
        """Plain text with no HTML is returned as-is."""
        assert _strip_html("No tags here") == "No tags here"

    def test_empty_string_returns_empty(self):
        """Empty string input returns empty string."""
        assert _strip_html("") == ""


# ---------------------------------------------------------------------------
# _normalise_contract_time()
# ---------------------------------------------------------------------------

class TestNormaliseContractTime:
    def test_full_time_mapped(self):
        """'Full-time' maps to 'full_time'."""
        assert _normalise_contract_time("Full-time") == "full_time"

    def test_part_time_mapped(self):
        """'Part-time' maps to 'part_time'."""
        assert _normalise_contract_time("Part-time") == "part_time"

    def test_contract_mapped(self):
        """'Contract' maps to 'contract'."""
        assert _normalise_contract_time("Contract") == "contract"

    def test_case_insensitive(self):
        """Mapping lookup is case-insensitive."""
        assert _normalise_contract_time("FULL-TIME") == "full_time"
        assert _normalise_contract_time("full-time") == "full_time"

    def test_unmapped_value_passes_through(self):
        """An unmapped value is returned as-is."""
        assert _normalise_contract_time("Freelance") == "Freelance"

    def test_empty_string_passes_through(self):
        """Empty string is returned as-is (no mapping applies)."""
        assert _normalise_contract_time("") == ""


class TestContractTimeMap:
    def test_map_contains_documented_values(self):
        """All documented Jooble contract type strings are in the map."""
        expected_keys = {"full-time", "part-time", "contract"}
        assert expected_keys == set(_CONTRACT_TIME_MAP.keys())


# ---------------------------------------------------------------------------
# JoobleClient constructor
# ---------------------------------------------------------------------------

class TestJoobleClientConstructor:
    def test_raises_when_jooble_config_absent(self):
        """Constructor raises ValueError when 'jooble' config block is absent."""
        try:
            JoobleClient(config={})
            assert False, "Expected ValueError"
        except ValueError as exc:
            assert "jooble" in str(exc).lower()

    def test_raises_when_api_key_missing(self):
        """Constructor raises ValueError when api_key is absent."""
        try:
            JoobleClient(config={"jooble": {"keywords": "python"}})
            assert False, "Expected ValueError"
        except ValueError as exc:
            assert "api_key" in str(exc).lower()

    def test_raises_when_api_key_empty_string(self):
        """Constructor raises ValueError when api_key is an empty string."""
        try:
            JoobleClient(config={"jooble": {"api_key": ""}})
            assert False, "Expected ValueError"
        except ValueError as exc:
            assert "api_key" in str(exc).lower()

    def test_succeeds_with_valid_api_key(self):
        """Constructor succeeds when api_key is provided."""
        client = JoobleClient(config={"jooble": {"api_key": "abc123"}})
        assert client is not None


# ---------------------------------------------------------------------------
# JoobleClient.normalise()
# ---------------------------------------------------------------------------

class TestJoobleNormalise:
    def test_maps_all_canonical_fields(self):
        """normalise() maps every Jooble field to the canonical schema."""
        client = _client()
        result = client.normalise(_RAW_JOB)

        assert result["source"] == "jooble"
        assert result["source_id"] == "987654321"
        assert result["title"] == "Senior Python Developer"
        assert result["company"] == ""  # Jooble doesn't expose company in the job dict
        assert result["location"] == "New York, NY"
        assert result["salary_min"] == 90000.0
        assert result["salary_max"] == 130000.0
        assert result["salary_period"] is None  # free-text; period cannot be determined
        assert result["contract_type"] is None
        assert result["contract_time"] == "full_time"
        assert result["redirect_url"] == "https://jooble.org/desc/987654321"
        assert result["created_at"] == "2026-03-10T08:00:00"

    def test_source_id_is_string(self):
        """normalise() always returns source_id as a string."""
        client = _client()
        result = client.normalise({**_RAW_JOB, "id": "123"})
        assert isinstance(result["source_id"], str)
        assert result["source_id"] == "123"

    def test_html_stripped_from_snippet(self):
        """normalise() strips HTML from the snippet field."""
        client = _client()
        result = client.normalise(_RAW_JOB)
        assert "<b>" not in result["description"]
        assert "<em>" not in result["description"]
        assert "Python" in result["description"]

    def test_salary_period_always_none(self):
        """salary_period is always None — Jooble salary is free-text."""
        client = _client()
        result = client.normalise(_RAW_JOB)
        assert result["salary_period"] is None

    def test_empty_salary_gives_none_none(self):
        """Empty salary field maps to salary_min=None, salary_max=None."""
        client = _client()
        result = client.normalise({**_RAW_JOB, "salary": ""})
        assert result["salary_min"] is None
        assert result["salary_max"] is None

    def test_missing_salary_key_gives_none_none(self):
        """Absent salary key maps to salary_min=None, salary_max=None."""
        client = _client()
        raw = {k: v for k, v in _RAW_JOB.items() if k != "salary"}
        result = client.normalise(raw)
        assert result["salary_min"] is None
        assert result["salary_max"] is None

    def test_part_time_contract_time_mapped(self):
        """'Part-time' job type maps to 'part_time'."""
        client = _client()
        result = client.normalise({**_RAW_JOB, "type": "Part-time"})
        assert result["contract_time"] == "part_time"

    def test_contract_type_always_none(self):
        """contract_type is always None."""
        client = _client()
        result = client.normalise(_RAW_JOB)
        assert result["contract_type"] is None

    def test_source_is_jooble_string(self):
        """source field is always the literal string 'jooble'."""
        client = _client()
        assert client.normalise({"id": "1"})["source"] == "jooble"

    def test_minimal_raw_dict_does_not_raise(self):
        """normalise() handles a minimal dict without crashing."""
        client = _client()
        result = client.normalise({"id": "1"})
        assert result["source"] == "jooble"
        assert result["source_id"] == "1"
        assert result["title"] == ""
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
# JoobleClient.fetch_page()
# ---------------------------------------------------------------------------

class TestJoobleClientFetchPage:
    def test_success_returns_normalised_jobs_list(self):
        """A 200 response returns a list of normalised listing dicts."""
        client = _client()
        mock_resp = _mock_response(200, {"totalCount": 1, "jobs": [_RAW_JOB]})

        with patch("job_sources.jooble.requests.post", return_value=mock_resp):
            results = client.fetch_page(1)

        assert len(results) == 1
        assert results[0]["source"] == "jooble"
        assert results[0]["title"] == "Senior Python Developer"

    def test_empty_jobs_returns_empty_list(self):
        """A 200 response with an empty jobs list returns []."""
        client = _client()
        mock_resp = _mock_response(200, {"totalCount": 0, "jobs": []})

        with patch("job_sources.jooble.requests.post", return_value=mock_resp):
            assert client.fetch_page(1) == []

    def test_missing_jobs_key_returns_empty_list(self):
        """A 200 response without a 'jobs' key returns []."""
        client = _client()
        mock_resp = _mock_response(200, {"totalCount": 0})

        with patch("job_sources.jooble.requests.post", return_value=mock_resp):
            assert client.fetch_page(1) == []

    def test_non_200_returns_empty_list(self):
        """A non-200 HTTP status returns []."""
        client = _client()
        mock_resp = _mock_response(500, {})

        with patch("job_sources.jooble.requests.post", return_value=mock_resp):
            assert client.fetch_page(1) == []

    def test_request_exception_returns_empty_list(self):
        """A network exception returns []."""
        client = _client()

        with patch(
            "job_sources.jooble.requests.post",
            side_effect=_req.RequestException("timeout"),
        ):
            assert client.fetch_page(1) == []

    def test_invalid_json_returns_empty_list(self):
        """A non-JSON response body returns []."""
        client = _client()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.side_effect = ValueError("not json")

        with patch("job_sources.jooble.requests.post", return_value=mock_resp):
            assert client.fetch_page(1) == []

    def test_page_number_sent_in_payload(self):
        """fetch_page() sends the correct page number in the POST body."""
        client = _client()
        mock_resp = _mock_response(200, {"totalCount": 0, "jobs": []})

        with patch("job_sources.jooble.requests.post", return_value=mock_resp) as mock_post:
            client.fetch_page(3)

        _, kwargs = mock_post.call_args
        assert kwargs["json"]["page"] == 3


# ---------------------------------------------------------------------------
# JoobleClient.total_pages()
# ---------------------------------------------------------------------------

class TestJoobleClientTotalPages:
    def test_computes_page_count_from_total_count(self):
        """total_pages() computes ceil(totalCount / results_per_page)."""
        client = _client(results_per_page=20)
        mock_resp = _mock_response(200, {"totalCount": 45, "jobs": []})

        with patch("job_sources.jooble.requests.post", return_value=mock_resp):
            # ceil(45 / 20) = 3, capped at default max_pages=5
            assert client.total_pages() == 3

    def test_capped_at_max_pages(self):
        """total_pages() does not exceed max_pages."""
        client = _client(results_per_page=10, max_pages=3)
        mock_resp = _mock_response(200, {"totalCount": 1000, "jobs": []})

        with patch("job_sources.jooble.requests.post", return_value=mock_resp):
            assert client.total_pages() == 3

    def test_returns_1_on_request_failure(self):
        """total_pages() returns 1 when the HTTP request fails."""
        client = _client()

        with patch(
            "job_sources.jooble.requests.post",
            side_effect=_req.RequestException("timeout"),
        ):
            assert client.total_pages() == 1

    def test_returns_1_when_total_count_zero(self):
        """total_pages() returns 1 when totalCount is 0."""
        client = _client()
        mock_resp = _mock_response(200, {"totalCount": 0, "jobs": []})

        with patch("job_sources.jooble.requests.post", return_value=mock_resp):
            assert client.total_pages() == 1

    def test_returns_1_when_total_count_missing(self):
        """total_pages() returns 1 when totalCount key is absent."""
        client = _client()
        mock_resp = _mock_response(200, {"jobs": []})

        with patch("job_sources.jooble.requests.post", return_value=mock_resp):
            assert client.total_pages() == 1

    def test_result_is_cached(self):
        """total_pages() only calls the API once; subsequent calls use cache."""
        client = _client(results_per_page=20)
        mock_resp = _mock_response(200, {"totalCount": 40, "jobs": []})

        with patch("job_sources.jooble.requests.post", return_value=mock_resp) as mock_post:
            assert client.total_pages() == 2
            assert client.total_pages() == 2
            assert mock_post.call_count == 1  # second call hit the cache

    def test_single_page_exact_fit(self):
        """total_pages() returns 1 when totalCount exactly equals results_per_page."""
        client = _client(results_per_page=20)
        mock_resp = _mock_response(200, {"totalCount": 20, "jobs": []})

        with patch("job_sources.jooble.requests.post", return_value=mock_resp):
            assert client.total_pages() == 1


# ---------------------------------------------------------------------------
# JoobleClient.pages()
# ---------------------------------------------------------------------------

class TestJoobleClientPages:
    def test_yields_normalised_listings_per_page(self):
        """pages() yields lists of normalised dicts across all pages.

        total_pages() fetches and caches page 1; pages() reuses that cache for
        page 1 and only makes a fresh HTTP call for page 2 onward.
        """
        client = _client(results_per_page=1, max_pages=2)
        total_count_resp = _mock_response(200, {"totalCount": 2, "jobs": [_RAW_JOB]})
        page2_resp = _mock_response(200, {"totalCount": 2, "jobs": [
            {**_RAW_JOB, "id": "999", "title": "DevOps Engineer"},
        ]})

        with patch("job_sources.jooble.requests.post", side_effect=[
            total_count_resp,  # total_pages() call — also caches page-1 results
            page2_resp,        # fetch_page(2) — page 1 served from cache
        ]):
            pages = list(client.pages())

        assert len(pages) == 2
        assert pages[0][0]["source"] == "jooble"
        assert pages[0][0]["source_id"] == "987654321"
        assert pages[1][0]["source_id"] == "999"

    def test_stops_early_when_page_returns_empty(self):
        """pages() stops early when a page returns no results.

        Page 1 is served from the cache populated by total_pages(), so the
        only fresh HTTP call is for page 2 which returns empty and halts iteration.
        """
        client = _client(results_per_page=10, max_pages=3)
        total_resp = _mock_response(200, {"totalCount": 30, "jobs": [_RAW_JOB]})
        empty_resp = _mock_response(200, {"totalCount": 30, "jobs": []})

        with patch("job_sources.jooble.requests.post", side_effect=[
            total_resp,  # total_pages() call — also caches page-1 results
            empty_resp,  # fetch_page(2) — triggers early stop; page 1 served from cache
        ]):
            pages = list(client.pages())

        assert len(pages) == 1


# ---------------------------------------------------------------------------
# SOURCES registry and settings_schema()
# ---------------------------------------------------------------------------

class TestSourcesRegistry:
    def test_jooble_registered(self):
        """SOURCES contains 'jooble' mapped to JoobleClient."""
        assert "jooble" in SOURCES
        assert SOURCES["jooble"] is JoobleClient

    def test_jooble_is_job_source_subclass(self):
        """JoobleClient is a subclass of JobSource."""
        assert issubclass(JoobleClient, JobSource)


class TestJoobleSettingsSchema:
    def test_has_display_name(self):
        """settings_schema() returns a display_name string."""
        schema = JoobleClient.settings_schema()
        assert isinstance(schema["display_name"], str)
        assert schema["display_name"]

    def test_has_required_api_key_field(self):
        """settings_schema() returns a required api_key password field."""
        schema = JoobleClient.settings_schema()
        fields = schema["fields"]
        assert len(fields) == 1

        field = fields[0]
        assert field["name"] == "api_key"
        assert field["type"] == "password"
        assert field["required"] is True


# ---------------------------------------------------------------------------
# JoobleClient — credential precedence
# ---------------------------------------------------------------------------

class TestJoobleClientCredentialPrecedence:
    """Verify the three-way credential resolution: credentials > config > ValueError."""

    def test_credentials_dict_used_when_provided(self):
        """When credentials dict has api_key, it is used directly."""
        client = JoobleClient(config={}, credentials={"api_key": "creds-key"})
        assert client._api_key == "creds-key"

    def test_credentials_dict_takes_precedence_over_config(self):
        """credentials api_key wins over config['jooble']['api_key']."""
        config = {"jooble": {"api_key": "config-key"}}
        client = JoobleClient(config=config, credentials={"api_key": "creds-key"})
        assert client._api_key == "creds-key"

    def test_fallback_to_config_when_credentials_empty(self):
        """When credentials is {}, api_key comes from config['jooble']."""
        config = {"jooble": {"api_key": "config-key"}}
        client = JoobleClient(config=config, credentials={})
        assert client._api_key == "config-key"

    def test_both_empty_raises_value_error(self):
        """When neither credentials nor config has api_key, raises ValueError."""
        try:
            JoobleClient(config={}, credentials={})
            assert False, "Expected ValueError"
        except ValueError as exc:
            assert "api_key" in str(exc).lower()

    def test_empty_string_in_credentials_falls_back_to_config(self):
        """credentials={"api_key": ""} is treated as absent and falls back to config.

        The ``or`` chain evaluates "" as falsy and continues to config lookup.
        """
        config = {"jooble": {"api_key": "config-key"}}
        client = JoobleClient(config=config, credentials={"api_key": ""})
        assert client._api_key == "config-key"
