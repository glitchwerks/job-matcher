"""
tests/test_plugin_isolation.py — Unit tests for ingest._safe_pages().

Regression tests for issue #193: a plugin that raises an unhandled exception
must not abort the entire ingest run.  The isolation wrapper should:

  - Log an ERROR with the full traceback (exc_info=True) for the crashing plugin
  - Stop iteration for the crashing source (no partial listings yielded after crash)
  - Allow the outer for-loop to continue to the next source

These tests are DB-free: they exercise _safe_pages() directly without calling
run() or touching the database.
"""

from __future__ import annotations

import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import ingest
from job_sources.base import JobSource


# ---------------------------------------------------------------------------
# Helper stubs
# ---------------------------------------------------------------------------

class _GoodSource(JobSource):
    """Plugin that yields two pages of one listing each without errors."""

    SOURCE = "good_source"

    def fetch_page(self, page: int) -> list[dict]:
        return [{"source": self.SOURCE, "source_id": str(page)}]

    def total_pages(self) -> int:
        return 2

    def normalise(self, raw: dict) -> dict:
        return raw

    @classmethod
    def settings_schema(cls) -> dict:
        return {"display_name": "Good", "fields": []}

    def pages(self):
        yield [{"source": self.SOURCE, "source_id": "1"}]
        yield [{"source": self.SOURCE, "source_id": "2"}]


class _CrashingSource(JobSource):
    """Plugin whose pages() raises RuntimeError immediately."""

    SOURCE = "crashing_source"

    def fetch_page(self, page: int) -> list[dict]:
        return []

    def total_pages(self) -> int:
        return 1

    def normalise(self, raw: dict) -> dict:
        return raw

    @classmethod
    def settings_schema(cls) -> dict:
        return {"display_name": "Crashing", "fields": []}

    def pages(self):
        raise RuntimeError("Simulated plugin crash — no API key")
        yield  # make it a generator


class _CrashMidwaySource(JobSource):
    """Plugin that yields one page then raises an exception."""

    SOURCE = "crashmid_source"

    def fetch_page(self, page: int) -> list[dict]:
        return []

    def total_pages(self) -> int:
        return 1

    def normalise(self, raw: dict) -> dict:
        return raw

    @classmethod
    def settings_schema(cls) -> dict:
        return {"display_name": "CrashMid", "fields": []}

    def pages(self):
        yield [{"source": self.SOURCE, "source_id": "before-crash"}]
        raise ValueError("Simulated mid-iteration crash")


# ---------------------------------------------------------------------------
# Tests: _safe_pages() on a well-behaved source
# ---------------------------------------------------------------------------

class TestSafePagesHappyPath:
    def test_yields_all_pages_when_plugin_is_clean(self):
        """_safe_pages() passes through all pages from a non-crashing plugin."""
        client = _GoodSource()
        pages = list(ingest._safe_pages(client))
        assert len(pages) == 2
        assert pages[0] == [{"source": "good_source", "source_id": "1"}]
        assert pages[1] == [{"source": "good_source", "source_id": "2"}]

    def test_empty_plugin_yields_nothing(self):
        """_safe_pages() yields nothing when pages() yields nothing."""

        class _EmptySource(_GoodSource):
            SOURCE = "empty_source"

            def pages(self):
                return iter([])

        pages = list(ingest._safe_pages(_EmptySource()))
        assert pages == []


# ---------------------------------------------------------------------------
# Tests: _safe_pages() on a crashing source
# ---------------------------------------------------------------------------

class TestSafePagesIsolation:
    def test_exception_is_caught_and_generator_stops(self):
        """_safe_pages() catches RuntimeError from pages() and stops iteration cleanly."""
        client = _CrashingSource()
        pages = list(ingest._safe_pages(client))
        assert pages == [], "No pages should be yielded from a crashing plugin"

    def test_exception_is_logged_at_error_level(self, caplog):
        """_safe_pages() logs an ERROR with exc_info when a plugin crashes."""
        client = _CrashingSource()
        with caplog.at_level(logging.ERROR, logger="ingest"):
            list(ingest._safe_pages(client))

        assert any(
            record.levelno == logging.ERROR and "crashing_source" in record.message
            for record in caplog.records
        ), f"Expected ERROR log mentioning 'crashing_source'; got:\n{caplog.text}"

    def test_traceback_is_included_in_log_record(self, caplog):
        """The ERROR log record includes exc_info so the full traceback is emitted."""
        client = _CrashingSource()
        with caplog.at_level(logging.ERROR, logger="ingest"):
            list(ingest._safe_pages(client))

        error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert error_records, "No ERROR records found"
        assert error_records[0].exc_info is not None, (
            "exc_info must be set so the traceback appears in the log file"
        )
        # The exc_info tuple must contain the actual exception class.
        exc_type = error_records[0].exc_info[0]
        assert exc_type is RuntimeError

    def test_mid_iteration_crash_stops_after_crash(self):
        """_safe_pages() yields pages before the crash and stops after it."""
        client = _CrashMidwaySource()
        pages = list(ingest._safe_pages(client))
        # Only the page yielded before the crash should be present.
        assert len(pages) == 1
        assert pages[0] == [{"source": "crashmid_source", "source_id": "before-crash"}]

    def test_mid_iteration_crash_is_logged(self, caplog):
        """_safe_pages() logs an ERROR for a mid-iteration crash."""
        client = _CrashMidwaySource()
        with caplog.at_level(logging.ERROR, logger="ingest"):
            list(ingest._safe_pages(client))

        assert any(
            record.levelno == logging.ERROR and "crashmid_source" in record.message
            for record in caplog.records
        )

    def test_loop_continues_to_next_source_after_crash(self):
        """A crashing plugin does not prevent subsequent sources from running.

        This is the regression test for issue #193: one failing plugin must not
        abort the entire ingest run.
        """
        crashing = _CrashingSource()
        good = _GoodSource()

        # Simulate the outer for-client-in-sources loop.
        all_pages = []
        for client in [crashing, good]:
            for page in ingest._safe_pages(client):
                all_pages.append(page)

        # crashing_source yields nothing; good_source yields 2 pages.
        assert len(all_pages) == 2, (
            "good_source pages were not reached after crashing_source failed"
        )

    def test_different_exception_types_are_caught(self):
        """_safe_pages() catches any BaseException subclass, not just RuntimeError."""
        for exc_class in (ValueError, KeyError, AttributeError, TypeError):
            class _DynCrash(_GoodSource):
                SOURCE = "dyn_crash"
                _exc = exc_class

                def pages(self):
                    raise self._exc("boom")
                    yield

            pages = list(ingest._safe_pages(_DynCrash()))
            assert pages == [], f"Expected empty pages for {exc_class.__name__} crash"
