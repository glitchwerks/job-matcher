"""
tests/test_job_sources_arbeitnow.py — Unit tests for ArbeitnowClient.

Covers:
  - normalise(): canonical field mapping, remote flag → location, Unix ts → ISO,
    HTML stripping, empty job_types → None contract_time
  - total_pages(): reads meta.last_page; fallback to 1 when meta absent
  - fetch_page(): HTTP 200 success, non-200 error, network exception, bad JSON
  - SOURCES registry: "arbeitnow" key maps to ArbeitnowClient
"""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from job_sources import SOURCES, ArbeitnowClient, JobSource
from job_sources.arbeitnow import _strip_html, _unix_to_iso


# ---------------------------------------------------------------------------
# Helper factories
# ---------------------------------------------------------------------------

def _client() -> ArbeitnowClient:
    return ArbeitnowClient(config={})


def _mock_response(status_code: int, json_data: dict) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data
    resp.raise_for_status = MagicMock()
    if status_code >= 400:
        import requests as _req
        resp.raise_for_status.side_effect = _req.HTTPError(response=resp)
    return resp


# A complete raw Arbeitnow listing matching the API contract.
_RAW_LISTING = {
    "slug": "senior-python-engineer-acme-corp",
    "title": "Senior Python Engineer",
    "company_name": "Acme Corp",
    "location": "Berlin, Germany",
    "remote": False,
    "job_types": ["full_time"],
    "description": "<p>We need a <strong>Python</strong> expert.</p>",
    "url": "https://www.arbeitnow.com/jobs/acme-corp/senior-python-engineer",
    "created_at": 1737000000,  # 2025-01-16T04:00:00Z
}


# ---------------------------------------------------------------------------
# _strip_html helper
# ---------------------------------------------------------------------------

class TestStripHtml:
    def test_removes_simple_tags(self):
        assert _strip_html("<p>Hello world</p>") == "Hello world"

    def test_decodes_html_entities(self):
        result = _strip_html("&amp; &lt;tag&gt;")
        assert "&amp;" not in result
        assert "&" in result

    def test_empty_string_returns_empty(self):
        assert _strip_html("") == ""

    def test_plain_text_unchanged(self):
        assert _strip_html("No tags here") == "No tags here"

    def test_nested_tags_flattened(self):
        result = _strip_html("<div><p>Para <strong>bold</strong> text</p></div>")
        assert "Para" in result
        assert "bold" in result
        assert "<" not in result


# ---------------------------------------------------------------------------
# _unix_to_iso helper
# ---------------------------------------------------------------------------

class TestUnixToIso:
    def test_known_timestamp(self):
        # 2025-01-16T04:00:00Z
        assert _unix_to_iso(1737000000) == "2025-01-16T04:00:00Z"

    def test_zero_timestamp(self):
        assert _unix_to_iso(0) == "1970-01-01T00:00:00Z"

    def test_none_returns_none(self):
        assert _unix_to_iso(None) is None

    def test_non_numeric_returns_none(self):
        assert _unix_to_iso("not-a-ts") is None

    def test_float_truncated_to_seconds(self):
        # Float with sub-second component — should still produce valid ISO string.
        result = _unix_to_iso(1737000000.9)
        assert result == "2025-01-16T04:00:00Z"


# ---------------------------------------------------------------------------
# ArbeitnowClient.normalise()
# ---------------------------------------------------------------------------

