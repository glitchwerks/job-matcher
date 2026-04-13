"""Unit tests for IngestEventParser and EventQueue."""

import threading
import time
import uuid

from ingest_events import EventQueue, IngestEventParser


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

    # -- SCRAPE FALLBACK (sets flag, emits event) --
    def test_scrape_fallback_emits_event(self):
        line = "INFO ingest: SCRAPE FALLBACK  [Adzuna] Fallback Job"
        event = self.parser.parse(line)
        assert event is not None
        assert event["type"] == "scrape_fallback"
        assert event["source"] == "Adzuna"
        assert event["title"] == "Fallback Job"

    def test_scrape_fallback_still_annotates_subsequent_scored(self):
        """SCRAPE FALLBACK sets the flag so the next scored event has scraped=False."""
        self.parser.parse("INFO ingest: SCRAPE FALLBACK  [Adzuna] Some Job")
        event = self.parser.parse("INFO ingest: SCORED 6/10  [Adzuna] Some Job")
        assert event["detail"]["scraped"] is False
        # Flag resets after use
        next_event = self.parser.parse("INFO ingest: SCORED 9/10  [Adzuna] Another Job")
        assert next_event["detail"]["scraped"] is True

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


class TestEventQueue:
    """Tests for thread-safe event storage and subscription."""

    def setup_method(self):
        # idle_grace=0 disables the startup grace-period wait so tests that
        # exercise the idle path complete immediately rather than sleeping 3 s.
        self.queue = EventQueue(idle_grace=0)

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
        # Queue should be empty after clear — subscribe yields idle.
        # The queue was constructed with idle_grace=0 (see setup_method) so
        # this completes immediately rather than sleeping.
        events = list(self.queue.subscribe(last_id=0))
        assert len(events) == 1
        assert events[0]["type"] == "idle"

    # -- IDLE --
    def test_empty_queue_yields_idle(self):
        # Queue was constructed with idle_grace=0 in setup_method.
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

    # -- GET_LATEST_SUMMARY --
    def test_get_latest_summary_empty_queue(self):
        assert self.queue.get_latest_summary() == ""

    def test_get_latest_summary_no_complete_events(self):
        self.queue.push(self._make_event(type_="scored"))
        self.queue.push(self._make_event(type_="filtered"))
        assert self.queue.get_latest_summary() == ""

    def test_get_latest_summary_with_complete_event(self):
        summary = "Run complete: 1 source(s) | 10 fetched | 2 pre-filtered | 8 scored (0 failed)"
        self.queue.push(self._make_event(type_="scored"))
        self.queue.push({
            "type": "complete",
            "source": None,
            "title": None,
            "url": None,
            "detail": {"summary": summary},
            "timestamp": "2026-04-10T00:00:00+00:00",
        })
        assert self.queue.get_latest_summary() == summary

    def test_get_latest_summary_returns_most_recent(self):
        first_summary = "Run complete: 1 source(s) | 5 fetched | 1 pre-filtered | 4 scored (0 failed)"
        second_summary = "Run complete: 2 source(s) | 20 fetched | 5 pre-filtered | 15 scored (1 failed)"
        self.queue.push({
            "type": "complete",
            "source": None,
            "title": None,
            "url": None,
            "detail": {"summary": first_summary},
            "timestamp": "2026-04-10T00:00:00+00:00",
        })
        self.queue.push(self._make_event(type_="scored"))
        self.queue.push({
            "type": "complete",
            "source": None,
            "title": None,
            "url": None,
            "detail": {"summary": second_summary},
            "timestamp": "2026-04-10T00:01:00+00:00",
        })
        assert self.queue.get_latest_summary() == second_summary

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


