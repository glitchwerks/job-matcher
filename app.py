"""
app.py — Flask web server for Job Matcher.

Thin routing layer only. All data access goes through db.py.
Business logic lives in ingest.py; none of it belongs here.
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from importlib.metadata import version as pkg_version, PackageNotFoundError

from flask import Flask, render_template, make_response, request, jsonify

import db

app = Flask(__name__)

DB_PATH: str = os.environ.get("DB_PATH", "jobs.db")
_KEYS_PATH: str = os.path.join(os.path.dirname(__file__), "keys.json")
_CONFIG_PATH: str = os.path.join(os.path.dirname(__file__), "config.json")

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
# Runtime version capture
# ---------------------------------------------------------------------------

def get_runtime_versions() -> list[dict]:
    """Return a list of {component, version} dicts for key runtime components.

    Called once at startup and cached in RUNTIME_VERSIONS. Each package lookup
    is wrapped in a try/except so a missing optional package (e.g. gunicorn)
    never crashes the server — it surfaces as 'n/a' instead.

    App version resolution order:
      1. VERSION file in the same directory as this module.
      2. Latest git tag via ``git describe --tags --abbrev=0``.
      3. Fallback string "dev".
    """
    def _pkg(name: str) -> str:
        try:
            return pkg_version(name)
        except PackageNotFoundError:
            return "n/a"

    # Python version — use the compact x.y.z form from version_info.
    python_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"

    # App version: VERSION file → git tag → "dev".
    version_file = os.path.join(os.path.dirname(__file__), "VERSION")
    if os.path.exists(version_file):
        with open(version_file, "r", encoding="utf-8") as fh:
            app_ver = fh.read().strip() or "dev"
    else:
        try:
            result = subprocess.run(
                ["git", "describe", "--tags", "--abbrev=0"],
                capture_output=True,
                text=True,
                cwd=os.path.dirname(__file__) or ".",
            )
            app_ver = result.stdout.strip() if result.returncode == 0 else "dev"
        except OSError:
            app_ver = "dev"

    return [
        {"component": "App",          "version": app_ver},
        {"component": "Python",       "version": python_ver},
        {"component": "Flask",        "version": _pkg("flask")},
        {"component": "anthropic",    "version": _pkg("anthropic")},
        {"component": "beautifulsoup4", "version": _pkg("beautifulsoup4")},
        {"component": "waitress",     "version": _pkg("waitress")},
    ]


RUNTIME_VERSIONS: list[dict] = get_runtime_versions()


# ---------------------------------------------------------------------------
# Config warnings
# ---------------------------------------------------------------------------

def _config_warnings() -> list[str]:
    """Return a list of human-readable warnings for missing/empty config."""
    warnings = []
    cfg = load_config()
    adzuna_id  = cfg.get("adzuna_app_id", "").strip()
    adzuna_key = cfg.get("adzuna_app_key", "").strip()
    # Also check env vars (ingest.py can override via env)
    if not adzuna_id:
        adzuna_id = os.environ.get("ADZUNA_APP_ID", "").strip()
    if not adzuna_key:
        adzuna_key = os.environ.get("ADZUNA_APP_KEY", "").strip()
    if not adzuna_id or not adzuna_key:
        warnings.append(
            "Adzuna credentials are not configured — ingest will not run. "
            "Add your App ID and App Key on the <a href=\"/settings\">Settings page</a>."
        )
    return warnings


# ---------------------------------------------------------------------------
# Template filters
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


@app.template_filter("parse_iso")
def parse_iso(value: str | None) -> datetime | None:
    """Parse an ISO 8601 string (with or without trailing 'Z') into a datetime.

    Returns None when ``value`` is None, empty, or cannot be parsed so that
    downstream filters (e.g. ``timeago``) can handle the None case gracefully.
    """
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.rstrip("Z"))
    except (ValueError, AttributeError):
        return None


@app.template_filter("timeago")
def timeago(dt: datetime | None) -> str:
    """Return a human-readable relative time string for a datetime, e.g. '3 hours ago'.

    Uses UTC now as the reference point. The input datetime is treated as UTC
    if it has no tzinfo. Falls back to the ISO 8601 string representation when
    the input is None or not a datetime, so the template never raises.

    Thresholds:
      < 2 minutes  → 'just now'
      < 60 minutes → 'N minutes ago'
      < 24 hours   → 'N hours ago'
      < 7 days     → 'N days ago'
      otherwise    → formatted as 'YYYY-MM-DD HH:MM UTC'
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
        # Clock skew or future timestamp — just show absolute.
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
    sort = request.args.get("sort", "").strip() or None

    listings = db.get_feed(
        threshold=threshold,
        min_score=min_score,
        remote_only=remote_only,
        search=search,
        job_type=job_type,
        sort=sort,
        db_path=DB_PATH,
    )
    job_types = db.get_job_types(db_path=DB_PATH)
    last_fetch_time = db.get_last_fetch_time(db_path=DB_PATH)
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
        config_warnings=_config_warnings(),
        running=_ingest_running(),
    )


