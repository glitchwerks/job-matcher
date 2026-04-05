"""
tests/test_job_sources_remoteok.py — Unit tests for RemoteOKClient.

Covers:
  - User-Agent header is sent on every request
  - API metadata element (first array item, lacks 'id'/'position') is skipped
  - Salary 0 is mapped to None
  - HTML tags are stripped from description
  - Empty location falls back to "Remote"
  - Successful fetch returns normalised listings
  - HTTP error returns empty list
  - Network exception returns empty list
  - Invalid JSON returns empty list
  - total_pages() always returns 1
  - SOURCES registry contains "remoteok"
  - make_source() returns a RemoteOKClient when job_source="remoteok"
"""

import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from job_sources import SOURCES, JobSource, make_source

RemoteOKClient = SOURCES["remoteok"]

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

_METADATA_ELEMENT = {
    "legal": "This data is property of RemoteOK",
    "apiVersion": "1.0",
    "updated": "2026-01-01T00:00:00Z",
}

_RAW_JOB = {
    "id": "abc123",
    "position": "Senior Python Engineer",
    "company": "Acme Corp",
    "location": "New York, NY",
    "salary_min": 120000,
    "salary_max": 160000,
    "tags": ["python", "django"],
    "description": "<p>We need a <strong>Python</strong> expert.</p>",
    "url": "https://remoteok.com/remote-jobs/abc123",
    "date": "2026-01-15T09:00:00Z",
}

_REMOTEOK_RESPONSE = [_METADATA_ELEMENT, _RAW_JOB]


def _make_client(config: dict | None = None) -> RemoteOKClient:
    return RemoteOKClient(config=config or {})


def _mock_response(status_code: int, json_data) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data
    return resp


# ---------------------------------------------------------------------------
# User-Agent header
# ---------------------------------------------------------------------------

class TestUserAgentHeader:
    def test_default_user_agent_is_sent(self):
        """The default User-Agent header is included in every request."""
        client = _make_client()
        mock_resp = _mock_response(200, _REMOTEOK_RESPONSE)

        with patch("job_sources._plugin_remoteok.requests.get", return_value=mock_resp) as mock_get:
            client.fetch_page(1)

        args, kwargs = mock_get.call_args
        headers = kwargs.get("headers", {})
        assert "User-Agent" in headers
        assert headers["User-Agent"] == "job-matcher-ui/1.0"

    def test_custom_user_agent_from_config(self):
        """A user_agent in config['remoteok'] overrides the default."""
        client = _make_client({"remoteok": {"user_agent": "my-app/2.0"}})
        mock_resp = _mock_response(200, _REMOTEOK_RESPONSE)

        with patch("job_sources._plugin_remoteok.requests.get", return_value=mock_resp) as mock_get:
            client.fetch_page(1)

        args, kwargs = mock_get.call_args
        assert kwargs["headers"]["User-Agent"] == "my-app/2.0"


# ---------------------------------------------------------------------------
# Metadata element skipping
# ---------------------------------------------------------------------------

class TestMetadataSkipping:
    def test_metadata_element_is_skipped(self):
        """The first array item (API metadata, lacks 'id'/'position') is not returned."""
        client = _make_client()
        mock_resp = _mock_response(200, _REMOTEOK_RESPONSE)

        with patch("job_sources._plugin_remoteok.requests.get", return_value=mock_resp):
            results = client.fetch_page(1)

        # Metadata object has no 'id'/'position', so it must not appear in results.
        source_ids = [r["source_id"] for r in results]
        assert "abc123" in source_ids
        # Metadata lacks 'id'; confirm no result maps to something from it.
        assert len(results) == 1

    def test_only_items_with_id_and_position_are_returned(self):
        """Items missing 'id' or 'position' are filtered out."""
        data = [
            {"legal": "metadata"},         # no id / position
            {"id": "x1"},                   # no position
            {"position": "Dev"},            # no id
            {"id": "x2", "position": "Engineer"},  # valid
        ]
        client = _make_client()
        mock_resp = _mock_response(200, data)

        with patch("job_sources._plugin_remoteok.requests.get", return_value=mock_resp):
            results = client.fetch_page(1)

        assert len(results) == 1
        assert results[0]["source_id"] == "x2"


# ---------------------------------------------------------------------------
# Salary mapping
# ---------------------------------------------------------------------------