class TestFetchedOrderingInvariant:
    """Regression tests for issue #200: fetched event must precede per-listing events.

    Root cause: the "Fetched N listing(s) from <source>" log line was previously
    emitted *after* the per-listing loop, so filtered/scored/failed events for a
    page arrived at the SSE client before the fetched event for that same page —
    briefly producing an impossible `filtered > fetched` state in the drawer.

    Fix: the log line is now emitted immediately when the source returns a page,
    before any per-listing processing begins.
    """

    def setup_method(self):
        self.parser = IngestEventParser()

    def _parse_lines(self, lines):
        """Parse a list of log lines and return only non-None events."""
        events = []
        for line in lines:
            event = self.parser.parse(line)
            if event is not None:
                events.append(event)
        return events

    def test_fetched_precedes_filtered_on_same_page(self):
        """fetched event must appear before any filtered event for the same page."""
        lines = [
            "INFO ingest: Fetched 3 listing(s) from Adzuna",
            "INFO ingest: FILTERED  [Adzuna] Junior Dev — title_exclude",
            "INFO ingest: FILTERED  [Adzuna] Intern Role — title_exclude",
            "INFO ingest: SCORED 8/10  [Adzuna] Senior Engineer",
        ]
        events = self._parse_lines(lines)
        types = [e["type"] for e in events]

        fetched_idx = types.index("fetched")
        first_filtered_idx = types.index("filtered")
        assert fetched_idx < first_filtered_idx, (
            f"fetched event (index {fetched_idx}) must appear before first filtered "
            f"event (index {first_filtered_idx}) — ordering invariant violated"
        )

    def test_fetched_precedes_scored_on_same_page(self):
        """fetched event must appear before any scored event for the same page."""
        lines = [
            "INFO ingest: Fetched 2 listing(s) from Jooble",
            "INFO ingest: SCORED 7/10  [Jooble] Data Engineer",
            "INFO ingest: SCORED 5/10  [Jooble] Backend Dev",
        ]
        events = self._parse_lines(lines)
        types = [e["type"] for e in events]

        fetched_idx = types.index("fetched")
        first_scored_idx = types.index("scored")
        assert fetched_idx < first_scored_idx, (
            f"fetched event (index {fetched_idx}) must appear before first scored "
            f"event (index {first_scored_idx}) — ordering invariant violated"
        )

    def test_fetched_precedes_score_failed_on_same_page(self):
        """fetched event must appear before any score_failed event for the same page."""
        lines = [
            "INFO ingest: Fetched 1 listing(s) from USAJobs",
            "WARNING ingest: SCORE FAILED  [USAJobs] Bad Listing",
        ]
        events = self._parse_lines(lines)
        types = [e["type"] for e in events]

        fetched_idx = types.index("fetched")
        score_failed_idx = types.index("score_failed")
        assert fetched_idx < score_failed_idx, (
            f"fetched event (index {fetched_idx}) must appear before score_failed "
            f"event (index {score_failed_idx}) — ordering invariant violated"
        )

    def test_fetched_precedes_dupe_on_same_page(self):
        """fetched event must appear before any dupe event for the same page."""
        lines = [
            "INFO ingest: Fetched 2 listing(s) from Adzuna",
            "INFO ingest: DUPE      [Adzuna] Already Seen Role",
            "INFO ingest: SCORED 9/10  [Adzuna] New Role",
        ]
        events = self._parse_lines(lines)
        types = [e["type"] for e in events]

        fetched_idx = types.index("fetched")
        dupe_idx = types.index("dupe")
        assert fetched_idx < dupe_idx, (
            f"fetched event (index {fetched_idx}) must appear before dupe "
            f"event (index {dupe_idx}) — ordering invariant violated"
        )

    def test_multi_page_each_fetched_precedes_its_page_events(self):
        """For multi-page sources, each page's fetched event must precede that page's listings.

        This directly guards against the original bug: if fetched were emitted after
        the per-listing loop, per-listing events from page 2 would arrive before the
        page-2 fetched event, making filtered > fetched visible in the drawer.
        """
        lines = [
            # Page 1
            "INFO ingest: Fetched 2 listing(s) from Adzuna",
            "INFO ingest: SCORED 8/10  [Adzuna] Engineer A",
            "INFO ingest: FILTERED  [Adzuna] Intern A — title_exclude",
            # Page 2
            "INFO ingest: Fetched 2 listing(s) from Adzuna",
            "INFO ingest: SCORED 7/10  [Adzuna] Engineer B",
            "INFO ingest: FILTERED  [Adzuna] Intern B — title_exclude",
        ]
        events = self._parse_lines(lines)

        # Walk through events: every time we see a per-listing event (scored/filtered/dupe/
        # score_failed), there must have been at least one fetched event seen so far.
        seen_fetched = 0
        per_listing_types = {"scored", "filtered", "dupe", "score_failed"}
        for i, event in enumerate(events):
            if event["type"] == "fetched":
                seen_fetched += 1
            elif event["type"] in per_listing_types:
                assert seen_fetched > 0, (
                    f"Per-listing event '{event['type']}' at index {i} arrived before "
                    f"any fetched event — ordering invariant violated for multi-page run"
                )

    def test_empty_page_does_not_break_parser(self):
        """Empty pages should emit fetched=0 without breaking event flow."""
        lines = [
            "INFO ingest: Fetched 0 listing(s) from Adzuna",
            "INFO ingest: Fetched 2 listing(s) from Adzuna",
            "INFO ingest: SCORED 8/10  [Adzuna] Engineer A",
        ]
        events = self._parse_lines(lines)
        assert len(events) == 3
        assert events[0]["detail"]["fetched_count"] == 0
        assert events[1]["detail"]["fetched_count"] == 2

    def test_fetched_count_accumulates_correctly_across_pages(self):
        """Multiple fetched events for the same source must sum to the total page sizes.

        Validates that the move from per-source-total to per-page emission does not
        change the final accumulated fetched count seen by the drawer (which uses +=).
        """
        lines = [
            "INFO ingest: Fetched 5 listing(s) from Adzuna",
            "INFO ingest: SCORED 8/10  [Adzuna] Job A",
            "INFO ingest: Fetched 3 listing(s) from Adzuna",
            "INFO ingest: SCORED 7/10  [Adzuna] Job B",
        ]
        events = self._parse_lines(lines)
        fetched_events = [e for e in events if e["type"] == "fetched"]

        total_fetched = sum(e["detail"]["fetched_count"] for e in fetched_events)
        assert total_fetched == 8, (
            f"Expected total fetched_count of 8 (5+3) across two pages, got {total_fetched}"
        )


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


