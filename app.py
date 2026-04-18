"""
app.py — Flask web server for Job Matcher.

Thin routing layer only. All data access goes through db.py.
Business logic lives in ingest.py; none of it belongs here.
"""

import ipaddress
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time as _time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from importlib.metadata import version as pkg_version, PackageNotFoundError

from dotenv import load_dotenv

from flask import Flask, render_template, make_response, request, jsonify, redirect, url_for, Response, session, stream_with_context, send_from_directory, abort

import db
from credentials import CredentialError, load_providers, save_providers
from paths import LOG_DIR
from ingest_events import IngestEventParser, event_queue
from io import BytesIO

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from providers import _PROVIDER_CLASS_MAP, build_provider_chain, generate_with_fallback
from providers.anthropic_provider import strip_fences
from providers.base import _sanitise_detail
from job_sources import get_sources
from ingest import validate_search_config, ValidationIssue

# Load environment variables from a local .env file if present.
# Precedence: parent-process env (shell, VSCode task, docker env_file) always
# wins. This covers the native `python app.py` path where no external env
# loader exists; under Docker, `env_file:` has already populated os.environ
# before this runs, so load_dotenv(override=False) is a no-op.
load_dotenv(override=False)

app = Flask(__name__)

# A stable secret key is required for session-based CSRF tokens.
# Refuse to start with an empty or placeholder value — a fresh random key on
# every restart invalidates session cookies and breaks CSRF protection.
_secret_key_env = os.environ.get("SECRET_KEY", "")
if not _secret_key_env or _secret_key_env.startswith("changeme"):
    raise RuntimeError(
        "SECRET_KEY must be set to a secure random value. "
        'Generate one with: python -c "import secrets; print(secrets.token_hex(32))" '
        "and set it in .env.dev / .env.prod."
    )
app.secret_key = _secret_key_env

# Inject environment and version globals so all templates can render the status bar.
app.jinja_env.globals['APP_ENV'] = os.environ.get('APP_ENV', 'local')
app.jinja_env.globals['APP_VERSION'] = os.environ.get('APP_VERSION', 'local')
DEMO_MODE: bool = False


# ---------------------------------------------------------------------------
# CSRF guard — private-network Origin/Referer check
# ---------------------------------------------------------------------------


def _is_trusted_host(host: str) -> bool:
    """Return True if *host* is localhost or any private/non-routable address.

    Uses ``ipaddress.is_private`` which covers RFC 1918 (10.x, 172.16–31.x,
    192.168.x), loopback (127.x.x.x, ::1), link-local (169.254.x.x,
    fe80::/10), and other non-routable ranges — all stdlib, no new dependency.

    ``host`` is the bare hostname or IP string extracted from a URL — brackets
    have already been stripped from IPv6 addresses (e.g. ``::1``, not
    ``[::1]``).
    """
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_private
    except ValueError:
        return False


def _is_localhost_request() -> bool:
    """Return True if the request originates from localhost or a private LAN address.

    Checks the ``Origin`` header first (set by most browsers on same-origin
    XHR/fetch), then falls back to ``Referer``.  If neither header is present
    the request is allowed through — curl and other CLI tools do not send
    Origin/Referer, so blocking headerless requests would break admin scripts
    and testing.

    The loop logic is:
    - Header present AND regex matches AND host is trusted  → return True
    - Header present AND regex matches AND host is NOT trusted → return False
    - Header present BUT regex does not match (e.g. "null") → continue to next header
    - No usable header found after the loop → return True (allow CLI/test clients)
    """
    for header in ("Origin", "Referer"):
        value = request.headers.get(header, "").strip()
        if not value:
            continue
        # Parse just the host portion — e.g. "http://localhost:5000/path" → "localhost"
        # The IPv6 alternative captures "[::1]" (brackets included) from "http://[::1]:5000".
        match = re.match(r"https?://(\[[^\]]+\]|[^/:]+)", value)
        if not match:
            # Header present but unparseable (e.g. "null") — try next header
            continue
        host = match.group(1).lower()
        # Strip brackets from IPv6 addresses before passing to ipaddress module
        if host.startswith("[") and host.endswith("]"):
            host = host[1:-1]
        if _is_trusted_host(host):
            return True
        return False  # Non-private origin found — block
    return True  # No Origin/Referer header — allow (CLI tools, tests)


@app.context_processor
def inject_demo_mode():
    """Inject demo_mode into all template contexts."""
    return {"demo_mode": DEMO_MODE}


@app.before_request
def csrf_localhost_guard():
    """Reject state-mutating requests that do not originate from a private network.

    This tool is designed for local/LAN use only.  Any POST, PUT, PATCH, or
    DELETE request whose ``Origin`` or ``Referer`` header resolves to a
    publicly-routable host is rejected with 403 to prevent cross-site request
    forgery.  Private addresses (localhost, RFC 1918, link-local) are allowed.

    Requests with no Origin/Referer (e.g. curl, test clients) are allowed
    through so that automated scripts and the pytest test suite are unaffected.
    """
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        if not _is_localhost_request():
            return jsonify({"error": "Forbidden: requests must originate from a private network"}), 403
_CONFIG_DIR: str = os.path.join(os.path.dirname(__file__), "config")
_KEYS_PATH: str = os.path.join(_CONFIG_DIR, "keys.json")
_CONFIG_PATH: str = os.path.join(_CONFIG_DIR, "config.json")
_PROFILE_PATH: str = os.path.join(_CONFIG_DIR, "profile.json")
_PROVIDERS_PATH: str = os.path.join(_CONFIG_DIR, "providers.json")

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

def load_config(path: str = "config/config.json") -> dict:
    """Load config/config.json if it exists; return safe defaults otherwise.

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


def _write_json_atomic(path: str, data: dict) -> None:
    """Write *data* as JSON to *path* atomically using a sibling .tmp file.

    Writes to ``<path>.tmp`` first, then renames to ``<path>``.  The tmp file
    is always cleaned up on failure so stale partials never accumulate.

    Args:
        path: Destination file path.
        data: Dict to serialise as indented JSON.

    Raises:
        OSError: If writing or renaming fails.
    """
    tmp_path = path + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, path)
    finally:
        try:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
        except OSError:
            pass


def load_profile(path: str = _PROFILE_PATH) -> dict:
    """Load config/profile.json if it exists; return an empty dict otherwise.

    Returns an empty dict (not hardcoded defaults) so the profile form shows
    blank fields rather than confusing placeholder values when the file is absent.

    Legacy migration: education entries that are plain strings (old format
    ``"education": ["B.S. in Computer Science"]``) are converted to structured
    dicts on load so the template never receives a string where it expects a dict.
    """
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}

    # Normalise legacy free-text education strings to structured dicts.
    raw_edu = data.get("education", [])
    if raw_edu and any(not isinstance(e, dict) for e in raw_edu):
        data["education"] = [
            {"degree_type": "", "degree_field": str(e), "school": "", "graduation_year": ""}
            if not isinstance(e, dict) else e
            for e in raw_edu
        ]

    return data


CONFIG = load_config()
db.init_db()

from job_sources.auto_register import ensure_plugins_registered  # noqa: E402
ensure_plugins_registered(_PROVIDERS_PATH)


# ---------------------------------------------------------------------------
# Profile form validation
# ---------------------------------------------------------------------------


def _validate_profile_form(threshold_str: str) -> list[str]:
    """Validate the structured profile form fields.

    Returns a list of human-readable error strings; empty list means valid.
    Validates only the fields that can be invalid in a structured form — raw
    JSON parsing errors are no longer possible since we own the field types.

    Args:
        threshold_str: The raw string value submitted for ``scoring.threshold``.
    """
    errors: list[str] = []

    # scoring.threshold must parse as a float in [0, 10].
    if not threshold_str.strip():
        errors.append("scoring.threshold is required")
    else:
        try:
            val = float(threshold_str.strip())
            if not (0 <= val <= 10):
                errors.append("scoring.threshold must be between 0 and 10")
        except ValueError:
            errors.append("scoring.threshold must be a number")

    return errors


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
    """Return a list of human-readable warnings for missing/empty config.

    Adzuna credentials are read from providers.json (via load_providers),
    consistent with how make_enabled_sources resolves them.  A warning is
    shown only when Adzuna is explicitly enabled (``enabled: true`` in
    providers.json) but its credentials are missing — if the user has
    disabled Adzuna or left it unconfigured, no warning is raised.
    Env var overrides (ADZUNA_APP_ID / ADZUNA_APP_KEY) are also honoured.
    """
    warnings = []
    try:
        providers = load_providers(providers_path=_PROVIDERS_PATH)
    except CredentialError:
        providers = {}

    adzuna_src: dict = (providers.get("job_sources") or {}).get("adzuna") or {}

    # Only warn when Adzuna is explicitly enabled but missing credentials.
    # If there is no entry at all, or enabled=False, the source is not expected
    # to run so showing "not configured" would be a false alarm.
    adzuna_explicitly_enabled = adzuna_src.get("enabled", False)
    if not adzuna_explicitly_enabled:
        return warnings

    adzuna_id  = str(adzuna_src.get("app_id",  "") or "").strip()
    adzuna_key = str(adzuna_src.get("app_key", "") or "").strip()
    # Also honour env var overrides (used in containerised / CI deployments).
    if not adzuna_id:
        adzuna_id = os.environ.get("ADZUNA_APP_ID", "").strip()
    if not adzuna_key:
        adzuna_key = os.environ.get("ADZUNA_APP_KEY", "").strip()
    if not adzuna_id or not adzuna_key:
        warnings.append(
            "Adzuna is enabled but credentials are not configured — it will be skipped. "
            "Add your App ID and App Key on the <a href=\"/settings\">Settings page</a>."
        )
    return warnings


def _get_search_validation_issues() -> list[ValidationIssue]:
    """Return search-config validation issues for enabled sources.

    Loads providers and config safely (returns empty list on any error) and
    delegates to :func:`ingest.validate_search_config`.  Used by the
    ``/settings`` GET render and the ``/api/ingest/preflight`` endpoint so
    the same logic is never duplicated.

    Returns:
        List of :class:`ingest.ValidationIssue` objects.  Empty when all
        enabled sources have complete search configuration.
    """
    try:
        providers = load_providers(providers_path=_PROVIDERS_PATH)
    except CredentialError:
        providers = {}

    try:
        config = load_config(_CONFIG_PATH)
    except SystemExit as exc:
        # config.json missing or malformed — treat as "no issues" so the
        # /settings page can still render and the user can fix the file.
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "Could not load config for search validation: %s", exc
        )
        return []

    return validate_search_config(config, providers)


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
    new_count = sum(1 for listing in listings if listing["opened_at"] is None)
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
        config_warnings=_config_warnings(),
        running=_ingest_running(),
    )


@app.route("/feed/fragment")
def feed_fragment():
    """Feed content fragment — returns only the listing cards (or empty state).

    Used by the ``ingestComplete`` HTMX listener to refresh just the
    ``#feed-content`` container after an ingest run completes, without
    reloading the full page (which would destroy the ingest drawer).

    Accepts the same filter query params as ``/``:
      - min_score, remote_only, search, job_type, sort
    """
    threshold = CONFIG["scoring"]["threshold"]
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
    new_count = sum(1 for listing in listings if listing["opened_at"] is None)
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


@app.route("/bookmarks")
def bookmarks():
    """Bookmarked listings only."""
    listings = db.get_bookmarks()
    return render_template(
        "index.html",
        listings=listings,
        view="bookmarks",
        config_warnings=_config_warnings(),
    )


@app.route("/bookmark/<int:listing_id>", methods=["POST"])
def toggle_bookmark(listing_id: int):
    """Toggle the bookmarked state for a listing.

    Delegates to db.toggle_bookmarked(), which performs the flip atomically
    in a single SQL statement so rapid double-clicks cannot produce a net
    no-op. Returns the re-rendered action button group as an HTMX partial.
    """
    listing = db.toggle_bookmarked(listing_id)
    if listing is None:
        return make_response("", 404)
    return render_template("_actions.html", listing=listing)


@app.route("/apply/<int:listing_id>", methods=["POST"])
def toggle_apply(listing_id: int):
    """Toggle the applied state for a listing.

    Delegates to db.toggle_applied(), which performs the flip atomically
    in a single SQL statement so rapid double-clicks cannot produce a net
    no-op. Returns the re-rendered action button group as an HTMX partial.
    """
    listing = db.toggle_applied(listing_id)
    if listing is None:
        return make_response("", 404)
    return render_template("_actions.html", listing=listing)


@app.route("/applied")
def applied():
    """Applied listings — all listings marked as applied, most recent first."""
    listings = db.get_applied()
    return render_template(
        "index.html",
        listings=listings,
        view="applied",
        config_warnings=_config_warnings(),
    )


@app.route("/snippets")
def snippets():
    """Snippet-scored listings — roles scored from short API descriptions rather than full JDs.

    Accepts the same filter query params as the main feed: ``sort``, ``search``,
    ``remote_only``, ``job_type``, and ``min_score``.
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

    threshold = CONFIG["scoring"]["threshold"]
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
        config_warnings=_config_warnings(),
    )