class TestSalaryMapping:
    def test_salary_zero_mapped_to_none(self):
        """salary_min and salary_max equal to 0 are mapped to None."""
        client = _make_client()
        raw = {**_RAW_JOB, "salary_min": 0, "salary_max": 0}
        result = client.normalise(raw)

        assert result["salary_min"] is None
        assert result["salary_max"] is None

    def test_salary_absent_mapped_to_none(self):
        """Missing salary_min / salary_max keys produce None."""
        client = _make_client()
        raw = {k: v for k, v in _RAW_JOB.items() if k not in ("salary_min", "salary_max")}
        result = client.normalise(raw)

        assert result["salary_min"] is None
        assert result["salary_max"] is None

    def test_nonzero_salary_preserved(self):
        """Positive salary values are passed through unchanged."""
        client = _make_client()
        result = client.normalise(_RAW_JOB)

        assert result["salary_min"] == 120000
        assert result["salary_max"] == 160000


# ---------------------------------------------------------------------------
# HTML stripping
# ---------------------------------------------------------------------------

class TestHtmlStripping:
    def test_html_tags_stripped_from_description(self):
        """HTML tags are removed from the description field."""
        client = _make_client()
        result = client.normalise(_RAW_JOB)

        assert "<p>" not in result["description"]
        assert "<strong>" not in result["description"]
        assert "Python" in result["description"]
        assert "expert" in result["description"]

    def test_empty_description_returns_empty_string(self):
        """An empty or absent description becomes an empty string."""
        client = _make_client()
        raw = {**_RAW_JOB, "description": ""}
        result = client.normalise(raw)
        assert result["description"] == ""

    def test_none_description_returns_empty_string(self):
        """A None description becomes an empty string."""
        client = _make_client()
        raw = {**_RAW_JOB, "description": None}
        result = client.normalise(raw)
        assert result["description"] == ""


# ---------------------------------------------------------------------------
# Location fallback
# ---------------------------------------------------------------------------

class TestLocationFallback:
    def test_empty_location_falls_back_to_remote(self):
        """An empty location string is replaced with 'Remote'."""
        client = _make_client()
        raw = {**_RAW_JOB, "location": ""}
        result = client.normalise(raw)
        assert result["location"] == "Remote"

    def test_none_location_falls_back_to_remote(self):
        """A None location is replaced with 'Remote'."""
        client = _make_client()
        raw = {**_RAW_JOB, "location": None}
        result = client.normalise(raw)
        assert result["location"] == "Remote"

    def test_whitespace_only_location_falls_back_to_remote(self):
        """A whitespace-only location is replaced with 'Remote'."""
        client = _make_client()
        raw = {**_RAW_JOB, "location": "   "}
        result = client.normalise(raw)
        assert result["location"] == "Remote"

    def test_non_empty_location_is_preserved(self):
        """A non-empty location string is passed through unchanged."""
        client = _make_client()
        result = client.normalise(_RAW_JOB)
        assert result["location"] == "New York, NY"


# ---------------------------------------------------------------------------
# Full normalise() canonical schema
# ---------------------------------------------------------------------------

class TestNormalise:
    def test_normalise_maps_all_canonical_fields(self):
        """normalise() produces all required canonical schema keys."""
        client = _make_client()
        result = client.normalise(_RAW_JOB)

        assert result["source"] == "remoteok"
        assert result["source_id"] == "abc123"
        assert result["title"] == "Senior Python Engineer"
        assert result["company"] == "Acme Corp"
        assert result["location"] == "New York, NY"
        assert result["salary_min"] == 120000
        assert result["salary_max"] == 160000
        assert result["contract_type"] is None
        assert result["contract_time"] is None
        assert result["salary_period"] is None  # RemoteOK API doesn't expose pay period
        assert result["redirect_url"] == "https://remoteok.com/remote-jobs/abc123"
        assert result["created_at"] == "2026-01-15T09:00:00Z"

    def test_contract_type_and_time_are_always_none(self):
        """contract_type and contract_time are always None (RemoteOK doesn't provide them)."""
        client = _make_client()
        result = client.normalise(_RAW_JOB)
        assert result["contract_type"] is None
        assert result["contract_time"] is None

    def test_source_is_always_remoteok(self):
        """normalise() always sets source='remoteok'."""
        client = _make_client()
        result = client.normalise({"id": "x", "position": "Dev"})
        assert result["source"] == "remoteok"