@app.route("/bookmarks")
def bookmarks():
    """Bookmarked listings only."""
    listings = db.get_bookmarks(db_path=DB_PATH)
    return render_template(
        "index.html",
        listings=listings,
        view="bookmarks",
        config_warnings=_config_warnings(),
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
        config_warnings=_config_warnings(),
    )


@app.route("/stats")
def stats():
    """API usage and cost statistics, plus runtime version information."""
    data = db.get_usage_stats(db_path=DB_PATH)
    return render_template(
        "stats.html",
        stats=data,
        view="stats",
        config_warnings=_config_warnings(),
        runtime_versions=RUNTIME_VERSIONS,
    )


@app.route("/dismiss/<int:listing_id>", methods=["POST"])
def dismiss(listing_id: int):
    """Dismiss a listing.

    Returns an empty 200 response. HTMX is configured to swap `outerHTML`
    on the card element, replacing it with the empty string — this removes
    the card from the DOM without a page reload.
    """
    db.set_dismissed(listing_id, 1, db_path=DB_PATH)
    return make_response("", 200)


def _mask_config_keys(data: dict) -> dict:
    """Return a deep copy of *data* with sensitive key values replaced by '***'.

    Any key whose name (lowercased) ends in ``_api_key``, ``_app_key``, or
    ``_app_id`` is considered sensitive.  The walk is recursive so nested dicts
    (e.g. ``search`` or ``prefilter`` sub-objects) are handled too.

    The original dict is never mutated — callers always receive a fresh copy.
    This is display-only: the masked value is never written back to disk.
    """
    import copy

    _SENSITIVE_SUFFIXES = ("_api_key", "_app_key", "_app_id")

    def _walk(obj):
        if isinstance(obj, dict):
            result = {}
            for k, v in obj.items():
                if isinstance(k, str) and k.lower().endswith(_SENSITIVE_SUFFIXES):
                    result[k] = "***"
                else:
                    result[k] = _walk(v)
            return result
        if isinstance(obj, list):
            return [_walk(item) for item in obj]
        return copy.deepcopy(obj)

    return _walk(data)


# ---------------------------------------------------------------------------
# Ingestion trigger — module-level handle prevents concurrent runs
# ---------------------------------------------------------------------------

# Holds the running Popen handle while ingest.py is active. None when idle.
_ingest_process: subprocess.Popen | None = None


def _ingest_running() -> bool:
    """Return True if an ingest subprocess is currently active.

    Polls the process exit code: if poll() returns None the process is still
    running. If it has exited, reset the handle to None so a new run can start.
    """
    global _ingest_process
    if _ingest_process is None:
        return False
    if _ingest_process.poll() is not None:
        # Process has finished — clear the handle.
        _ingest_process = None
        return False
    return True


def _render_ingest_idle() -> str:
    """Return the HTML partial for the idle 'Run Ingestion' button."""
    return render_template("_ingest_trigger.html", running=False)


def _render_ingest_running() -> str:
    """Return the HTML partial for the in-progress status element."""
    return render_template("_ingest_trigger.html", running=True)


@app.route("/ingest/trigger", methods=["POST"])
def ingest_trigger():
    """Spawn ingest.py as a background subprocess.

    Returns 202 with the 'Running...' HTML partial when the process is started.
    Returns 409 with a JSON error body if a run is already in progress — the
    caller can check Content-Type to distinguish the two response shapes.

    Uses sys.executable so the subprocess runs in the same virtualenv as the
    app server, picking up all installed dependencies automatically.
    """
    global _ingest_process
    if _ingest_running():
        return jsonify({"error": "already running"}), 409

    # Build command from optional UI parameters.
    hours_raw = request.form.get("hours", "25").strip()
    rescore = request.form.get("rescore") == "1"

    try:
        hours = int(hours_raw)
    except (ValueError, TypeError):
        hours = 25

    cmd = [sys.executable, "ingest.py", "--hours", str(hours)]
    if rescore:
        cmd.append("--rescore")

    try:
        _ingest_process = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, PermissionError) as e:
        return jsonify({"error": f"Failed to start ingestion: {e}"}), 500

    resp = make_response(_render_ingest_running(), 202)
    resp.headers["Content-Type"] = "text/html"
    return resp


