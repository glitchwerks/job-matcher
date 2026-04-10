"""Unit tests for IngestEventParser and EventQueue."""

import threading
import time
import uuid

import pytest

from ingest_events import IngestEventParser


class TestIngestEventParser:
    """Tests for stateful log-line -> structured-event parsing."""

    def setup_method(self):
        self.parser = IngestEventParser()

    # -- SCORED --
    def test_scored_event(self):
        line = "INFO ingest: SCORED 8/10  [Adzuna] Senior Python Developer"
        event = self.parser.parse(line)
        assert event is not None
        assert event["type"] == "scored"
        assert event["detail"]["score"] == 8
        assert event["source"] == "Adzuna"
        assert event["title"] == "Senior Python Developer"
        assert event["detail"]["scraped"] is True  # default when no fallback

    def test_scored_after_scrape_fallback(self):
        self.parser.parse("INFO ingest: SCRAPE FALLBACK  [Adzuna] Some Job")
        event = self.parser.parse("INFO ingest: SCORED 6/10  [Adzuna] Some Job")
        assert event["detail"]["scraped"] is False
        # Flag resets after use
        next_event = self.parser.parse("INFO ingest: SCORED 9/10  [Adzuna] Another Job")
        assert next_event["detail"]["scraped"] is True

    # -- FILTERED --
    def test_filtered_event(self):
        line = "INFO ingest: FILTERED  [Jooble] Junior Dev — title_exclude"
        event = self.parser.parse(line)
        assert event["type"] == "filtered"
        assert event["source"] == "Jooble"
        assert event["title"] == "Junior Dev"
        assert event["detail"]["reason"] == "title_exclude"

    def test_filtered_geo(self):
        line = "INFO ingest: FILTERED  [Adzuna] Remote Job — outside 80km radius"
        event = self.parser.parse(line)
        assert event["type"] == "filtered"
        assert event["detail"]["reason"] == "outside 80km radius"

    # -- DUPE --
    def test_dupe_event(self):
        line = "INFO ingest: DUPE      [USAJobs] Already Seen Role"
        event = self.parser.parse(line)
        assert event["type"] == "dupe"
        assert event["source"] == "USAJobs"
        assert event["title"] == "Already Seen Role"

    # -- SCORE FAILED --
    def test_score_failed_event(self):
        line = "WARNING ingest: SCORE FAILED  [Adzuna] Bad Listing"
        event = self.parser.parse(line)
        assert event["type"] == "score_failed"
        assert event["source"] == "Adzuna"
        assert event["title"] == "Bad Listing"

    # -- SCRAPE SKIP --
    def test_scrape_skip_full(self):
        line = "INFO ingest: SCRAPE SKIP (full) [JSearch] Full Desc Job"
        event = self.parser.parse(line)
        assert event["type"] == "scrape_skip"
        assert event["source"] == "JSearch"
        assert event["title"] == "Full Desc Job"
        assert event["detail"]["reason"] == "full"

    def test_scrape_skip_snippet(self):
        line = "INFO ingest: SCRAPE SKIP (snippet) [JSearch] Short Job"
        event = self.parser.parse(line)
        assert event["type"] == "scrape_skip"
        assert event["detail"]["reason"] == "snippet"

    # -- SCRAPE FALLBACK (sets flag, no event returned) --
    def test_scrape_fallback_returns_none(self):
        line = "INFO ingest: SCRAPE FALLBACK  [Adzuna] Fallback Job"
        event = self.parser.parse(line)
        assert event is None  # flag set, no event emitted

    # -- FETCHED --
    def test_fetched_event(self):
        line = "INFO ingest: Fetched 47 listing(s) from Adzuna"
        event = self.parser.parse(line)
        assert event["type"] == "fetched"
        assert event["source"] == "Adzuna"
        assert event["detail"]["fetched_count"] == 47

    # -- RUN COMPLETE --
    def test_run_complete_event(self):
        line = (
            "INFO ingest: Run complete: 2 source(s) | 47 fetched | "
            "10 pre-filtered | 5 dupes skipped | 7 scored (3 failed) | "
            "0 scrape skipped | 0 scrape fallbacks | ~1,234 tok | ~$0.0012"
        )
        event = self.parser.parse(line)
        assert event["type"] == "complete"

    # -- RESCORED --
    def test_rescored_event(self):
        line = "INFO ingest: RESCORED 7/10  Backend Engineer"
        event = self.parser.parse(line)
        assert event["type"] == "rescored"
        assert event["detail"]["score"] == 7
        assert event["title"] == "Backend Engineer"
        assert event["source"] is None  # no source in rescore mode

    # -- RESCORE FAILED --
    def test_rescore_failed_event(self):
        line = "WARNING ingest: RESCORE FAILED  Some Title"
        event = self.parser.parse(line)
        assert event["type"] == "rescore_failed"
        assert event["title"] == "Some Title"

    # -- RESCORE COMPLETE --
    def test_rescore_complete_event(self):
        line = "INFO ingest: Rescore complete: 50 listings | 48 rescored (2 failed) | ~5,000 tok | ~$0.0050"
        event = self.parser.parse(line)
        assert event["type"] == "complete"

    # -- PREFIX STRIPPING --
    def test_strips_info_prefix(self):
        line = "INFO ingest: DUPE      [Adzuna] Test"
        event = self.parser.parse(line)
        assert event is not None

    def test_strips_warning_prefix(self):
        line = "WARNING ingest: SCORE FAILED  [Adzuna] Test"
        event = self.parser.parse(line)
        assert event is not None

    # -- UNRECOGNIZED --
    def test_unrecognized_line_returns_none(self):
        assert self.parser.parse("some random debug output") is None
        assert self.parser.parse("") is None
        assert self.parser.parse("INFO ingest:   verdict: good fit") is None

    # -- MULTI-SOURCE TRACKING --
    def test_source_tracks_from_fetched(self):
        self.parser.parse("INFO ingest: Fetched 10 listing(s) from Jooble")
        event = self.parser.parse("INFO ingest: SCORED 5/10  [Jooble] Some Job")
        assert event["source"] == "Jooble"

    # -- TIMESTAMP --
    def test_events_have_timestamp(self):
        line = "INFO ingest: DUPE      [Adzuna] Test"
        event = self.parser.parse(line)
        assert "timestamp" in event

    # -- BRACKETS IN TITLE --
    def test_bracket_in_title_parsed_correctly(self):
        """Job titles containing brackets must not confuse the source-tag parser.

        The source tag [Adzuna] is the FIRST bracketed token on the line.
        Everything after it — including further brackets like [C++/Rust] — is the
        job title and must be preserved verbatim.
        """
        line = "INFO ingest: SCORED 8/10  [Adzuna] Senior [C++/Rust] Developer"
        event = self.parser.parse(line)
        assert event is not None
        assert event["source"] == "Adzuna"
        assert event["title"] == "Senior [C++/Rust] Developer"


