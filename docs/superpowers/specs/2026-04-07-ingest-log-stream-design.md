# Live Ingest Log Stream — Design Specification

**Date:** 2026-04-07
**Status:** Revised (post-review)

---

## Summary

Add real-time streaming of ingest pipeline events to the web UI via a slide-out drawer. Users can watch the ingest run as it happens — seeing which listings are fetched, filtered, scored, or failed — with a rolling tally and per-source breakdown.

---

## Motivation

Currently, running `ingest.py` shows only a "Running..." spinner with 2-second HTMX polling. Users have no visibility into what's happening until the run completes. This makes it hard to spot issues (bad filters, provider timeouts, scrape failures) in real time. The log stream gives immediate feedback, and the structured event format makes it easy to scan for problems.

---

## Design Decisions

| Decision | Choice | Alternatives Considered |
|---|---|---|
| Transport | Server-Sent Events (SSE) | WebSocket (overkill for one-way), Enhanced polling (latency, wasted requests) |
| Log parsing | Parse stdout in `app.py` (Approach A) | Structured JSON from `ingest.py` (larger change), File tailing (Windows issues) |
| UI placement | Slide-out drawer from right edge | Inline panel (disrupts layout), Dedicated page (requires navigation) |
| Log scope | Live only (current run) | Live + last run, Full history (more complexity, less value for v1) |
| Page navigation | Reconnect + replay via `Last-Event-ID` cursor | Scope to feed only, `hx-boost` partial navigation (future) |
| Event format | Structured typed events with source attribution | Raw passthrough (poor UX), Lightly formatted (limited interactivity) |
| Thread pool constraint | **waitress uses a fixed thread pool.** Each SSE connection holds a worker thread for the duration of the ingest run. Since this is currently a single-user application, this is acceptable. A connection limit of 2 SSE connections (`max_sse_connections = 2`) is enforced at the endpoint level (return 429 if exceeded). If the application becomes multi-user in the future, this should be revisited — options include switching to short-polling, using an async server for the SSE endpoint, or moving to a dedicated event service. | Async server (larger infrastructure change), short-polling (higher latency) |

---

## Architecture

### Data Flow

```
ingest.py (subprocess stdout)
    │ raw log lines
    ▼
StdoutReader thread (app.py)
    │ reads line-by-line, strips logging prefix
    ▼
IngestEventParser (ingest_events.py)
    │ regex parsing → structured events
    ▼
EventQueue (ingest_events.py)
    │ thread-safe list, holds all events from current run
    ▼
GET /ingest/stream (SSE endpoint)
    │ yields events as text/event-stream
    ▼
Browser EventSource (via HTMX sse extension)
    │ receives events, renders in drawer
    ▼
Slide-out drawer with rolling tally + per-source breakdown
```

---

## New Module: `ingest_events.py`

Contains all event parsing and queue logic. No other module needs to know about log line formats.

### IngestEventParser

Stateful parser (tracks current source, scrape fallback flag).

- **Input:** raw stdout line (string)
- **Output:** structured event dict, or `None` for unparseable lines

**Log line format:** `%(levelname)s %(name)s: %(message)s`
Example: `INFO ingest: SCORED 7/10  [Adzuna] Senior Python Developer`

The parser strips the logging prefix (`INFO ingest: ` or `WARNING ingest: `) before matching. Lines that don't match any pattern are silently ignored — this handles verbose mode multi-line output, debug lines, etc.

**Patterns matched (message portion after stripping prefix):**

| Message pattern | Event type |
|---|---|
| `SCORED <score>/10  [<source>] <title>` | `scored` |
| `FILTERED  [<source>] <title> — <reason>` | `filtered` |
| `DUPE      [<source>] <title>` | `dupe` |
| `SCORE FAILED  [<source>] <title>` | `score_failed` |
| `SCRAPE FALLBACK  [<source>] <title>` | Sets flag; next `scored` event gets `scraped: false` |
| `SCRAPE SKIP      [<source>] <title>` | `scrape_skip` (source provided full description) |
| `Fetched <count> listing(s) from <source>` | `fetched` |
| `Run complete: <summary>` | `complete` |
| `RESCORED <score>/10  <title>` | `rescored` |
| `RESCORE FAILED  <title>` | `rescore_failed` |
| `Rescore complete: <summary>` | `complete` (rescore variant) |