@app.route("/ingest/status")
def ingest_status():
    """Poll endpoint — returns an HTML partial reflecting current ingest state.

    While the process is running, returns the polling div so HTMX keeps
    refreshing. Once it stops, returns the idle button and triggers a feed
    refresh by setting HX-Trigger so the caller can react.
    """
    running = _ingest_running()
    html = _render_ingest_running() if running else _render_ingest_idle()
    resp = make_response(html, 200)
    resp.headers["Content-Type"] = "text/html"
    if not running:
        # Signal HTMX to reload the feed once the job completes.
        resp.headers["HX-Trigger"] = "ingestComplete"
    return resp


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
    """Settings page — manage API keys, preferred provider, and Adzuna credentials.

    GET:  Reads keys.json and config.json and passes only boolean has_key/has_id
          flags to the template — raw credential values are never sent to the browser.
    POST: Merges submitted form values over the existing keys.json and config.json.
          A blank field means "keep existing"; a non-blank field replaces the stored
          value. Model is always updated (not secret). Adzuna fields update config.json.
    """
    saved = False
    error = None

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

        try:
            with open(_KEYS_PATH, "w", encoding="utf-8") as f:
                json.dump(keys_data, f, indent=2)
        except OSError:
            error = "Could not save settings — check file permissions."

        if error is None:
            # Handle Adzuna credentials — load current config, merge, write back.
            adzuna_id = request.form.get("adzuna_app_id", "").strip()
            adzuna_key = request.form.get("adzuna_app_key", "").strip()
            if adzuna_id or adzuna_key:
                cfg_data = load_config(_CONFIG_PATH)
                if adzuna_id:
                    cfg_data["adzuna_app_id"] = adzuna_id
                if adzuna_key:
                    cfg_data["adzuna_app_key"] = adzuna_key
                try:
                    with open(_CONFIG_PATH, "w", encoding="utf-8") as f:
                        json.dump(cfg_data, f, indent=2)
                except OSError:
                    error = "Could not save settings — check file permissions."

        if error is None:
            saved = True

        # Re-load after write so the template reflects the current state.
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

    # Adzuna status flags — read from config.json (never pass raw values).
    cfg_data = load_config(_CONFIG_PATH)
    has_adzuna_id = bool(cfg_data.get("adzuna_app_id", "").strip())
    has_adzuna_key = bool(cfg_data.get("adzuna_app_key", "").strip())

    return render_template(
        "settings.html",
        view="settings",
        providers=providers_ctx,
        preferred_provider=keys_data.get("preferred_provider", "anthropic"),
        saved=saved,
        error=error,
        has_adzuna_id=has_adzuna_id,
        has_adzuna_key=has_adzuna_key,
    )


@app.route("/profile", methods=["GET", "POST"])
def profile():
    """Profile page — view and edit config.json via the browser.

    GET:  Reads config.json, masks any sensitive key fields (``*_api_key``,
          ``*_app_key``, ``*_app_id``), pretty-prints the result as JSON, and
          renders it in a ``<textarea>`` for editing.

    POST: Validates the submitted JSON. If parsing fails, returns a 400 with an
          inline error and leaves config.json untouched. If the JSON is valid,
          overwrites config.json and shows a success notice. Masked values
          (``"***"``) are never written back to disk — if the submitted JSON
          still contains ``"***"`` for a sensitive field, the original value
          from the current config is preserved.
    """
    saved = False
    error = None
    status_code = 200

    if request.method == "POST":
        raw = request.form.get("config_json", "")
        try:
            submitted = json.loads(raw)
        except json.JSONDecodeError as exc:
            error = f"Invalid JSON: {exc}"
            status_code = 400
            submitted = None

        if submitted is not None:
            # Merge: if a sensitive field still holds the masked sentinel,
            # preserve the original value so we never overwrite with "***".
            existing = load_config(_CONFIG_PATH)
            _SENSITIVE_SUFFIXES = ("_api_key", "_app_key", "_app_id")

            def _restore_masked(new_obj, orig_obj):
                """Recursively replace '***' sentinel values with originals."""
                if not isinstance(new_obj, dict):
                    return new_obj
                result = {}
                for k, v in new_obj.items():
                    if (
                        isinstance(k, str)
                        and k.lower().endswith(_SENSITIVE_SUFFIXES)
                        and v == "***"
                        and isinstance(orig_obj, dict)
                        and k in orig_obj
                    ):
                        result[k] = orig_obj[k]
                    elif isinstance(v, dict):
                        result[k] = _restore_masked(v, orig_obj.get(k, {}) if isinstance(orig_obj, dict) else {})
                    else:
                        result[k] = v
                return result

            to_write = _restore_masked(submitted, existing)

            try:
                with open(_CONFIG_PATH, "w", encoding="utf-8") as f:
                    json.dump(to_write, f, indent=2)
                saved = True
            except OSError:
                error = "Could not save config — check file permissions."

    # Always re-read from disk (after write or on GET) for the textarea.
    cfg_display = load_config(_CONFIG_PATH)
    masked = _mask_config_keys(cfg_display)
    config_json_str = json.dumps(masked, indent=2)

    return render_template(
        "profile.html",
        view="profile",
        config_json=config_json_str,
        saved=saved,
        error=error,
    ), status_code


