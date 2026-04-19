# Plan: Administration Tab

## Revision History

- **2026-04-11 v1** — Initial plan.
- **2026-04-11 v2** — Revised after adversarial review by the `inquisitor` agent. Key changes:
  - **Phase 5 rewritten.** Replaced log-grep parser with a new `ingest_runs` DB table written by `ingest.py` at run start/end. The panel now reads rows, not log file tails. This fixes every failure mode the v1 parser had (concurrency, filename collisions, parser drift, Ofelia detection).
  - **Phase 4 descoped.** Download-only. The in-browser log viewer, regex parser, multi-line record handling, and log-level color chips are all removed. Eliminates the XSS surface and the STYLE_GUIDE semantic-token violation in one move.
  - **Phase 2 expanded.** Adds session-based CSRF protection to `/admin/clear-db`. The current "type DELETE" check is a typo guard, not CSRF defense.
  - **New infrastructure prerequisite (inside Issue D).** Extract `LOG_DIR` into a shared `paths.py` module. It is currently a local variable inside `_configure_file_logging` at `ingest.py:69` that `app.py` cannot access.
  - **Branching strategy corrected.** B/C/D/E merge **serially**, not in parallel — they all touch `app.py` and `templates/admin.html` in overlapping line ranges.
  - **Phase 1 verification gains a nav-width check.** The current nav has 6 tabs; this makes 7.
  - **Phase 2 `listing_count` cleanup is now a concrete grep, not a TODO.**
  - **Out of scope updated.** "No DB table" is removed — the plan now adds exactly one table. "In-browser log viewer" is added to out of scope.

## Context

The Settings page has grown to host features that are not "settings" in the strict sense — most notably **Clear Database** (a destructive ops action). **Dependencies Versions** currently lives on `/stats` for no clear reason and also belongs in an admin area.

Two frequently-asked operational questions have no answer in the UI today:

1. **"What happened in the last ingest run?"** — per-run logs exist on disk at `logs/ingest_YYYYMMDD_HHMMSS.log` via `FileHandler` in `ingest.py:84–85`, but there is no way to download them from the browser, and the filename format is hard to read.
2. **"Is the scheduled ingest actually running?"** — Ofelia runs `python ingest.py --hours 25 @daily` inside the prod container, but the UI has no persistent view of last-run state. The in-memory `_last_run` dict is lost on Flask restart, and scheduled runs can't populate it anyway (they happen in a different process).

This plan introduces a new **Administration** tab that consolidates existing ops features and adds two new ones. It uses the Settings page pattern, reuses existing data paths where possible, and adds exactly **one** new database table (`ingest_runs`) to replace what v1 of this plan tried to do by parsing log files at request time.

Supersedes issue #175 (simpler read-only last-run panel targeted at Settings).

---

## Issue Strategy

Single parent milestone: **1.2 "Ingest log stream"** (already active).

Issues to create before implementation starts:

| # | Title | Purpose |
|---|-------|---------|
| **A** | `feat(ui): add Administration tab shell` | Shell only — new `/admin` route, nav link in all 4 page templates, empty `templates/admin.html` following Settings pattern. Verifies 7-tab nav width at the desktop breakpoint. Blocker for all children. |
| **B** | `refactor(ui): move Clear Database to Admin tab + add CSRF` | UI move plus session-based CSRF token on the destructive route. Route stays at `/admin/clear-db`. Remove danger-zone from `settings.html`. |
| **C** | `refactor(ui): move Dependencies Versions from Stats to Admin tab` | UI move. Shift `RUNTIME_VERSIONS` consumption from `/stats` into `/admin`. |
| **D** | `feat(admin): download ingest logs` | Extract `LOG_DIR` into `paths.py` (prereq). New `/admin/logs` list + raw download. **No in-browser viewer.** |
| **E** | `feat(ingest): ingest_runs table + scheduled run panel` | New `ingest_runs` DB table written by `ingest.py` at run start/end. New `/admin/schedule-state` reads from DB. Supersedes #175 — close #175 with a pointer. |