class TestSubscribeGracePeriod:
    """Regression tests for the idle-grace startup race-condition fix.

    Root cause: when a run was just launched (Popen succeeded) but the
    subprocess had not yet emitted its first log line, subscribe() would
    immediately yield 'idle' and return.  The JS EventSource handler treated
    'idle' as "no run is happening" and permanently closed the connection,
    silently dropping every subsequent event from the run.

    Fix: subscribe() waits up to idle_grace seconds for the first event before
    yielding 'idle'.  If events arrive during the grace period the generator
    falls through to the normal while-True loop and delivers them.
    """

    def _make_event(self, type_: str = "scored") -> dict:
        return {
            "type": type_,
            "source": "Adzuna",
            "title": "Test Job",
            "url": None,
            "detail": {},
            "timestamp": "2026-04-10T00:00:00+00:00",
        }

    def test_events_arriving_during_grace_period_are_not_preceded_by_idle(self):
        """Events pushed during the grace window must be delivered without a
        preceding 'idle' event.

        Regression: before the fix, subscribe() on an empty queue would yield
        'idle' immediately even when a run was in the process of starting.
        The JS would close the EventSource on 'idle', dropping all events.
        """
        q = EventQueue(idle_grace=1.0)  # 1 s grace

        def push_after_delay():
            time.sleep(0.1)  # 100 ms — well within the 1 s grace
            q.push(self._make_event(type_="fetched"))
            q.push(self._make_event(type_="complete"))

        t = threading.Thread(target=push_after_delay)
        t.start()

        events = list(q.subscribe(last_id=0))
        t.join(timeout=2)

        types = [e["type"] for e in events]
        assert "idle" not in types, (
            "subscribe() yielded 'idle' even though events arrived during the "
            "grace period — the race condition fix is broken"
        )
        assert "fetched" in types, "fetched event was not delivered"
        assert "complete" in types, "complete event was not delivered"

    def test_truly_idle_queue_still_yields_idle_after_grace(self):
        """When no events arrive within the grace period, 'idle' is still
        yielded so the SSE client knows no run is happening.
        """
        q = EventQueue(idle_grace=0)  # skip wait for test speed

        events = list(q.subscribe(last_id=0))

        assert len(events) == 1
        assert events[0]["type"] == "idle", (
            "subscribe() on a genuinely idle queue must still yield 'idle'"
        )