### EventQueue

Thread-safe event store (single global instance shared between reader thread and SSE endpoint).

| Method | Behavior |
|---|---|
| `push(event)` | Appends event with auto-incrementing ID, sets the `threading.Event` to wake blocked subscribers |
| `subscribe(last_id=0)` | Generator yielding events from `last_id` onward. Blocks via `threading.Event.wait(timeout=1.0)` when caught up. Returns when it yields an event with type `complete` or `aborted`. If `wait()` times out and the queue has a terminal event (`complete`/`aborted`) that was already yielded, the generator returns. |
| `clear()` | Resets for a new ingest run; generates a new `run_id` (UUID) |

**When no ingest is running and the queue is empty,** `subscribe()` immediately yields a synthetic `idle` event and returns. The SSE endpoint checks queue state before calling subscribe.

**Memory management:**
- After a terminal event (`complete` or `aborted`) is pushed, a cleanup timer starts.
- 60 seconds after the last SSE connection disconnects (tracked via a connection counter), the queue is cleared.
- This prevents unbounded memory growth while still allowing reconnection/replay shortly after a run ends.
- Maximum queue size: 5000 events. If exceeded, oldest events are evicted (FIFO). This is a safety valve — typical runs produce ~500 events.

---

## Event Schema

All events share a common envelope. Fields marked as type-specific are only present on the listed event types.

```json
{
  "id": 1,
  "run_id": "a3f2c1d0-...",
  "type": "scored | filtered | dupe | score_failed | scrape_skip | fetched | complete | aborted | idle | rescored | rescore_failed",
  "source": "Adzuna | Jooble | USAJobs | ...",
  "title": "Senior Python Developer",
  "url": "https://...",
  "detail": {
    "score": 8.5,
    "matched_skills": ["Python", "AWS"],
    "scraped": true,
    "reason": "title_exclude",
    "fetched_count": 47,
    "page": 1,
    "total_pages": 3
  },
  "timestamp": "2026-04-07T14:23:01Z"
}
```

**`run_id`:** A UUID generated when `EventQueue.clear()` is called at the start of each run. Used by the SSE endpoint to detect stale reconnections (see SSE Connection Lifecycle).

**Type-specific fields in `detail`:**

| Field | Present on |
|---|---|
| `score`, `matched_skills`, `scraped` | `scored`, `rescored` |
| `reason` | `filtered`, `dupe` |
| `fetched_count`, `page`, `total_pages` | `fetched` |

---

## Changes to Existing Code

### `app.py` — Ingest subprocess management

- Change stdout from `tempfile.TemporaryFile()` to `subprocess.PIPE`
- Spawn a `StdoutReader` daemon thread that reads from the pipe and feeds the parser → queue
- On ingest start: call `event_queue.clear()` before spawning
- Existing `/ingest/status` endpoint stays as-is for backward compatibility
- Module-level constant: `max_sse_connections = 2`

#### StdoutReader thread — error handling requirements

The thread MUST have a top-level `try/except Exception` that logs the error and pushes an `aborted` event to the queue. Additional requirements:

- If the reader thread dies due to an exception, it MUST call `process.kill()` on the child process to prevent a hanging subprocess.
- The thread detects EOF on the pipe (subprocess exited) and pushes either a `complete` event (if it parsed the summary line) or an `aborted` event (if the process exited without a summary line or with a non-zero exit code).
- The `StdoutReader` thread does NOT acquire `_ingest_lock` — it only reads from the pipe and writes to the queue.

#### Locking coordination