@app.route("/settings/config")
def settings_config_redirect():
    from flask import redirect, url_for
    return redirect(url_for("profile"), code=301)


# ---------------------------------------------------------------------------
# Key validation
# ---------------------------------------------------------------------------

def _validate_anthropic(api_key: str, model: str) -> str:
    """Send a 1-token test call to Anthropic and return a state string.

    Returns one of: 'valid', 'invalid_key', 'unknown_model', 'unreachable'.
    The api_key is never logged or included in any return value.
    """
    import anthropic as _anthropic
    try:
        client = _anthropic.Anthropic(api_key=api_key)
        client.messages.create(
            model=model,
            max_tokens=1,
            messages=[{"role": "user", "content": "hi"}],
        )
        return "valid"
    except _anthropic.AuthenticationError:
        return "invalid_key"
    except _anthropic.PermissionDeniedError:
        return "invalid_key"
    except _anthropic.NotFoundError:
        return "unknown_model"
    except Exception:
        return "unreachable"


def _validate_openai(api_key: str, model: str) -> str:
    """Send a 1-token test call to OpenAI and return a state string.

    Returns one of: 'valid', 'invalid_key', 'unknown_model', 'unreachable'.
    The api_key is never logged or included in any return value.
    """
    import openai as _openai
    try:
        client = _openai.OpenAI(api_key=api_key)
        client.chat.completions.create(
            model=model,
            max_tokens=1,
            messages=[{"role": "user", "content": "hi"}],
        )
        return "valid"
    except _openai.AuthenticationError:
        return "invalid_key"
    except _openai.PermissionDeniedError:
        return "invalid_key"
    except _openai.NotFoundError:
        return "unknown_model"
    except Exception:
        return "unreachable"


def _validate_gemini(api_key: str, model: str) -> str:
    """Send a 1-token test call to Google Gemini and return a state string.

    Returns one of: 'valid', 'invalid_key', 'unknown_model', 'unreachable'.
    The api_key is never logged or included in any return value.

    Google's SDK raises varied exception types depending on failure mode; we
    inspect the lowercased message string to distinguish auth vs model errors.
    """
    from google import genai as _genai
    try:
        _client = _genai.Client(api_key=api_key)
        _client.models.generate_content(
            model=model,
            contents="hi",
        )
        return "valid"
    except Exception as exc:
        exc_str = str(exc).lower()
        # Google signals auth failures with specific keywords in the message.
        if any(kw in exc_str for kw in ("api_key_invalid", "invalid api key", "unauthenticated", "permission denied")):
            return "invalid_key"
        # Model-not-found errors include "not found" or "404" in the message.
        if "not found" in exc_str or "404" in exc_str:
            return "unknown_model"
        return "unreachable"


@app.route("/api/validate-keys", methods=["POST"])
def validate_keys():
    """Validate each configured LLM provider by making a minimal 1-token test call.

    Returns an HTML partial (not JSON) intended for HTMX to swap into the page.
    Each provider gets one of five states: valid, invalid_key, unknown_model,
    unreachable, not_configured.

    No API key values are logged or returned in the response.
    """
    keys_data = _load_keys()
    results = {}

    _validators = {
        "anthropic": _validate_anthropic,
        "openai":    _validate_openai,
        "gemini":    _validate_gemini,
    }

    for provider, validator in _validators.items():
        cfg = keys_data["providers"].get(provider, {})
        api_key = cfg.get("api_key", "").strip()
        model   = cfg.get("model", "").strip()

        if not api_key:
            results[provider] = "not_configured"
            continue

        try:
            results[provider] = validator(api_key, model)
        except Exception:
            # Belt-and-suspenders: a failure in one provider must not block others.
            results[provider] = "unreachable"

    return render_template("_validation_results.html", results=results)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    app.run(debug=debug, port=5000)