# ---------------------------------------------------------------------------
# fetch_page() error handling
# ---------------------------------------------------------------------------

class TestFetchPage:
    def test_fetch_page_success_returns_normalised_listings(self):
        """A 200 response returns a list of normalised dicts."""
        client = _make_client()
        mock_resp = _mock_response(200, _REMOTEOK_RESPONSE)

        with patch("job_sources._plugin_remoteok.requests.get", return_value=mock_resp):
            results = client.fetch_page(1)

        assert len(results) == 1
        assert results[0]["source"] == "remoteok"

    def test_fetch_page_non_200_returns_empty_list(self):
        """A non-200 HTTP response returns an empty list."""
        client = _make_client()
        mock_resp = _mock_response(500, {})

        with patch("job_sources._plugin_remoteok.requests.get", return_value=mock_resp):
            results = client.fetch_page(1)

        assert results == []

    def test_fetch_page_request_exception_returns_empty_list(self):
        """A network exception returns an empty list."""
        import requests as req

        client = _make_client()

        with patch(
            "job_sources._plugin_remoteok.requests.get",
            side_effect=req.RequestException("timeout"),
        ):
            results = client.fetch_page(1)

        assert results == []

    def test_fetch_page_invalid_json_returns_empty_list(self):
        """A non-JSON response returns an empty list."""
        client = _make_client()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.side_effect = ValueError("not json")

        with patch("job_sources._plugin_remoteok.requests.get", return_value=mock_resp):
            results = client.fetch_page(1)

        assert results == []

    def test_fetch_page_non_list_json_returns_empty_list(self):
        """A JSON response that is not an array returns an empty list."""
        client = _make_client()
        mock_resp = _mock_response(200, {"error": "something went wrong"})

        with patch("job_sources._plugin_remoteok.requests.get", return_value=mock_resp):
            results = client.fetch_page(1)

        assert results == []

    def test_fetch_page_page_arg_ignored(self):
        """fetch_page() ignores the page argument and always fetches all listings."""
        client = _make_client()
        mock_resp = _mock_response(200, _REMOTEOK_RESPONSE)

        with patch("job_sources._plugin_remoteok.requests.get", return_value=mock_resp) as mock_get:
            client.fetch_page(99)

        # The URL should always be the single RemoteOK endpoint regardless of page.
        args, kwargs = mock_get.call_args
        assert args[0] == "https://remoteok.com/api"

    def test_fetch_page_caches_result(self):
        """Calling fetch_page() twice only makes one HTTP request (cached)."""
        client = _make_client()
        mock_resp = _mock_response(200, _REMOTEOK_RESPONSE)

        with patch("job_sources._plugin_remoteok.requests.get", return_value=mock_resp) as mock_get:
            client.fetch_page(1)
            client.fetch_page(1)

        assert mock_get.call_count == 1


# ---------------------------------------------------------------------------
# total_pages()
# ---------------------------------------------------------------------------

class TestTotalPages:
    def test_total_pages_returns_one(self):
        """total_pages() always returns 1 (RemoteOK is a single-page API)."""
        client = _make_client()
        assert client.total_pages() == 1


# ---------------------------------------------------------------------------
# SOURCES registry
# ---------------------------------------------------------------------------

class TestSourcesRegistry:
    def test_remoteok_registered(self):
        """SOURCES contains 'remoteok' mapped to RemoteOKClient."""
        assert "remoteok" in SOURCES
        assert SOURCES["remoteok"] is RemoteOKClient

    def test_remoteok_is_job_source_subclass(self):
        """RemoteOKClient is a subclass of JobSource."""
        assert issubclass(RemoteOKClient, JobSource)


# ---------------------------------------------------------------------------
# make_source() factory
# ---------------------------------------------------------------------------

class TestMakeSource:
    def test_make_source_returns_remoteok_client(self):
        """make_source() returns a RemoteOKClient when job_source='remoteok'."""
        config = {"job_source": "remoteok"}
        source = make_source(config)
        assert isinstance(source, RemoteOKClient)

    def test_make_source_remoteok_is_job_source_instance(self):
        """The RemoteOKClient returned by make_source() is a JobSource."""
        config = {"job_source": "remoteok"}
        source = make_source(config)
        assert isinstance(source, JobSource)