class TestArbeitnowClientNormalise:
    def test_canonical_fields_mapped(self):
        """All canonical fields are populated from the raw listing."""
        client = _client()
        result = client.normalise(_RAW_LISTING)

        assert result["source"] == "arbeitnow"
        assert result["source_id"] == "senior-python-engineer-acme-corp"
        assert result["title"] == "Senior Python Engineer"
        assert result["company"] == "Acme Corp"
        assert result["location"] == "Berlin, Germany"
        assert result["contract_time"] == "full_time"
        assert result["redirect_url"] == "https://www.arbeitnow.com/jobs/acme-corp/senior-python-engineer"
        assert result["created_at"] == "2025-01-16T04:00:00Z"

    def test_salary_and_contract_type_always_none(self):
        """salary_min, salary_max, and contract_type are always None."""
        client = _client()
        result = client.normalise(_RAW_LISTING)

        assert result["salary_min"] is None
        assert result["salary_max"] is None
        assert result["contract_type"] is None

    def test_html_stripped_from_description(self):
        """HTML tags are stripped from the description field."""
        client = _client()
        result = client.normalise(_RAW_LISTING)

        assert "<p>" not in result["description"]
        assert "<strong>" not in result["description"]
        assert "Python" in result["description"]

    def test_remote_flag_true_no_location_gives_remote_string(self):
        """When remote=True and location is empty, location becomes 'Remote'."""
        client = _client()
        raw = {**_RAW_LISTING, "remote": True, "location": ""}
        result = client.normalise(raw)

        assert result["location"] == "Remote"

    def test_remote_flag_true_with_location_keeps_location(self):
        """When remote=True but location is non-empty, location is preserved."""
        client = _client()
        raw = {**_RAW_LISTING, "remote": True, "location": "London, UK"}
        result = client.normalise(raw)

        assert result["location"] == "London, UK"

    def test_remote_flag_false_no_location_stays_empty(self):
        """When remote=False and location is empty, location stays empty."""
        client = _client()
        raw = {**_RAW_LISTING, "remote": False, "location": ""}
        result = client.normalise(raw)

        assert result["location"] == ""

    def test_empty_job_types_gives_none_contract_time(self):
        """An empty job_types list maps to contract_time=None."""
        client = _client()
        raw = {**_RAW_LISTING, "job_types": []}
        result = client.normalise(raw)

        assert result["contract_time"] is None

    def test_missing_job_types_gives_none_contract_time(self):
        """Absent job_types key maps to contract_time=None."""
        client = _client()
        raw = {k: v for k, v in _RAW_LISTING.items() if k != "job_types"}
        result = client.normalise(raw)

        assert result["contract_time"] is None

    def test_multiple_job_types_uses_first(self):
        """When job_types has multiple entries, only the first is used."""
        client = _client()
        raw = {**_RAW_LISTING, "job_types": ["contract", "part_time"]}
        result = client.normalise(raw)

        assert result["contract_time"] == "contract"

    def test_unix_timestamp_converted_to_iso(self):
        """created_at Unix timestamp is converted to ISO 8601 string."""
        client = _client()
        result = client.normalise({**_RAW_LISTING, "created_at": 0})

        assert result["created_at"] == "1970-01-01T00:00:00Z"

    def test_missing_created_at_gives_none(self):
        """Missing created_at key produces created_at=None."""
        client = _client()
        raw = {k: v for k, v in _RAW_LISTING.items() if k != "created_at"}
        result = client.normalise(raw)

        assert result["created_at"] is None

    def test_minimal_raw_dict_does_not_raise(self):
        """normalise() does not crash on a mostly-empty raw dict."""
        client = _client()
        result = client.normalise({"slug": "test-slug"})

        assert result["source"] == "arbeitnow"
        assert result["source_id"] == "test-slug"
        assert result["title"] == ""
        assert result["company"] == ""

    def test_source_is_always_arbeitnow(self):
        """source field is always the string 'arbeitnow'."""
        client = _client()
        assert client.normalise({})["source"] == "arbeitnow"

    def test_result_contains_all_canonical_keys(self):
        """normalise() output contains all required canonical schema keys."""
        required_keys = {
            "source", "source_id", "title", "company", "location",
            "salary_min", "salary_max", "salary_period", "contract_type", "contract_time",
            "description", "redirect_url", "created_at",
        }
        client = _client()
        result = client.normalise(_RAW_LISTING)

        assert required_keys.issubset(result.keys())


# ---------------------------------------------------------------------------
# ArbeitnowClient.total_pages()
# ---------------------------------------------------------------------------

