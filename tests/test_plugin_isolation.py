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


# ---------------------------------------------------------------------------
# Tests: _safe_pages() catches SystemExit (regression for silent process kill)
# ---------------------------------------------------------------------------

class TestSafePagesSystemExit:
    """SystemExit raised inside a plugin must be caught by _safe_pages so the
    outer sources loop can continue.  Before the fix, SystemExit (a BaseException,
    not an Exception) would propagate straight through the ``except Exception:``
    clause and silently kill the process with no traceback logged.
    """

    def _make_exit_source(self, code: int):
        """Return a JobSource whose pages() calls sys.exit(code)."""
        class _ExitSource(_GoodSource):
            SOURCE = "exit_source"
            _exit_code = code

            def pages(self):
                import sys
                sys.exit(self._exit_code)
                yield  # make it a generator

        return _ExitSource()

    def test_sys_exit_does_not_propagate(self):
        """_safe_pages() must not let SystemExit escape — process must survive."""
        client = self._make_exit_source(1)
        # If SystemExit propagates, list() will raise and pytest will mark this
        # test as an error rather than a failure.
        pages = list(ingest._safe_pages(client))
        assert pages == [], "No pages should be yielded when a plugin calls sys.exit()"

    def test_sys_exit_is_logged_at_error_level(self, caplog):
        """_safe_pages() must log an ERROR that includes the exit code."""
        client = self._make_exit_source(1)
        with caplog.at_level(logging.ERROR, logger="ingest"):
            list(ingest._safe_pages(client))

        assert any(
            record.levelno == logging.ERROR and "exit_source" in record.message
            for record in caplog.records
        ), f"Expected ERROR log mentioning 'exit_source'; got:\n{caplog.text}"

    def test_sys_exit_log_includes_exit_code(self, caplog):
        """The ERROR log must include the exit code so the cause is diagnosable."""
        client = self._make_exit_source(1)
        with caplog.at_level(logging.ERROR, logger="ingest"):
            list(ingest._safe_pages(client))

        error_msgs = [r.message for r in caplog.records if r.levelno == logging.ERROR]
        assert error_msgs, "No ERROR records found"
        # The exit code (1) must appear in the message.
        assert any("1" in msg for msg in error_msgs), (
            f"Exit code not mentioned in ERROR message. Got: {error_msgs}"
        )

    def test_sys_exit_traceback_is_included(self, caplog):
        """The ERROR record must have exc_info set so the full traceback is emitted."""
        client = self._make_exit_source(1)
        with caplog.at_level(logging.ERROR, logger="ingest"):
            list(ingest._safe_pages(client))

        error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert error_records, "No ERROR records found"
        assert error_records[0].exc_info is not None, (
            "exc_info must be set so the traceback appears in the log file"
        )
        exc_type = error_records[0].exc_info[0]
        assert exc_type is SystemExit

    def test_loop_continues_after_sys_exit(self):
        """A plugin that calls sys.exit() must not prevent subsequent sources."""
        exit_source = self._make_exit_source(1)
        good = _GoodSource()

        all_pages = []
        for client in [exit_source, good]:
            for page in ingest._safe_pages(client):
                all_pages.append(page)

        # exit_source yields nothing; good_source yields 2 pages.
        assert len(all_pages) == 2, (
            "good_source pages were not reached after exit_source called sys.exit()"
        )


# ---------------------------------------------------------------------------
# Tests: _safe_pages() re-raises GeneratorExit (PEP 479)
# ---------------------------------------------------------------------------

