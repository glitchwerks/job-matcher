"""
app.py — Flask web server for Job Matcher.

Thin routing layer only. All data access goes through db.py.
Business logic lives in ingest.py; none of it belongs here.
"""

import json
import os

from flask import Flask, render_template, make_response, request

import db

app = Flask(__name__)

DB_PATH: str = os.environ.get("DB_PATH", "jobs.db")
_KEYS_PATH: str = os.path.join(os.path.dirname(__file__), "keys.json")

# Default structure mirrors keys.example.json — used when keys.json is absent.
_KEYS_DEFAULTS: dict = {
    "providers": {
        "anthropic": {"api_key": "", "model": "claude-haiku-4-5-20251001"},
        "openai":    {"api_key": "", "model": "gpt-4o-mini"},
        "gemini":    {"api_key": "", "model": "gemini-1.5-flash"},
    },
    "preferred_provider": "anthropic",
}


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
db.init_db(db_path=DB_PATH)


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
    """Main feed: listings scored at or above the configured threshold.

    Accepts optional query params for filtering:
      - min_score: float override for the score floor
      - remote_only: "1" to restrict to remote listings
      - search: text matched against title and company
    """
    threshold = CONFIG["scoring"]["threshold"]

    min_score_raw = request.args.get("min_score")
    try:
        min_score = float(min_score_raw) if min_score_raw else None
    except ValueError:
        min_score = None
    remote_only = request.args.get("remote_only") == "1"
    search = request.args.get("search", "").strip() or None
    job_type = request.args.get("job_type", "").strip() or None

    listings = db.get_feed(
        threshold=threshold,
        min_score=min_score,
        remote_only=remote_only,
        search=search,
        job_type=job_type,
        db_path=DB_PATH,
    )
    job_types = db.get_job_types(db_path=DB_PATH)
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
    )


@app.route("/bookmarks")
def bookmarks():
    """Bookmarked listings only."""
    listings = db.get_bookmarks(db_path=DB_PATH)
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
    listing = db.get_listing_by_id(listing_id, db_path=DB_PATH)
    if listing is None:
        return make_response("", 404)

    new_value = 0 if listing["bookmarked"] else 1
    db.set_bookmarked(listing_id, new_value, db_path=DB_PATH)

    # Re-fetch to get the authoritative updated state.
    listing = db.get_listing_by_id(listing_id, db_path=DB_PATH)
    return render_template("_actions.html", listing=listing)


@app.route("/apply/<int:listing_id>", methods=["POST"])
def toggle_apply(listing_id: int):
    """Toggle the applied state for a listing.

    Reads the current state, flips it, writes it back, then returns the
    re-rendered action button group as an HTMX partial. Same read-modify-write
    pattern as toggle_bookmark — only the action row is swapped in the DOM.
    """
    listing = db.get_listing_by_id(listing_id, db_path=DB_PATH)
    if listing is None:
        return make_response("", 404)

    new_value = 0 if listing["applied"] else 1
    db.set_applied(listing_id, new_value, db_path=DB_PATH)

    # Re-fetch to get the authoritative updated state.
    listing = db.get_listing_by_id(listing_id, db_path=DB_PATH)
    return render_template("_actions.html", listing=listing)


@app.route("/applied")
def applied():
    """Applied listings — all listings marked as applied, most recent first."""
    listings = db.get_applied(db_path=DB_PATH)
    return render_template(
        "index.html",
        listings=listings,
        view="applied",
    )


@app.route("/stats")
def stats():
    """API usage and cost statistics."""
    data = db.get_usage_stats(db_path=DB_PATH)
    return render_template("stats.html", stats=data, view="stats")


@app.route("/dismiss/<int:listing_id>", methods=["POST"])
def dismiss(listing_id: int):
    """Dismiss a listing.

    Returns an empty 200 response. HTMX is configured to swap `outerHTML`
    on the card element, replacing it with the empty string — this removes
    the card from the DOM without a page reload.
    """
    db.set_dismissed(listing_id, 1, db_path=DB_PATH)
    return make_response("", 200)


def _load_keys() -> dict:
    """Load keys.json if it exists, otherwise return a copy of the defaults.

    Returns a deep copy so callers can mutate freely without touching the
    module-level default structure.
    """
    import copy
    if not os.path.exists(_KEYS_PATH):
        return copy.deepcopy(_KEYS_DEFAULTS)
    try:
        with open(_KEYS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        # Guarantee every expected provider key exists, using defaults as
        # fallback for any provider absent from the file.
        data.setdefault("providers", {})
        for provider, defaults in _KEYS_DEFAULTS["providers"].items():
            data["providers"].setdefault(provider, copy.deepcopy(defaults))
            data["providers"][provider].setdefault("api_key", "")
            data["providers"][provider].setdefault("model", defaults["model"])
        data.setdefault("preferred_provider", _KEYS_DEFAULTS["preferred_provider"])
        return data
    except (json.JSONDecodeError, OSError):
        return copy.deepcopy(_KEYS_DEFAULTS)


@app.route("/settings", methods=["GET", "POST"])
def settings():
    """Settings page — manage API keys and preferred provider.

    GET:  Reads keys.json and passes only boolean has_key flags to the
          template — raw key values are never sent to the browser.
    POST: Merges submitted form values over the existing keys.json.
          A blank key field means "keep existing"; a non-blank field
          replaces the stored key. Model is always updated (not secret).
    """
    saved = False

    if request.method == "POST":
        keys_data = _load_keys()

        for provider in ("anthropic", "openai", "gemini"):
            submitted_key = request.form.get(f"{provider}_key", "").strip()
            submitted_model = request.form.get(f"{provider}_model", "").strip()

            # Only update the stored key when the field was filled in.
            if submitted_key:
                keys_data["providers"][provider]["api_key"] = submitted_key

            # Model is never secret — always round-trip whatever was submitted.
            if submitted_model:
                keys_data["providers"][provider]["model"] = submitted_model

        preferred = request.form.get("preferred_provider", "").strip()
        if preferred in ("anthropic", "openai", "gemini"):
            keys_data["preferred_provider"] = preferred

        with open(_KEYS_PATH, "w", encoding="utf-8") as f:
            json.dump(keys_data, f, indent=2)

        saved = True
        # Re-load the just-written file so the template reflects current state.
        keys_data = _load_keys()
    else:
        keys_data = _load_keys()

    # Build the template context — only booleans for key presence, never values.
    providers_ctx = {}
    for provider in ("anthropic", "openai", "gemini"):
        cfg = keys_data["providers"][provider]
        providers_ctx[provider] = {
            "has_key": bool(cfg.get("api_key", "").strip()),
            "model": cfg.get("model", _KEYS_DEFAULTS["providers"][provider]["model"]),
        }

    return render_template(
        "settings.html",
        view="settings",
        providers=providers_ctx,
        preferred_provider=keys_data.get("preferred_provider", "anthropic"),
        saved=saved,
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    app.run(debug=debug, port=5000)