@app.route("/stats")
def stats():
    """API usage and cost statistics, plus runtime version information."""
    data = db.get_usage_stats()
    return render_template(
        "stats.html",
        stats=data,
        view="stats",
        config_warnings=_config_warnings(),
    )


@app.route("/dismiss/<int:listing_id>", methods=["POST"])
def dismiss(listing_id: int):
    """Dismiss a listing.

    Returns an empty 200 response. HTMX is configured to swap `outerHTML`
    on the card element, replacing it with the empty string — this removes
    the card from the DOM without a page reload.
    """
    db.set_dismissed(listing_id, 1)
    return make_response("", 200)


@app.route("/listings/<int:listing_id>/open", methods=["POST"])
def mark_listing_opened(listing_id: int):
    """Mark a listing as opened (first-time expand) and clear its New badge.

    Called fire-and-forget by HTMX when the user expands a card for the first
    time.  The operation is idempotent — if the listing is already marked
    opened, the DB write is a no-op.

    Returns an HTMX out-of-band swap fragment that removes the badge-new element
    from the DOM immediately.  The CSS rule `.card-details[open] .badge-new` is
    kept as a belt-and-suspenders fallback, but some browsers do not trigger a
    style recalculation for <summary> descendants when <details> gains [open],
    so relying solely on CSS is not reliable across all browsers.
    """
    db.mark_opened(listing_id)
    # hx-swap-oob="outerHTML" replaces the target element entirely with the new
    # element.  An empty <span> with the same id effectively removes the badge.
    oob_fragment = f'<span id="badge-new-{listing_id}" hx-swap-oob="outerHTML"></span>'
    return oob_fragment, 200


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

# Protects concurrent access to _ingest_process and _last_run from waitress
# thread-pool workers.  Any read-modify-write on these globals must hold this
# lock so two simultaneous POST /ingest/trigger requests cannot both pass the
# "not running" check and spawn duplicate subprocesses.
_ingest_lock: threading.Lock = threading.Lock()

# Holds the running Popen handle while ingest.py is active. None when idle.
_ingest_process: subprocess.Popen | None = None

# Legacy handle — no longer written; kept so existing tests/monkeypatches that
# set _ingest_log_file still work without AttributeError.
_ingest_log_file: "object | None" = None

# Stores the result of the most recently completed ingest run.
_last_run: dict | None = None

# Set to True when _ingest_running() first observes that the subprocess has
# exited.  Consumed (cleared back to False) by the first /ingest/status
# response that sends HX-Trigger: ingestComplete, so the event fires exactly
# once per run — not on every subsequent idle poll.
_ingest_just_completed: bool = False

# Maximum number of concurrent SSE connections to /ingest/stream.
# Limited to 2 to prevent resource exhaustion — each connection holds an open
# HTTP connection plus an event queue subscription. Typical use case is 1
# browser tab; 2 allows for tab duplication or a background monitoring process.
MAX_SSE_CONNECTIONS: int = 2

# Matches the summary line that ingest.py prints at the end of each run:
#   Run complete: 2 source(s) | 25 fetched | 10 pre-filtered | 5 dupes skipped |
#                7 scored (3 failed) | 0 scrape skipped | 0 scrape fallbacks | ~1,234 tok | ~$0.0012
_INGEST_SUMMARY_RE = re.compile(
    r"Run complete:\s*\d+\s*source\(s\)\s*\|"  # source count prefix
    r"\s*(\d+)\s*fetched\s*\|"                  # group 1 = fetched
    r".*?(\d+)\s*pre-filtered\s*\|"             # group 2 = pre-filtered
    r".*?scored\s*\((\d+)\s*failed\)",           # group 3 = score-failed
    re.IGNORECASE,
)


def _parse_ingest_summary(output: str) -> dict:
    """Parse the summary line from ingest.py stdout and return a result dict.

    Expected format (single line emitted by ingest.py ``run()``):
      Run complete: 25 fetched | 10 pre-filtered | 5 dupes skipped |
                    7 scored (3 failed) | 0 scrape skipped | 0 scrape fallbacks | ~1,234 tok | ~$0.0012

    Extracted fields:
      new      — listings fetched from Adzuna this run
      filtered — listings dropped by the pre-filter
      errors   — listings that failed scoring

    If the pattern is not found (e.g. the process was killed or produced no
    output), all counts default to zero so the template always has a safe value.
    """
    m = _INGEST_SUMMARY_RE.search(output)
    if m:
        return {
            "new": int(m.group(1)),
            "filtered": int(m.group(2)),
            "errors": int(m.group(3)),
            "completed_at": datetime.now(timezone.utc),
        }
    return {"new": 0, "filtered": 0, "errors": 0, "completed_at": datetime.now(timezone.utc)}


