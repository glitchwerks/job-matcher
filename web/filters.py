"""Jinja2 template filters for the Job Matcher web layer.

Contains pure-function filters registered with the Flask app via
``web.__init__.create_app()``.  None of these functions import from
``app`` or touch Flask application state directly — registration is
the caller's responsibility.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional


def salary_fmt(listing: dict) -> Optional[str]:
    """Format a salary range from a listing dict.

    Returns a string like ``'$120k–$160k'``, ``'~$130k–$155k'``
    (predicted), ``'$120k+'`` (min only), or ``None`` if both salary
    fields are absent.

    Keeping this in Python rather than Jinja keeps the template
    readable and the formatting logic testable.

    Args:
        listing: A job-listing dict that may contain ``salary_min``,
            ``salary_max``, and ``salary_is_predicted`` keys.

    Returns:
        A formatted salary string, or ``None`` when no salary data
        is present.
    """
    lo = listing.get("salary_min")
    hi = listing.get("salary_max")
    predicted = listing.get("salary_is_predicted")

    if lo is None and hi is None:
        return None

    prefix = "~" if predicted else ""

    def fmt_k(val: int | float) -> str:
        """Format a raw salary value as a rounded ``$Nk`` string."""
        k = int(round(val / 1000))
        return f"${k}k"

    if lo is not None and hi is not None:
        return f"{prefix}{fmt_k(lo)}–{fmt_k(hi)}"
    if lo is not None:
        return f"{prefix}{fmt_k(lo)}+"
    return f"{prefix}{fmt_k(hi)}"


def parse_iso(value: Optional[str]) -> Optional[datetime]:
    """Parse an ISO 8601 string (with or without trailing ``'Z'``) into a datetime.

    Returns ``None`` when ``value`` is ``None``, empty, or cannot be
    parsed so that downstream filters (e.g. ``timeago``) can handle
    the ``None`` case gracefully.

    Args:
        value: An ISO 8601 datetime string, e.g.
            ``"2024-01-15T12:30:00Z"``.

    Returns:
        A ``datetime`` object, or ``None`` on parse failure.
    """
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.rstrip("Z"))
    except (ValueError, AttributeError):
        return None


def timeago(dt: Optional[datetime]) -> str:
    """Return a human-readable relative time string for a datetime.

    Uses UTC now as the reference point.  The input datetime is
    treated as UTC if it has no ``tzinfo``.  Falls back to the ISO
    8601 string representation when the input is ``None`` or not a
    ``datetime``, so the template never raises.

    Thresholds::

        < 2 minutes  → 'just now'
        < 60 minutes → 'N minutes ago'
        < 24 hours   → 'N hours ago'
        < 7 days     → 'N days ago'
        otherwise    → 'YYYY-MM-DD HH:MM UTC'

    Args:
        dt: A ``datetime`` object, or ``None``.

    Returns:
        A human-readable relative time string.
    """
    if dt is None:
        return "never"
    if not isinstance(dt, datetime):
        return str(dt)

    # Treat naive datetimes as UTC to match how fetched_at is stored.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    now = datetime.now(tz=timezone.utc)
    delta = now - dt
    total_seconds = int(delta.total_seconds())

    if total_seconds < 0:
        # Clock skew or future timestamp — show absolute.
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    if total_seconds < 120:
        return "just now"
    if total_seconds < 3600:
        minutes = total_seconds // 60
        return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    if total_seconds < 86400:
        hours = total_seconds // 3600
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    if total_seconds < 604800:
        days = total_seconds // 86400
        return f"{days} day{'s' if days != 1 else ''} ago"
    return dt.strftime("%Y-%m-%d %H:%M UTC")
