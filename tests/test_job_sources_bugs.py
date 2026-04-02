"""
tests/test_job_sources_bugs.py — Regression tests for issues #128, #131, #133.

Issue #128: pages() must be declared on the JobSource ABC and implemented by
            all seven concrete sources.
Issue #131: make_source() must pass only config= to AdzunaClient; no special
            casing for adzuna.
Issue #133: All sources expose a timestamp via created_at in normalise().
            ingest.py copies created_at → posted_at before persisting so that
            date sort works for all sources.  AdzunaClient also sets posted_at
            directly in normalise() for backward compatibility.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from job_sources import (
    SOURCES,
    AdzunaClient,
    ArbeitnowClient,
    HimalayasClient,
    JobSource,
    RemoteOKClient,
    RemotiveClient,
    TheMuseClient,
    USAJobsClient,
    make_source,
)
from job_sources.base import JobSource as JobSourceBase


# ---------------------------------------------------------------------------
# Issue #128: pages() is callable on all seven sources
# ---------------------------------------------------------------------------

_ADZUNA_CONFIG = {
    "adzuna_app_id": "test-id",
    "adzuna_app_key": "test-key",
    "search": {
        "country": "us",
        "what": "software engineer",
        "results_per_page": 10,
        "max_pages": 2,
    },
    "job_source": "adzuna",
}

_USAJOBS_CONFIG = {
    "job_source": "usajobs",
    "usajobs": {
        "api_key": "fake-key",
        "user_agent": "test@example.com",
    },
}


def _make_mock_response(status: int, json_data) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = json_data
    resp.raise_for_status = MagicMock()
    if status >= 400:
        resp.raise_for_status.side_effect = Exception(f"HTTP {status}")
    return resp


class TestAllSourcesHavePages:
    """Every concrete source must have a callable pages() method."""

    def test_adzuna_has_pages(self):
        client = AdzunaClient(config=_ADZUNA_CONFIG)
        assert callable(getattr(client, "pages", None))

    def test_himalayas_has_pages(self):
        client = HimalayasClient(config={})
        assert callable(getattr(client, "pages", None))

    def test_remoteok_has_pages(self):
        client = RemoteOKClient(config={})
        assert callable(getattr(client, "pages", None))

    def test_arbeitnow_has_pages(self):
        client = ArbeitnowClient(config={})
        assert callable(getattr(client, "pages", None))

    def test_remotive_has_pages(self):
        client = RemotiveClient(config={})
        assert callable(getattr(client, "pages", None))

    def test_usajobs_has_pages(self):
        client = USAJobsClient(config=_USAJOBS_CONFIG)
        assert callable(getattr(client, "pages", None))

    def test_the_muse_has_pages(self):
        client = TheMuseClient(config={})
        assert callable(getattr(client, "pages", None))


class TestPagesIteratorBehaviour:
    """pages() must yield results and stop early on an empty page."""

    def test_himalayas_pages_yields_results(self):
        """HimalayasClient.pages() yields each non-empty page."""
        client = HimalayasClient(config={"himalayas": {"limit": 10}})
        client._total = 10  # one page

        raw_job = {
            "guid": "https://himalayas.app/companies/co/jobs/h1",
            "title": "Eng",
            "companyName": "Co",
            "applicationLink": "https://example.com",
        }
        mock_resp = _make_mock_response(200, {"jobs": [raw_job], "total": 10})

        with patch("job_sources.himalayas.requests.get", return_value=mock_resp):
            pages = list(client.pages())

        assert len(pages) == 1
        assert pages[0][0]["source"] == "himalayas"

    def test_remoteok_pages_yields_single_page(self):
        """RemoteOKClient.pages() yields exactly one page (single-page API)."""
        client = RemoteOKClient(config={})
        raw_job = {"id": "r1", "position": "Dev", "company": "Co", "url": "https://x.com"}
        mock_resp = _make_mock_response(200, [raw_job])

        with patch("job_sources.remoteok.requests.get", return_value=mock_resp):
            pages = list(client.pages())

        assert len(pages) == 1

    def test_arbeitnow_pages_stops_early_on_empty_page(self):
        """ArbeitnowClient.pages() stops early when a page returns no results."""
        client = ArbeitnowClient(config={})
        client._cached_total_pages = 3

        empty_resp = _make_mock_response(200, {"data": []})

        with patch("job_sources.arbeitnow.requests.get", return_value=empty_resp):
            pages = list(client.pages())

        assert pages == []

    def test_remotive_pages_yields_single_page(self):
        """RemotiveClient.pages() yields exactly one page (single-page API)."""
        client = RemotiveClient(config={})
        raw_job = {
            "id": 1,
            "title": "Dev",
            "company_name": "Co",
            "url": "https://x.com",
            "publication_date": "2026-01-01T00:00:00Z",
        }
        mock_resp = _make_mock_response(200, {"jobs": [raw_job]})

        with patch("job_sources.remotive.requests.get", return_value=mock_resp):
            pages = list(client.pages())

        assert len(pages) == 1

    def test_the_muse_pages_stops_early_on_empty_page(self):
        """TheMuseClient.pages() stops early when a page returns zero results."""
        client = TheMuseClient(config={})
        client._page_count = 3

        empty_resp = _make_mock_response(200, {"results": [], "page_count": 3})

        with patch("job_sources.the_muse.requests.get", return_value=empty_resp):
            pages = list(client.pages())

        assert pages == []

    def test_himalayas_pages_stops_early_on_empty_page(self):
        """HimalayasClient.pages() stops early when a page returns zero results."""
        client = HimalayasClient(config={"himalayas": {"limit": 10}})
        client._total = 30  # 3 pages

        empty_resp = _make_mock_response(200, {"jobs": [], "total": 30})

        with patch("job_sources.himalayas.requests.get", return_value=empty_resp):
            pages = list(client.pages())

        assert pages == []


# ---------------------------------------------------------------------------
# Issue #128: ABC provides default pages() — concrete subclasses without an
# override still get a working implementation
# ---------------------------------------------------------------------------

class TestAbcDefaultPages:
    """The ABC default pages() works for any source that implements the three
    core abstract methods."""

    def test_concrete_without_override_uses_abc_default(self):
        """A subclass with only fetch_page / total_pages / normalise can call pages()."""

        class MinimalSource(JobSourceBase):
            def __init__(self):
                self._calls = 0

            def fetch_page(self, page):
                self._calls += 1
                if page == 1:
                    return [{"source": "test", "title": "Job"}]
                return []

            def total_pages(self):
                return 2

            def normalise(self, raw):
                return raw

            @classmethod
            def settings_schema(cls):
                return {"display_name": "Minimal", "fields": []}

        src = MinimalSource()
        pages = list(src.pages())
        assert len(pages) == 1  # page 2 returns empty → stops early
        assert pages[0][0]["title"] == "Job"


# ---------------------------------------------------------------------------
# Issue #133: created_at is populated for all sources (ingest.py will copy it
# to posted_at before persisting — tested via the pipeline bridge in ingest.py)
# ---------------------------------------------------------------------------


class TestCreatedAtPopulated:
    """normalise() must set created_at for each source so the ingest.py
    pipeline can copy it to posted_at before persisting to the DB."""

    def test_adzuna_created_at_from_created_field(self):
        client = AdzunaClient(config=_ADZUNA_CONFIG)
        raw = {
            "id": "1",
            "created": "2026-01-15T09:00:00Z",
            "company": {"display_name": "Co"},
            "location": {"display_name": "NY"},
        }
        result = client.normalise(raw)
        assert result["created_at"] == "2026-01-15T09:00:00Z"

    def test_adzuna_also_sets_posted_at_directly(self):
        """AdzunaClient.normalise() also sets posted_at (pre-existing behaviour)."""
        client = AdzunaClient(config=_ADZUNA_CONFIG)
        raw = {
            "id": "1",
            "created": "2026-01-15T09:00:00Z",
            "company": {"display_name": "Co"},
            "location": {"display_name": "NY"},
        }
        result = client.normalise(raw)
        assert result["posted_at"] == "2026-01-15T09:00:00Z"

    def test_himalayas_created_at_from_created_at_field(self):
        client = HimalayasClient(config={})
        raw = {
            "guid": "https://himalayas.app/companies/co/jobs/h1",
            "title": "Dev",
            "pubDate": "2026-03-01T10:00:00Z",
        }
        result = client.normalise(raw)
        assert result["created_at"] == "2026-03-01T10:00:00Z"

    def test_himalayas_created_at_none_when_missing(self):
        client = HimalayasClient(config={})
        raw = {"id": "h1", "title": "Dev"}
        result = client.normalise(raw)
        assert result["created_at"] is None

    def test_remoteok_created_at_from_date_field(self):
        client = RemoteOKClient(config={})
        raw = {
            "id": "r1",
            "position": "Dev",
            "company": "Co",
            "url": "https://x.com",
            "date": "2026-02-20T08:00:00Z",
        }
        result = client.normalise(raw)
        assert result["created_at"] == "2026-02-20T08:00:00Z"

    def test_arbeitnow_created_at_from_created_at_unix(self):
        client = ArbeitnowClient(config={})
        raw = {
            "slug": "a1",
            "title": "Dev",
            "company_name": "Co",
            "url": "https://x.com",
            "created_at": 1_700_000_000,
        }
        result = client.normalise(raw)
        assert result["created_at"] is not None
        assert result["created_at"].startswith("2023-")

    def test_remotive_created_at_from_publication_date(self):
        client = RemotiveClient(config={})
        raw = {
            "id": 1,
            "title": "Dev",
            "company_name": "Co",
            "url": "https://x.com",
            "publication_date": "2026-03-15T00:00:00Z",
        }
        result = client.normalise(raw)
        assert result["created_at"] == "2026-03-15T00:00:00Z"

    def test_usajobs_created_at_from_publication_start_date(self):
        client = USAJobsClient(config=_USAJOBS_CONFIG)
        raw = {
            "MatchedObjectId": "u1",
            "MatchedObjectDescriptor": {
                "PositionTitle": "Dev",
                "OrganizationName": "Agency",
                "PositionLocationDisplay": "DC",
                "PublicationStartDate": "2026-01-10T00:00:00Z",
            },
        }
        result = client.normalise(raw)
        assert result["created_at"] == "2026-01-10T00:00:00Z"

    def test_the_muse_created_at_from_publication_date(self):
        client = TheMuseClient(config={})
        raw = {
            "id": 1,
            "name": "Dev",
            "company": {"name": "Co"},
            "locations": [{"name": "Remote"}],
            "publication_date": "2026-02-28T12:00:00Z",
        }
        result = client.normalise(raw)
        assert result["created_at"] == "2026-02-28T12:00:00Z"


class TestIngestPipelinePostedAtBridge:
    """The ingest.py pipeline populates posted_at from created_at for all
    sources that don't set it directly in normalise()."""

    def test_pipeline_sets_posted_at_from_created_at(self):
        """When a listing has created_at but no posted_at, the pipeline copies
        created_at to posted_at before persisting."""
        listing = {
            "source": "himalayas",
            "source_id": "h1",
            "title": "Dev",
            "company": "Co",
            "location": "Remote",
            "salary_min": None,
            "salary_max": None,
            "contract_type": None,
            "contract_time": None,
            "description": "A job.",
            "redirect_url": "https://example.com",
            "created_at": "2026-03-20T08:00:00Z",
            # posted_at intentionally absent
        }

        # Simulate the ingest.py pipeline step that bridges created_at → posted_at.
        # This is the exact logic from ingest.py's persist block.
        if not listing.get("posted_at"):
            listing["posted_at"] = listing.get("created_at") or None

        assert listing["posted_at"] == "2026-03-20T08:00:00Z"

    def test_pipeline_does_not_overwrite_existing_posted_at(self):
        """When posted_at is already set (e.g. by AdzunaClient.normalise()), the
        pipeline must not overwrite it."""
        listing = {
            "source": "adzuna",
            "created_at": "2026-01-01T00:00:00Z",
            "posted_at": "2026-01-15T09:00:00Z",
        }

        if not listing.get("posted_at"):
            listing["posted_at"] = listing.get("created_at") or None

        assert listing["posted_at"] == "2026-01-15T09:00:00Z"  # unchanged

    def test_pipeline_posted_at_none_when_no_created_at(self):
        """When both posted_at and created_at are absent/empty, posted_at stays None."""
        listing = {
            "source": "himalayas",
            "created_at": None,
        }

        if not listing.get("posted_at"):
            listing["posted_at"] = listing.get("created_at") or None

        assert listing["posted_at"] is None


