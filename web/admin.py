"""Admin blueprint — database management, log downloads, and schedule health.

Owns the 5 routes for administration tasks:
  GET   /admin                            administration page
  POST  /admin/clear-db                   delete all listings
  GET   /admin/logs                       list available ingest log files
  GET   /admin/logs/<filename>/download   download an ingest log file
  GET   /admin/schedule-state             ingest run history and badge

Also owns:
  _LOG_FILENAME_RE      — strict regex for ingest log filenames
  SCHEDULE_WARN_HOURS   — amber-badge threshold (hours since last run)
  SCHEDULE_CRITICAL_HOURS — red-badge threshold (hours since last run)
"""

from __future__ import annotations

import os
import re
import secrets
from datetime import datetime, timezone

from flask import (
    Blueprint,
    abort,
    make_response,
    render_template,
    request,
    send_from_directory,
    session,
)

import db
from paths import LOG_DIR
from services.provider_schemas import RUNTIME_VERSIONS

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

_LOG_FILENAME_RE = re.compile(
    r"^ingest_(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})\.log$"
)

# Scheduler health thresholds (hours since last scheduled run).
SCHEDULE_WARN_HOURS = 25
SCHEDULE_CRITICAL_HOURS = 49

admin_bp = Blueprint("admin_bp", __name__)


@admin_bp.route("/admin", endpoint="admin")
def admin():
    """Administration page — runtime info, log downloads, ingest schedule.

    Establishes the session CSRF token required by the danger-zone
    ``/admin/clear-db`` action and renders the administration page with
    listing count and runtime version information.

    Returns:
        Rendered ``admin.html`` template with status 200.
    """
    session.setdefault("csrf_token", secrets.token_urlsafe(32))
    listing_count = db.get_listing_count()
    return render_template(
        "admin.html",
        view="admin",
        listing_count=listing_count,
        csrf_token=session["csrf_token"],
        runtime_versions=RUNTIME_VERSIONS,
    )


@admin_bp.route("/admin/clear-db", methods=["POST"],
                endpoint="admin_clear_db")
def admin_clear_db():
    """Delete all rows from the listings table.

    Requires the ``confirmation`` form field to equal exactly
    ``"DELETE"`` (case-sensitive).  Any other value is rejected with
    400 so that a misconfigured HTMX request or stray form submit cannot
    wipe data silently.

    On success the deleted row count is logged with a UTC timestamp and
    an HTMX-compatible HTML fragment is returned so the caller can swap
    it into the confirmation panel target.

    Returns:
        200 HTML fragment on success.
        400 HTML fragment when the confirmation phrase is wrong or the
            CSRF token is invalid.
        500 HTML fragment on database error.
    """
    from flask import current_app  # noqa: PLC0415

    # CSRF check — token must match the session value from GET /admin.
    csrf_token = request.form.get("csrf_token", "")
    if not csrf_token or csrf_token != session.get("csrf_token"):
        html = (
            '<p class="save-error" id="clear-db-result">'
            "Invalid or missing CSRF token — request rejected."
            "</p>"
        )
        return make_response(html, 400)

    confirmation = request.form.get("confirmation", "").strip()

    if confirmation != "DELETE":
        html = (
            '<p class="save-error" id="clear-db-result">'
            "Confirmation phrase did not match — database was not"
            " cleared."
            "</p>"
        )
        return make_response(html, 400)

    try:
        conn = db.get_connection()
        try:
            deleted = db.clear_all_listings(conn)
        finally:
            conn.close()
    except Exception as exc:  # pragma: no cover — DB errors rare in tests
        current_app.logger.error(
            "clear_all_listings failed: %s", exc
        )
        html = (
            '<p class="save-error" id="clear-db-result">'
            f"Database error — listings were not cleared: {exc}"
            "</p>"
        )
        return make_response(html, 500)

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    current_app.logger.info(
        "[%s] admin/clear-db: deleted %d listing(s).", ts, deleted
    )

    noun = "listing" if deleted == 1 else "listings"
    html = (
        f'<p class="save-notice" id="clear-db-result">'
        f"{deleted} {noun} deleted successfully."
        f"</p>"
        f'<div id="clear-db-panel" style="display:none"></div>'
    )
    return make_response(html, 200)