All five issues → Milestone 1.2. Issue **A** must merge before B/C/D/E can start. B, C, D, E each touch `app.py` and `templates/admin.html` in overlapping line ranges, so they merge **serially** into the `feature/admin-tab` primary branch. There is no realistic parallelization.

---

## Phase 1 — Administration Tab Shell (Issue A)

**Goal:** Add the tab, the route, and an empty scaffolded page that follows the Settings pattern. No features yet.

### Files to modify

| File | Change |
|---|---|
| `templates/index.html` | Add `<a href="/admin" class="nav-tab{% if view == 'admin' %} active{% endif %}">admin</a>` to nav (after `settings` link) |
| `templates/settings.html` | Same nav addition |
| `templates/stats.html` | Same nav addition |
| `templates/profile.html` | Same nav addition |
| `templates/admin.html` | **NEW** — mirrors `settings.html` structure: header + nav + `<h1>Administration</h1>` + tab buttons for "Runtime / Logs / Schedule / Danger Zone" (empty panes ready for phases 2–5) |
| `app.py` | Add `@app.route("/admin")` returning `render_template("admin.html", view="admin")`. Place near the `/settings` route for locality. |

### Admin page tab structure (inside the page, mirroring Settings)

Four panes, each populated by one of the phases below:

1. **Runtime** (phase 3) — Dependencies Versions table
2. **Logs** (phase 4) — Ingest log list + raw download only
3. **Schedule** (phase 5) — DB-backed scheduled ingest state panel
4. **Danger Zone** (phase 2) — Clear Database form, now CSRF-protected

Use the existing `.settings-tabs` + `.tab-pane` pattern from `templates/settings.html:234–257`. Copy the tab-switching JS from `templates/settings.html:551–580` into `admin.html`. No new CSS tokens needed.

### Nav-width verification (new in v2)

Current nav has 6 tabs (feed / bookmarks / applied / stats / profile / settings). Adding `admin` makes 7. Before merging Issue A:

- Load `/admin` at the desktop breakpoint (1280px min) and confirm the 7-tab nav fits without wrapping or horizontal scroll.
- If it doesn't fit, **block A's merge**. Follow-up options (not in scope for A): shorten labels, introduce a compact nav mode, or drop a legacy tab.

### Tests

- `tests/test_admin_page.py` (new) — `GET /admin` returns 200, contains "Administration" heading, all 4 empty panes present.
- Update existing nav-test (or add one) to assert the admin tab link is present on all 4 pages.

---

## Phase 2 — Move Clear Database + Add CSRF (Issue B)

**Current location:** `templates/settings.html:486–547` (danger-zone section), route at `app.py:2036–2091`.

**Target:** Admin tab → "Danger Zone" pane, protected by a session-based CSRF token.

### The CSRF hole this closes

`admin_clear_db` currently only verifies `request.form["confirmation"] == "DELETE"`. That is a typo guard, not CSRF defense. Any same-origin page (a malicious tab, an XSS payload in the app, an HTMX swap that injects a hidden form) can POST the required value. Moving the UI without fixing this inherits the hole. The app is described as "single-user, routes remain public" — that is a description of the problem, not a justification.

### CSRF approach — no new dependency

Use a session-based token. Flask already uses signed sessions via `SECRET_KEY`.

- On `/admin` render: if `session.get("csrf_token")` is absent, generate via `secrets.token_urlsafe(32)` and store in session.
- The Danger Zone form includes `<input type="hidden" name="csrf_token" value="{{ csrf_token }}">`.
- `admin_clear_db` verifies `request.form.get("csrf_token") == session.get("csrf_token")` **before** the `confirmation == "DELETE"` check. Missing or mismatched → 400.
- No token rotation on submit — single-user app, not worth the cost of breaking tabs.

This is intentionally minimal. Full Flask-WTF is overkill for one destructive form. Introduce Flask-WTF only if future work adds more protected POSTs.

### Other changes

- **`app.py`** — route `/admin/clear-db` URL unchanged. Add the CSRF check at the top of the handler. Add `csrf_token` generation inside `admin()`.
- **`templates/settings.html`** — remove lines 486–547 (the entire danger-zone section).
- **`templates/admin.html`** — add the danger-zone section to the "Danger Zone" pane. Include the hidden `csrf_token` field.
- **`app.py`** `admin()` view — pass `listing_count=db.get_listing_count()` **and** `csrf_token=session["csrf_token"]`.