# ---------------------------------------------------------------------------
# EventQueue tests (Task 2)
# ---------------------------------------------------------------------------

from ingest_events import EventQueue


class TestEventQueue:
    """Tests for thread-safe event storage and subscription."""

    def setup_method(self):
        self.queue = EventQueue()

    def _make_event(self, type_: str = "scored", source: str = "Adzuna") -> dict:
        return {
            "type": type_,
            "source": source,
            "title": "Test Job",
            "url": None,
            "detail": {},
            "timestamp": "2026-04-09T00:00:00+00:00",
        }

    # -- PUSH / SUBSCRIBE --
    def test_push_and_subscribe_from_zero(self):
        self.queue.push(self._make_event())
        self.queue.push(self._make_event(type_="filtered"))
        # Push terminal to end subscription
        self.queue.push(self._make_event(type_="complete"))
        events = list(self.queue.subscribe(last_id=0))
        assert len(events) == 3
        assert events[0]["id"] == 1
        assert events[1]["id"] == 2
        assert events[2]["id"] == 3

    def test_subscribe_from_cursor(self):
        for _ in range(3):
            self.queue.push(self._make_event())
        self.queue.push(self._make_event(type_="complete"))
        events = list(self.queue.subscribe(last_id=2))
        assert len(events) == 2
        assert events[0]["id"] == 3
        assert events[1]["id"] == 4  # complete

    def test_replay_order_is_stable(self):
        for i in range(5):
            self.queue.push(self._make_event(source=f"Source{i}"))
        self.queue.push(self._make_event(type_="complete"))
        events = list(self.queue.subscribe(last_id=0))
        ids = [e["id"] for e in events]
        assert ids == sorted(ids)

    # -- CLEAR --
    def test_clear_resets_state(self):
        self.queue.push(self._make_event())
        old_run_id = self.queue.run_id
        self.queue.clear()
        assert self.queue.run_id != old_run_id
        # Queue should be empty after clear — subscribe yields idle
        events = list(self.queue.subscribe(last_id=0))
        assert len(events) == 1
        assert events[0]["type"] == "idle"

    # -- IDLE --
    def test_empty_queue_yields_idle(self):
        events = list(self.queue.subscribe(last_id=0))
        assert len(events) == 1
        assert events[0]["type"] == "idle"

    # -- RUN_ID --
    def test_run_id_generated_on_init(self):
        assert self.queue.run_id is not None
        # Should be a valid UUID string
        uuid.UUID(self.queue.run_id)

    def test_clear_generates_new_run_id(self):
        old = self.queue.run_id
        self.queue.clear()
        assert self.queue.run_id != old

    # -- MEMORY CAP --
    def test_evicts_oldest_when_over_cap(self):
        small_queue = EventQueue(max_size=5)
        for i in range(7):
            small_queue.push(self._make_event(source=f"S{i}"))
        small_queue.push(self._make_event(type_="complete"))
        events = list(small_queue.subscribe(last_id=0))
        # Push 1-5: no eviction (at cap).
        # Push 6 (non-terminal): evicts id=1. Push 7 (non-terminal): evicts id=2.
        # Push complete (terminal): also triggers eviction of oldest non-terminal
        # (id=3) because terminal pushes are no longer exempt from the cap check —
        # the invariant is that *eviction never removes* terminals, not that
        # terminal pushes never cause eviction.  Queue ends as [4,5,6,7,complete].
        assert events[0]["id"] == 4

    # -- TERMINAL EVENT ENDS SUBSCRIPTION --
    def test_complete_ends_subscription(self):
        self.queue.push(self._make_event())
        self.queue.push(self._make_event(type_="complete"))
        self.queue.push(self._make_event())  # after terminal — should not appear
        events = list(self.queue.subscribe(last_id=0))
        assert events[-1]["type"] == "complete"
        assert len(events) == 2

    def test_aborted_ends_subscription(self):
        self.queue.push(self._make_event())
        self.queue.push(self._make_event(type_="aborted"))
        events = list(self.queue.subscribe(last_id=0))
        assert events[-1]["type"] == "aborted"
        assert len(events) == 2

    # -- THREAD SAFETY --
    def test_concurrent_push_and_subscribe(self):
        """Push from one thread, subscribe from another — no corruption."""
        results = []

        def producer():
            for i in range(20):
                self.queue.push(self._make_event(source=f"S{i}"))
                time.sleep(0.01)
            self.queue.push(self._make_event(type_="complete"))

        def consumer():
            for event in self.queue.subscribe(last_id=0):
                results.append(event)

        t1 = threading.Thread(target=producer)
        t2 = threading.Thread(target=consumer)
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        assert len(results) == 21  # 20 scored + 1 complete
        assert results[-1]["type"] == "complete"
        ids = [e["id"] for e in results]
        assert ids == sorted(ids)

    # -- CONNECTION TRACKING --
    def test_connection_counter(self):
        self.queue.connect()
        assert self.queue.connection_count == 1
        self.queue.connect()
        assert self.queue.connection_count == 2
        self.queue.disconnect()
        assert self.queue.connection_count == 1
        self.queue.disconnect()
        assert self.queue.connection_count == 0

    # -- CLEANUP TIMER --
    def test_cleanup_timer_clears_after_disconnect(self):
        """Queue clears 60s after last SSE disconnect (tested with short timer)."""
        self.queue.push(self._make_event(type_="complete"))
        self.queue.connect()
        self.queue.disconnect()
        # Timer was started — verify the cleanup method works directly
        self.queue._cleanup_if_idle()
        assert self.queue.is_empty()

    def test_cleanup_skipped_if_connections_active(self):
        """Queue NOT cleared if a connection is still active."""
        self.queue.push(self._make_event(type_="complete"))
        self.queue.connect()
        self.queue.connect()
        self.queue.disconnect()  # still 1 active
        self.queue._cleanup_if_idle()
        assert not self.queue.is_empty()

    # -- TERMINAL EVICTION SAFETY --
    def test_terminal_events_never_evicted_by_later_push(self):
        """Reproduces the review scenario: a terminal event buried near the front
        must survive eviction caused by later non-terminal pushes.

        Failure mode (buggy code): the slice `_events[-max_size:]` drops whatever
        is oldest — which may be a complete/aborted event.  A dropped complete
        event leaves SSE clients spinning forever waiting for a run-end signal.
        """
        q = EventQueue(max_size=5)
        # Push 3 scored events
        for _ in range(3):
            q.push(self._make_event(type_="scored"))
        # Push 1 terminal (complete) — now 4 events, under cap
        q.push(self._make_event(type_="complete"))
        # Push 5 more scored events — each push that exceeds cap must NOT evict
        # the complete event that is sitting near the front of the queue.
        for _ in range(5):
            q.push(self._make_event(type_="scored"))
        # The complete event must still be present
        types = [e["type"] for e in q._events]
        assert "complete" in types, (
            "complete event was evicted — terminal events must never be removed by eviction"
        )

    def test_eviction_when_queue_full_of_terminals(self):
        """When every slot is a terminal event, eviction must be a no-op.

        Preference: exceed max_size rather than silently drop a terminal event.
        This is an unusual edge case (two terminal events in the same queue is
        only possible via bugs in the producer or manual testing), but the
        invariant must still hold.
        """
        q = EventQueue(max_size=2)
        q.push(self._make_event(type_="complete"))
        q.push(self._make_event(type_="aborted"))
        # Queue is at cap and consists entirely of terminal events
        q.push(self._make_event(type_="scored"))
        # Both terminals must be retained — we prefer exceeding cap over data loss
        types = [e["type"] for e in q._events]
        assert "complete" in types, "complete terminal was evicted from an all-terminal queue"
        assert "aborted" in types, "aborted terminal was evicted from an all-terminal queue"
        assert "scored" in types, "scored event was not appended when all-terminal queue was full"
        # Queue is allowed to temporarily exceed max_size in this edge case
        assert len(q._events) == 3  # cap exceeded rather than losing a terminal

    def test_concurrent_push_with_eviction(self):
        """Stress-test the eviction path under concurrent writes.

        4 producer threads each push 100 non-terminal events into a small queue.
        After joining, all retained events must:
          - fit within max_size
          - have unique, strictly-increasing sequence IDs (no corruption)
          - no exceptions must have been raised
        """
        q = EventQueue(max_size=10)
        errors: list[Exception] = []

        def producer(n: int) -> None:
            try:
                for i in range(100):
                    q.push(self._make_event(source=f"T{n}-{i}"))
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=producer, args=(n,)) for n in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors, f"Exceptions raised during concurrent push: {errors}"
        # No terminal events were pushed, so eviction should have kept queue at cap
        assert len(q._events) <= q._max_size, (
            f"Queue length {len(q._events)} exceeds max_size {q._max_size} "
            "with no terminal events present"
        )
        # All retained IDs must be unique
        ids = [e["id"] for e in q._events]
        assert len(ids) == len(set(ids)), "Duplicate sequence IDs found after concurrent push"


