"""
app.py — Flask web server for Job Matcher.

Thin routing layer only. All data access goes through db.py.
Business logic lives in ingest.py; none of it belongs here.
"""

import ipaddress
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
from datetime import datetime, timezone
from importlib.metadata import version as pkg_version, PackageNotFoundError

from flask import Flask, render_template, make_response, request, jsonify, redirect, url_for

import db
from credentials import CredentialError, load_providers, save_providers
from io import BytesIO

from pypdf import PdfReader

from providers import _PROVIDER_CLASS_MAP, build_provider_chain, generate_with_fallback
from providers.anthropic_provider import strip_fences
from providers.base import _sanitise_detail
from job_sources import get_sources

app = Flask(__name__)

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
    """
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


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
        runtime_versions=RUNTIME_VERSIONS,
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

# Temp file that receives subprocess stdout, avoiding OS pipe-buffer deadlock.
_ingest_log_file: "tempfile.SpooledTemporaryFile | None" = None

# Stores the result of the most recently completed ingest run.
_last_run: dict | None = None

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


def _ingest_running() -> bool:
    """Return True if an ingest subprocess is currently active.

    Acquires ``_ingest_lock`` before touching shared state so concurrent calls
    from waitress worker threads are serialised.

    Polls the process exit code: if poll() returns None the process is still
    running. If it has exited, read the temp log file to capture stdout, parse
    the summary into ``_last_run``, and reset the handle to None so a new run
    can start.
    """
    global _ingest_process, _ingest_log_file, _last_run
    with _ingest_lock:
        if _ingest_process is None:
            return False
        if _ingest_process.poll() is not None:
            # Process has finished — read temp log and clear handles.
            output = ""
            if _ingest_log_file is not None:
                try:
                    _ingest_log_file.seek(0)
                    output = _ingest_log_file.read()
                    if isinstance(output, bytes):
                        output = output.decode("utf-8", errors="replace")
                except (OSError, ValueError):
                    output = ""
                finally:
                    try:
                        _ingest_log_file.close()
                    except OSError:
                        pass
                    _ingest_log_file = None
            _last_run = _parse_ingest_summary(output)
            _ingest_process = None
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

    Returns 202 with the 'Running...' HTML partial when the process is started.
    Returns 409 with a JSON error body if a run is already in progress — the
    caller can check Content-Type to distinguish the two response shapes.

    Uses sys.executable so the subprocess runs in the same virtualenv as the
    app server, picking up all installed dependencies automatically.

    stdout is redirected to a NamedTemporaryFile rather than subprocess.PIPE.
    This avoids the OS pipe-buffer deadlock: if the process emits more than
    ~64 KB of output (common with 200+ listings), a PIPE write blocks until the
    reader drains it — but app.py only reads after the process exits, causing
    a hang.  A temp file has no such size limit.
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
            log_file = tempfile.TemporaryFile(mode="w+", suffix=".log", prefix="ingest_")
            proc = subprocess.Popen(
                cmd,
                stdout=log_file,
                stderr=subprocess.STDOUT,
            )
        except (OSError, PermissionError) as e:
            return jsonify({"error": f"Failed to start ingestion: {e}"}), 500

        _ingest_log_file = log_file
        _ingest_process = proc

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


def _build_llm_schemas(
    llm_section: dict,
    provider_order: list[str],
) -> list[tuple[str, dict, bool, dict]]:
    """Build the ordered llm_schemas list for the settings template.

    Returns a list of ``(provider_key, schema_dict, has_values, current_values)``
    tuples.  Providers in *provider_order* come first (unknown/duplicate keys
    skipped), followed by any registry providers not listed, in registry
    insertion order.

    ``has_values`` is ``True`` only when every required field in the schema has
    a non-blank stored value.  Checking all required fields (not just
    ``api_key``) prevents a provider with a key but an empty model string from
    falsely showing "● configured".

    ``current_values`` maps non-password field names to their stored value (or
    the field's ``default`` if not yet stored).  This dict is passed to the
    template so that non-password inputs can be pre-populated, ensuring that
    the placeholder default is actually submitted when the user saves without
    explicitly editing the field.

    Args:
        llm_section:    The ``"llm"`` sub-dict from ``providers.json``.
        provider_order: The ``provider_order`` list from ``providers.json``.
    """
    seen: set[str] = set()
    schemas: list[tuple[str, dict, bool, dict]] = []

    def _make_entry(key: str) -> tuple[str, dict, bool, dict]:
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
        return (key, schema, has_values, current_values)

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
        updates: dict = {"llm": {}, "job_sources": {}}

        # LLM providers: iterate registry so new providers are handled automatically.
        for provider_key, cls in _PROVIDER_CLASS_MAP.items():
            schema = cls.settings_schema()
            provider_updates: dict = {}
            for field in schema["fields"]:
                field_name = field["name"]
                form_key = f"{provider_key}__{field_name}"
                value = request.form.get(form_key, "").strip()
                provider_updates[field_name] = value  # blank → save_providers skips it
            if provider_updates:
                updates["llm"][provider_key] = provider_updates

        # Job sources: save enabled flag for all; save credential fields for keyed sources.
        for source_key, cls in get_sources().items():
            schema = cls.settings_schema()
            source_updates: dict = {}

            # Checkbox: unchecked = not submitted = False
            enabled = request.form.get(f"{source_key}__enabled") == "on"
            source_updates["enabled"] = enabled

            for field in schema["fields"]:
                field_name = field["name"]
                form_key = f"{source_key}__{field_name}"
                value = request.form.get(form_key, "").strip()
                source_updates[field_name] = value

            updates["job_sources"][source_key] = source_updates

        try:
            save_providers(updates, providers_path=_PROVIDERS_PATH)
        except OSError:
            error = "Could not save settings — check file permissions."

        # Save technical search fields (results_per_page, max_pages) to config.json.
        if error is None and active_tab == "search":
            existing_cfg = load_config(_CONFIG_PATH)
            existing_search = existing_cfg.get("search") or {}
            rpp_str = request.form.get("search_results_per_page", "").strip()
            mp_str = request.form.get("search_max_pages", "").strip()
            updated_search = dict(existing_search)
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

    source_schemas: list[tuple[str, dict, bool, bool, bool]] = []
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
        source_schemas.append((key, schema, has_values, is_enabled, credentials_required))

    # POST-with-error: re-render the form (not a redirect) so the error is shown.
    saved = False  # POST always redirects on success; reaching here means error or GET
    if request.method == "POST" and error:
        pass  # fall through to render with error

    listing_count = db.get_listing_count()

    # Pass technical search fields to the Search Settings tab.
    search_cfg = load_config(_CONFIG_PATH).get("search") or {}

    return render_template(
        "settings.html",
        view="settings",
        llm_schemas=llm_schemas,
        source_schemas=source_schemas,
        active_tab=active_tab,
        saved=saved,
        error=error,
        listing_count=listing_count,
        search_cfg=search_cfg,
    )


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
    except Exception as exc:
        raise ValueError(f"Could not read PDF: {exc}") from exc


_IMPORT_PROMPT_FRESH = """You are extracting structured profile data from a resume/CV.

RESUME TEXT:
{resume_text}

Extract the following fields and respond with ONLY a JSON object. No explanation, no markdown, no code fences.

The JSON must have exactly these keys:
- "primary_skills": array of objects, each with "skill" (string), "years" (integer estimate), "status" ("active" or "dormant")
- "education": array of strings, each formatted as "Degree, Institution, Year" (e.g. "BS Computer Science, MIT, 2015")
- "seniority": string inferred from job titles (e.g. "Junior", "Mid-level", "Senior", "Staff", "Lead", "Principal")
- "preferred_industries": array of strings inferred from work history (e.g. "fintech", "healthtech", "developer tooling")
- "location_center": string from contact info if present (e.g. "Miami, FL"), or null if not found

If a field cannot be confidently extracted, use an empty array, empty string, or null as appropriate. Do not guess or hallucinate values.

JSON only:"""

_IMPORT_PROMPT_MERGE = """You are extracting structured profile data from a resume/CV to merge with an existing candidate profile.

EXISTING PROFILE:
{current_profile}

RESUME TEXT:
{resume_text}

Extract the following fields and respond with ONLY a JSON object. No explanation, no markdown, no code fences.

The JSON must have exactly these keys:
- "primary_skills": array of objects, each with "skill" (string), "years" (integer estimate), "status" ("active" or "dormant"). Include ALL skills from both the resume and existing profile. Do not remove existing skills.
- "education": array of strings, each formatted as "Degree, Institution, Year". Include entries from both resume and existing profile. Do not duplicate identical entries.
- "seniority": string inferred from job titles. If the existing profile already has a seniority value, keep it unchanged. Only fill this if the existing value is empty.
- "preferred_industries": array of strings inferred from work history. Include industries from both resume and existing profile without duplicates.
- "location_center": string from contact info if present (e.g. "Miami, FL"), or null if not found. If the existing profile has a location, keep it.

If a field cannot be confidently extracted, preserve the existing value. Do not guess or hallucinate values.

JSON only:"""


def _build_import_prompt(resume_text: str, mode: str, current_profile: dict | None) -> str:
    """Build the LLM prompt for PDF resume import.

    Args:
        resume_text:     Extracted plain text from the uploaded PDF.
        mode:            ``"fresh"`` or ``"merge"``.
        current_profile: Existing profile dict (used only in merge mode).

    Returns:
        Formatted prompt string ready to send to the LLM.
    """
    if mode == "merge" and current_profile:
        return _IMPORT_PROMPT_MERGE.format(
            current_profile=json.dumps(current_profile, indent=2),
            resume_text=resume_text,
        )
    return _IMPORT_PROMPT_FRESH.format(resume_text=resume_text)


def _parse_import_response(raw: str) -> dict | None:
    """Parse the LLM's JSON response for a PDF import request.

    Strips markdown code fences, parses JSON, and fills missing keys with
    safe defaults so callers can always rely on the expected keys existing.

    Args:
        raw: Raw text response from the LLM.

    Returns:
        Parsed dict with all expected keys, or ``None`` if parsing fails.
    """
    try:
        cleaned = strip_fences(raw)
        data = json.loads(cleaned)
    except (json.JSONDecodeError, ValueError):
        return None
    data.setdefault("primary_skills", [])
    data.setdefault("education", [])
    data.setdefault("seniority", "")
    data.setdefault("preferred_industries", [])
    data.setdefault("location_center", None)
    return data


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

    # Skills: existing preserved, new appended as "skill, Nyr, status" strings
    existing_skills = list(current.get("primary_skills", []))
    existing_skill_names = {s.split(",")[0].strip().lower() for s in existing_skills}
    for skill_obj in imported.get("primary_skills", []):
        name = skill_obj.get("skill", "")
        if name.lower() not in existing_skill_names:
            years = skill_obj.get("years", 0)
            status = skill_obj.get("status", "active")
            existing_skills.append(f"{name}, {years}yr, {status}")
            existing_skill_names.add(name.lower())
    result["primary_skills"] = existing_skills

    # Education: append new, skip duplicates (case-insensitive)
    existing_edu = list(current.get("education", []))
    existing_edu_lower = {e.lower() for e in existing_edu}
    for entry in imported.get("education", []):
        if entry.lower() not in existing_edu_lower:
            existing_edu.append(entry)
            existing_edu_lower.add(entry.lower())
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


# ---------------------------------------------------------------------------
# PDF resume import — endpoint
# ---------------------------------------------------------------------------


@app.route("/profile/import-pdf", methods=["POST"])
def profile_import_pdf():
    """Import profile data from an uploaded PDF resume via LLM extraction.

    Accepts a multipart/form-data POST with:
    - ``file``: PDF file upload (required).
    - ``mode``: ``"fresh"`` (default) or ``"merge"``.

    Returns JSON — does NOT write profile.json.  The response payload is
    intended for client-side form pre-fill so the user can review before saving.

    Returns:
        200 ``{"success": True, "profile": {...}, "model_used": "provider/model"}``
        400 invalid input (no file, non-PDF, unreadable PDF)
        422 extracted text too short to be useful
        502 LLM failure (all providers failed or unparseable response)
        503 no LLM provider configured
    """
    # Validate file
    uploaded = request.files.get("file")
    if not uploaded or not uploaded.filename:
        return jsonify({"success": False, "error": "No file uploaded."}), 400
    if not uploaded.filename.lower().endswith(".pdf"):
        return jsonify({"success": False, "error": "Only PDF files are accepted."}), 400

    mode = request.form.get("mode", "fresh")
    if mode not in ("fresh", "merge"):
        mode = "fresh"

    # Extract text
    pdf_bytes = uploaded.read()
    try:
        resume_text = _extract_pdf_text(pdf_bytes)
    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400

    if len(resume_text.strip()) < 50:
        return jsonify({"success": False, "error": "Could not extract meaningful text from this PDF."}), 422

    # Build provider chain
    providers_dict = _load_providers_safe()
    chain = build_provider_chain(providers_dict)
    if not chain:
        return jsonify({"success": False, "error": "No LLM provider is configured. Add one in Settings first."}), 503

    # Build prompt and call LLM
    current_profile = load_profile(_PROFILE_PATH) if mode == "merge" else None
    prompt = _build_import_prompt(resume_text, mode, current_profile)
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
        formatted_skills = []
        for s in parsed.get("primary_skills", []):
            name = s.get("skill", "")
            years = s.get("years", 0)
            status = s.get("status", "active")
            formatted_skills.append(f"{name}, {years}yr, {status}")
        profile_result = {
            "primary_skills": formatted_skills,
            "education": parsed.get("education", []),
            "seniority": parsed.get("seniority", ""),
            "preferred_industries": parsed.get("preferred_industries", []),
            "location_center": parsed.get("location_center"),
        }

    return jsonify({"success": True, "profile": profile_result, "model_used": model_used}), 200


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

            new_profile: dict = {
                "primary_skills": _parse_repeating_rows(request.form, "primary_skills"),
                "anti_preferences": _parse_repeating_rows(request.form, "anti_preferences"),
                "education": _parse_repeating_rows(request.form, "education"),
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

    return render_template(
        "profile.html",
        view="profile",
        prof=prof,
        cfg=cfg,
        saved=saved,
        error=error,
    ), status_code


@app.route("/settings/config")
def settings_config_redirect():
    return redirect(url_for("profile"), code=301)


# ---------------------------------------------------------------------------
# Admin actions
# ---------------------------------------------------------------------------

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
    app.run(debug=debug, port=5000)