### `listing_count` cleanup (concrete, not a TODO)

Before removing the kwarg from the `/settings` route, grep for remaining uses:

```powershell
Select-String -Path templates\settings.html -Pattern "listing_count"
Select-String -Path app.py -Pattern "listing_count" -Context 0,2
```

Only remove `listing_count=...` from `settings()` if the grep shows zero remaining references in `settings.html` after the danger-zone block is deleted. If there are other references (header badge, save-success panel, etc.), leave the parameter in place.

### Tests

- `tests/test_clear_db.py` (existing, 15 tests) — update any test that asserts the danger zone is reachable via `/settings` to assert it on `/admin` instead.
- **New CSRF tests**:
  - POST with correct `confirmation=DELETE` but **missing** `csrf_token` → 400, no DB change.
  - POST with correct `confirmation=DELETE` but **mismatched** `csrf_token` → 400, no DB change.
  - POST with both valid → 200, DB cleared (existing happy-path test updated to include the token).

---

## Phase 3 — Move Dependencies Versions (Issue C)

**Current location:** `app.py:266–314` (`get_runtime_versions()` + `RUNTIME_VERSIONS` cached at startup), rendered in `templates/stats.html:116–133`.

**Target:** Admin tab → "Runtime" pane.

### Changes

- **`app.py`** — leave `get_runtime_versions()` and `RUNTIME_VERSIONS` in place. Update `admin()` to pass `runtime_versions=RUNTIME_VERSIONS`. Update `stats()` to stop passing it.
- **`templates/admin.html`** — add the runtime table to the "Runtime" pane. Copy the `{% for row in runtime_versions %}` block from `stats.html:116–133` verbatim.
- **`templates/stats.html`** — remove the Runtime section (lines 116–133).

### Tests

- `tests/test_stats.py` — remove any assertion that `/stats` contains the Runtime section.
- `tests/test_admin_page.py` — assert `/admin` contains the Runtime section and at least one known package (e.g. `flask`).

---

## Phase 4 — Download Ingest Logs (Issue D)

**Current state:** Per-run log files at `logs/ingest_YYYYMMDD_HHMMSS.log` via `FileHandler` in `ingest.py:84–85`; retention of 30 files via auto-prune in `ingest.py:102–111`. No UI surface today.

**Scope — descoped from v1: download only.** No in-browser log viewer, no regex parser, no HTML rendering of log content. This removes:

- **The entire XSS surface** — job listing titles, URLs, and any logged user data cannot reach a template.
- **The multi-line record problem** — Python tracebacks with no leading timestamp are no longer something this phase has to handle.
- **The STYLE_GUIDE semantic-token violation** — no need for `--log-info-*` / `--log-warn-*` / `--log-error-*` color chips.

If users later need in-browser viewing, revisit as a follow-up with a proper escaping contract and multi-line record parser.

### Prerequisite: extract `LOG_DIR` into a shared module

`LOG_DIR` is currently a local variable inside `_configure_file_logging` at `ingest.py:69`, computed from `os.environ.get("LOG_DIR", ...)`. `app.py` does not import it and has no notion of where log files live. This must be fixed **first**, inside Issue D, before the log routes work.

- **New file `paths.py`** at repo root:
  ```python
  """Shared filesystem paths used by both ingest and app processes."""
  import os
  from pathlib import Path

  def get_log_dir() -> Path:
      """Return the absolute log directory, honoring the LOG_DIR env var."""
      return Path(os.environ.get("LOG_DIR", "logs")).resolve()

  LOG_DIR: Path = get_log_dir()
  ```
- **`ingest.py`** — import `LOG_DIR` from `paths.py`. The env-var fallback stays, but now lives in one place.
- **`app.py`** — `from paths import LOG_DIR`.
- **Tests** — fixtures that write fake log files must monkeypatch `paths.LOG_DIR` (or a helper that reads it) to a temp dir. Do **not** set env vars from tests; they pollute other tests.

### New routes

