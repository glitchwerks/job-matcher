/**
 * Ingest Log Stream Drawer
 *
 * Manages the slide-out drawer that displays real-time ingest events via SSE.
 *
 * Responsibilities:
 *   - EventSource lifecycle (open, close, reconnect via Last-Event-ID)
 *   - Event rendering by type (scored, filtered, dupe, fetched, complete, etc.)
 *   - Rolling tally aggregation (client-side — no server round-trips)
 *   - Per-source breakdown table
 *   - Auto-scroll with "pinned to bottom" detection
 *   - Pulse-dot live indicator
 *   - FAB show/hide mirroring drawer open/close state
 *   - Keyboard dismiss (Escape) and sessionStorage open-state persistence
 *   - "Connection lost" notice on onerror
 *
 * Dependencies: none. Vanilla JS only — no framework, no build step.
 */
(function () {
  "use strict";

  // ---------------------------------------------------------------------------
  // DOM refs
  // ---------------------------------------------------------------------------

  var drawer      = document.getElementById("ingest-drawer");
  var fab         = document.getElementById("ingest-fab");
  var closeBtn    = document.getElementById("ingest-drawer-close");
  var eventList   = document.getElementById("ingest-event-list");
  var breakdownEl = document.getElementById("ingest-source-breakdown");

  // Pulse dots — one in the drawer header, one on the FAB
  var pulseHeader = document.getElementById("ingest-pulse-header");
  var pulseFab    = document.getElementById("ingest-pulse");

  // Screen-reader-only live region for terminal event announcements
  var srAnnounce = document.getElementById("ingest-sr-announce");

  // Tally counter elements (keyed by tally category)
  var tallyEls = {
    fetched:  document.getElementById("tally-fetched"),
    filtered: document.getElementById("tally-filtered"),
    dupes:    document.getElementById("tally-dupes"),
    skipped:  document.getElementById("tally-skipped"),
    scored:   document.getElementById("tally-scored"),
    failed:   document.getElementById("tally-failed"),
  };

  // Guard: bail if the drawer isn't on this page
  if (!drawer || !fab) { return; }

  // ---------------------------------------------------------------------------
  // State
  // ---------------------------------------------------------------------------

  var tally = { fetched: 0, filtered: 0, dupes: 0, skipped: 0, scored: 0, failed: 0 };
  // { "Adzuna": { fetched: 0, filtered: 0, passed: 0 }, ... }
  var sourceTally = {};

  var eventSource   = null;  // current EventSource, or null when closed
  var lastEventId   = null;  // last "run_id:event_id" string received
  var isReplay      = true;  // true during initial burst of replayed events
  var isLive        = false; // true while an active EventSource is open

  // Auto-scroll: stay pinned to bottom unless user has scrolled up
  var autoScrollPinned = true;
  var SCROLL_THRESHOLD = 40; // px from bottom before we stop auto-scrolling

  // ---------------------------------------------------------------------------
  // Drawer open / close
  // ---------------------------------------------------------------------------

  function openDrawer() {
    drawer.classList.add("ingest-drawer--open");
    fab.classList.add("ingest-fab--hidden");
    fab.setAttribute("aria-expanded", "true");
    sessionStorage.setItem("ingest-drawer-open", "1");
    // Re-establish the SSE stream if it was closed (e.g. drawer was dismissed
    // mid-run via closeDrawer → closeSSE). connectSSE() is a no-op if the
    // stream is already open.
    connectSSE();
  }

  function closeDrawer() {
    closeSSE();
    drawer.classList.remove("ingest-drawer--open");
    fab.classList.remove("ingest-fab--hidden");
    fab.setAttribute("aria-expanded", "false");
    sessionStorage.setItem("ingest-drawer-open", "0");
  }

  closeBtn.addEventListener("click", closeDrawer);
  fab.addEventListener("click", openDrawer);

  // Dismiss on Escape key when drawer is open
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && drawer.classList.contains("ingest-drawer--open")) {
      closeDrawer();
    }
  });

  // Restore open state from sessionStorage across page reloads
  if (sessionStorage.getItem("ingest-drawer-open") === "1") {
    openDrawer();
  }

  // ---------------------------------------------------------------------------
  // Auto-scroll
  // ---------------------------------------------------------------------------

  eventList.addEventListener("scroll", function () {
    var distFromBottom = eventList.scrollHeight - eventList.scrollTop - eventList.clientHeight;
    autoScrollPinned = distFromBottom < SCROLL_THRESHOLD;
  });

  function scrollToBottom() {
    if (autoScrollPinned) {
      eventList.scrollTop = eventList.scrollHeight;
    }
  }

  // ---------------------------------------------------------------------------
  // Pulse-dot live indicator
  // ---------------------------------------------------------------------------

  function setPulseLive(live) {
    var cls = "ingest-pulse-dot--live";
    if (live) {
      pulseHeader.classList.add(cls);
      pulseFab.classList.add(cls);
    } else {
      pulseHeader.classList.remove(cls);
      pulseFab.classList.remove(cls);
    }
  }

  // ---------------------------------------------------------------------------
  // Tally
  // ---------------------------------------------------------------------------

  function updateTallyDisplay() {
    Object.keys(tally).forEach(function (key) {
      if (tallyEls[key]) { tallyEls[key].textContent = tally[key]; }
    });
  }

  function updateSourceBreakdown() {
    var names = Object.keys(sourceTally);
    if (names.length === 0) {
      breakdownEl.innerHTML = "";
      return;
    }
    var html = "";
    names.forEach(function (name) {
      var s = sourceTally[name];
      html += '<div class="ingest-source-row">' +
        '<span class="ingest-source-name">' + escapeHtml(name) + '</span>' +
        '<span>' + s.fetched + '\u00a0fetched\u00a0/\u00a0' +
                   s.filtered + '\u00a0filtered\u00a0/\u00a0' +
                   s.passed   + '\u00a0passed</span>' +
        '</div>';
    });
    breakdownEl.innerHTML = html;
  }

  function ensureSource(name) {
    if (name && !sourceTally[name]) {
      sourceTally[name] = { fetched: 0, filtered: 0, passed: 0 };
    }
  }

  function trackTally(event) {
    var type   = event.type;
    var source = (event && event.source) || null;

    if (type === "fetched") {
      tally.fetched += (event.detail && event.detail.fetched_count) || 0;
      ensureSource(source);
      if (source) { sourceTally[source].fetched += (event.detail && event.detail.fetched_count) || 0; }
    } else if (type === "filtered") {
      tally.filtered++;
      ensureSource(source);
      if (source) { sourceTally[source].filtered++; }
    } else if (type === "dupe") {
      tally.dupes++;
    } else if (type === "scrape_skip") {
      tally.skipped++;
    } else if (type === "scored" || type === "rescored") {
      tally.scored++;
      if (source) {
        ensureSource(source);
        sourceTally[source].passed++;
      }
    } else if (type === "score_failed" || type === "rescore_failed") {
      tally.failed++;
    }

    updateTallyDisplay();
    updateSourceBreakdown();
  }

  // ---------------------------------------------------------------------------
  // Event rendering
  // ---------------------------------------------------------------------------

  /**
   * Create and append a DOM element for the given event.
   * replay=true suppresses the slide-in animation (used for replayed events).
   */
  function renderEvent(event, replay) {
    // idle events are bookkeeping-only — do not render anything
    if (event.type === "idle") { return; }

    var el  = document.createElement("div");
    var cls = "ingest-event ingest-event--" + event.type;
    if (replay) { cls += " ingest-event--replay"; }
    el.className = cls;

    switch (event.type) {

      case "fetched":
        el.textContent = "Fetched " +
          ((event.detail && event.detail.fetched_count) || 0) +
          " from " + (event.source || "?");
        break;

      case "scored":
      case "rescored":
        el.innerHTML =
          (event.source
            ? '<span class="ingest-event-source">' + escapeHtml(event.source) + "</span>"
            : "") +
          '<span class="ingest-event-title">' + escapeHtml(event.title || "") + "</span>" +
          '<span class="ingest-event-tag">' + ((event.detail && event.detail.score) || 0) + "/10</span>" +
          (event.detail && event.detail.scraped === false
            ? '<span class="ingest-event-tag">SNIPPET</span>'
            : event.type === "scored"
              ? '<span class="ingest-event-tag">FULL</span>'
              : "");
        break;

      case "filtered":
        el.innerHTML =
          (event.source
            ? '<span class="ingest-event-source">' + escapeHtml(event.source) + "</span>"
            : "") +
          '<span class="ingest-event-title">' + escapeHtml(event.title || "") + "</span>" +
          '<span class="ingest-event-tag">' +
            escapeHtml((event.detail && event.detail.reason) || "filtered") +
          "</span>";
        break;

      case "dupe":
        el.innerHTML =
          (event.source
            ? '<span class="ingest-event-source">' + escapeHtml(event.source) + "</span>"
            : "") +
          '<span class="ingest-event-title">' + escapeHtml(event.title || "") + "</span>" +
          '<span class="ingest-event-tag">already seen</span>';
        break;

      case "score_failed":
      case "rescore_failed":
        el.innerHTML =
          (event.source
            ? '<span class="ingest-event-source">' + escapeHtml(event.source) + "</span>"
            : "") +
          '<span class="ingest-event-title">' + escapeHtml(event.title || "") + "</span>" +
          '<span class="ingest-event-tag">FAILED</span>';
        break;

      case "scrape_skip":
        el.innerHTML =
          (event.source
            ? '<span class="ingest-event-source">' + escapeHtml(event.source) + "</span>"
            : "") +
          '<span class="ingest-event-title">' + escapeHtml(event.title || "") + "</span>" +
          '<span class="ingest-event-tag">full from source</span>';
        break;

      case "complete":
        el.textContent = "Run complete";
        break;

      case "aborted":
        el.textContent = (event.detail && event.detail.error)
          ? "Ingest aborted: " + escapeHtml(event.detail.error)
          : "Ingest run failed unexpectedly";
        break;

      default:
        // Unknown event type — skip rendering
        return;
    }

    eventList.appendChild(el);
    scrollToBottom();
  }

  // ---------------------------------------------------------------------------
  // Connection-lost notice
  // ---------------------------------------------------------------------------

  function showConnectionLost() {
    // Avoid duplicate notices
    if (eventList.querySelector(".ingest-connection-lost")) { return; }
    var el = document.createElement("div");
    el.className = "ingest-connection-lost";
    el.textContent = "Connection lost — reconnecting\u2026";
    eventList.appendChild(el);
    scrollToBottom();
  }

  function removeConnectionLost() {
    var el = eventList.querySelector(".ingest-connection-lost");
    if (el) { el.parentNode.removeChild(el); }
  }

  // ---------------------------------------------------------------------------
  // SSE connection
  // ---------------------------------------------------------------------------

  function connectSSE() {
    if (eventSource) { return; } // already open

    isReplay = true;
    isLive   = true;
    setPulseLive(true);
    removeConnectionLost();

    // Open the drawer so the user sees events arrive
    openDrawer();

    // Build URL — include Last-Event-ID as query param for environments
    // where custom request headers aren't forwarded to SSE endpoints.
    // The EventSource spec sends Last-Event-ID automatically as a header on
    // reconnect; we only need the query param for the initial connection when
    // we want to resume a previous run (not currently needed, so we just open
    // the plain URL and rely on the header mechanism for reconnects).
    eventSource = new EventSource("/ingest/stream");

    eventSource.onmessage = function (e) {
      // Track the raw event ID for reconnect (browser sets Last-Event-ID from the
      // "id:" field automatically, but we store it ourselves for clarity).
      if (e.lastEventId) { lastEventId = e.lastEventId; }

      var data;
      try {
        data = JSON.parse(e.data);
      } catch (_) {
        return;
      }

      // idle → no active run, nothing to show
      if (data.type === "idle") {
        isLive = false;
        setPulseLive(false);
        closeSSE();
        return;
      }

      var currentlyReplay = isReplay;
      renderEvent(data, currentlyReplay);

      // Only count live (non-replayed) events toward tallies.
      // Replayed events still render to the DOM so the user can see them,
      // but they must not double-count on reconnect.
      if (!currentlyReplay) {
        trackTally(data);
      }

      // Switch out of replay mode after the first animation frame — by then the
      // browser has synchronously dispatched all buffered/replayed events.
      if (isReplay) {
        requestAnimationFrame(function () { isReplay = false; });
      }

      // Terminal events: close the stream and announce to screen readers.
      if (data.type === "complete" || data.type === "aborted") {
        isLive = false;
        setPulseLive(false);
        if (srAnnounce) {
          srAnnounce.textContent = data.type === "complete"
            ? "Ingest run complete. " + tally.scored + " scored, " + tally.filtered + " filtered."
            : "Ingest run aborted. Connection lost.";
        }
        closeSSE();
      }
    };

    eventSource.onerror = function () {
      // onerror fires both on network drop AND on clean server close.
      // If we already handled a terminal event (isLive=false), we're done.
      if (!isLive) { return; }

      // Still live → transient connection error. Show notice; browser will
      // auto-reconnect with Last-Event-ID so we don't lose events.
      setPulseLive(false);
      showConnectionLost();

      // If the EventSource has already closed itself (readyState CLOSED=2),
      // the browser won't reconnect automatically — do it manually.
      if (eventSource && eventSource.readyState === 2) {
        closeSSE();
        // Brief delay before retrying so we don't hammer a down server.
        setTimeout(connectSSE, 3000);
      }
    };
  }

  function closeSSE() {
    if (eventSource) {
      eventSource.close();
      eventSource = null;
    }
  }

  // ---------------------------------------------------------------------------
  // Reset on new ingest run
  // ---------------------------------------------------------------------------

  function resetDrawer() {
    eventList.innerHTML  = "";
    tally = { fetched: 0, filtered: 0, dupes: 0, skipped: 0, scored: 0, failed: 0 };
    sourceTally          = {};
    lastEventId          = null;
    autoScrollPinned     = true;
    updateTallyDisplay();
    breakdownEl.innerHTML = "";
  }

  // ---------------------------------------------------------------------------
  // HTMX integration — auto-connect when ingest is triggered via the UI
  // ---------------------------------------------------------------------------

  document.body.addEventListener("htmx:afterRequest", function (e) {
    var detail = e.detail || {};
    var pathInfo = detail.pathInfo || {};
    var path = pathInfo.requestPath || (detail.xhr && detail.xhr.responseURL) || "";
    if (path.indexOf("/ingest/trigger") !== -1 && detail.successful) {
      // A new ingest run has started — close any existing stream, reset state,
      // then connect after a brief pause to let the subprocess start.
      closeSSE();
      resetDrawer();
      setTimeout(connectSSE, 500);
    }
  });

  // ---------------------------------------------------------------------------
  // Utility
  // ---------------------------------------------------------------------------

  /**
   * Escape a string for safe HTML insertion.
   * Uses the browser's own text node serialisation — no regex hacks.
   */
  function escapeHtml(str) {
    var div = document.createElement("div");
    div.appendChild(document.createTextNode(String(str)));
    return div.innerHTML;
  }

  // ---------------------------------------------------------------------------
  // On page load: connect immediately to pick up any active or completed run
  // ---------------------------------------------------------------------------

  connectSSE();

}());