- `_ingest_lock` protects `_ingest_process` and `_ingest_log_file` as before.
- The `StdoutReader` thread is the **single source of truth** for "ingest is done" — it detects pipe EOF and pushes the terminal event.
- `_ingest_running()` continues to check `poll()` for backward compatibility (the `/ingest/status` endpoint), but the SSE stream relies on the queue's terminal event, not `poll()`.

### New endpoint: `GET /ingest/stream`

```python
Response(
    generate(),
    mimetype='text/event-stream',
    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'}
)
```

- Returns HTTP 429 if `max_sse_connections` (2) would be exceeded.
- Generator reads `Last-Event-ID` from request headers for replay cursor.
- Generator checks `run_id` from `Last-Event-ID`: if it references a different `run_id` than the current queue, discard the cursor and replay from the beginning of the current run.
- Yields events in SSE format: `id: {id}\ndata: {json}\n\n`
- Returns after yielding a `complete` or `aborted` event.
- Increments/decrements the connection counter tracked by `EventQueue` for cleanup timing.

---

## Rescore Mode

When `ingest.py` runs with `--rescore`, it emits a different set of log lines:

- `RESCORED <score>/10  <title>` — no source attribution (rescoring existing DB listings)
- `RESCORE FAILED  <title>`
- `Rescore complete: <count> listings | <rescored> rescored (<failed> failed) | ~<tokens> tok | ~$<cost>`

The parser handles these and maps them to `rescored`, `rescore_failed`, and `complete` event types respectively.

The drawer shows a simplified view during rescore: no source breakdown, just a flat list of rescored/failed events with a tally. The tally footer shows only `Rescored` (green) and `Failed` (red) counters during a rescore run.

---

## Frontend

### New partial: `templates/_ingest_drawer.html`

Included in base layout via `{% include '_ingest_drawer.html' %}`.

Contains:
- Drawer shell
- Progress bar
- Event list container
- Tally footer
- Per-source breakdown
- FAB (floating action button) button

All styling via classes in `static/style.css` using existing design tokens.

### Drawer Behavior

- Slides in from right edge (400px wide, full height below header)
- CSS transition: `transform 0.3s ease`
- **X button:** minimizes (hides drawer, shows FAB) — does NOT destroy state
- **FAB:** reopens with all accumulated events intact
- Open/closed state persisted in `sessionStorage` across page navigations
- **Pulse dot in header:** green animated while live, muted when idle

### Event Rendering (vanilla JS)

| Event type | Rendered as |
|---|---|
| `fetched` | Section divider: amber monospace text flanked by horizontal rules (e.g. `── FETCHED 47 FROM ADZUNA ──`) |
| `scored` | Title, score, `FULL`/`SNIPPET` scrape tag, matched skill chips |
| `filtered` | Title, reason tag (e.g. `[title_exclude]`) |
| `dupe` | Title, "already seen" label |
| `score_failed` | Title, failure reason |
| `scrape_skip` | Title, "full description from source" label (muted) |
| `rescored` | Title, new score |
| `rescore_failed` | Title, failure label |
| `aborted` | Error banner: "Ingest run failed unexpectedly" |

All job events show source attribution (small muted label, right-aligned).

### Rolling Tally (sticky footer)

Counters updated on each event received:

| Counter | Color coding |
|---|---|
| Fetched | Amber |
| Filtered | Muted |
| Dupes | Muted |
| Scrape Skipped | Muted |
| Scored | Green |
| Failed | Red |

For rescore mode, show a different tally: `Rescored` (Green) and `Failed` (Red) only.

### Per-Source Breakdown (below tally)

- One row per active source
- Columns: source name, fetched / filtered / passed counts
- Rows appear as each source's first event arrives
- Not shown during rescore mode

### SSE Connection Lifecycle