- `GET /admin/logs` — HTML fragment (HTMX-swappable) listing log files as a table. Columns: **Timestamp** (parsed from filename as `YYYY-MM-DD HH:MM:SS`), **Size** (human-readable KB/MB), **Action** (`Download` button). Sorted newest first. Data source: `os.scandir(LOG_DIR)` filtered to `ingest_*.log`.
- `GET /admin/logs/<filename>/download` — `send_from_directory(LOG_DIR, filename, as_attachment=True)` with regex validation on filename plus a symlink resolve check (see Security).

### Templates

- **`templates/admin.html`** — "Logs" pane is `<div id="admin-logs-list" hx-get="/admin/logs" hx-trigger="load">` that loads on page render.
- **`templates/admin/_log_list.html`** (new partial) — the table fragment returned by `/admin/logs`.

### Filename-to-timestamp parsing

```
ingest_20260411_143022.log  →  2026-04-11 14:30:22
```

Regex: `^ingest_(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})\.log$`. Helper `_parse_log_filename_timestamp()` in `app.py`. Invalid filenames are filtered out of the list (defensive — handles stray files in `logs/`).

### Security

- **Path traversal** — regex validation on filename in every route. Never trust raw user input as path component. `send_from_directory` (not `send_file`) already rejects parent-directory traversal.
- **Symlink escape** — after resolving the target, assert `final_path.resolve()` is still inside `LOG_DIR.resolve()`. An attacker who can plant a symlink in `logs/` (requires host write access — remote, but cheap to defend against) cannot escape the log directory.
- **Content-Type**: `text/plain; charset=utf-8` on download.

### Tests

`tests/test_admin_logs.py` (new). Fixture monkeypatches `paths.LOG_DIR` to a temp dir.

- **Happy path**: 3 fake log files → list returns 3 rows, sorted newest first.
- **Timestamp parsing**: `ingest_20260411_143022.log` → displays as `2026-04-11 14:30:22`.
- **Download**: file served with `Content-Disposition: attachment`, original filename preserved.
- **Path traversal**: `GET /admin/logs/..%2F..%2Fetc%2Fpasswd/download` → 404.
- **Malformed filename**: `ingest_bad.log` → 404 (fails regex).
- **Symlink escape**: create a symlink in `LOG_DIR` pointing to `/etc/passwd` (or Windows equivalent); attempt to download → 404 (blocked by resolve check).
- **Retention race**: list returns a file, file is deleted (simulating auto-prune), download is attempted → 404 with no server error.
- **Empty dir**: list returns an empty table with "No logs yet" placeholder.
- **Permission denied**: create a file the process cannot read → excluded from the list, no 500.

---

## Phase 5 — `ingest_runs` Table + Scheduled Run Panel (Issue E)

**Supersedes #175.** Close #175 with a comment pointing to Issue E.

**Decision reversed from v1: add exactly one DB table.** Parsing log files at request time was conceptually dishonest (it was a grep, not a job state system), could not distinguish scheduled vs. manual runs, could not detect scheduler failure, and would silently drift when log formats changed. A small table solves all of it in a handful of lines.

### New table

```sql
CREATE TABLE IF NOT EXISTS ingest_runs (
    id              SERIAL PRIMARY KEY,
    trigger_source  TEXT NOT NULL,          -- 'scheduled' | 'manual_cli' | 'manual_ui'
    started_at      TIMESTAMPTZ NOT NULL,
    finished_at     TIMESTAMPTZ,            -- NULL while running or killed mid-run
    status          TEXT NOT NULL,          -- 'running' | 'success' | 'failed'
    fetched         INTEGER DEFAULT 0,
    filtered        INTEGER DEFAULT 0,
    scored          INTEGER DEFAULT 0,
    failed_count    INTEGER DEFAULT 0,
    cost_usd        NUMERIC(10, 4) DEFAULT 0,
    log_filename    TEXT,                   -- cross-ref with Phase 4 downloads
    error_message   TEXT                    -- populated on failure
);

CREATE INDEX IF NOT EXISTS idx_ingest_runs_started_at ON ingest_runs (started_at DESC);
```