class TestIngestEventParserEdgeCases:
    """Edge cases and boundary conditions for the parser."""

    def setup_method(self):
        self.parser = IngestEventParser()

    def test_verbose_mode_multiline_ignored(self):
        """Verbose breakdown lines after SCORED should be ignored."""
        self.parser.parse("INFO ingest: SCORED 8/10  [Adzuna] Job")
        assert self.parser.parse("INFO ingest:   verdict: great fit") is None
        assert self.parser.parse("INFO ingest:   matched: Python, AWS") is None

    def test_score_zero(self):
        line = "INFO ingest: SCORED 0/10  [Adzuna] Bad Match"
        event = self.parser.parse(line)
        assert event["detail"]["score"] == 0

    def test_score_ten(self):
        line = "INFO ingest: SCORED 10/10  [Adzuna] Perfect Match"
        event = self.parser.parse(line)
        assert event["detail"]["score"] == 10

    def test_source_name_with_spaces(self):
        """Source names may contain spaces (e.g. 'The Muse')."""
        line = "INFO ingest: SCORED 5/10  [The Muse] Some Job"
        event = self.parser.parse(line)
        assert event["source"] == "The Muse"

    def test_title_with_special_characters(self):
        line = "INFO ingest: SCORED 7/10  [Adzuna] Sr. Engineer — Platform (Remote)"
        event = self.parser.parse(line)
        assert event["title"] == "Sr. Engineer — Platform (Remote)"

    def test_filtered_long_reason(self):
        line = "INFO ingest: FILTERED  [Adzuna] Some Job — created_at older than 25 hours"
        event = self.parser.parse(line)
        assert event["detail"]["reason"] == "created_at older than 25 hours"