| State | Behavior |
|---|---|
| Page load, ingest running or events exist | Establish `EventSource` to `/ingest/stream` |
| Reconnection | Browser-native `Last-Event-ID` for automatic replay |
| Reconnection with stale run ID | Replay from beginning of current run |
| Replayed events | Render without slide-in animation (instant batch) |
| Live events | Render with slide-in animation |
| `complete` event received | Pulse dot goes idle, connection closes naturally |
| `aborted` event received | Pulse dot goes idle, show error state (red text, "Ingest failed" message), connection closes |
| Error | Treat as ended, show idle state |
| 429 response | Display "too many connections" message, do not retry |

### Auto-Scroll

- Track if scroll position is within 40px of bottom ("pinned")
- When pinned, auto-scroll on each new event
- Manual scroll up pauses auto-scroll
- Scroll back to bottom re-pins

---

## Nice-to-Haves (not gating v1)

1. **Job posting link arrow** — small ↗ on each event card linking to the listing URL. Requires the URL to be available in the log line or event data.
2. **`hx-boost` partial navigation** — persistent drawer without full page reloads. Architectural change that benefits the whole app, not just this feature.

---

## Testing

### Unit Tests: `tests/test_ingest_events.py`

**`TestIngestEventParser`**
- Each log line pattern → correct event type and fields (including `scrape_skip`, `rescored`, `rescore_failed`)
- Logging prefix stripping: `INFO ingest: ` and `WARNING ingest: ` are stripped before matching
- Malformed / unrecognized lines return `None`
- Multi-source tracking: current source updates on fetch events
- Scrape fallback flag propagates correctly to the next `scored` event
- Rescore mode lines parse correctly (no source attribution)

**`TestEventQueue`**
- `push` / `subscribe` from different cursor positions
- Replay order is stable
- `clear()` resets ID counter, event list, and generates a new `run_id`
- Concurrent thread access (reader thread + SSE generator) does not corrupt state
- `subscribe()` on empty queue with no active ingest yields `idle` immediately
- Memory management: queue evicts oldest events when size exceeds 5000
- Cleanup timer: queue clears 60 seconds after last connection disconnects

### Integration Tests: `tests/test_ingest_stream.py`

- Flask test client `GET /ingest/stream` → verify SSE wire format (`id:`, `data:`, `event:` fields)
- `Last-Event-ID` replay: push 5 events, connect at `id=2`, verify only events 3–5 are replayed
- Stale `run_id` in `Last-Event-ID`: verify replay starts from beginning of current run
- Empty queue with no active ingest → immediate `idle` event
- `complete` event causes generator to return (stream closes)
- `aborted` event causes generator to return (stream closes)
- Third concurrent SSE connection returns HTTP 429

### Manual Verification Checklist

- [ ] Open drawer, start ingest, watch events stream in real time
- [ ] Navigate to another page during ingest, return — verify replay rebuilds the log
- [ ] Minimize drawer during ingest, reopen — verify events accumulated while closed
- [ ] Verify tally counters match the final run summary
- [ ] Test with multiple sources active simultaneously
- [ ] Kill the ingest process mid-run — verify `aborted` event appears and drawer shows error state
- [ ] Start ingest with `--rescore` — verify simplified tally (rescored/failed only, no source breakdown)
- [ ] Open two browser tabs during ingest — verify both stream correctly; a third tab should receive 429

---

## Files Changed

| File | Change |
|---|---|
| `ingest_events.py` | NEW — event parser, `EventQueue`, event schema |
| `app.py` | Subprocess stdout → `PIPE`, `StdoutReader` thread, SSE endpoint, queue integration, `max_sse_connections` constant |
| `templates/_ingest_drawer.html` | NEW — drawer partial |
| `templates/base.html` | Include drawer partial |
| `static/style.css` | Drawer component styles |
| `static/ingest-drawer.js` | NEW (optional) — drawer JS logic (or inline in partial) |
| `docs/STYLE_GUIDE.md` | Document new drawer component classes |
| `tests/test_ingest_events.py` | NEW — parser + queue unit tests |
| `tests/test_ingest_stream.py` | NEW — SSE integration tests |