class TestArbeitnowClientTotalPages:
    def test_returns_last_page_from_meta(self):
        """total_pages() returns meta.last_page from the API response."""
        client = _client()
        mock_resp = _mock_response(200, {
            "data": [],
            "meta": {"current_page": 1, "last_page": 12},
        })

        with patch("job_sources.arbeitnow.requests.get", return_value=mock_resp):
            assert client.total_pages() == 12

    def test_returns_1_when_meta_absent(self):
        """total_pages() falls back to 1 when 'meta' key is not in the response."""
        client = _client()
        mock_resp = _mock_response(200, {"data": []})

        with patch("job_sources.arbeitnow.requests.get", return_value=mock_resp):
            assert client.total_pages() == 1

    def test_returns_1_when_last_page_absent(self):
        """total_pages() falls back to 1 when meta.last_page is missing."""
        client = _client()
        mock_resp = _mock_response(200, {"data": [], "meta": {"current_page": 1}})

        with patch("job_sources.arbeitnow.requests.get", return_value=mock_resp):
            assert client.total_pages() == 1

    def test_returns_1_on_request_failure(self):
        """total_pages() returns 1 when the HTTP request fails."""
        import requests as _req
        client = _client()

        with patch(
            "job_sources.arbeitnow.requests.get",
            side_effect=_req.RequestException("timeout"),
        ):
            assert client.total_pages() == 1

    def test_result_is_cached(self):
        """total_pages() only calls the API once; subsequent calls use cache."""
        client = _client()
        mock_resp = _mock_response(200, {
            "data": [],
            "meta": {"current_page": 1, "last_page": 5},
        })

        with patch("job_sources.arbeitnow.requests.get", return_value=mock_resp) as mock_get:
            assert client.total_pages() == 5
            assert client.total_pages() == 5
            assert mock_get.call_count == 1  # second call hit the cache


# ---------------------------------------------------------------------------
# ArbeitnowClient.fetch_page()
# ---------------------------------------------------------------------------

class TestArbeitnowClientFetchPage:
    def test_success_returns_raw_data_list(self):
        """A 200 response returns the raw 'data' array unchanged."""
        client = _client()
        mock_resp = _mock_response(200, {"data": [_RAW_LISTING]})

        with patch("job_sources.arbeitnow.requests.get", return_value=mock_resp):
            results = client.fetch_page(1)

        assert len(results) == 1
        assert results[0]["slug"] == "senior-python-engineer-acme-corp"

    def test_empty_data_returns_empty_list(self):
        """A 200 response with an empty data array returns []."""
        client = _client()
        mock_resp = _mock_response(200, {"data": []})

        with patch("job_sources.arbeitnow.requests.get", return_value=mock_resp):
            assert client.fetch_page(1) == []

    def test_missing_data_key_returns_empty_list(self):
        """A 200 response without a 'data' key returns []."""
        client = _client()
        mock_resp = _mock_response(200, {})

        with patch("job_sources.arbeitnow.requests.get", return_value=mock_resp):
            assert client.fetch_page(1) == []

    def test_non_200_returns_empty_list(self):
        """A non-200 HTTP status returns [] and logs a warning."""
        client = _client()
        mock_resp = _mock_response(500, {})

        with patch("job_sources.arbeitnow.requests.get", return_value=mock_resp):
            assert client.fetch_page(2) == []

    def test_network_exception_returns_empty_list(self):
        """A network-level exception returns []."""
        import requests as _req
        client = _client()

        with patch(
            "job_sources.arbeitnow.requests.get",
            side_effect=_req.RequestException("DNS failure"),
        ):
            assert client.fetch_page(1) == []

    def test_invalid_json_returns_empty_list(self):
        """A non-JSON response body returns []."""
        client = _client()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.side_effect = ValueError("not json")

        with patch("job_sources.arbeitnow.requests.get", return_value=mock_resp):
            assert client.fetch_page(1) == []

    def test_page_param_passed_to_api(self):
        """fetch_page() passes the correct ?page=N query parameter."""
        client = _client()
        mock_resp = _mock_response(200, {"data": []})

        with patch("job_sources.arbeitnow.requests.get", return_value=mock_resp) as mock_get:
            client.fetch_page(3)
            _, kwargs = mock_get.call_args
            assert kwargs["params"]["page"] == 3


# ---------------------------------------------------------------------------
# SOURCES registry
# ---------------------------------------------------------------------------

class TestSourcesRegistry:
    def test_arbeitnow_registered(self):
        """SOURCES contains 'arbeitnow' mapped to ArbeitnowClient."""
        assert "arbeitnow" in SOURCES
        assert SOURCES["arbeitnow"] is ArbeitnowClient

    def test_arbeitnow_is_job_source_subclass(self):
        """ArbeitnowClient is a subclass of JobSource."""
        assert issubclass(ArbeitnowClient, JobSource)

    def test_arbeitnow_instantiates_via_generic_factory_path(self):
        """make_source() can instantiate ArbeitnowClient via the generic path."""
        from job_sources import make_source

        config = {"job_source": "arbeitnow"}
        source = make_source(config)
        assert isinstance(source, ArbeitnowClient)