Migration runs in `db.init_db()` wrapped in `try/except` following the existing pattern documented in `CLAUDE.md` ("Schema migration uses `ALTER TABLE ... ADD COLUMN` wrapped in try/except"). First run creates the table; subsequent runs are no-ops.

### Write sites in `ingest.py`

Two writes per run:

1. **At run start** (after logging is configured, before fetching begins):
   ```python
   run_id = db.create_ingest_run(
       trigger_source=_detect_trigger_source(),
       log_filename=Path(log_file_path).name,
   )
   ```

   `_detect_trigger_source()` returns:
   - `"scheduled"` if env var `INGEST_TRIGGER=scheduled` is set (Ofelia sets this; see compose change below).
   - `"manual_ui"` if env var `INGEST_TRIGGER=ui` is set (Flask subprocess trigger sets this).
   - `"manual_cli"` otherwise.

2. **At run completion** — wrap the main loop in `try/finally`:
   ```python
   try:
       # ... existing pipeline ...
       db.finish_ingest_run(run_id, status="success",
                            counts=summary_counts, cost_usd=total_cost)
   except Exception as exc:
       db.finish_ingest_run(run_id, status="failed",
                            error_message=str(exc)[:500])
       raise
   ```

The existing `"INGEST RUN COMPLETE"` log marker is kept for human log-reading but no longer drives any UI state.

### Ofelia + UI trigger wiring

- Update `docker-compose.prod.yml` so the Ofelia job command sets `INGEST_TRIGGER=scheduled`:
  ```yaml
  ofelia.job-exec.ingest.command: "bash -c 'INGEST_TRIGGER=scheduled python ingest.py --hours 25'"
  ```
- Update the existing Flask UI trigger path in `app.py` (the route that spawns a manual run) to set `INGEST_TRIGGER=manual_ui` on the subprocess `env=` argument.

### Read site in `app.py`

New route `GET /admin/schedule-state` returns an HTML fragment. Query:

```sql
SELECT * FROM ingest_runs ORDER BY started_at DESC LIMIT 10;
```

The panel displays:

- **Last run** — most recent row: trigger_source, started_at, finished_at, status, counts, cost.
- **Scheduled health badge**, computed as:
  - **Green** — most recent `trigger_source='scheduled'` row is less than 25h old and status='success'.
  - **Amber** — most recent scheduled row is 25–49h old, or status='running'.
  - **Red** — no scheduled row in last 49h, or most recent scheduled row has status='failed'.
- **Recent runs** — small table of the last 10 rows, all trigger sources.

Tier colors (`--score-high-*` / `--score-mid-*` / `--score-low-*`) are semantically correct here: success / warning / error. This does **not** violate the STYLE_GUIDE rule — the rule bans decorative tier color use, not semantic error states.

### Templates

- **`templates/admin.html`** — "Schedule" pane is `<div hx-get="/admin/schedule-state" hx-trigger="load, every 60s">`. 60s (not 30s) — the underlying event is daily, so 30s was pure waste.
- **`templates/admin/_schedule_state.html`** (new partial) — the panel fragment.

### No more Ofelia "detection" via config label

The v1 plan proposed a free-text `admin.scheduler_label` config key the user typed manually. That was a label, not detection. Delete the idea. The table answers "is the scheduler running?" by "did a `trigger_source='scheduled'` row land in the last 25h?" That is real detection.

### Stale `running` rows

A SIGKILL'd ingest leaves a row at status='running' forever. Not fatal, but noisy. Add a startup sweep in `db.init_db()`:

```sql
UPDATE ingest_runs
   SET status='failed',
       error_message='process died — detected on next startup'
 WHERE status='running'
   AND started_at < NOW() - INTERVAL '1 hour';
```

One hour is generous — real runs finish in minutes.

### Tests

- `tests/test_ingest_runs_table.py` (new):
  - Migration creates table on empty DB.
  - Migration is a no-op on DB with existing table.
  - `create_ingest_run()` returns an id; row has status='running' and finished_at=NULL.
  - `finish_ingest_run(..., status='success', ...)` updates the row correctly.
  - `finish_ingest_run(..., status='failed', error_message=...)` stores truncated error.
  - Startup sweep flips stale `running` rows older than 1h to `failed`.