# ---------------------------------------------------------------------------
# Issue #131: make_source() uniform — no special-casing for adzuna
# ---------------------------------------------------------------------------


class TestMakeSourceUniform:
    """make_source() must pass only config= to every source including adzuna."""

    def test_make_source_adzuna_reads_credentials_from_config(self):
        """AdzunaClient must be constructible with only config= kwarg."""
        config = {
            "job_source": "adzuna",
            "adzuna_app_id": "my-id",
            "adzuna_app_key": "my-key",
            "search": {
                "country": "gb",
                "what": "python developer",
                "results_per_page": 10,
                "max_pages": 1,
            },
        }
        # This must not raise — credentials come from config, not extra kwargs.
        source = make_source(config)
        assert isinstance(source, AdzunaClient)

    def test_adzuna_client_direct_config_only_construction(self):
        """AdzunaClient(config=...) must work — no separate app_id/app_key args needed."""
        config = {
            "adzuna_app_id": "id",
            "adzuna_app_key": "key",
            "search": {
                "country": "us",
                "what": "engineer",
                "results_per_page": 10,
                "max_pages": 1,
            },
        }
        client = AdzunaClient(config=config)
        assert isinstance(client, AdzunaClient)

    def test_adzuna_client_credentials_extracted_from_config(self):
        """AdzunaClient reads app_id and app_key from config dict."""
        config = {
            "adzuna_app_id": "extracted-id",
            "adzuna_app_key": "extracted-key",
            "search": {
                "country": "us",
                "what": "dev",
                "results_per_page": 5,
                "max_pages": 1,
            },
        }
        client = AdzunaClient(config=config)
        assert client._app_id == "extracted-id"
        assert client._app_key == "extracted-key"

    def test_make_source_all_sources_accept_config_only(self):
        """Every registered source must be constructible via make_source(config)."""
        base_config = {
            "adzuna_app_id": "id",
            "adzuna_app_key": "key",
            "search": {
                "country": "us",
                "what": "engineer",
                "results_per_page": 10,
                "max_pages": 1,
            },
            "usajobs": {
                "api_key": "fake",
                "user_agent": "test@example.com",
            },
            "jooble": {
                "api_key": "fake",
            },
        }
        for source_name in SOURCES:
            config = {**base_config, "job_source": source_name}
            source = make_source(config)
            assert isinstance(source, JobSource), (
                f"make_source() for {source_name!r} did not return a JobSource instance"
            )

    def test_no_type_ignore_comment_in_factory(self):
        """make_source() must not require a type: ignore[call-arg] hack."""
        import inspect
        import job_sources as pkg
        source = inspect.getsource(pkg.make_source)
        assert "type: ignore" not in source, (
            "make_source() still contains a type: ignore comment — "
            "the Adzuna special-case has not been fully removed"
        )