class TestSafePagesGeneratorExitReraise:
    """GeneratorExit must propagate out of _safe_pages unchanged.

    GeneratorExit is NOT a plugin error — it is raised when the consumer
    breaks out of the loop early (e.g. ``break`` or an exception in the
    outer for-body).  Before this fix, ``except BaseException:`` swallowed
    it and logged a spurious "raised an unhandled exception" message.
    """

    def test_generator_exit_propagates(self):
        """Breaking out of the _safe_pages loop must not raise GeneratorExit
        to the caller — Python's generator protocol handles it internally.
        After a break, iteration simply stops with no exception escaping.
        """
        client = _GoodSource()
        gen = ingest._safe_pages(client)
        # Consume one page then close the generator (simulates a break).
        first = next(gen)
        assert first == [{"source": "good_source", "source_id": "1"}]
        # Explicitly closing a generator triggers GeneratorExit internally.
        # It must not propagate to the caller as an unhandled exception.
        gen.close()  # Would raise if GeneratorExit were re-raised to the caller.

    def test_generator_exit_does_not_log_unhandled_exception(self, caplog):
        """Breaking out of the outer loop must NOT produce an ERROR log
        claiming the plugin raised an unhandled exception.
        """
        client = _GoodSource()
        with caplog.at_level(logging.ERROR, logger="ingest"):
            gen = ingest._safe_pages(client)
            next(gen)  # get first page
            gen.close()  # close early

        error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
        spurious = [
            r for r in error_records
            if "unhandled exception" in r.message or "raised an unhandled" in r.message
        ]
        assert not spurious, (
            "GeneratorExit from early loop exit must NOT produce an 'unhandled exception' "
            f"error log.  Got: {[r.message for r in spurious]}"
        )


# ---------------------------------------------------------------------------
# Tests: per-listing failure isolation in the run() inner loop
# ---------------------------------------------------------------------------