- `tests/test_ingest_integration_runs.py` (new) — full `ingest.py` invocation against a throwaway DB:
  - Scheduled trigger (env var set) → row has `trigger_source='scheduled'`, status='success'.
  - Manual CLI trigger → `trigger_source='manual_cli'`.
  - Simulated mid-run crash (raise an exception inside the pipeline) → row has status='failed' with `finished_at` set.
  - Two concurrent runs → two distinct rows in `ingest_runs` with no collisions.

- `tests/test_admin_schedule_state.py` (new):
  - Empty `ingest_runs` → panel shows "No runs recorded yet".
  - One successful scheduled run <25h ago → green badge, correct counts.
  - Last scheduled run 30h ago → amber badge.
  - Last scheduled run status='failed' → red badge.
  - Last scheduled run 50h ago → red badge with "scheduler may be down".
  - Panel includes last 10 rows regardless of status.

- `tests/test_ingest_process_isolation.py` (new):
  - Kill `ingest.py` with SIGKILL mid-run → row left at status='running' with finished_at=NULL; next Flask startup sweep reclassifies to 'failed'.

---

## File Paths Summary

### New files
- `paths.py` — shared filesystem paths (Phase 4 prereq, used by Phases 4 and 5)
- `templates/admin.html` — main Admin page (Phase 1)
- `templates/admin/_log_list.html` — log table partial (Phase 4)
- `templates/admin/_schedule_state.html` — schedule panel partial (Phase 5)
- `tests/test_admin_page.py` — Phase 1/2/3 tests
- `tests/test_admin_logs.py` — Phase 4 tests
- `tests/test_ingest_runs_table.py` — Phase 5 DB layer tests
- `tests/test_ingest_integration_runs.py` — Phase 5 full-process tests
- `tests/test_admin_schedule_state.py` — Phase 5 route/panel tests
- `tests/test_ingest_process_isolation.py` — Phase 5 cross-process tests

### Modified files
- `templates/index.html`, `templates/settings.html`, `templates/stats.html`, `templates/profile.html` — add admin nav link
- `templates/settings.html` — remove danger-zone section
- `templates/stats.html` — remove runtime section
- `app.py` — new routes `/admin`, `/admin/logs`, `/admin/logs/<f>/download`, `/admin/schedule-state`; `admin()` view passes `listing_count`, `runtime_versions`, `csrf_token`; `admin_clear_db` gains CSRF verification; import `LOG_DIR` from `paths.py`; Flask UI ingest trigger sets `INGEST_TRIGGER=manual_ui` on the subprocess environment
- `ingest.py` — import `LOG_DIR` from `paths.py`; add `_detect_trigger_source()`; wrap main pipeline in `try/finally` with `db.create_ingest_run()` / `db.finish_ingest_run()` calls
- `db.py` — new `create_ingest_run()`, `finish_ingest_run()`, `get_recent_ingest_runs()` functions; `init_db()` creates `ingest_runs` table and runs stale-row sweep
- `docker-compose.prod.yml` — Ofelia job command sets `INGEST_TRIGGER=scheduled`
- `CLAUDE.md` — add Administration tab to the architecture section; add `ingest_runs` to the database schema notes; document the `INGEST_TRIGGER` env var convention

### Existing functions/utilities to reuse
- `db.get_listing_count()` — Phase 2, pass to Admin view
- `db.clear_all_listings()` — Phase 2, already wired to `/admin/clear-db`
- `get_runtime_versions()` + `RUNTIME_VERSIONS` — Phase 3, cached at startup in `app.py:266–314`
- `send_from_directory` from Flask — Phase 4, no new download infrastructure needed
- `FileHandler` + retention loop in `ingest.py:84–111` — Phase 4, writes the files we serve
- Settings tab switching JS at `templates/settings.html:551–580` — Phase 1, copy into admin.html
- Style tokens `--bg-surface`, `--text-primary`, `--text-accent`, `--border-mid`, `--score-high-*`, `--score-mid-*`, `--score-low-*` — all phases (tier colors on the Phase 5 health badge are semantically correct — success/warning/error — not decorative)

---

## Verification

### Phase-by-phase smoke tests