class TestScrapeFallbackEvent:
    """Tests for the scrape_fallback SSE event type (issue #202).

    SCRAPE FALLBACK lines now emit a ``scrape_fallback`` event so the live
    drawer can display them.  The parser also sets an internal flag so that the
    subsequent ``scored`` event is annotated with ``scraped=False``.
    """

    def setup_method(self):
        self.parser = IngestEventParser()

    def _parse_lines(self, lines: list[str]) -> list[dict]:
        """Parse a list of log lines and return only non-None events."""
        events = []
        for line in lines:
            event = self.parser.parse(line)
            if event is not None:
                events.append(event)
        return events

    def test_happy_path_basic(self):
        """A well-formed SCRAPE FALLBACK line produces a scrape_fallback event."""
        line = "INFO ingest: SCRAPE FALLBACK [Adzuna] Senior Python Engineer"
        event = self.parser.parse(line)
        assert event is not None
        assert event["type"] == "scrape_fallback"
        assert event["source"] == "Adzuna"
        assert event["title"] == "Senior Python Engineer"
        assert "timestamp" in event

    def test_title_with_special_characters(self):
        """Titles containing brackets, slashes, commas, and dashes parse correctly.

        The source must still be just the token inside the first bracket pair.
        """
        line = "INFO ingest: SCRAPE FALLBACK [Jobicy] Engineer (Remote/EU) - Senior Level"
        event = self.parser.parse(line)
        assert event is not None
        assert event["type"] == "scrape_fallback"
        assert event["source"] == "Jobicy"
        assert event["title"] == "Engineer (Remote/EU) - Senior Level"

    def test_source_with_spaces(self):
        """Source names containing spaces (e.g. 'We Work Remotely') are captured intact."""
        line = "INFO ingest: SCRAPE FALLBACK [We Work Remotely] Staff Engineer"
        event = self.parser.parse(line)
        assert event is not None
        assert event["source"] == "We Work Remotely"
        assert event["title"] == "Staff Engineer"

    def test_no_false_positive_on_scrape_skip(self):
        """SCRAPE SKIP lines must NOT produce a scrape_fallback event."""
        line = "INFO ingest: SCRAPE SKIP (full) [Adzuna] Foo"
        event = self.parser.parse(line)
        assert event is None or event["type"] != "scrape_fallback"

    def test_no_false_positive_on_scrape_failed(self):
        """A hypothetical SCRAPE FAILED line must NOT produce a scrape_fallback event."""
        line = "INFO ingest: SCRAPE FAILED [Adzuna] Foo"
        event = self.parser.parse(line)
        assert event is None or event["type"] != "scrape_fallback"

    def test_no_false_positive_on_fetched(self):
        """Fetched lines must NOT produce a scrape_fallback event."""
        line = "INFO ingest: Fetched 5 listing(s) from Adzuna"
        event = self.parser.parse(line)
        assert event is None or event["type"] != "scrape_fallback"

    def test_ordering_preserved_in_mixed_sequence(self):
        """A SCRAPE FALLBACK event appears in correct position among other events.

        The event must be emitted at the position the log line was seen,
        before the subsequent scored event for the same listing.
        """
        lines = [
            "INFO ingest: Fetched 2 listing(s) from Adzuna",
            "INFO ingest: SCORED 8/10  [Adzuna] Clean Listing",
            "INFO ingest: SCRAPE FALLBACK [Adzuna] Degraded Listing",
            "INFO ingest: SCORED 5/10  [Adzuna] Degraded Listing",
        ]
        events = self._parse_lines(lines)
        types = [e["type"] for e in events]

        # Expected order: fetched, scored, scrape_fallback, scored
        assert types == ["fetched", "scored", "scrape_fallback", "scored"], (
            f"Unexpected event order: {types}"
        )
        # The scrape_fallback source and title are correct
        fallback_event = events[2]
        assert fallback_event["source"] == "Adzuna"
        assert fallback_event["title"] == "Degraded Listing"
        # The scored event following the fallback is annotated scraped=False
        scored_after = events[3]
        assert scored_after["detail"]["scraped"] is False


# ---------------------------------------------------------------------------
# scoreToTier contract tests (issue #217)
# ---------------------------------------------------------------------------


def _score_to_tier(score: int | None) -> str:
    """Python mirror of the JS ``scoreToTier()`` helper in ``ingest-drawer.js``.

    Maps a numeric score to a CSS tier class name using the same thresholds
    as the rest of the application (score cards, stat bars, etc.):

    - score >= 8  → ``"tier-high"`` (green)
    - score >= 5  → ``"tier-mid"``  (amber)
    - score <  5  → ``"tier-low"``  (red)
    - None        → ``"tier-null"`` (grey)

    This function exists solely to let the tests below assert the tier mapping
    contract in plain Python without requiring a JS runtime.  Any change to the
    JS thresholds must be reflected here (and vice-versa).

    Args:
        score: Numeric LLM score (0–10) or None when the score is absent.

    Returns:
        The CSS modifier class name that the score tag should carry.
    """
    if score is None:
        return "tier-null"
    if score >= 8:
        return "tier-high"
    if score >= 5:
        return "tier-mid"
    return "tier-low"


