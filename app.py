"""
app.py — Flask web server for Job Matcher.

Thin routing layer only. All data access goes through db.py.
Business logic lives in ingest.py; none of it belongs here.
"""

import json
import os

from flask import Flask, render_template, make_response

import db

app = Flask(__name__)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config(path: str = "config.json") -> dict:
    """Load config.json if it exists; return safe defaults otherwise.

    This allows the server to start and display the UI even before the user
    has created their config file.
    """
    defaults = {
        "scoring": {
            "threshold": 7.0,
        }
    }
    if not os.path.exists(path):
        return defaults
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        # Ensure scoring.threshold has a fallback even if key is missing.
        data.setdefault("scoring", {})
        data["scoring"].setdefault("threshold", 7.0)
        return data
    except (json.JSONDecodeError, OSError):
        return defaults


CONFIG = load_config()


# ---------------------------------------------------------------------------
# Template filter
# ---------------------------------------------------------------------------

@app.template_filter("salary_fmt")
def salary_fmt(listing: dict) -> str | None:
    """Format a salary range from a listing dict.

    Returns a string like '$120k–$160k', '~$130k–$155k' (predicted),
    '$120k' (min only), or None if both salary fields are absent.

    Keeping this in Python rather than Jinja keeps the template readable and
    the formatting logic testable.
    """
    lo = listing.get("salary_min")
    hi = listing.get("salary_max")
    predicted = listing.get("salary_is_predicted")

    if lo is None and hi is None:
        return None

    prefix = "~" if predicted else ""

    def fmt_k(val: int | float) -> str:
        k = int(round(val / 1000))
        return f"${k}k"

    if lo is not None and hi is not None:
        return f"{prefix}{fmt_k(lo)}–{fmt_k(hi)}"
    if lo is not None:
        return f"{prefix}{fmt_k(lo)}+"
    return f"{prefix}{fmt_k(hi)}"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def feed():
    """Main feed: listings scored at or above the configured threshold."""
    threshold = CONFIG["scoring"]["threshold"]
    listings = db.get_feed(threshold)
    return render_template(
        "index.html",
        listings=listings,
        view="feed",
        threshold=threshold,
    )


@app.route("/bookmarks")
def bookmarks():
    """Bookmarked listings only."""
    listings = db.get_bookmarks()
    return render_template(
        "index.html",
        listings=listings,
        view="bookmarks",
    )


@app.route("/bookmark/<int:listing_id>", methods=["POST"])
def toggle_bookmark(listing_id: int):
    """Toggle the bookmarked state for a listing.

    Reads the current state, flips it, writes it back, then returns the
    re-rendered action button group as an HTMX partial. HTMX swaps this
    into the DOM in place of the existing action row, so the star icon
    updates without a full page reload.
    """
    listing = db.get_listing_by_id(listing_id)
    if listing is None:
        return make_response("", 404)

    new_value = 0 if listing["bookmarked"] else 1
    db.set_bookmarked(listing_id, new_value)

    # Re-fetch to get the authoritative updated state.
    listing = db.get_listing_by_id(listing_id)
    return render_template("_actions.html", listing=listing)


@app.route("/dismiss/<int:listing_id>", methods=["POST"])
def dismiss(listing_id: int):
    """Dismiss a listing.

    Returns an empty 200 response. HTMX is configured to swap `outerHTML`
    on the card element, replacing it with the empty string — this removes
    the card from the DOM without a page reload.
    """
    db.set_dismissed(listing_id, 1)
    return make_response("", 200)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app.run(debug=True, port=5000)
