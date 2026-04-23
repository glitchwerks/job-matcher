"""Feed blueprint — read-only listing views and action endpoints.

Owns the 10 routes that display and interact with job listings:
  GET  /                            main scored feed
  GET  /feed/fragment               HTMX fragment refresh after ingest
  GET  /bookmarks                   bookmarked listings
  POST /bookmark/<id>               toggle bookmark
  POST /apply/<id>                  toggle applied
  GET  /applied                     applied listings
  GET  /snippets                    snippet-scored listings
  GET  /stats                       API usage stats
  POST /dismiss/<id>                dismiss a listing
  POST /listings/<id>/open          mark listing opened
"""

from __future__ import annotations

from flask import Blueprint, make_response, render_template, request

import db
from services import ingest_control
from services.provider_schemas import _config_warnings

def _get_config() -> dict:
    """Return the current CONFIG dict from ``services.profile_store``.

    Calls ``load_config()`` at request time so that test fixtures using
    ``monkeypatch.setattr(profile_store, "_CONFIG_PATH", ...)`` are
    picked up on each request.  Returns an empty dict on any read error
    so the feed can still render with default values.

    Returns:
        The current config dict, or ``{}`` if the file is absent or
        malformed.
    """
    import services.profile_store as _ps
    try:
        return _ps.load_config(_ps._CONFIG_PATH)
    except SystemExit:
        return {}


def _get_providers_path() -> str:
    """Return the current _PROVIDERS_PATH from ``services.profile_store``.

    Reads the attribute from the canonical module at call time so that
    ``monkeypatch.setattr(profile_store, "_PROVIDERS_PATH", ...)`` in
    the test suite takes effect for feed routes without any coupling to
    ``app.py``.

    Returns:
        The providers.json file path string.
    """
    import services.profile_store as _ps
    return _ps._PROVIDERS_PATH


feed_bp = Blueprint("feed", __name__)


@feed_bp.route("/")
def feed():
    """Main feed: listings scored at or above the configured threshold.

    Accepts optional query params for filtering:
      - min_score: float override for the score floor
      - remote_only: "1" to restrict to remote listings
      - search: text matched against title and company
      - job_type: filter by contract/job type
      - sort: sort order key
    """
    config = _get_config()
    threshold = config.get("scoring", {}).get("threshold", 7.0)
    if not isinstance(threshold, (int, float)) or threshold < 0:
        threshold = 7.0

    min_score_raw = request.args.get("min_score")
    try:
        min_score = float(min_score_raw) if min_score_raw else None
    except ValueError:
        min_score = None
    remote_only = request.args.get("remote_only") == "1"
    search = request.args.get("search", "").strip() or None
    job_type = request.args.get("job_type", "").strip() or None
    sort = request.args.get("sort", "").strip() or None

    listings = db.get_feed(
        threshold=threshold,
        min_score=min_score,
        remote_only=remote_only,
        search=search,
        job_type=job_type,
        sort=sort,
    )
    job_types = db.get_job_types()
    last_fetch_time = db.get_last_fetch_time()
    new_count = sum(
        1 for listing in listings if listing["opened_at"] is None
    )
    return render_template(
        "index.html",
        listings=listings,
        view="feed",
        threshold=threshold,
        min_score=min_score,
        remote_only=remote_only,
        search=search,
        job_type=job_type,
        job_types=job_types,
        sort=sort,
        last_fetch_time=last_fetch_time,
        new_count=new_count,
        config_warnings=_config_warnings(
            providers_path=_get_providers_path()
        ),
        running=ingest_control._ingest_running(),
    )


@feed_bp.route("/feed/fragment")
def feed_fragment():
    """Feed content fragment — listing cards only (no page chrome).

    Used by the ``ingestComplete`` HTMX listener to refresh just the
    ``#feed-content`` container after an ingest run completes, without
    reloading the full page (which would destroy the ingest drawer).

    Accepts the same filter query params as ``/``:
      - min_score, remote_only, search, job_type, sort
    """
    config = _get_config()
    threshold = config.get("scoring", {}).get("threshold", 7.0)
    if not isinstance(threshold, (int, float)) or threshold < 0:
        threshold = 7.0

    min_score_raw = request.args.get("min_score")
    try:
        min_score = float(min_score_raw) if min_score_raw else None
    except ValueError:
        min_score = None
    remote_only = request.args.get("remote_only") == "1"
    search = request.args.get("search", "").strip() or None
    job_type = request.args.get("job_type", "").strip() or None
    sort = request.args.get("sort", "").strip() or None

    listings = db.get_feed(
        threshold=threshold,
        min_score=min_score,
        remote_only=remote_only,
        search=search,
        job_type=job_type,
        sort=sort,
    )
    last_fetch_time = db.get_last_fetch_time()
    new_count = sum(
        1 for listing in listings if listing["opened_at"] is None
    )
    resp = make_response(
        render_template(
            "_feed_fragment.html",
            listings=listings,
            threshold=threshold,
            new_count=new_count,
            last_fetch_time=last_fetch_time,
        ),
        200,
    )
    resp.headers["Content-Type"] = "text/html"
    return resp