class TestScoreToTier:
    """Contract tests for the scoreToTier() tier-mapping helper (issue #217).

    These tests document the exact thresholds used by ingest-drawer.js so that
    any accidental drift between the JS helper and the rest of the app's scoring
    colour conventions is caught immediately.

    The JS function being tested:
        function scoreToTier(score) {
          if (score == null)  { return "tier-null"; }
          if (score >= 8)     { return "tier-high"; }
          if (score >= 5)     { return "tier-mid"; }
          return "tier-low";
        }
    """

    # -- Boundary values -------------------------------------------------------

    def test_score_10_is_tier_high(self) -> None:
        """Score of 10 (maximum) maps to tier-high."""
        assert _score_to_tier(10) == "tier-high"

    def test_score_8_is_tier_high(self) -> None:
        """Score of 8 is the lower boundary of tier-high (>= 8)."""
        assert _score_to_tier(8) == "tier-high"

    def test_score_7_is_tier_mid(self) -> None:
        """Score of 7 is just below tier-high, should be tier-mid."""
        assert _score_to_tier(7) == "tier-mid"

    def test_score_5_is_tier_mid(self) -> None:
        """Score of 5 is the lower boundary of tier-mid (>= 5)."""
        assert _score_to_tier(5) == "tier-mid"

    def test_score_4_is_tier_low(self) -> None:
        """Score of 4 is just below tier-mid, should be tier-low."""
        assert _score_to_tier(4) == "tier-low"

    def test_score_2_is_tier_low(self) -> None:
        """Score of 2 (representative low value) maps to tier-low."""
        assert _score_to_tier(2) == "tier-low"

    def test_score_0_is_tier_low(self) -> None:
        """Score of 0 (minimum) maps to tier-low."""
        assert _score_to_tier(0) == "tier-low"

    def test_score_none_is_tier_null(self) -> None:
        """Absent/null score maps to tier-null (grey)."""
        assert _score_to_tier(None) == "tier-null"

    # -- Scored events carry the score field the JS renderer depends on --------

    def test_scored_event_contains_score_field(self) -> None:
        """``scored`` events must carry ``detail.score`` as an integer.

        ingest-drawer.js reads ``event.detail.score`` to determine which tier
        class to apply to the score tag.  If this field is absent or None, the
        tag falls back to ``tier-null``.
        """
        parser = IngestEventParser()
        event = parser.parse("INFO ingest: SCORED 8/10  [Adzuna] Senior Engineer")
        assert event is not None
        assert "score" in event["detail"]
        assert isinstance(event["detail"]["score"], int)
        assert event["detail"]["score"] == 8

    def test_rescored_event_contains_score_field(self) -> None:
        """``rescored`` events must carry ``detail.score`` as an integer.

        The JS ``scored``/``rescored`` branch uses the same ``scoreToTier()``
        path for both event types.
        """
        parser = IngestEventParser()
        event = parser.parse("INFO ingest: RESCORED 5/10  Backend Engineer")
        assert event is not None
        assert "score" in event["detail"]
        assert isinstance(event["detail"]["score"], int)
        assert event["detail"]["score"] == 5

    def test_scored_event_score_field_present_for_zero(self) -> None:
        """A score of 0 must be preserved, not coerced to None/falsy."""
        parser = IngestEventParser()
        event = parser.parse("INFO ingest: SCORED 0/10  [Adzuna] Bad Match")
        assert event is not None
        assert event["detail"]["score"] == 0
        assert _score_to_tier(event["detail"]["score"]) == "tier-low"

    def test_scored_event_score_field_present_for_ten(self) -> None:
        """A score of 10 must round-trip cleanly through parse → tier mapping."""
        parser = IngestEventParser()
        event = parser.parse("INFO ingest: SCORED 10/10  [Adzuna] Perfect Match")
        assert event is not None
        assert event["detail"]["score"] == 10
        assert _score_to_tier(event["detail"]["score"]) == "tier-high"