class TestListingFailureIsolation:
    """A per-listing pipeline failure must be caught, logged, and skipped —
    not abort the entire ingest run.

    These tests drive the inner loop body directly via run() with heavy
    monkeypatching so we do not need a real database or LLM provider.
    """

    def _make_run_args(self, tmp_path):
        """Write minimal config/profile/providers JSON files into tmp_path."""
        import json

        config = {
            "search": {
                "country": "gb",
                "what": "software engineer",
                "where": "london",
                "max_days_old": 1,
                "max_pages": 1,
                "results_per_page": 10,
            },
            "scoring": {
                "threshold": 6,
            },
        }
        profile = {
            "primary_skills": [],
            "anti_preferences": [],
            "seniority": "",
            "preferred_industries": [],
            "scoring_notes": "",
        }
        providers = {
            "llm": {},
            "provider_order": [],
            "job_sources": {},
        }

        config_path = str(tmp_path / "config.json")
        profile_path = str(tmp_path / "profile.json")
        providers_path = str(tmp_path / "providers.json")

        with open(config_path, "w") as f:
            json.dump(config, f)
        with open(profile_path, "w") as f:
            json.dump(profile, f)
        with open(providers_path, "w") as f:
            json.dump(providers, f)

        return config_path, profile_path, providers_path

    def test_one_bad_listing_does_not_abort_run(self, tmp_path, monkeypatch, caplog):
        """A ValueError from prefilter() on one listing must not stop the run.

        The bad listing should produce a LISTING FAILED log; the next listing
        in the same page must still be processed (reaching score_listing_with_fallback).
        """
        import ingest as ing

        config_path, profile_path, providers_path = self._make_run_args(tmp_path)

        # Two listings: first raises, second is fine.
        listing_bad  = {"source": "test_src", "source_id": "1", "title": "Bad Listing",
                        "redirect_url": "http://example.com/1", "description": "desc"}
        listing_good = {"source": "test_src", "source_id": "2", "title": "Good Listing",
                        "redirect_url": "http://example.com/2", "description": "desc"}

        call_count = {"n": 0}

        def _bad_prefilter(listing, config):
            call_count["n"] += 1
            if listing["source_id"] == "1":
                raise ValueError("Injected per-listing failure")
            return None  # don't filter the good listing

        processed_titles = []

        def _fake_score(listing, profile, chain, dead_providers):
            processed_titles.append(listing["title"])
            return None  # score failure is fine; we just want to know it was reached

        # Stub everything that touches I/O.
        monkeypatch.setattr(ing, "prefilter", _bad_prefilter)
        monkeypatch.setattr(ing, "score_listing_with_fallback", _fake_score)
        monkeypatch.setattr(ing.db, "init_db", lambda: None)
        monkeypatch.setattr(ing.db, "get_connection", lambda: _FakeConnection())
        monkeypatch.setattr(ing.db, "listing_exists", lambda conn, src, sid: False)
        monkeypatch.setattr(ing.db, "listing_exists_by_url", lambda conn, url: False)
        monkeypatch.setattr(ing.db, "insert_listing", lambda listing: None)
        monkeypatch.setattr(ing, "scrape_description",
                            lambda url, fallback="": (fallback, False))
        monkeypatch.setattr(ing, "make_enabled_sources",
                            lambda providers, config: [_SinglePageSource([listing_bad, listing_good])])
        monkeypatch.setattr(ing, "build_provider_chain", lambda providers: [])
        monkeypatch.setattr(ing, "load_providers",
                            lambda **kw: {"llm": {}, "provider_order": [], "job_sources": {}})
        monkeypatch.setattr(
            "job_sources.auto_register.ensure_plugins_registered",
            lambda path: None,
        )

        with caplog.at_level(logging.ERROR, logger="ingest"):
            ing.run(
                config_path=config_path,
                profile_path=profile_path,
                providers_path=providers_path,
            )

        # The bad listing must produce a LISTING FAILED error log.
        failed_records = [
            r for r in caplog.records
            if r.levelno == logging.ERROR and "LISTING FAILED" in r.message
        ]
        assert failed_records, (
            f"Expected a 'LISTING FAILED' ERROR log; got:\n{caplog.text}"
        )
        assert "Bad Listing" in failed_records[0].message

        # The good listing must have been scored (pipeline continued past the error).
        assert "Good Listing" in processed_titles, (
            "Good listing was not processed after bad listing raised an exception"
        )

    def test_listing_failed_counter_in_summary(self, tmp_path, monkeypatch, caplog):
        """The run-complete summary must include a non-zero listing error count."""
        import ingest as ing

        config_path, profile_path, providers_path = self._make_run_args(tmp_path)

        listing_bad = {"source": "test_src", "source_id": "1", "title": "Bad Listing",
                       "redirect_url": "http://example.com/1", "description": "desc"}

        def _bad_prefilter(listing, config):
            raise RuntimeError("Injected crash")

        monkeypatch.setattr(ing, "prefilter", _bad_prefilter)
        monkeypatch.setattr(ing, "score_listing_with_fallback", lambda **kw: None)
        monkeypatch.setattr(ing.db, "init_db", lambda: None)
        monkeypatch.setattr(ing.db, "get_connection", lambda: _FakeConnection())
        monkeypatch.setattr(ing.db, "listing_exists", lambda conn, src, sid: False)
        monkeypatch.setattr(ing.db, "listing_exists_by_url", lambda conn, url: False)
        monkeypatch.setattr(ing.db, "insert_listing", lambda listing: None)
        monkeypatch.setattr(ing, "scrape_description",
                            lambda url, fallback="": (fallback, False))
        monkeypatch.setattr(ing, "make_enabled_sources",
                            lambda providers, config: [_SinglePageSource([listing_bad])])
        monkeypatch.setattr(ing, "build_provider_chain", lambda providers: [])
        monkeypatch.setattr(ing, "load_providers",
                            lambda **kw: {"llm": {}, "provider_order": [], "job_sources": {}})
        monkeypatch.setattr(
            "job_sources.auto_register.ensure_plugins_registered",
            lambda path: None,
        )

        with caplog.at_level(logging.INFO, logger="ingest"):
            ing.run(
                config_path=config_path,
                profile_path=profile_path,
                providers_path=providers_path,
            )

        summary_records = [
            r for r in caplog.records
            if "Run complete" in r.message
        ]
        assert summary_records, "No 'Run complete' log found"
        # The summary should mention "1 listing error(s)"
        assert "1 listing error" in summary_records[0].message, (
            f"Expected '1 listing error' in summary; got: {summary_records[0].message}"
        )


# ---------------------------------------------------------------------------
# Helper stubs for TestListingFailureIsolation
# ---------------------------------------------------------------------------

class _FakeConnection:
    """Minimal context-manager stub for db.get_connection()."""

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class _SinglePageSource(_GoodSource):
    """A JobSource that yields one page containing the given listings."""

    SOURCE = "test_src"

    def __init__(self, listings):
        self._listings = listings

    def pages(self):
        yield list(self._listings)