1. **Phase 1**: `curl -s localhost:5000/admin` returns HTML with "Administration" heading and four tab buttons. Clicking each tab switches panes. At 1280px viewport, the 7-tab nav fits without wrap or horizontal scroll.
2. **Phase 2**:
   - Navigate to `/admin` → Danger Zone pane → enter "DELETE" → submit. Row count goes to zero.
   - Craft a request missing `csrf_token` → 400, DB unchanged.
   - Craft a request with mismatched `csrf_token` → 400, DB unchanged.
   - Navigate to `/settings` → danger zone is gone.
3. **Phase 3**: Navigate to `/admin` → Runtime pane → table shows all expected packages. Navigate to `/stats` → runtime section is gone.
4. **Phase 4**:
   - After a manual `python ingest.py` run, navigate to `/admin` → Logs pane → newest file appears at top with readable timestamp.
   - Click "Download" → file downloads with original filename.
   - Attempt `GET /admin/logs/..%2F..%2Fetc%2Fpasswd/download` → 404.
   - In dev, `python app.py` without `LOG_DIR` env var → list loads without 500 (uses `./logs` fallback from `paths.py`).
5. **Phase 5**:
   - Fresh DB, run `python ingest.py` → one row with `trigger_source='manual_cli'` and status='success'.
   - Run with `$env:INGEST_TRIGGER="scheduled"; python ingest.py` → row has `trigger_source='scheduled'`.
   - Navigate to `/admin` → Schedule pane → shows last run with correct badge color.
   - Kill `ingest.py` mid-run → row stays at status='running'; restart Flask → startup sweep flips stale rows to status='failed'.
   - Empty table → panel shows "No runs recorded yet".

### Full test suite

`pytest` — must be green end-to-end, not just the new files. Full suite is mandatory per recent CI lessons (scoped verification has missed stale contracts twice in recent PRs).

### Manual cross-page regression

- All existing pages (feed, bookmarks, applied, stats, profile, settings) still load with the new nav link present and no broken layout.
- `/settings` still functions after danger-zone removal.
- `/stats` still functions after runtime-section removal.
- CSRF token generation does not break existing Flask session handling.

---

## Out of Scope

- **Editing the scheduler from the UI** — Ofelia config lives in `docker-compose.prod.yml`. No UI for tuning `@daily` cadence.
- **Triggering ingest from the Admin tab** — the ingest trigger already exists elsewhere; not moving it.
- **In-browser log viewer with formatting** — descoped from v1 due to XSS surface. Raw download only. Revisit if/when a proper escaping contract and multi-line record parser are designed.
- **Log viewer pagination / search** — no viewer, no pagination.
- **Full Flask-WTF CSRF integration** — one destructive form uses a minimal session-token approach. Introduce Flask-WTF only if future work adds more protected POSTs.
- **Permissions / auth on `/admin` itself** — single-user app, routes remain public. The CSRF fix on `admin_clear_db` is targeted at the one destructive action, not a site-wide auth model.
- **Admin tab on mobile** — desktop-first, matches the rest of the app.
- **Retroactive population of `ingest_runs`** — the table starts empty. Historical runs are not backfilled from existing log files.

---

## Dependencies and ordering

```
A (shell) ──► B (Clear DB + CSRF) ──► C (Deps move) ──► D (Download Logs) ──► E (Runs table + Schedule panel)
```

**Serial, not parallel.** A must land first. B/C/D/E each touch `app.py` and `templates/admin.html` in overlapping line ranges — attempting parallel sub-branches produces constant merge conflicts. Execute them in the order above on sub-branches `feature/admin-tab-<issue>` that PR into the primary `feature/admin-tab` branch. The primary branch PRs into `main` after all children land.

Recommended order rationale:
- **A first** — blocker for everything.
- **B second** — CSRF is a latent security issue; fix it as soon as the tab exists.
- **C third** — trivial UI move, keeps momentum.
- **D fourth** — introduces `paths.py`, which has no dependents yet; isolates the infrastructure refactor from the larger Phase 5 change.
- **E last** — largest change (new DB table + `ingest.py` rewiring + new route + compose config change). Land everything else first so E has a clean base.

Close #175 when E's PR opens, with a comment pointing to E.