@admin_bp.route("/admin/logs", endpoint="admin_logs")
def admin_logs():
    """Return an HTML fragment listing available ingest log files.

    Scans ``LOG_DIR`` for files matching ``_LOG_FILENAME_RE``, sorts
    newest first, and renders the ``admin/_log_list.html`` fragment.

    Returns:
        Rendered ``admin/_log_list.html`` template fragment.
    """
    logs = []
    try:
        for entry in os.scandir(LOG_DIR):
            if not entry.is_file():
                continue
            m = _LOG_FILENAME_RE.match(entry.name)
            if not m:
                continue
            try:
                size = entry.stat().st_size
            except OSError:
                continue
            timestamp = (
                f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
                f" {m.group(4)}:{m.group(5)}:{m.group(6)}"
            )
            if size >= 1_048_576:
                size_str = f"{size / 1_048_576:.1f} MB"
            elif size >= 1024:
                size_str = f"{size / 1024:.1f} KB"
            else:
                size_str = f"{size} B"
            logs.append({
                "filename": entry.name,
                "timestamp": timestamp,
                "size": size_str,
            })
    except FileNotFoundError:
        pass  # LOG_DIR doesn't exist yet — return empty list

    logs.sort(key=lambda x: x["filename"], reverse=True)
    return render_template("admin/_log_list.html", logs=logs)


@admin_bp.route(
    "/admin/logs/<filename>/download",
    endpoint="admin_log_download"
)
def admin_log_download(filename: str):
    """Download an ingest log file.

    Validates ``filename`` against ``_LOG_FILENAME_RE`` and performs a
    symlink-escape check before serving.

    Args:
        filename: The log filename from the URL path segment.

    Returns:
        The log file as an attachment with MIME type
        ``text/plain; charset=utf-8``.

    Raises:
        404: If the filename fails validation, resolves outside
             ``LOG_DIR``, or does not exist on disk.
    """
    if not _LOG_FILENAME_RE.match(filename):
        abort(404)

    target = (LOG_DIR / filename).resolve()

    # Symlink escape check — resolved path must be inside LOG_DIR.
    try:
        target.relative_to(LOG_DIR.resolve())
    except ValueError:
        abort(404)

    if not target.is_file():
        abort(404)

    return send_from_directory(
        LOG_DIR,
        filename,
        as_attachment=True,
        mimetype="text/plain; charset=utf-8",
    )


@admin_bp.route("/admin/schedule-state", endpoint="admin_schedule_state")
def admin_schedule_state():
    """Return an HTML fragment showing ingest run history and health badge.

    Computes a badge colour (green / amber / red / none) based on the
    age and status of the most recent scheduled run, then renders the
    ``admin/_schedule_state.html`` fragment.

    A database error is caught and treated as an empty run list so that
    a DB outage never breaks the admin page.

    Returns:
        Rendered ``admin/_schedule_state.html`` template fragment.
    """
    try:
        runs = db.get_recent_ingest_runs(10)
    except Exception:  # noqa: BLE001 — schedule state is best-effort
        runs = []

    badge = "none"
    badge_text = "No runs recorded yet"

    if runs:
        scheduled_runs = [
            r for r in runs if r.get("trigger_source") == "scheduled"
        ]

        if scheduled_runs:
            last_scheduled = scheduled_runs[0]
            age_hours = None
            if last_scheduled.get("started_at"):
                started = last_scheduled["started_at"]
                if hasattr(started, "tzinfo") and started.tzinfo:
                    now = datetime.now(timezone.utc)
                else:
                    now = datetime.utcnow()  # noqa: DTZ003
                age_hours = (now - started).total_seconds() / 3600

            if last_scheduled.get("status") == "failed":
                badge = "red"
                badge_text = "Last scheduled run failed"
            elif (
                age_hours is not None
                and age_hours > SCHEDULE_CRITICAL_HOURS
            ):
                badge = "red"
                badge_text = (
                    f"Scheduler may be down — no scheduled run in"
                    f" {SCHEDULE_CRITICAL_HOURS}+ hours"
                )
            elif (
                age_hours is not None
                and age_hours > SCHEDULE_WARN_HOURS
            ):
                badge = "amber"
                badge_text = (
                    f"Last scheduled run was {SCHEDULE_WARN_HOURS}+"
                    " hours ago"
                )
            elif last_scheduled.get("status") == "running":
                badge = "amber"
                badge_text = "Scheduled run in progress"
            else:
                badge = "green"
                badge_text = "Scheduler healthy"
        else:
            badge = "none"
            badge_text = "No scheduled runs recorded"

    return render_template(
        "admin/_schedule_state.html",
        runs=runs,
        badge=badge,
        badge_text=badge_text,
    )