@feed_bp.route("/bookmarks")
def bookmarks():
    """Bookmarked listings only."""
    listings = db.get_bookmarks()
    return render_template(
        "index.html",
        listings=listings,
        view="bookmarks",
        config_warnings=_config_warnings(
            providers_path=_get_providers_path()
        ),
    )


@feed_bp.route("/bookmark/<int:listing_id>", methods=["POST"])
def toggle_bookmark(listing_id: int):
    """Toggle the bookmarked state for a listing.

    Delegates to db.toggle_bookmarked(), which performs the flip
    atomically so rapid double-clicks cannot produce a net no-op.
    Returns the re-rendered action button group as an HTMX partial.

    Args:
        listing_id: Primary key of the listing to bookmark.

    Returns:
        Rendered ``_actions.html`` partial, or empty 404 if not found.
    """
    listing = db.toggle_bookmarked(listing_id)
    if listing is None:
        return make_response("", 404)
    return render_template("_actions.html", listing=listing)


@feed_bp.route("/apply/<int:listing_id>", methods=["POST"])
def toggle_apply(listing_id: int):
    """Toggle the applied state for a listing.

    Delegates to db.toggle_applied(), which performs the flip
    atomically so rapid double-clicks cannot produce a net no-op.
    Returns the re-rendered action button group as an HTMX partial.

    Args:
        listing_id: Primary key of the listing to toggle.

    Returns:
        Rendered ``_actions.html`` partial, or empty 404 if not found.
    """
    listing = db.toggle_applied(listing_id)
    if listing is None:
        return make_response("", 404)
    return render_template("_actions.html", listing=listing)


@feed_bp.route("/applied")
def applied():
    """Applied listings — all listings marked as applied, most recent first."""
    listings = db.get_applied()
    return render_template(
        "index.html",
        listings=listings,
        view="applied",
        config_warnings=_config_warnings(
            providers_path=_get_providers_path()
        ),
    )


@feed_bp.route("/snippets")
def snippets():
    """Snippet-scored listings.

    Roles scored from short API descriptions rather than full JDs.
    Accepts the same filter query params as the main feed: ``sort``,
    ``search``, ``remote_only``, ``job_type``, and ``min_score``.
    """
    sort = request.args.get("sort", "").strip() or None
    search = request.args.get("search", "").strip() or None
    remote_only = request.args.get("remote_only") == "1"
    job_type = request.args.get("job_type", "").strip() or None
    raw_min_score = request.args.get("min_score", "").strip()
    min_score: float | None = None
    if raw_min_score:
        try:
            min_score = float(raw_min_score)
        except ValueError:
            min_score = None

    config = _get_config()
    threshold = config.get("scoring", {}).get("threshold", 7.0)
    if not isinstance(threshold, (int, float)) or threshold < 0:
        threshold = 7.0
    job_types = db.get_job_types()
    listings = db.get_snippet_feed(
        threshold=threshold,
        min_score=min_score,
        remote_only=remote_only,
        search=search,
        job_type=job_type,
        sort=sort,
    )
    return render_template(
        "snippets.html",
        listings=listings,
        view="snippets",
        sort=sort,
        search=search,
        remote_only=remote_only,
        job_type=job_type,
        job_types=job_types,
        threshold=threshold,
        min_score=min_score,
        config_warnings=_config_warnings(
            providers_path=_get_providers_path()
        ),
    )


@feed_bp.route("/stats")
def stats():
    """API usage and cost statistics, plus runtime version information."""
    data = db.get_usage_stats()
    return render_template(
        "stats.html",
        stats=data,
        view="stats",
        config_warnings=_config_warnings(
            providers_path=_get_providers_path()
        ),
    )


@feed_bp.route("/dismiss/<int:listing_id>", methods=["POST"])
def dismiss(listing_id: int):
    """Dismiss a listing.

    Returns an empty 200 response. HTMX is configured to swap
    ``outerHTML`` on the card element, replacing it with the empty
    string — this removes the card from the DOM without a page reload.

    Args:
        listing_id: Primary key of the listing to dismiss.
    """
    db.set_dismissed(listing_id, 1)
    return make_response("", 200)


@feed_bp.route("/listings/<int:listing_id>/open", methods=["POST"])
def mark_listing_opened(listing_id: int):
    """Mark a listing as opened (first-time expand) and clear its New badge.

    Called fire-and-forget by HTMX when the user expands a card for the
    first time.  The operation is idempotent — if the listing is already
    marked opened, the DB write is a no-op.

    Returns an HTMX out-of-band swap fragment that removes the badge-new
    element from the DOM immediately.  The CSS rule
    ``.card-details[open] .badge-new`` is kept as a belt-and-suspenders
    fallback, but some browsers do not trigger a style recalculation for
    ``<summary>`` descendants when ``<details>`` gains [open], so relying
    solely on CSS is not reliable across all browsers.

    Args:
        listing_id: Primary key of the listing to mark as opened.

    Returns:
        An ``hx-swap-oob`` fragment that removes the badge element.
    """
    db.mark_opened(listing_id)
    oob_fragment = (
        f'<span id="badge-new-{listing_id}" hx-swap-oob="outerHTML"></span>'
    )
    return oob_fragment, 200
