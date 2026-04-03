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
from providers import _PROVIDER_CLASS_MAP
from providers.base import _sanitise_detail
from job_sources import SOURCES

app = Flask(__name__)

DB_PATH: str = os.environ.get("DB_PATH", "jobs.db")


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


CONFIG = load_config()
db.init_db(db_path=DB_PATH)


# ---------------------------------------------------------------------------
# Config validation (mirrors ingest.load_config requirements)
# ---------------------------------------------------------------------------

# These mirror ingest._REQUIRED_SEARCH and ingest._REQUIRED_SCORING so that
# the profile editor rejects a save that would crash the next ingest run.
_PROFILE_REQUIRED_SEARCH = ("country", "what", "results_per_page", "max_pages")
_PROFILE_REQUIRED_SCORING = ("threshold",)


def _validate_config_dict(data: dict) -> list[str]:
    """Return a list of missing required key paths in *data*.

    Validates the same structural requirements that ``ingest.load_config()``
    checks before running the pipeline:

    * ``scoring.threshold`` must be present (no env-var fallback exists).
    * If a ``search`` block is present, all four Adzuna search keys must be
      there too — submitting a partial ``search`` block would break the next
      Adzuna ingest run.

    Top-level Adzuna credential keys (``adzuna_app_id``, ``adzuna_app_key``)
    are intentionally excluded from this check because they can be satisfied
    via ``ADZUNA_APP_ID``/``ADZUNA_APP_KEY`` environment variables and are
    optional when using non-Adzuna sources.

    Returns an empty list when the config is valid.
    """
    missing: list[str] = []

    scoring = data.get("scoring")
    if not isinstance(scoring, dict):
        missing.append("scoring (must be an object)")
    else:
        for key in _PROFILE_REQUIRED_SCORING:
            if key not in scoring:
                missing.append(f"scoring.{key}")
        if "threshold" in scoring and not isinstance(scoring["threshold"], (int, float)):
            missing.append("scoring.threshold (must be a number)")

    # Only validate search sub-keys when the caller has included a search block.
    search = data.get("search")
    if isinstance(search, dict):
        for key in _PROFILE_REQUIRED_SEARCH:
            if key not in search:
                missing.append(f"search.{key}")

    return missing


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

    Delegates to db.toggle_bookmarked(), which performs the flip atomically
    in a single SQL statement so rapid double-clicks cannot produce a net
    no-op. Returns the re-rendered action button group as an HTMX partial.
    """
    listing = db.toggle_bookmarked(listing_id, db_path=DB_PATH)
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
    listing = db.toggle_applied(listing_id, db_path=DB_PATH)
    if listing is None:
        return make_response("", 404)
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
    job_types = db.get_job_types(db_path=DB_PATH)
    listings = db.get_snippet_feed(
        threshold=threshold,
        min_score=min_score,
        remote_only=remote_only,
        search=search,
        job_type=job_type,
        sort=sort,
        db_path=DB_PATH,
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
    db.mark_opened(listing_id, db_path=DB_PATH)
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
        for source_key, cls in SOURCES.items():
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
    for key, cls in SOURCES.items():
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

    listing_count = db.get_listing_count(db_path=DB_PATH)

    return render_template(
        "settings.html",
        view="settings",
        llm_schemas=llm_schemas,
        source_schemas=source_schemas,
        active_tab=active_tab,
        saved=saved,
        error=error,
        listing_count=listing_count,
    )


@app.route("/profile", methods=["GET", "POST"])
def profile():
    """Profile page — view and edit config.json via the browser.

    GET:  Reads config.json, masks any sensitive key fields (``*_api_key``,
          ``*_app_key``, ``*_app_id``), pretty-prints the result as JSON, and
          renders it in a ``<textarea>`` for editing.

    POST: Validates the submitted JSON. If parsing fails or required keys are
          missing, returns a 400/422 with an inline error and leaves config.json
          untouched. If the JSON is valid, overwrites config.json and shows a
          success notice. Masked values (``"***"``) are never written back to
          disk — if the submitted JSON still contains ``"***"`` for a sensitive
          field, the original value from the current config is preserved.
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
            # Validate required keys before touching disk.
            missing_keys = _validate_config_dict(submitted)
            if missing_keys:
                error = "Missing required config key(s): " + ", ".join(missing_keys)
                status_code = 422
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
        conn = db.get_connection(DB_PATH)
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
    if source_key not in SOURCES:
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
        cls = SOURCES[source_key]
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
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    app.run(debug=debug, port=5000)