def _stdout_reader(proc: subprocess.Popen) -> None:
    """Daemon thread: reads ingest subprocess stdout line-by-line,
    parses each into a structured event, and pushes to the global queue.

    On exception: kills the subprocess and pushes an aborted event.
    On EOF without a complete event: pushes an aborted event so SSE clients
    disconnect cleanly rather than spinning forever.
    """
    parser = IngestEventParser()
    saw_complete = False
    try:
        # readline() returns '' (empty string, not '\n') at EOF — iter sentinel stops on that
        for raw_line in iter(proc.stdout.readline, ""):
            line = raw_line.rstrip("\n")
            if not line:
                continue
            try:
                event = parser.parse(line)
            except Exception:
                app.logger.exception("IngestEventParser failed on line: %r", line)
                continue
            if event is not None:
                if event["type"] == "complete":
                    saw_complete = True
                event_queue.push(event)
    except Exception:
        app.logger.exception("StdoutReader crashed")
        try:
            proc.kill()
        except OSError:
            pass
        event_queue.push({
            "type": "aborted",
            "source": None,
            "title": None,
            "url": None,
            "detail": {"error": "reader thread crashed"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        return

    # EOF — subprocess exited
    if not saw_complete:
        exit_code = proc.wait()
        event_queue.push({
            "type": "aborted",
            "source": None,
            "title": None,
            "url": None,
            "detail": {"error": f"process exited with code {exit_code}"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })


def _ingest_running() -> bool:
    """Return True if an ingest subprocess is currently active.

    Acquires ``_ingest_lock`` before touching shared state so concurrent calls
    from waitress worker threads are serialised.

    Polls the process exit code: if poll() returns None the process is still
    running. If it has exited, read the temp log file to capture stdout, parse
    the summary into ``_last_run``, reset the handle to None so a new run can
    start, and set ``_ingest_just_completed`` so the next /ingest/status
    response fires ``HX-Trigger: ingestComplete`` exactly once.
    """
    global _ingest_process, _ingest_log_file, _last_run, _ingest_just_completed
    with _ingest_lock:
        if _ingest_process is None:
            return False
        if _ingest_process.poll() is not None:
            # Process has exited — extract summary from event queue for
            # backward compat.  Clean up legacy log file handle if present
            # (no-op for new PIPE-based runs).
            if _ingest_log_file is not None:
                try:
                    _ingest_log_file.close()
                except (OSError, ValueError):
                    pass
                _ingest_log_file = None
            _last_run = _parse_ingest_summary(event_queue.get_latest_summary())
            _ingest_process = None
            # Mark the running→idle transition so /ingest/status sends
            # HX-Trigger: ingestComplete exactly once (not on every idle poll).
            _ingest_just_completed = True
            return False
        return True


def _render_ingest_idle() -> str:
    """Return the HTML partial for the idle 'Run Ingestion' button."""
    return render_template("_ingest_trigger.html", running=False, last_run=_last_run)


def _render_ingest_running() -> str:
    """Return the HTML partial for the in-progress status element."""
    return render_template("_ingest_trigger.html", running=True)


@app.route("/ingest/trigger", methods=["POST"])
def ingest_trigger():
    """Spawn ingest.py as a background subprocess.

    Returns 202 with the 'Running...' HTML partial when the process starts.
    Returns 409 with a JSON error body if a run is already in progress — the
    caller can check Content-Type to distinguish the two response shapes.

    Uses sys.executable so the subprocess runs in the same virtualenv as the
    app server, picking up all installed dependencies automatically.

    stdout and stderr are merged and piped via subprocess.PIPE to a
    StdoutReader daemon thread. The reader parses each line into a structured
    event and pushes it to the global event queue for real-time SSE
    consumption by /ingest/stream subscribers.
    """
    global _ingest_process, _ingest_log_file

    # Build command from optional UI parameters before taking the lock so the
    # critical section stays as short as possible.
    hours_raw = request.form.get("hours", "25").strip()
    rescore = request.form.get("rescore") == "1"

    try:
        hours = int(hours_raw)
    except (ValueError, TypeError):
        hours = 25

    cmd = [sys.executable, "ingest.py", "--hours", str(hours)]
    if rescore:
        cmd.append("--rescore")

    with _ingest_lock:
        # Re-check inside the lock: another thread may have started a process
        # between our pre-lock poll and now.
        if _ingest_process is not None and _ingest_process.poll() is None:
            return jsonify({"error": "already running"}), 409

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,  # line-buffered
                # Force unbuffered output from the child so log lines reach
                # the parent pipe immediately even when stderr is not a tty.
                env={**os.environ, "PYTHONUNBUFFERED": "1", "INGEST_TRIGGER": "manual_ui"},
            )
        except (OSError, PermissionError) as e:
            return jsonify({"error": f"Failed to start ingestion: {e}"}), 500
        event_queue.clear()

        _ingest_process = proc
        _ingest_log_file = None  # no longer used; kept for backward compat

        reader = threading.Thread(
            target=_stdout_reader,
            args=(proc,),
            daemon=True,
        )
        reader.start()

    resp = make_response(_render_ingest_running(), 202)
    resp.headers["Content-Type"] = "text/html"
    return resp


@app.route("/api/ingest/preflight", methods=["GET"])
def ingest_preflight():
    """Pre-flight validation endpoint for the ingest drawer.

    Returns a JSON object describing whether the current configuration is
    valid enough to start an ingest run.  The ingest drawer calls this
    before enabling the "Run Ingestion" button so users learn about
    configuration gaps before submitting the form.

    Returns:
        200 with ``{"ok": true}`` when all enabled sources are fully
        configured.
        422 with ``{"ok": false, "issues": [...]}`` when one or more enabled
        sources have missing or empty required search fields.  Each issue in
        the list has the shape
        ``{"source": "<key>", "missing_fields": ["country", ...]}``.
    """
    issues = _get_search_validation_issues()
    if not issues:
        return jsonify({"ok": True})

    return jsonify({
        "ok": False,
        "issues": [
            {
                "source": issue.source_key,
                "missing_fields": issue.missing_fields,
            }
            for issue in issues
        ],
    }), 422


@app.route("/ingest/status")
def ingest_status():
    """Poll endpoint — returns an HTML partial reflecting current ingest state.

    While the process is running, returns the polling div so HTMX keeps
    refreshing. Once it stops, returns the idle button.

    ``HX-Trigger: ingestComplete`` is sent only on the running→idle transition
    (i.e. the first idle response after a run finishes), not on every
    subsequent idle poll. This prevents the ``ingestComplete`` listener from
    firing repeatedly and causing an infinite refresh loop.
    """
    global _ingest_just_completed
    running = _ingest_running()
    html = _render_ingest_running() if running else _render_ingest_idle()
    resp = make_response(html, 200)
    resp.headers["Content-Type"] = "text/html"
    if not running and _ingest_just_completed:
        # Consume the flag — subsequent idle polls will NOT carry this header.
        _ingest_just_completed = False
        resp.headers["HX-Trigger"] = "ingestComplete"
    return resp


@app.route("/ingest/stream")
def ingest_stream():
    """SSE endpoint streaming real-time ingest events.

    Yields events from the EventQueue in SSE wire format. Supports replay
    via Last-Event-ID header (format: "{run_id}:{event_id}"). Returns 429
    if max connections exceeded.
    """
    if event_queue.connection_count >= MAX_SSE_CONNECTIONS:
        return jsonify({"error": "too many connections"}), 429

    # Parse Last-Event-ID: "{run_id}:{event_id}" or just "{event_id}"
    last_event_id_raw = request.headers.get("Last-Event-ID", "")
    last_id = 0
    if last_event_id_raw:
        parts = last_event_id_raw.rsplit(":", 1)
        if len(parts) == 2:
            req_run_id, id_str = parts
            try:
                candidate_id = int(id_str)
            except ValueError:
                candidate_id = 0
            # Stale run_id → replay from beginning
            if req_run_id == event_queue.run_id:
                last_id = candidate_id
        else:
            try:
                last_id = int(parts[0])
            except ValueError:
                last_id = 0

    def generate():
        event_queue.connect()
        try:
            for event in event_queue.subscribe(last_id=last_id):
                eid = event.get("id", 0)
                run_id = event.get("run_id", event_queue.run_id)
                data = json.dumps(event, separators=(",", ":"))
                yield f"id: {run_id}:{eid}\ndata: {data}\n\n"
        finally:
            event_queue.disconnect()

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


def _build_llm_schemas(
    llm_section: dict,
    provider_order: list[str],
) -> list[tuple[str, dict, bool, dict, set]]:
    """Build the ordered llm_schemas list for the settings template.

    Returns a list of ``(provider_key, schema_dict, has_values, current_values,
    populated_fields)`` tuples.  Providers in *provider_order* come first
    (unknown/duplicate keys skipped), followed by any registry providers not
    listed, in registry insertion order.

    ``has_values`` is ``True`` only when every required field in the schema has
    a non-blank stored value.  Checking all required fields (not just
    ``api_key``) prevents a provider with a key but an empty model string from
    falsely showing "● configured".

    ``current_values`` maps non-password field names to their stored value (or
    the field's ``default`` if not yet stored).  This dict is passed to the
    template so that non-password inputs can be pre-populated, ensuring that
    the placeholder default is actually submitted when the user saves without
    explicitly editing the field.

    ``populated_fields`` is a set of field names that have a non-empty stored
    value.  The template uses this to conditionally render the Clear button
    next to password fields — the button only appears when there is actually
    something stored to clear.

    Args:
        llm_section:    The ``"llm"`` sub-dict from ``providers.json``.
        provider_order: The ``provider_order`` list from ``providers.json``.
    """
    seen: set[str] = set()
    schemas: list[tuple[str, dict, bool, dict, set]] = []

    def _make_entry(key: str) -> tuple[str, dict, bool, dict, set]:
        cls = _PROVIDER_CLASS_MAP[key]
        schema = cls.settings_schema()
        cfg = llm_section.get(key) or {}
        has_values = all(
            bool(cfg.get(f["name"], "").strip())
            for f in schema["fields"]
            if f.get("required")
        )
        current_values = {
            f["name"]: cfg.get(f["name"]) or f.get("default") or ""
            for f in schema["fields"]
            if f.get("type") != "password"
        }
        populated_fields = {
            f["name"] for f in schema["fields"]
            if bool(cfg.get(f["name"], "").strip())
        }
        return (key, schema, has_values, current_values, populated_fields)

    for key in provider_order:
        if key in _PROVIDER_CLASS_MAP and key not in seen:
            schemas.append(_make_entry(key))
            seen.add(key)
    for key in _PROVIDER_CLASS_MAP:
        if key not in seen:
            schemas.append(_make_entry(key))
            seen.add(key)
    return schemas


def _load_providers_safe() -> dict:
    """Load providers.json and return the parsed dict, or an empty skeleton on error.

    Uses ``_PROVIDERS_PATH`` as the primary credential store.  Falls back to
    migration from ``_KEYS_PATH`` / ``_CONFIG_PATH`` when ``providers.json`` is
    absent.  Returns safe empty defaults when :exc:`CredentialError` is raised
    so the settings UI always renders even before any credentials are configured.

    Returns:
        providers.json-shaped dict with ``provider_order``, ``llm``, and
        ``job_sources`` keys guaranteed to be present.
    """
    try:
        data = load_providers(
            providers_path=_PROVIDERS_PATH,
            keys_path=_KEYS_PATH,
            config_path=_CONFIG_PATH,
        )
    except CredentialError:
        data = {}

    data.setdefault("provider_order", [])
    data.setdefault("llm", {})
    data.setdefault("job_sources", {})
    return data


@app.route("/settings", methods=["GET", "POST"])
def settings():
    """Settings page — manage LLM provider credentials and job source credentials.

    GET:  Builds ``llm_schemas`` and ``source_schemas`` from the provider/source
          registries and passes only boolean ``has_values`` flags — never raw
          credential values — to the template.  Tab is selected via ``?tab=``
          query param (default: ``llm``).

    POST: Parses namespaced form fields (``<provider_key>__<field_name>``),
          deep-merges non-blank values into ``providers.json`` via
          :func:`credentials.save_providers`, then redirects to
          ``GET /settings?tab=<active_tab>``.
    """
    error = None

    if request.method == "POST":
        active_tab = request.form.get("tab", "llm").strip()

        # --- Build updates dict from namespaced form fields ---
        # Only populate the section that corresponds to the active tab.  Processing
        # the other section would send blank values for every field not present in
        # the submitted form, causing _deep_merge to overwrite previously-saved
        # credentials with empty strings (cross-tab wipe bug, issue #71).
        updates: dict = {}

        if active_tab == "llm":
            updates["llm"] = {}
            # LLM providers: iterate registry so new providers are handled automatically.
            # Only include fields that have a non-empty value so that providers the
            # user left blank are not merged into providers.json as empty strings
            # (within-tab wipe bug, issue #71).  A user who explicitly clears a field
            # will have submitted a blank for a provider that already had a value — we
            # distinguish "provider's form was on the page and submitted blank" from
            # "provider wasn't on the page at all" by limiting this block to
            # active_tab == "llm" above.
            #
            # Load the current stored state once so we can fill in missing
            # non-password field defaults when the JS dirty-tracker omits
            # unchanged fields from the POST body (fixes issue #231).
            _current_providers = _load_providers_safe()
            _current_llm = _current_providers.get("llm") or {}
            for provider_key, cls in _PROVIDER_CLASS_MAP.items():
                schema = cls.settings_schema()
                provider_updates: dict = {}
                for field in schema["fields"]:
                    field_name = field["name"]
                    form_key = f"{provider_key}__{field_name}"
                    raw = request.form.get(form_key)
                    if raw is None:
                        # Field not present in form at all — skip to preserve
                        # any existing stored value.
                        continue
                    stripped = raw.strip()
                    # No-JS guard: skip empty password fields unless the
                    # explicit __clear__ flag is present.  This prevents a
                    # native (no-JS) form submit from wiping an existing key
                    # just because the password placeholder was left blank.
                    if field.get("type") == "password" and stripped == "":
                        clear_key = f"__clear__{provider_key}__{field_name}"
                        if request.form.get(clear_key) != "1":
                            continue
                    provider_updates[field_name] = stripped
                # After processing normal fields, check for explicit __clear__
                # flags on password fields.  The flag writes "" regardless of
                # whether the password form field was also submitted.
                for field in schema["fields"]:
                    if field.get("type") != "password":
                        continue
                    clear_key = f"__clear__{provider_key}__{field['name']}"
                    if request.form.get(clear_key) == "1":
                        provider_updates[field["name"]] = ""
                # When the provider is being updated (at least one field was
                # submitted), ensure every non-password field that was NOT in
                # the POST body (because JS dirty-tracking only sends changed
                # fields) is written with its current stored value or its
                # schema default.  Without this, a user who only edits the
                # API key and never touches the model dropdown will end up with
                # no model in providers.json, causing has_values to return False
                # and the provider to show as "not configured" after every save.
                if provider_updates:
                    stored_cfg = _current_llm.get(provider_key) or {}
                    for field in schema["fields"]:
                        if field.get("type") == "password":
                            continue
                        field_name = field["name"]
                        if field_name in provider_updates:
                            continue
                        stored_val = stored_cfg.get(field_name, "")
                        if not stored_val:
                            default_val = field.get("default", "")
                            if default_val:
                                provider_updates[field_name] = default_val
                    updates["llm"][provider_key] = provider_updates

        elif active_tab == "sources":
            updates["job_sources"] = {}
            # Job sources: JS dirty-tracking sends only the fields the user
            # actually changed, so we must skip sources that have no form data
            # at all.  A source is "touched" when any of its namespaced fields
            # (credentials or the enabled checkbox) appears in the POST body.
            # This prevents the server from overwriting stored credentials or
            # toggling the enabled flag for sources the user never interacted
            # with (issue #89 — client-side dirty tracking companion fix).
            for source_key, cls in get_sources().items():
                schema_fields = cls.settings_schema()["fields"]
                cred_keys = [f"{source_key}__{f['name']}" for f in schema_fields]
                clear_keys = [f"__clear__{source_key}__{f['name']}" for f in schema_fields]
                enabled_key = f"{source_key}__enabled"
                # Skip this source entirely when none of its form keys are present.
                # Include __clear__ keys in this check: when the JS Clear button
                # is clicked, submitDirty() may send only the __clear__ flag
                # (plus the empty credential field after the client fix), but
                # this defense-in-depth ensures the server never skips a source
                # that has an explicit clear flag even if the credential field
                # is absent from the POST body.
                source_in_form = any(
                    request.form.get(k) is not None
                    for k in cred_keys + [enabled_key] + clear_keys
                )
                if not source_in_form:
                    continue

                source_updates: dict = {}

                # Checkbox: only update enabled when the field was explicitly
                # submitted.  JS dirty-tracking sends the checkbox only when
                # the user actually toggled it: 'on' = checked, '' = unchecked.
                # If the field is absent entirely (user only changed a
                # credential), leave the stored enabled state untouched.
                if enabled_key in request.form:
                    source_updates["enabled"] = request.form.get(enabled_key) == "on"

                for field in schema_fields:
                    field_name = field["name"]
                    form_key = f"{source_key}__{field_name}"
                    raw = request.form.get(form_key)
                    if raw is None:
                        continue
                    stripped = raw.strip()
                    # No-JS guard: skip empty password fields unless the
                    # explicit __clear__ flag is present.
                    if field.get("type") == "password" and stripped == "":
                        clear_key = f"__clear__{source_key}__{field_name}"
                        if request.form.get(clear_key) != "1":
                            continue
                    source_updates[field_name] = stripped
                # Explicit __clear__ flags for password fields.
                for field in schema_fields:
                    if field.get("type") != "password":
                        continue
                    clear_key = f"__clear__{source_key}__{field['name']}"
                    if request.form.get(clear_key) == "1":
                        source_updates[field["name"]] = ""

                updates["job_sources"][source_key] = source_updates

        try:
            save_providers(updates, providers_path=_PROVIDERS_PATH)
        except OSError:
            error = "Could not save settings — check file permissions."

        # Save search fields (country, what, where, results_per_page,
        # max_pages) to config.json.
        if error is None and active_tab == "search":
            existing_cfg = load_config(_CONFIG_PATH)
            existing_search = existing_cfg.get("search") or {}
            updated_search = dict(existing_search)

            # Free-text search fields — store as-is (stripped).
            for field_name in ("search_country", "search_what", "search_where"):
                raw = request.form.get(field_name, "").strip()
                config_key = field_name[len("search_"):]  # strip "search_" prefix
                if raw:
                    updated_search[config_key] = raw
                elif field_name in request.form:
                    # Explicit empty submission — allow clearing the field.
                    updated_search.pop(config_key, None)

            # Numeric search fields.
            rpp_str = request.form.get("search_results_per_page", "").strip()
            mp_str = request.form.get("search_max_pages", "").strip()
            if rpp_str:
                try:
                    updated_search["results_per_page"] = int(rpp_str)
                except ValueError:
                    pass
            if mp_str:
                try:
                    updated_search["max_pages"] = int(mp_str)
                except ValueError:
                    pass

            updated_cfg = dict(existing_cfg)
            updated_cfg["search"] = updated_search
            try:
                _write_json_atomic(_CONFIG_PATH, updated_cfg)
            except OSError:
                error = "Could not save config — check file permissions."

        if error is None:
            return redirect(url_for("settings", tab=active_tab))

    # --- GET (or POST with error) ---
    active_tab = request.args.get("tab", "llm").strip()
    providers_data = _load_providers_safe()
    llm_section: dict = providers_data.get("llm") or {}
    sources_section: dict = providers_data.get("job_sources") or {}

    # provider_order from providers.json determines display sequence.
    provider_order: list[str] = providers_data.get("provider_order") or []
    llm_schemas = _build_llm_schemas(llm_section, provider_order)

    source_schemas: list[tuple[str, dict, bool, bool, bool, set]] = []
    for key, cls in get_sources().items():
        schema = cls.settings_schema()
        cfg = sources_section.get(key) or {}
        required_fields = [f["name"] for f in schema["fields"] if f.get("required")]
        if required_fields:
            has_values = all(bool(cfg.get(fn, "").strip()) for fn in required_fields)
        else:
            has_values = False  # no-credential sources are never "configured"
        is_enabled = bool(cfg.get("enabled", False))
        credentials_required = bool(required_fields)
        populated_fields = {
            f["name"] for f in schema["fields"]
            if bool(cfg.get(f["name"], "").strip())
        }
        source_schemas.append((key, schema, has_values, is_enabled, credentials_required, populated_fields))

    # POST-with-error: re-render the form (not a redirect) so the error is shown.
    saved = False  # POST always redirects on success; reaching here means error or GET
    if request.method == "POST" and error:
        pass  # fall through to render with error

    # Pass search fields and validation issues to the Search Settings tab.
    search_cfg = load_config(_CONFIG_PATH).get("search") or {}
    search_issues = _get_search_validation_issues()

    return render_template(
        "settings.html",
        view="settings",
        llm_schemas=llm_schemas,
        source_schemas=source_schemas,
        active_tab=active_tab,
        saved=saved,
        error=error,
        search_cfg=search_cfg,
        search_issues=search_issues,
    )


def _parse_education_rows(form) -> list[dict]:
    """Extract a list of structured education objects from the education table form fields.

    Reads the four parallel ``edu_type[]``, ``edu_field[]``, ``edu_school[]``,
    and ``edu_year[]`` arrays from the submitted form and zips them into
    structured dicts.  Rows where all four fields are empty are silently
    discarded.

    Args:
        form: The Flask ``request.form`` ImmutableMultiDict.

    Returns:
        List of dicts, each with keys ``degree_type``, ``degree_field``,
        ``school``, and ``graduation_year``.
    """
    types = form.getlist("edu_type[]")
    fields = form.getlist("edu_field[]")
    schools = form.getlist("edu_school[]")
    years = form.getlist("edu_year[]")

    # Zip to the shortest list to guard against mismatched row counts.
    rows = []
    for deg_type, deg_field, school, year in zip(types, fields, schools, years):
        deg_type = deg_type.strip()
        deg_field = deg_field.strip()
        school = school.strip()
        year = year.strip()
        # Discard non-numeric year values to prevent nonsense input from being persisted.
        if year and not year.isdigit():
            year = ""
        # Skip rows where every field is empty.
        if not any([deg_type, deg_field, school, year]):
            continue
        rows.append({
            "degree_type": deg_type,
            "degree_field": deg_field,
            "school": school,
            "graduation_year": year,
        })
    return rows


def _parse_repeating_rows(form, field_name: str) -> list[str]:
    """Extract a list of non-empty strings from repeating form row inputs.

    The repeating-row pattern names inputs as ``<field_name>[]``, submitting
    one value per row.  Empty rows (whitespace-only) are discarded so the
    stored array does not contain blank entries.

    Args:
        form: The Flask ``request.form`` ImmutableMultiDict.
        field_name: Base name used in the HTML (e.g. ``"primary_skills"``).

    Returns:
        List of stripped non-empty strings.
    """
    values = form.getlist(f"{field_name}[]")
    return [v.strip() for v in values if v.strip()]


# ---------------------------------------------------------------------------
# PDF resume import — helpers
# ---------------------------------------------------------------------------


def _extract_pdf_text(pdf_bytes: bytes) -> str:
    """Extract all text from a PDF given its raw bytes.

    Args:
        pdf_bytes: Raw bytes of the uploaded PDF file.

    Returns:
        Concatenated text from all pages (empty string if no text found).

    Raises:
        ValueError: If pypdf cannot parse the bytes as a valid PDF.
    """
    try:
        reader = PdfReader(BytesIO(pdf_bytes))
        return "".join(page.extract_text() or "" for page in reader.pages)
    except (PdfReadError, ValueError, IOError) as exc:
        raise ValueError(f"Could not read PDF: {exc}") from exc


_IMPORT_PROMPT_FRESH = """You are extracting structured profile data from a resume/CV.

RESUME TEXT:
{resume_text}

Extract the following fields and respond with ONLY a JSON object. No explanation, no markdown, no code fences.

The JSON must have exactly these keys:
- "primary_skills": array of objects, each with "skill" (string), "years" (integer estimate), "status" ("active" or "dormant")
- "education": array of objects, each with "degree_type" (e.g. "B.S.", "M.S."), "degree_field" (e.g. "Computer Science"), "school" (institution name), "graduation_year" (four-digit year string)
- "seniority": string inferred from job titles (e.g. "Junior", "Mid-level", "Senior", "Staff", "Lead", "Principal")
- "preferred_industries": array of strings inferred from work history (e.g. "fintech", "healthtech", "developer tooling")
- "location_center": string from contact info if present (e.g. "Miami, FL"), or null if not found

If a field cannot be confidently extracted, use an empty array, empty string, or null as appropriate. Do not guess or hallucinate values.

JSON only:"""

# Appended to _IMPORT_PROMPT_FRESH (before the final "JSON only:" sentinel)
# when the caller opts in to prefilter title suggestions.  Kept separate so
# the base prompt is byte-for-byte identical when the toggle is off.
_IMPORT_PROMPT_PREFILTER_EXTENSION = """
Additionally, suggest job-title keyword filters based on the roles this
candidate has held and the jobs they would plausibly target:
- "prefilter_suggestions": object with exactly two keys:
  - "title_include": array of lowercase substring strings that SHOULD appear
    in a job title for it to be relevant (e.g. ["engineer", "developer"])
  - "title_exclude": array of lowercase substring strings that should NEVER
    appear in a job title (e.g. ["manager", "director", "intern"])

Rules for prefilter_suggestions:
- Use simple substrings, not regular expressions.
- All strings must be lowercase.
- "title_include" and "title_exclude" must be completely disjoint — no string
  may appear in both lists (case-insensitively).
- If you cannot confidently suggest filters, use empty arrays for both keys.
- Do NOT include "require_contract_time" or "require_contract_type" — those
  are separate user preferences, not resume-derived."""


def _build_import_prompt(
    resume_text: str,
    suggest_filters: bool = False,
) -> str:
    """Build the LLM prompt for PDF resume import.

    Both fresh and merge modes use the same extraction-only prompt.  Merging
    is handled deterministically by ``_merge_import_result()`` after the LLM
    responds, so the LLM never needs to see the existing profile.

    When ``suggest_filters`` is ``True`` the prefilter extension is appended
    to the prompt so the LLM also returns ``prefilter_suggestions``.  When it
    is ``False`` the prompt is byte-for-byte identical to the legacy prompt —
    no extra tokens are charged.

    Args:
        resume_text: Extracted plain text from the uploaded PDF.
        suggest_filters: When ``True``, ask the LLM to additionally return
            ``prefilter_suggestions`` (title_include / title_exclude arrays).

    Returns:
        Formatted prompt string ready to send to the LLM.
    """
    if suggest_filters:
        # Insert the prefilter extension before the closing "JSON only:" line.
        base = _IMPORT_PROMPT_FRESH.rstrip()
        # Remove the trailing sentinel, add extension, restore sentinel.
        sentinel = "JSON only:"
        if base.endswith(sentinel):
            base = base[: -len(sentinel)].rstrip()
        return (
            base
            + "\n"
            + _IMPORT_PROMPT_PREFILTER_EXTENSION.strip()
            + "\n\nJSON only:"
        ).format(resume_text=resume_text)
    return _IMPORT_PROMPT_FRESH.format(resume_text=resume_text)


# Maximum length (characters) for a single prefilter pattern string.  Bounding
# LLM output prevents pathologically long patterns from bloating config.json.
_MAX_PATTERN_LEN = 64

# Maximum number of patterns allowed in a single title_include or title_exclude
# list within prefilter_suggestions.
_MAX_PATTERNS_PER_LIST = 32


def _parse_import_response(raw: str) -> dict | None:
    """Parse the LLM's JSON response for a PDF import request.

    Strips markdown code fences, parses JSON, and fills missing keys with
    safe defaults so callers can always rely on the expected keys existing.

    If ``prefilter_suggestions`` is present its ``title_include`` and
    ``title_exclude`` arrays are validated to be disjoint (case-insensitive).
    Validation failures in the suggestions section drop only that key — the
    core profile data is still returned so the caller does not 502 the whole
    request because of an optional field.  Only a failure to parse the
    top-level JSON at all causes a ``None`` return.

    Each pattern string must be ≤ ``_MAX_PATTERN_LEN`` characters and each list
    must contain ≤ ``_MAX_PATTERNS_PER_LIST`` items.  Over-limit or invalid
    suggestions are dropped with a warning rather than rejecting the whole
    response.

    Args:
        raw: Raw text response from the LLM.

    Returns:
        Parsed dict with all expected keys, or ``None`` if the top-level JSON
        itself cannot be parsed.
    """
    try:
        cleaned = strip_fences(raw)
        data = json.loads(cleaned)
    except (json.JSONDecodeError, ValueError):
        app.logger.error(
            "[import] _parse_import_response: failed to parse LLM "
            "response as JSON — raw body (first 500 chars): %r",
            raw[:500],
        )
        return None
    data.setdefault("primary_skills", [])
    data.setdefault("education", [])
    data.setdefault("seniority", "")
    data.setdefault("preferred_industries", [])
    data.setdefault("location_center", None)

    # Validate prefilter_suggestions when present.  Any validation failure
    # drops only this optional key so the core profile is still returned.
    if "prefilter_suggestions" in data:
        pf = data["prefilter_suggestions"]
        if isinstance(pf, dict):
            inc = [str(s).lower() for s in pf.get("title_include", [])]
            exc = [str(s).lower() for s in pf.get("title_exclude", [])]

            # Enforce per-list length cap.  Drop rather than truncate so the
            # LLM cannot silently bloat the config.
            if len(inc) > _MAX_PATTERNS_PER_LIST or len(exc) > _MAX_PATTERNS_PER_LIST:
                app.logger.warning(
                    "[import] _parse_import_response: prefilter_suggestions "
                    "list too long (include=%d, exclude=%d, max=%d) — "
                    "dropping suggestions; profile data preserved.",
                    len(inc),
                    len(exc),
                    _MAX_PATTERNS_PER_LIST,
                )
                del data["prefilter_suggestions"]
            else:
                # Enforce per-pattern length cap.
                over_len = [s for s in inc + exc if len(s) > _MAX_PATTERN_LEN]
                if over_len:
                    app.logger.warning(
                        "[import] _parse_import_response: prefilter_suggestions "
                        "contains patterns exceeding max length (%d chars): %r — "
                        "dropping suggestions; profile data preserved.",
                        _MAX_PATTERN_LEN,
                        over_len[:5],
                    )
                    del data["prefilter_suggestions"]
                else:
                    overlap = set(inc) & set(exc)
                    if overlap:
                        app.logger.warning(
                            "[import] _parse_import_response: prefilter_suggestions "
                            "title_include/title_exclude overlap — dropping suggestions; "
                            "profile data preserved. Overlapping terms: %r",
                            overlap,
                        )
                        del data["prefilter_suggestions"]
                    else:
                        # Normalise to lowercase lists in-place.
                        data["prefilter_suggestions"] = {
                            "title_include": inc,
                            "title_exclude": exc,
                        }
        else:
            # Unexpected type — drop the key rather than passing bad data.
            del data["prefilter_suggestions"]

    return data


_DEGREE_PREFIX_RE = re.compile(
    r"^(B\.S\.|BS|B\.A\.|BA|M\.S\.|MS|M\.A\.|MA|Ph\.D\.|PhD|MBA"
    r"|Master of Science|Master of Arts|Master of Business Administration"
    r"|Bachelor of Science|Bachelor of Arts|Bachelor of Engineering"
    r"|Doctor of Philosophy|Doctor of|Associate of|Associate)(?=\s|$)",
    re.IGNORECASE,
)
_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")


def _normalise_education(entries: list) -> list[dict]:
    """Normalise a list of education entries to structured dicts.

    Handles three cases per entry:

    * **Flat string** — attempts regex-based parsing into the four structured
      fields (``degree_type``, ``degree_field``, ``school``,
      ``graduation_year``).  Falls back to stuffing the whole string into
      ``degree_field`` if parsing fails.
    * **Dict with missing keys** — fills absent keys with ``""``.
    * **Well-formed dict** — passed through unchanged.

    Args:
        entries: Raw education list from the LLM response.

    Returns:
        List of dicts each containing exactly the four structured keys.
    """
    _EMPTY = {"degree_type": "", "degree_field": "", "school": "", "graduation_year": ""}

    def _parse_flat(s: str) -> dict:
        result = dict(_EMPTY)
        # Extract 4-digit year first.
        year_m = _YEAR_RE.search(s)
        if year_m:
            result["graduation_year"] = year_m.group(0)
            s = (s[: year_m.start()] + s[year_m.end() :]).strip(" ,").lstrip()

        # Attempt to match a known degree prefix at the start.
        prefix_m = _DEGREE_PREFIX_RE.match(s)
        if prefix_m:
            result["degree_type"] = prefix_m.group(0).strip()
            remainder = s[prefix_m.end() :].strip()
            # Handle "in <field>" connector (e.g. "Master of Science in Data Science")
            if remainder.lower().startswith("in "):
                remainder = remainder[3:].strip()
            # Remaining text split by ", " gives field then school (or just field).
            parts = [p.strip() for p in remainder.split(",", 1)]
            result["degree_field"] = parts[0] if parts else ""
            result["school"] = parts[1] if len(parts) > 1 else ""
        else:
            # No recognised degree prefix — split by "," and use heuristics.
            parts = [p.strip() for p in s.split(",")]
            if len(parts) >= 3:
                # e.g. "Computer Science, MIT, ..." — unlikely but defensible
                result["degree_type"] = ""
                result["degree_field"] = parts[0]
                result["school"] = parts[1]
            elif len(parts) == 2:
                result["degree_field"] = parts[0]
                result["school"] = parts[1]
            elif parts:
                result["degree_field"] = parts[0]
            else:
                result["degree_field"] = s  # fallback: preserve whole string

        return result

    normalised = []
    for entry in entries:
        if isinstance(entry, str):
            normalised.append(_parse_flat(entry.strip()))
        elif isinstance(entry, dict):
            normalised.append({
                "degree_type": entry.get("degree_type", ""),
                "degree_field": entry.get("degree_field", ""),
                "school": entry.get("school", ""),
                "graduation_year": str(entry.get("graduation_year", "")),
            })
        else:
            # Unexpected type — convert to string and fall back.
            normalised.append(_parse_flat(str(entry)))
    return normalised


def _merge_import_result(current: dict, imported: dict) -> dict:
    """Merge LLM-extracted import data into the existing profile.

    Merging rules:
    - Skills: preserve all existing; append new ones (case-insensitive dedup).
    - Education: preserve all existing; append new ones (case-insensitive dedup).
    - Seniority: keep existing if non-empty; otherwise use imported value.
    - Industries: union of both lists, case-insensitive dedup.
    - Location: keep existing center if set; otherwise use imported value.

    Args:
        current:  Existing profile dict (may be empty).
        imported: Parsed LLM response dict from ``_parse_import_response()``.

    Returns:
        Merged profile dict containing all combined data.
    """
    result = {}

    # Skills: existing preserved (as structured objects), new appended from import.
    # Existing skills may be structured dicts or legacy flat strings — normalise
    # to structured objects so the merged result is always typed.
    def _normalise_skill(s: object) -> dict:
        """Convert a legacy flat string or a structured dict to a skill object."""
        if isinstance(s, dict):
            return s
        # Legacy format: "Python, 5yr, active" or "Python, 5yr, dormant"
        parts = [p.strip() for p in str(s).split(",")]
        description = parts[0] if parts else str(s)
        years = 0
        active = True
        if len(parts) >= 2:
            yr_part = parts[1].lower().replace("yr", "").strip()
            try:
                years = int(yr_part)
            except ValueError:
                pass
        if len(parts) >= 3:
            active = parts[2].lower().strip() != "dormant"
        return {"description": description, "years_active": years, "active": active}

    existing_skills: list[dict] = [_normalise_skill(s) for s in current.get("primary_skills", [])]
    existing_skill_names = {s["description"].lower() for s in existing_skills}
    for skill_obj in imported.get("primary_skills", []):
        name = skill_obj.get("skill", "")
        if name.lower() not in existing_skill_names:
            years = skill_obj.get("years", 0)
            status = skill_obj.get("status", "active")
            existing_skills.append({
                "description": name,
                "years_active": int(years) if years else 0,
                "active": status != "dormant",
            })
            existing_skill_names.add(name.lower())
    result["primary_skills"] = existing_skills

    # Education: append new structured objects, skip duplicates (all four fields, case-insensitive).
    # Existing entries may be structured dicts or legacy flat strings — normalise to dicts.
    def _normalise_edu(e: object) -> dict:
        """Convert a legacy flat string or a structured dict to an education object."""
        return _normalise_education([e])[0]

    def _edu_key(e: dict) -> tuple:
        """Return a case-folded 4-tuple for dedup comparison."""
        return (
            e.get("degree_type", "").lower(),
            e.get("degree_field", "").lower(),
            e.get("school", "").lower(),
            e.get("graduation_year", "").lower(),
        )

    existing_edu: list[dict] = [_normalise_edu(e) for e in current.get("education", [])]
    existing_edu_keys = {_edu_key(e) for e in existing_edu}
    for entry in imported.get("education", []):
        entry_norm = _normalise_edu(entry)
        key = _edu_key(entry_norm)
        if key not in existing_edu_keys:
            existing_edu.append(entry_norm)
            existing_edu_keys.add(key)
    result["education"] = existing_edu

    # Seniority: keep existing if set, fill from import if empty
    current_seniority = current.get("seniority", "")
    result["seniority"] = current_seniority if current_seniority else imported.get("seniority", "")

    # Industries: union, deduplicated
    existing_industries = list(current.get("preferred_industries", []))
    existing_lower = {i.lower() for i in existing_industries}
    for industry in imported.get("preferred_industries", []):
        if industry.lower() not in existing_lower:
            existing_industries.append(industry)
            existing_lower.add(industry.lower())
    result["preferred_industries"] = existing_industries

    # Location: keep existing if set
    current_location = current.get("location", {})
    current_center = current_location.get("center", "") if isinstance(current_location, dict) else ""
    result["location_center"] = current_center if current_center else imported.get("location_center")

    return result


def _merge_prefilter_suggestions(
    existing_prefilter: dict,
    suggestions: dict,
) -> dict:
    """Merge LLM-suggested prefilter patterns into the existing prefilter block.

    Merge rules:
    - ``title_include``: case-insensitive union of existing and suggested
      patterns; existing user-added patterns are never removed.
    - ``title_exclude``: same union-then-dedup rule.
    - All other prefilter keys (``require_contract_time``,
      ``require_contract_type``, etc.) are preserved unchanged from
      ``existing_prefilter``.

    The caller is responsible for ensuring ``suggestions`` has already passed
    the disjoint-set check in ``_parse_import_response()`` — this function
    does not re-validate.

    Args:
        existing_prefilter: The current ``prefilter`` block from
            ``config.json`` (may be empty dict).
        suggestions: The ``prefilter_suggestions`` dict from the parsed LLM
            response, containing ``title_include`` and ``title_exclude`` lists
            of lowercase strings.

    Returns:
        A new prefilter dict with merged title patterns and all other keys
        preserved from ``existing_prefilter``.
    """
    result = dict(existing_prefilter)

    def _merge_list(key: str) -> list[str]:
        """Return deduped union of existing and suggested values for *key*.

        Both existing and suggested patterns are normalised to lowercase so
        the merged output is consistently cased.  Filter matching is already
        case-insensitive, so this is semantically neutral while avoiding
        mixed-case lists like ``["Engineer", "developer"]`` in config.json.
        """
        existing: list[str] = [v.lower() for v in (existing_prefilter.get(key) or [])]
        existing_set = set(existing)
        merged = list(existing)
        for term in suggestions.get(key, []):
            term_lower = term.lower()
            if term_lower not in existing_set:
                merged.append(term_lower)
                existing_set.add(term_lower)
        return merged

    result["title_include"] = _merge_list("title_include")
    result["title_exclude"] = _merge_list("title_exclude")
    return result


# ---------------------------------------------------------------------------
# PDF resume import — async job tracking
# ---------------------------------------------------------------------------

# Text length threshold above which the import is dispatched to a background
# thread rather than blocking the Flask request.  Adjust as needed.
_PDF_ASYNC_THRESHOLD = 10_000

# Job store: maps job_id (str UUID) → job dict.
# Each entry: {id, status, result, error, created_at, started_at}
# status values: "pending" | "running" | "complete" | "failed"
_pdf_jobs: dict = {}
_pdf_jobs_lock = threading.Lock()

# Bounded thread pool for async PDF imports — prevents resource exhaustion.
_pdf_executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="pdf-import")
_MAX_CONCURRENT_PDF_JOBS = 3

# Completed/failed jobs are pruned after this many seconds.
_PDF_JOB_TTL_SECONDS = 300  # 5 minutes
# Running jobs older than this are marked failed (hung LLM call protection).
_PDF_JOB_TIMEOUT_SECONDS = 300  # 5 minutes

# Rate-limit pruning so it doesn't run on every status poll.
_last_prune_time: float = 0.0
_PRUNE_INTERVAL_SECONDS = 30


def _prune_pdf_jobs() -> None:
    """Timeout stuck jobs and remove old completed/failed jobs.

    Rate-limited to run at most once per ``_PRUNE_INTERVAL_SECONDS`` to avoid
    O(n) iteration on every status poll.  Not exported — internal helper only.
    """
    global _last_prune_time
    now_mono = _time.monotonic()
    if now_mono - _last_prune_time < _PRUNE_INTERVAL_SECONDS:
        return
    _last_prune_time = now_mono

    now = datetime.now(timezone.utc).timestamp()
    cutoff = now - _PDF_JOB_TTL_SECONDS
    with _pdf_jobs_lock:
        # Timeout stuck running jobs
        for job in _pdf_jobs.values():
            if (
                job["status"] == "running"
                and job.get("started_at")
                and now - job["started_at"] > _PDF_JOB_TIMEOUT_SECONDS
            ):
                job["status"] = "failed"
                job["error"] = "Job timed out after 5 minutes."

        # Remove old completed/failed jobs
        to_delete = [
            jid
            for jid, job in _pdf_jobs.items()
            if job["status"] in ("complete", "failed")
            and job["created_at"] < cutoff
        ]
        for jid in to_delete:
            del _pdf_jobs[jid]


def _run_pdf_import_job(
    job_id: str,
    resume_text: str,
    mode: str,
    providers_dict: dict,
    profile_path: str,
    suggest_filters: bool = False,
) -> None:
    """Worker function executed in a daemon thread for large PDF imports.

    Calls the LLM provider chain synchronously (which can take 5–30 s), then
    stores the result or error in ``_pdf_jobs`` under ``job_id``.

    Args:
        job_id:          UUID string identifying the job in ``_pdf_jobs``.
        resume_text:     Pre-validated, sanitised resume text to send to LLM.
        mode:            ``"fresh"`` or ``"merge"``.
        providers_dict:  Loaded providers config dict (captured at request
                         time).
        profile_path:    Filesystem path to the profile JSON (for merge mode).
        suggest_filters: When ``True``, the LLM is additionally asked to
                         return ``prefilter_suggestions``
                         (title_include / title_exclude).
    """

    with _pdf_jobs_lock:
        _pdf_jobs[job_id]["status"] = "running"
        _pdf_jobs[job_id]["started_at"] = datetime.now(timezone.utc).timestamp()

    try:
        chain = build_provider_chain(providers_dict)
        if not chain:
            with _pdf_jobs_lock:
                _pdf_jobs[job_id]["status"] = "failed"
                _pdf_jobs[job_id]["error"] = (
                    "No LLM provider is configured. Add one in Settings first."
                )
            return

        current_profile = load_profile(profile_path) if mode == "merge" else None
        prompt = _build_import_prompt(resume_text, suggest_filters=suggest_filters)
        result = generate_with_fallback(prompt, chain, set())
        if result is None:
            with _pdf_jobs_lock:
                _pdf_jobs[job_id]["status"] = "failed"
                _pdf_jobs[job_id]["error"] = (
                    "All LLM providers failed. Check your API keys in Settings."
                )
            return

        raw_text, model_used = result
        parsed = _parse_import_response(raw_text)
        if parsed is None:
            with _pdf_jobs_lock:
                _pdf_jobs[job_id]["status"] = "failed"
                _pdf_jobs[job_id]["error"] = (
                    "LLM returned an unparseable response. Try again."
                )
            return

        if mode == "merge":
            profile_result = _merge_import_result(current_profile, parsed)
        else:
            structured_skills = []
            for s in parsed.get("primary_skills", []):
                name = s.get("skill", "")
                years = s.get("years", 0)
                status = s.get("status", "active")
                structured_skills.append({
                    "description": name,
                    "years_active": int(years) if years else 0,
                    "active": status != "dormant",
                })
            profile_result = {
                "primary_skills": structured_skills,
                "education": _normalise_education(parsed.get("education", [])),
                "seniority": parsed.get("seniority", ""),
                "preferred_industries": parsed.get("preferred_industries", []),
                "location_center": parsed.get("location_center"),
            }

        job_result: dict = {
            "success": True,
            "profile": profile_result,
            "model_used": model_used,
        }
        if suggest_filters and "prefilter_suggestions" in parsed:
            job_result["prefilter_suggestions"] = parsed["prefilter_suggestions"]

        with _pdf_jobs_lock:
            _pdf_jobs[job_id]["status"] = "complete"
            _pdf_jobs[job_id]["result"] = job_result

    except (ValueError, KeyError, TypeError, RuntimeError, OSError) as exc:
        with _pdf_jobs_lock:
            _pdf_jobs[job_id]["status"] = "failed"
            _pdf_jobs[job_id]["error"] = f"Import error: {exc}"
    except Exception as exc:  # noqa: BLE001 — daemon thread; must capture all failures
        with _pdf_jobs_lock:
            _pdf_jobs[job_id]["status"] = "failed"
            _pdf_jobs[job_id]["error"] = f"Unexpected error: {exc}"


# ---------------------------------------------------------------------------
# PDF resume import — endpoint
# ---------------------------------------------------------------------------


@app.route("/profile/import-pdf", methods=["POST"])
def profile_import_pdf():
    """Import profile data from an uploaded PDF resume via LLM extraction.

    Accepts a multipart/form-data POST with:
    - ``file``: PDF file upload (required, max 10 MB).
    - ``mode``: ``"fresh"`` (default) or ``"merge"``.

    **Small PDFs** (extracted text ≤ ``_PDF_ASYNC_THRESHOLD`` chars) are
    processed synchronously and return the result directly.

    **Large PDFs** (extracted text > ``_PDF_ASYNC_THRESHOLD`` chars) are
    dispatched to a daemon thread; the response is HTTP 202 with a ``job_id``
    that the client must poll via ``GET /profile/import-pdf/status/<job_id>``.

    Returns JSON — does NOT write profile.json.  The response payload is
    intended for client-side form pre-fill so the user can review before saving.

    .. note::
        **CSRF protection**: the endpoint is guarded by the app's
        localhost/private-network origin check, which rejects cross-origin
        requests from outside the trusted network.

    Returns:
        200 ``{"success": True, "profile": {...}, "model_used": "provider/model"}``
        202 ``{"async": True, "job_id": "<uuid>"}`` (large PDF, poll for result)
        400 invalid input (no file, non-PDF, unreadable PDF)
        413 file or extracted text exceeds size limits
        422 extracted text too short to be useful
        502 LLM failure (all providers failed or unparseable response)
        503 no LLM provider configured
    """
    import uuid as _uuid

    # Validate file
    uploaded = request.files.get("file")
    if not uploaded or not uploaded.filename:
        return jsonify({"success": False, "error": "No file uploaded."}), 400
    if not uploaded.filename.lower().endswith(".pdf"):
        return jsonify({"success": False, "error": "Only PDF files are accepted."}), 400

    mode = request.form.get("mode", "fresh")
    if mode not in ("fresh", "merge"):
        mode = "fresh"

    # Optional prefilter title-filter suggestions (off by default).
    suggest_filters = request.form.get("suggest_filters") == "1"

    # Extract text
    pdf_bytes = uploaded.read()
    if len(pdf_bytes) > 10 * 1024 * 1024:
        return jsonify({"success": False, "error": "PDF exceeds the 10 MB size limit."}), 413
    try:
        resume_text = _extract_pdf_text(pdf_bytes)
    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400

    if len(resume_text.strip()) < 50:
        return jsonify({"success": False, "error": "Could not extract meaningful text from this PDF."}), 422

    # Prompt injection mitigation: enforce length cap and strip control characters
    if len(resume_text) > 50_000:
        return jsonify({"success": False, "error": "Extracted PDF text exceeds the 50,000 character limit."}), 413
    resume_text = "".join(ch for ch in resume_text if ch.isprintable() or ch in "\n\r\t")

    # Dispatch large PDFs asynchronously to avoid blocking the Flask thread.
    if len(resume_text) > _PDF_ASYNC_THRESHOLD:
        job_id = str(_uuid.uuid4())
        with _pdf_jobs_lock:
            active = sum(
                1 for j in _pdf_jobs.values()
                if j["status"] in ("pending", "running")
            )
            if active >= _MAX_CONCURRENT_PDF_JOBS:
                return jsonify({
                    "success": False,
                    "error": "Too many concurrent imports. Please wait and try again.",
                }), 429
            _pdf_jobs[job_id] = {
                "id": job_id,
                "status": "pending",
                "result": None,
                "error": None,
                "created_at": datetime.now(timezone.utc).timestamp(),
            }
        providers_dict = _load_providers_safe()
        _pdf_executor.submit(
            _run_pdf_import_job,
            job_id,
            resume_text,
            mode,
            providers_dict,
            _PROFILE_PATH,
            suggest_filters,
        )
        return jsonify({"async": True, "job_id": job_id}), 202

    # Small PDF — synchronous path
    providers_dict = _load_providers_safe()
    chain = build_provider_chain(providers_dict)
    if not chain:
        return jsonify({"success": False, "error": "No LLM provider is configured. Add one in Settings first."}), 503

    # Build prompt and call LLM
    current_profile = load_profile(_PROFILE_PATH) if mode == "merge" else None
    prompt = _build_import_prompt(resume_text, suggest_filters=suggest_filters)
    result = generate_with_fallback(prompt, chain, set())
    if result is None:
        return jsonify({"success": False, "error": "All LLM providers failed. Check your API keys in Settings."}), 502

    raw_text, model_used = result

    # Parse response
    parsed = _parse_import_response(raw_text)
    if parsed is None:
        return jsonify({"success": False, "error": "LLM returned an unparseable response. Try again."}), 502

    # Apply merge or format for fresh
    if mode == "merge":
        profile_result = _merge_import_result(current_profile, parsed)
    else:
        structured_skills = []
        for s in parsed.get("primary_skills", []):
            name = s.get("skill", "")
            years = s.get("years", 0)
            status = s.get("status", "active")
            structured_skills.append({
                "description": name,
                "years_active": int(years) if years else 0,
                "active": status != "dormant",
            })
        profile_result = {
            "primary_skills": structured_skills,
            "education": _normalise_education(parsed.get("education", [])),
            "seniority": parsed.get("seniority", ""),
            "preferred_industries": parsed.get("preferred_industries", []),
            "location_center": parsed.get("location_center"),
        }

    response_payload: dict = {
        "success": True,
        "profile": profile_result,
        "model_used": model_used,
    }
    if suggest_filters and "prefilter_suggestions" in parsed:
        response_payload["prefilter_suggestions"] = parsed["prefilter_suggestions"]

    return jsonify(response_payload), 200


@app.route("/profile/import-pdf/status/<job_id>", methods=["GET"])
def profile_import_pdf_status(job_id: str):
    """Poll the status of an async PDF import job.

    Args:
        job_id: UUID returned by ``POST /profile/import-pdf`` when a large PDF
                was submitted (response contained ``"async": True``).

    Returns:
        200 ``{"status": "pending"}`` or ``{"status": "running"}``
        200 ``{"status": "complete", "result": {...}}`` — same shape as sync 200
        200 ``{"status": "failed", "error": "..."}``
        404 if ``job_id`` is unknown or has already been pruned
    """
    _prune_pdf_jobs()

    with _pdf_jobs_lock:
        job = _pdf_jobs.get(job_id)

    if job is None:
        return jsonify({"error": "Job not found."}), 404

    status = job["status"]
    if status in ("pending", "running"):
        return jsonify({"status": status}), 200
    if status == "complete":
        return jsonify({"status": "complete", "result": job["result"]}), 200
    # status == "failed"
    return jsonify({"status": "failed", "error": job["error"]}), 200


@app.route("/profile", methods=["GET", "POST"])
def profile():
    """Profile page — structured form for candidate preferences.

    GET:  Loads both ``profile.json`` and the candidate-facing subset of
          ``config.json``, and passes structured dicts to the template.  No
          raw JSON is exposed; no sensitive fields are present.

    POST: Parses individual form fields, writes ``profile.json`` from the
          profile fields, and deep-merges only the candidate-facing config
          fields (``search.*`` candidate keys, ``scoring.threshold``,
          ``prefilter.*``) back into ``config.json`` — leaving technical keys
          (``results_per_page``, ``max_pages``, ``model``, etc.) untouched.
          Returns 422 on validation errors without touching either file.
    """
    saved = False
    error = None
    status_code = 200

    if request.method == "POST":
        # --- Validate before touching disk ---
        threshold_str = request.form.get("scoring_threshold", "")
        validation_errors = _validate_profile_form(threshold_str)
        if validation_errors:
            error = "; ".join(validation_errors)
            status_code = 422
        else:
            # Collect any additional field-level validation errors.
            field_errors: list[str] = []

            # Build profile.json dict from profile fields.
            location_block: dict = {}
            loc_center = request.form.get("location_center", "").strip()
            loc_radius = request.form.get("location_radius_km", "").strip()
            loc_fallback = request.form.get("location_geocode_fallback", "pass").strip()
            loc_notes = request.form.get("location_notes", "").strip()
            if loc_center:
                location_block["center"] = loc_center
            if loc_radius:
                try:
                    radius = float(loc_radius)
                    if radius > 0:
                        location_block["radius_km"] = radius
                    else:
                        field_errors.append("location.radius_km must be greater than 0")
                except ValueError:
                    field_errors.append("location.radius_km must be a number")
            location_block["geocode_fallback"] = loc_fallback or "pass"
            if loc_notes:
                location_block["notes"] = loc_notes

            # Parse structured primary_skills fields.
            # Each skill is submitted as parallel arrays:
            #   skill_description[]   — the skill name
            #   skill_years_active[]  — years of experience (integer)
            #   skill_active_idx[]    — indices (0-based) of rows where active=true
            # We use an index list for active because unchecked checkboxes are not
            # submitted by browsers; the hidden-input trick captures which rows
            # the user toggled ON.
            descriptions = request.form.getlist("skill_description[]")
            years_raw = request.form.getlist("skill_years_active[]")
            active_indices_raw = request.form.getlist("skill_active_idx[]")
            try:
                active_indices = {int(x) for x in active_indices_raw if x.strip()}
            except ValueError:
                active_indices = set()

            primary_skills: list[dict] = []
            for i, desc in enumerate(descriptions):
                desc = desc.strip()
                if not desc:
                    continue  # skip empty rows
                years_str = years_raw[i] if i < len(years_raw) else "0"
                try:
                    years = int(years_str)
                except (ValueError, TypeError):
                    field_errors.append(
                        f"Primary skill '{desc}': years must be a whole number, got '{years_str}'"
                    )
                    continue
                if years < 0:
                    field_errors.append(
                        f"Primary skill '{desc}': years_active cannot be negative"
                    )
                primary_skills.append({
                    "description": desc,
                    "years_active": years,
                    "active": i in active_indices,
                })

            new_profile: dict = {
                "primary_skills": primary_skills,
                "anti_preferences": _parse_repeating_rows(request.form, "anti_preferences"),
                "education": _parse_education_rows(request.form),
                "seniority": request.form.get("seniority", "").strip(),
                "preferred_industries": _parse_repeating_rows(request.form, "preferred_industries"),
                "location": location_block,
                "scoring_notes": _parse_repeating_rows(request.form, "scoring_notes"),
            }

            # Build the candidate-facing config.json subset.
            # Read existing config first so we can merge (preserving technical keys).
            existing_cfg = load_config(_CONFIG_PATH)
            existing_search = existing_cfg.get("search") or {}
            existing_scoring = existing_cfg.get("scoring") or {}
            existing_prefilter = existing_cfg.get("prefilter") or {}

            # Candidate search fields — only update these; leave results_per_page
            # and max_pages (technical fields managed on the Settings page) alone.
            salary_min_str = request.form.get("search_salary_min", "").strip()
            distance_str = request.form.get("search_distance", "").strip()
            max_days_str = request.form.get("search_max_days_old", "").strip()

            updated_search = dict(existing_search)
            updated_search["country"] = request.form.get("search_country", "").strip()
            updated_search["what"] = request.form.get("search_what", "").strip()
            updated_search["where"] = request.form.get("search_where", "").strip()
            if distance_str:
                try:
                    dist = int(distance_str)
                    if dist >= 0:
                        updated_search["distance"] = dist
                    else:
                        field_errors.append("search.distance must be 0 or greater")
                except ValueError:
                    field_errors.append("search.distance must be a whole number")
            else:
                updated_search.pop("distance", None)
            if salary_min_str:
                try:
                    sal = int(salary_min_str)
                    if sal >= 0:
                        updated_search["salary_min"] = sal
                    else:
                        field_errors.append("search.salary_min must be 0 or greater")
                except ValueError:
                    field_errors.append("search.salary_min must be a whole number")
            else:
                updated_search.pop("salary_min", None)
            if max_days_str:
                try:
                    days = int(max_days_str)
                    if days > 0:
                        updated_search["max_days_old"] = days
                    else:
                        field_errors.append("search.max_days_old must be greater than 0")
                except ValueError:
                    field_errors.append("search.max_days_old must be a whole number")
            else:
                updated_search.pop("max_days_old", None)

            # Bail out before touching disk if any field-level errors were found.
            if field_errors:
                error = "; ".join(field_errors)
                status_code = 422

            if not field_errors:
                # scoring.threshold — parse is already validated above.
                updated_scoring = dict(existing_scoring)
                updated_scoring["threshold"] = float(threshold_str.strip())

                # prefilter fields.
                require_contract_time_raw = request.form.get("prefilter_require_contract_time", "").strip()
                require_contract_type_raw = request.form.get("prefilter_require_contract_type", "").strip()
                updated_prefilter = dict(existing_prefilter)
                updated_prefilter["title_include"] = _parse_repeating_rows(request.form, "prefilter_title_include")
                updated_prefilter["title_exclude"] = _parse_repeating_rows(request.form, "prefilter_title_exclude")
                updated_prefilter["require_contract_time"] = require_contract_time_raw or None
                updated_prefilter["require_contract_type"] = require_contract_type_raw or None

                new_cfg = dict(existing_cfg)
                new_cfg["search"] = updated_search
                new_cfg["scoring"] = updated_scoring
                new_cfg["prefilter"] = updated_prefilter

                # Write profile.json atomically.
                try:
                    _write_json_atomic(_PROFILE_PATH, new_profile)
                    _write_json_atomic(_CONFIG_PATH, new_cfg)
                    saved = True
                except OSError:
                    error = "Could not save — check file permissions."
                    status_code = 500

    # Load current values for the form (GET, or POST after error).
    cfg = load_config(_CONFIG_PATH)
    prof = load_profile(_PROFILE_PATH)

    # Establish the session CSRF token so the import drawer can include it on
    # the POST /api/apply-prefilter-suggestions request.
    session.setdefault("csrf_token", secrets.token_urlsafe(32))

    return render_template(
        "profile.html",
        view="profile",
        prof=prof,
        cfg=cfg,
        saved=saved,
        error=error,
        csrf_token=session["csrf_token"],
    ), status_code


@app.route("/settings/config")
def settings_config_redirect():
    return redirect(url_for("profile"), code=301)


# ---------------------------------------------------------------------------
# Admin actions
# ---------------------------------------------------------------------------

_LOG_FILENAME_RE = re.compile(r"^ingest_(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})\.log$")

# Scheduler health thresholds (hours since last scheduled run).
SCHEDULE_WARN_HOURS = 25
SCHEDULE_CRITICAL_HOURS = 49


@app.route("/admin")
def admin():
    """Administration page — runtime info, log downloads, ingest schedule, and database ops."""
    session.setdefault("csrf_token", secrets.token_urlsafe(32))
    listing_count = db.get_listing_count()
    return render_template(
        "admin.html",
        view="admin",
        listing_count=listing_count,
        csrf_token=session["csrf_token"],
        runtime_versions=RUNTIME_VERSIONS,
    )


@app.route("/admin/clear-db", methods=["POST"])
def admin_clear_db():
    """Delete all rows from the listings table.

    Requires the ``confirmation`` form field to equal exactly ``"DELETE"``
    (case-sensitive).  Any other value is rejected with 400 so that a
    misconfigured HTMX request or stray form submit cannot wipe data silently.

    On success the deleted row count is logged with a UTC timestamp and an
    HTMX-compatible HTML fragment is returned so the caller can swap it into
    the confirmation panel target.  The fragment includes the success notice
    and resets the danger-zone panel to its collapsed initial state so the
    user sees clear feedback without a full page reload.

    Returns:
        200 HTML fragment on success.
        400 HTML fragment when the confirmation phrase is wrong.
        500 HTML fragment on database error.
    """
    # CSRF check — token must match the session value established on GET /admin.
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
            "Confirmation phrase did not match — database was not cleared."
            "</p>"
        )
        return make_response(html, 400)

    try:
        conn = db.get_connection()
        try:
            deleted = db.clear_all_listings(conn)
        finally:
            conn.close()
    except Exception as exc:  # pragma: no cover — DB errors are rare in tests
        app.logger.error("clear_all_listings failed: %s", exc)
        html = (
            '<p class="save-error" id="clear-db-result">'
            f"Database error — listings were not cleared: {exc}"
            "</p>"
        )
        return make_response(html, 500)

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    app.logger.info("[%s] admin/clear-db: deleted %d listing(s).", ts, deleted)

    # Return an HTML fragment that:
    # 1. Replaces the confirmation panel with a success notice.
    # 2. Hides the danger-zone panel (collapsed back to just the trigger button).
    noun = "listing" if deleted == 1 else "listings"
    html = (
        f'<p class="save-notice" id="clear-db-result">'
        f"{deleted} {noun} deleted successfully."
        f"</p>"
        f'<div id="clear-db-panel" style="display:none"></div>'
    )
    return make_response(html, 200)


@app.route("/admin/logs")
def admin_logs():
    """Return an HTML fragment listing available ingest log files."""
    logs = []
    try:
        for entry in os.scandir(LOG_DIR):
            if not entry.is_file():
                continue
            m = _LOG_FILENAME_RE.match(entry.name)
            if not m:
                continue
            # Check readability
            try:
                size = entry.stat().st_size
            except OSError:
                continue
            timestamp = f"{m.group(1)}-{m.group(2)}-{m.group(3)} {m.group(4)}:{m.group(5)}:{m.group(6)}"
            # Human-readable size
            if size >= 1_048_576:
                size_str = f"{size / 1_048_576:.1f} MB"
            elif size >= 1024:
                size_str = f"{size / 1024:.1f} KB"
            else:
                size_str = f"{size} B"
            logs.append({"filename": entry.name, "timestamp": timestamp, "size": size_str})
    except FileNotFoundError:
        pass  # LOG_DIR doesn't exist yet — empty list

    logs.sort(key=lambda x: x["filename"], reverse=True)  # newest first
    return render_template("admin/_log_list.html", logs=logs)


@app.route("/admin/logs/<filename>/download")
def admin_log_download(filename):
    """Download an ingest log file."""
    # Validate filename against strict regex
    if not _LOG_FILENAME_RE.match(filename):
        abort(404)

    target = (LOG_DIR / filename).resolve()

    # Symlink escape check — resolved path must be inside LOG_DIR.
    # Use relative_to() rather than startswith() so the check is
    # case-insensitive-safe and handles path separators correctly on Windows.
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


@app.route("/admin/schedule-state")
def admin_schedule_state():
    """Return an HTML fragment showing ingest run history and scheduler health."""
    try:
        runs = db.get_recent_ingest_runs(10)
    # Catch-all: schedule state is best-effort; a DB error must never break the admin page.
    except Exception:  # noqa: BLE001
        runs = []

    # Compute health badge
    badge = "none"  # no data
    badge_text = "No runs recorded yet"

    if runs:
        # Find most recent scheduled run
        scheduled_runs = [r for r in runs if r.get("trigger_source") == "scheduled"]

        if scheduled_runs:
            last_scheduled = scheduled_runs[0]
            age_hours = None
            if last_scheduled.get("started_at"):
                started = last_scheduled["started_at"]
                if hasattr(started, "tzinfo") and started.tzinfo:
                    now = datetime.now(timezone.utc)
                else:
                    now = datetime.utcnow()
                age_hours = (now - started).total_seconds() / 3600

            if last_scheduled.get("status") == "failed":
                badge = "red"
                badge_text = "Last scheduled run failed"
            elif age_hours is not None and age_hours > SCHEDULE_CRITICAL_HOURS:
                badge = "red"
                badge_text = f"Scheduler may be down — no scheduled run in {SCHEDULE_CRITICAL_HOURS}+ hours"
            elif age_hours is not None and age_hours > SCHEDULE_WARN_HOURS:
                badge = "amber"
                badge_text = f"Last scheduled run was {SCHEDULE_WARN_HOURS}+ hours ago"
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


# ---------------------------------------------------------------------------
# Key validation
# ---------------------------------------------------------------------------

_VALIDATE_TIMEOUT_SECONDS = 5
"""Per-provider timeout for key validation API calls."""


def _validate_with_timeout(validator, api_key: str, model: str) -> tuple[str, str | None]:
    """Run *validator(api_key, model)* in a daemon thread with a fixed timeout.

    Returns the validator's ``(state, detail)`` tuple, or a synthetic
    ``('unreachable', ...)`` tuple if the call does not complete within
    ``_VALIDATE_TIMEOUT_SECONDS``.

    Args:
        validator: Callable ``(api_key, model) -> tuple[str, str | None]``.
        api_key:   Provider API key string.
        model:     Provider model name string.

    Returns:
        ``(state, detail)`` where *state* is one of: ``'valid'``,
        ``'invalid_key'``, ``'unknown_model'``, ``'unreachable'``.
        *detail* is ``None`` on success or a short error string on failure.
    """
    result_holder: list[tuple[str, str | None]] = []

    def _target() -> None:
        try:
            result_holder.append(validator(api_key, model))
        except Exception as exc:
            result_holder.append(("unreachable", _sanitise_detail(str(exc), api_key)))

    t = threading.Thread(target=_target, daemon=True)
    t.start()
    t.join(timeout=_VALIDATE_TIMEOUT_SECONDS)
    if t.is_alive():
        # Thread is still blocked (network hang) — treat as unreachable.
        return ("unreachable", f"Timed out after {_VALIDATE_TIMEOUT_SECONDS}s")
    return result_holder[0] if result_holder else ("unreachable", None)


@app.route("/api/apply-prefilter-suggestions", methods=["POST"])
def apply_prefilter_suggestions():
    """Merge LLM-suggested title filters into config.json prefilter block.

    Accepts a form-encoded POST with fields:

    * ``csrf_token`` — session-scoped CSRF token (required; 403 on mismatch)
    * ``title_include`` — JSON-encoded array of include patterns
    * ``title_exclude`` — JSON-encoded array of exclude patterns

    The suggestions are merged (union-then-dedup, case-insensitive) into the
    existing ``config.json`` ``prefilter`` block via
    ``_merge_prefilter_suggestions()``.  All other prefilter keys
    (``require_contract_time``, ``require_contract_type``) are preserved.

    The disjoint-set invariant is enforced here too: if the POST body itself
    contains overlapping include/exclude terms the request is rejected with
    400 so malformed client payloads cannot corrupt config.

    Returns:
        200 ``{"success": True}`` on success.
        400 on missing/invalid input or overlapping include/exclude terms.
        403 on CSRF token mismatch.
        500 on config read/write failure.
    """
    # CSRF check — token must match the session value established on GET /profile.
    csrf_token = request.form.get("csrf_token", "")
    if not csrf_token or csrf_token != session.get("csrf_token"):
        return jsonify({
            "success": False,
            "error": "Invalid or missing CSRF token — request rejected.",
        }), 403

    inc_json = request.form.get("title_include", "")
    exc_json = request.form.get("title_exclude", "")

    try:
        inc_raw = json.loads(inc_json) if inc_json else None
        exc_raw = json.loads(exc_json) if exc_json else None
    except (json.JSONDecodeError, ValueError):
        inc_raw = None
        exc_raw = None

    if not isinstance(inc_raw, list) or not isinstance(exc_raw, list):
        return jsonify({
            "success": False,
            "error": (
                "title_include and title_exclude must be JSON-encoded arrays."
            ),
        }), 400

    inc = [str(s).lower() for s in inc_raw]
    exc = [str(s).lower() for s in exc_raw]

    # Intentional double-check: _parse_import_response validates the LLM response,
    # but the form submission could be tampered between the preview render and the
    # Apply click.  Re-validate at the HTTP boundary.
    overlap = set(inc) & set(exc)
    if overlap:
        return jsonify({
            "success": False,
            "error": (
                "title_include and title_exclude must be disjoint. "
                f"Overlapping terms: {sorted(overlap)}"
            ),
        }), 400

    try:
        with open(_CONFIG_PATH, "r", encoding="utf-8") as fh:
            cfg = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc_io:
        app.logger.error(
            "[apply-prefilter-suggestions] failed to read config: %s", exc_io
        )
        return jsonify({
            "success": False,
            "error": "Could not read config.json.",
        }), 500

    existing_prefilter = cfg.get("prefilter") or {}
    cfg["prefilter"] = _merge_prefilter_suggestions(
        existing_prefilter,
        {"title_include": inc, "title_exclude": exc},
    )

    try:
        with open(_CONFIG_PATH, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2)
            fh.write("\n")
    except OSError as exc_io:
        app.logger.error(
            "[apply-prefilter-suggestions] failed to write config: %s", exc_io
        )
        return jsonify({
            "success": False,
            "error": "Could not write config.json.",
        }), 500

    return jsonify({"success": True}), 200


@app.route("/api/validate-keys", methods=["POST"])
def validate_keys():
    """Validate each configured LLM provider by making a minimal 1-token test call.

    Loops ``_PROVIDER_CLASS_MAP`` so new providers are included automatically
    without any template or route changes.

    Returns an HTML partial (not JSON) intended for HTMX to swap into the page.
    Each provider gets one of five states: valid, invalid_key, unknown_model,
    unreachable, not_configured.  Each provider call is bounded to
    ``_VALIDATE_TIMEOUT_SECONDS`` seconds; a timeout maps to ``unreachable``.

    No API key values are logged or returned in the response.
    """
    providers_data = _load_providers_safe()
    llm_section: dict = providers_data.get("llm") or {}

    providers_list = []
    for provider_key, cls in _PROVIDER_CLASS_MAP.items():
        schema = cls.settings_schema()
        display_name: str = schema.get("display_name", provider_key.title())

        cfg = llm_section.get(provider_key, {})
        api_key = cfg.get("api_key", "").strip()
        model   = cfg.get("model", "").strip()

        if not api_key:
            state = "not_configured"
            detail = None
        else:
            state, detail = _validate_with_timeout(cls.validate_credentials, api_key, model)

        providers_list.append({
            "key":          provider_key,
            "display_name": display_name,
            "state":        state,
            "detail":       detail,
        })

    return render_template("_validation_results.html", providers=providers_list)


@app.route("/api/providers/reorder", methods=["POST"])
def api_providers_reorder():
    """Persist a new LLM provider fallback order.

    Expects JSON body: ``{"order": ["anthropic", "gemini", "openai"]}``

    * All entries must be known keys in ``_PROVIDER_CLASS_MAP``; unknown keys → 400.
    * ``order`` may be a subset of the registry (omitted providers are appended at
      runtime by ``build_provider_chain()``).
    * Writes only ``provider_order`` at the top level of ``providers.json``.
    * Returns the rendered ``_provider_order.html`` fragment on success (200).
    * Returns a plain-text error message on failure (400/500).
    """
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return "Invalid request body — expected JSON object.", 400

    order = body.get("order")
    if not isinstance(order, list):
        return "Missing or invalid 'order' field — expected a JSON array.", 400

    if not all(isinstance(k, str) for k in order):
        return "All entries in 'order' must be strings.", 400

    unknown = [k for k in order if k not in _PROVIDER_CLASS_MAP]
    if unknown:
        return f"Unknown provider key(s): {', '.join(unknown)}", 400

    if len(order) != len(set(order)):
        return "Duplicate provider key(s) in order list.", 400

    try:
        save_providers({"provider_order": order}, providers_path=_PROVIDERS_PATH)
    except OSError:
        return "Could not save order — check file permissions.", 500

    # Re-build llm_schemas in the new order for the response fragment.
    # We re-read providers.json here (rather than using the in-memory `order`
    # list alone) to pick up the has_values flags from the just-written file.
    providers_data = _load_providers_safe()
    llm_section: dict = providers_data.get("llm") or {}
    llm_schemas = _build_llm_schemas(llm_section, order)

    return render_template("_provider_order.html", llm_schemas=llm_schemas)


@app.route("/api/job-sources/<source_key>/toggle", methods=["POST"])
def api_job_source_toggle(source_key: str):
    """Persist the enabled/disabled state for a single job source.

    Designed for HTMX ``hx-trigger="change"`` on the source toggle checkbox so
    the change is saved immediately without requiring a full form submit.

    Request body (JSON)::

        {"enabled": true}   # or false

    Validation rules:

    * ``source_key`` must exist in the ``SOURCES`` registry → 404 if unknown.
    * When ``enabled=true``, all ``required`` credential fields for the source
      must have non-empty values already stored in ``providers.json`` → 422 if
      any are missing.
    * When ``enabled=false``, no credential check is performed.

    Returns:
        200 JSON ``{"ok": true}`` on success.
        404 JSON ``{"error": "..."}`` for unknown source keys.
        422 JSON ``{"error": "..."}`` when required credentials are missing.
        400 plain text for a malformed request body.
        500 plain text if the file cannot be written.
    """
    if source_key not in get_sources():
        return jsonify({"error": f"Unknown job source: {source_key!r}"}), 404

    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return "Invalid request body — expected JSON object.", 400

    if "enabled" not in body:
        return "Missing 'enabled' field in request body.", 400

    enabled = body["enabled"]
    if not isinstance(enabled, bool):
        return "The 'enabled' field must be a boolean (true or false).", 400

    # When enabling, verify required credentials are already stored.
    if enabled:
        cls = get_sources()[source_key]
        schema = cls.settings_schema()
        required_fields = [f for f in schema.get("fields", []) if f.get("required")]

        if required_fields:
            providers_data = _load_providers_safe()
            src_cfg: dict = (providers_data.get("job_sources") or {}).get(source_key) or {}
            missing = [
                f["label"]
                for f in required_fields
                if not str(src_cfg.get(f["name"], "")).strip()
            ]
            if missing:
                display_name = schema.get("display_name", source_key)
                fields_str = " and ".join(missing)
                return jsonify({
                    "error": (
                        f"{display_name} requires {fields_str} before it can be enabled. "
                        "Add credentials in the Settings form and save, then try again."
                    )
                }), 422

    try:
        save_providers(
            {"job_sources": {source_key: {"enabled": enabled}}},
            providers_path=_PROVIDERS_PATH,
        )
    except OSError:
        return "Could not save — check file permissions.", 500

    return jsonify({"ok": True}), 200


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Job Matcher web server")
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Run in demo mode: uses jobs.demo.db and demo config/profile files",
    )
    args = parser.parse_args()

    if args.demo:
        DEMO_MODE = True
        # TODO: demo mode is not supported in the PostgreSQL deployment
        _PROFILE_PATH = os.path.join(_CONFIG_DIR, "profile.demo.json")
        _PROVIDERS_PATH = os.path.join(_CONFIG_DIR, "providers.demo.json")
        print("Demo mode enabled — using demo config files.")

    db.init_db()
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    # threaded=True is required for SSE (/ingest/stream) — without it Flask's
    # dev server is single-threaded and an open SSE connection blocks all other
    # requests, causing 429 errors.  Docker deployments use waitress (multi-
    # threaded) via the Dockerfile CMD and never execute this code path.
    app.run(debug=debug, port=5000, threaded=True)
