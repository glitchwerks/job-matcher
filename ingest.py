"""
ingest.py — Ingestion pipeline for Job Matcher.

Orchestrates the full fetch → pre-filter → scrape → score → persist pipeline.
Run directly:  python ingest.py

Pipeline stages
---------------
1. Load config.json and profile.json
2. Initialise the database schema via db.init_db()
3. Page through Adzuna results via AdzunaClient
4. For each listing:
   a. Pre-filter on title keywords, salary floor, and contract type/time
   b. Dedup check against the database
   c. Scrape the full job description from redirect_url (fallback to snippet)
   d. Score against the candidate profile via Claude
   e. Persist to jobs.db
5. Print a run-summary line
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
import requests
from bs4 import BeautifulSoup
from geopy.distance import geodesic
from geopy.geocoders import Nominatim

import db
from job_sources import make_enabled_sources
from providers import build_provider_chain, LLMProvider
from credentials import CredentialError, load_providers

_CONFIG_DIR = os.path.join(os.path.dirname(__file__), "config")
_DEFAULT_CONFIG_PATH = os.path.join(_CONFIG_DIR, "config.json")
_DEFAULT_PROFILE_PATH = os.path.join(_CONFIG_DIR, "profile.json")
_DEFAULT_KEYS_PATH = os.path.join(_CONFIG_DIR, "keys.json")
_DEFAULT_PROVIDERS_PATH = os.path.join(_CONFIG_DIR, "providers.json")

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("ingest")

# Set to True by --verbose / -v CLI flag. Used at scoring callsites to emit
# the full breakdown (verdict, matched/missing skills, concerns) at INFO level.
_verbose = False

# Set by _configure_file_logging() so that run() can record the log filename
# in the ingest_runs table without needing a return value from that function.
_current_log_file: str | None = None


def _detect_trigger_source() -> str:
    """Determine how this ingest run was triggered.

    Reads the ``INGEST_TRIGGER`` environment variable.  Recognised values are
    ``'scheduled'`` (set by Ofelia) and ``'ui'``/``'manual_ui'`` (set by the
    Flask UI subprocess call).  Anything else is treated as a manual CLI run.

    Returns:
        One of ``'scheduled'``, ``'manual_ui'``, or ``'manual_cli'``.
    """
    trigger = os.environ.get("INGEST_TRIGGER", "").lower()
    if trigger == "scheduled":
        return "scheduled"
    if trigger in ("ui", "manual_ui"):
        return "manual_ui"
    return "manual_cli"


def _configure_file_logging() -> None:
    """Attach a FileHandler writing to a timestamped per-run log file.

    Files are named  ingest_YYYYMMDD_HHMMSS.log  and stored in the same
    logs/ directory as before.  Old files are pruned to keep at most
    MAX_LOG_FILES runs (default 30).

    Called only from the __main__ entry point so that importing this module
    during tests does not create directories or open file handles.
    """
    MAX_LOG_FILES = 30

    from paths import get_log_dir
    log_dir = str(get_log_dir())
    try:
        os.makedirs(log_dir, exist_ok=True)
    except (PermissionError, OSError) as exc:
        logging.warning(
            "File logging unavailable — cannot create log directory %s (%s). "
            "Continuing with stdout-only logging.",
            log_dir,
            exc,
        )
        return

    ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = os.path.join(log_dir, f"ingest_{ts}.log")
    global _current_log_file
    _current_log_file = log_file

    try:
        handler = logging.FileHandler(log_file, encoding="utf-8")
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )
        logging.getLogger().addHandler(handler)
        logger.info("Logging to file: %s", log_file)
    except (PermissionError, OSError) as exc:
        logging.warning(
            "File logging unavailable — cannot open %s (%s). Continuing with stdout-only logging.",
            log_file,
            exc,
        )
        return

    # Prune oldest files beyond the retention limit.
    existing = sorted(
        (f for f in os.scandir(log_dir) if f.name.startswith("ingest_") and f.name.endswith(".log")),
        key=lambda e: e.name,
    )
    for old in existing[:-MAX_LOG_FILES]:
        try:
            os.remove(old.path)
        except OSError:
            pass

# ---------------------------------------------------------------------------
# Config and profile loading
# ---------------------------------------------------------------------------

_REQUIRED_TOP_LEVEL: tuple[str, ...] = ()  # No top-level required keys remain; source credentials are in providers.json
_REQUIRED_SEARCH = ("country", "what", "results_per_page", "max_pages")
_REQUIRED_SCORING = ("threshold",)


def load_config(path: str = _DEFAULT_CONFIG_PATH) -> dict:
    """Load and validate config/config.json.

    Raises SystemExit with a descriptive message if the file cannot be read
    or any required key is missing.

    LLM provider API keys are no longer validated here — they are loaded from
    ``config/providers.json`` via :func:`credentials.load_providers`.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            config = json.load(fh)
    except FileNotFoundError:
        raise SystemExit(f"Config file not found: {path}")
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Config file is not valid JSON: {exc}")

    missing: list[str] = []

    for key in _REQUIRED_TOP_LEVEL:
        if key not in config or not config[key]:
            missing.append(key)

    search = config.get("search", {})
    for key in _REQUIRED_SEARCH:
        if key not in search:
            missing.append(f"search.{key}")

    scoring = config.get("scoring", {})
    for key in _REQUIRED_SCORING:
        if key not in scoring:
            missing.append(f"scoring.{key}")

    if missing:
        raise SystemExit(
            "Missing or empty required config keys: " + ", ".join(missing)
        )

    return config



def load_profile(path: str = _DEFAULT_PROFILE_PATH) -> dict:
    """Load config/profile.json and return the parsed dict.

    Raises SystemExit if the file cannot be read or is not valid JSON.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        raise SystemExit(f"Profile file not found: {path}")
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Profile file is not valid JSON: {exc}")


# ---------------------------------------------------------------------------
# Adzuna API client
# ---------------------------------------------------------------------------
# AdzunaClient has moved to job_sources/adzuna.py and now implements the
# JobSource protocol.  It is re-exported here via the import at the top of
# this module so any code that imported AdzunaClient from ingest continues
# to work without modification.


# ---------------------------------------------------------------------------
# Pre-filter
# ---------------------------------------------------------------------------

# Multipliers to convert a salary figure to an annual equivalent before
# comparing against a configured annual floor. Unknown periods are treated
# as pass-through (fail open) rather than dropping potentially good listings.
_PERIOD_MULTIPLIERS: dict[str, float] = {
    "hourly": 2080.0,   # 40 hrs/week × 52 weeks
    "daily": 260.0,     # 5 days/week × 52 weeks
    "annual": 1.0,
    "yearly": 1.0,
}


def prefilter(listing: dict, config: dict) -> str | None:
    """Check whether a listing passes all configured heuristic filters.

    Checks (each skipped if the corresponding config key is absent/empty):

    * **title_include** — title must match at least one pattern.
    * **title_exclude** — title must match zero patterns.
    * **salary floor** — listing's salary_max must be >= configured minimum
      (listings with no salary data at all are allowed through).
    * **require_contract_time** — listing's contract_time must match.
    * **require_contract_type** — listing's contract_type must match.

    Args:
        listing: Normalised listing dict.
        config: Full config dict.

    Returns:
        None if the listing passes all checks (i.e. should be scored).
        A short descriptive string identifying the first failing check.
    """
    pf = config.get("prefilter", {})
    title = listing.get("title", "")
    title_lower = title.lower()

    # Title include — must match at least one pattern.
    include_patterns: list[str] = pf.get("title_include", [])
    if include_patterns:
        if not any(pat.lower() in title_lower for pat in include_patterns):
            return f'title_include: no match for "{title}"'

    # Title exclude — must match none.
    exclude_patterns: list[str] = pf.get("title_exclude", [])
    if exclude_patterns:
        for pat in exclude_patterns:
            if pat.lower() in title_lower:
                return f'title_exclude: "{pat}" matched "{title}"'

    # Salary floor — only checked when listing has a salary_max value.
    # Normalize salary_max to an annual figure before comparing to the annual floor.
    # Unknown/absent period → skip the check (fail open rather than drop good listings).
    salary_max = listing.get("salary_max")
    configured_floor = (
        pf.get("salary_min")
        or config.get("search", {}).get("salary_min")
        or 0
    )
    if configured_floor and salary_max is not None:
        salary_period = (listing.get("salary_period") or "").lower().strip()
        multiplier = _PERIOD_MULTIPLIERS.get(salary_period)
        if multiplier is None:
            # Unknown period — cannot safely compare to an annual floor; let it through.
            pass
        else:
            try:
                annual_max = float(salary_max) * multiplier
                if annual_max < float(configured_floor):
                    return f"salary: max {salary_max} ({salary_period or 'unknown period'}) below floor {configured_floor}"
            except (TypeError, ValueError):
                pass  # If we can't compare, let it through.

    # Contract time.
    require_time: str | None = pf.get("require_contract_time")
    if require_time is not None:
        actual_time = listing.get("contract_time", "")
        if actual_time and actual_time.lower() != require_time.lower():
            return f'contract_time: got "{actual_time}" expected "{require_time}"'

    # Contract type.
    require_type: str | None = pf.get("require_contract_type")
    if require_type is not None:
        actual_type = listing.get("contract_type", "")
        if actual_type and actual_type.lower() != require_type.lower():
            return f'contract_type: got "{actual_type}" expected "{require_type}"'

    return None


# ---------------------------------------------------------------------------
# Geospatial filter
# ---------------------------------------------------------------------------

# Keywords that indicate a listing is location-agnostic — always pass the
# geospatial filter regardless of the configured radius.
_REMOTE_KEYWORDS = ("remote", "worldwide")

# Nominatim's usage policy requires a descriptive user-agent string and a
# maximum request rate of 1 req/sec.  The geocache eliminates repeat calls
# on subsequent runs, so this rate limit only applies to new locations.
_GEOCODE_USER_AGENT = "job_matcher/1.0 (github.com/job-matcher)"
_GEOCODE_MIN_INTERVAL_S = 1.0  # Nominatim policy: 1 req/sec max


def _is_remote_location(location: str) -> bool:
    """Return True if location text indicates a remote/global listing.

    Listings containing "remote" or "worldwide" (case-insensitive) bypass the
    geospatial radius check entirely.

    Args:
        location: Raw location string from a job listing.

    Returns:
        True when the location signals the role is remote or worldwide.
    """
    loc_lower = location.lower()
    return any(kw in loc_lower for kw in _REMOTE_KEYWORDS)


def _geocode_location(location_text: str, geolocator) -> tuple[float, float] | None:
    """Resolve a location string to (lat, lon) via Nominatim.

    The caller is responsible for enforcing rate limits between calls.

    Args:
        location_text: Human-readable location string to geocode.
        geolocator:    Configured ``geopy.geocoders.Nominatim`` instance.

    Returns:
        ``(lat, lon)`` tuple on success, or ``None`` if Nominatim cannot
        resolve the string.
    """
    try:
        result = geolocator.geocode(location_text, timeout=10)
    except Exception as exc:  # noqa: BLE001 — geopy can raise various errors
        logger.debug("Geocoding error for %r: %s", location_text, exc)
        return None

    if result is None:
        return None
    return (result.latitude, result.longitude)


class GeoFilter:
    """Manages geospatial filtering for a single ingest run.

    Encapsulates the per-run Nominatim geolocator instance, the in-memory
    coordinate cache (layered on top of the DB geocache), rate-limit state,
    and run statistics.  Instantiate once per ``run()`` call; call
    :meth:`check` for each listing.

    When ``profile`` does not contain a ``location`` block with both ``center`` and
    ``radius_km``, :meth:`is_active` returns ``False`` and :meth:`check`
    is always a no-op — no geocoding is attempted.

    Attributes:
        hits:           Number of lookups served from the DB geocache.
        misses:         Number of Nominatim API calls made.
        failed:         Number of locations that could not be geocoded.
        geo_discarded:  Number of listings discarded by the radius check.
    """

    def __init__(self, profile: dict) -> None:
        loc = profile.get("location", {})
        self._center_str: str | None = loc.get("center")
        self._radius_km: float | None = loc.get("radius_km")
        self._fallback: str = (loc.get("geocode_fallback") or "pass").lower()

        # In-memory layer over the DB geocache — avoids repeated DB hits for
        # the same location within a single run.
        self._mem: dict[str, tuple[float, float] | None] = {}

        # Rate-limit state for Nominatim (1 req/sec max).
        self._last_geocode_ts: float = 0.0

        # Lazy-initialised geolocator — only created when needed.
        self._geolocator = None

        # Run statistics.
        self.hits = 0
        self.misses = 0
        self.failed = 0
        self.geo_discarded = 0

        # Geocode the center up-front so :meth:`check` doesn't have to
        # handle it per-listing.  Only done when the filter is active.
        self._center_coords: tuple[float, float] | None = None
        if self.is_active and self._center_str:
            self._center_coords = self._resolve(self._center_str)
            if self._center_coords is None:
                logger.warning(
                    "geo_filter: location.center %r could not be geocoded; "
                    "geospatial filter will be skipped for this run",
                    self._center_str,
                )

    @property
    def is_active(self) -> bool:
        """True when both ``location.center`` and ``location.radius_km`` are set."""
        return bool(self._center_str) and self._radius_km is not None

    def _geolocator_instance(self):
        """Return (or lazily create) the Nominatim geolocator."""
        if self._geolocator is None:
            self._geolocator = Nominatim(user_agent=_GEOCODE_USER_AGENT)
        return self._geolocator

    def _resolve(self, location_text: str) -> tuple[float, float] | None:
        """Return (lat, lon) for a location string, populating caches as a side effect.

        Lookup order:
        1. In-memory cache (free).
        2. DB geocache (single row SELECT).
        3. Nominatim API call (rate-limited to 1 req/sec).

        Args:
            location_text: The raw location string to resolve.

        Returns:
            ``(lat, lon)`` on success, or ``None`` if unresolvable.
        """
        # 1. In-memory hit.
        if location_text in self._mem:
            return self._mem[location_text]

        # 2. DB cache hit.
        with db.get_connection() as conn:
            db_hits = db.geocache_get_many(conn, [location_text])
        if location_text in db_hits:
            self.hits += 1
            coords = db_hits[location_text]
            self._mem[location_text] = coords
            return coords

        # 3. Nominatim API call.
        geolocator = self._geolocator_instance()

        # Enforce 1 req/sec.
        elapsed = time.monotonic() - self._last_geocode_ts
        if elapsed < _GEOCODE_MIN_INTERVAL_S:
            time.sleep(_GEOCODE_MIN_INTERVAL_S - elapsed)

        coords = _geocode_location(location_text, geolocator)
        self._last_geocode_ts = time.monotonic()
        self.misses += 1

        if coords is not None:
            with db.get_connection() as conn:
                db.geocache_put(conn, location_text, coords[0], coords[1])
            self._mem[location_text] = coords
        else:
            self.failed += 1
            logger.debug("Could not geocode location: %r", location_text)
            self._mem[location_text] = None

        return coords

    def check(self, listing: dict) -> str | None:
        """Return None if the listing passes the geospatial filter, or a reason string.

        This method is a no-op (always returns None) when:
        - The filter is not active (``location.center`` / ``location.radius_km`` absent).
        - The center could not be geocoded.
        - The listing location contains "remote" or "worldwide".

        For listings whose location cannot be geocoded, the
        ``location.geocode_fallback`` profile field controls the outcome:
        - ``"pass"`` (default) → return None.
        - ``"discard"`` → return a rejection reason string.

        Args:
            listing: Normalised listing dict.

        Returns:
            None to pass, or a descriptive string to reject.
        """
        if not self.is_active or self._center_coords is None:
            return None

        location = (listing.get("location") or "").strip()

        # Remote/worldwide listings always pass.
        if not location or _is_remote_location(location):
            return None

        listing_coords = self._resolve(location)

        if listing_coords is None:
            if self._fallback == "discard":
                self.geo_discarded += 1
                return f'geo_filter: location "{location}" could not be geocoded (fallback=discard)'
            return None

        distance_km = geodesic(self._center_coords, listing_coords).km

        if distance_km > self._radius_km:
            self.geo_discarded += 1
            return (
                f'geo_filter: "{location}" is {distance_km:.0f} km from '
                f'"{self._center_str}" (radius {self._radius_km} km)'
            )

        return None


# Backwards-compatible module-level helper for unit tests.
def geo_filter(
    listing: dict,
    profile: dict,
    geocache: dict[str, tuple[float, float] | None],
) -> str | None:
    """Thin wrapper around :class:`GeoFilter` for use in unit tests.

    Accepts a pre-built geocache dict rather than the DB, so tests can inject
    coordinates without touching the database.

    Args:
        listing:   Normalised listing dict.
        profile:   Candidate profile dict.
        geocache:  Mapping of location_text → (lat, lon) or None.

    Returns:
        None if the listing passes, or a rejection reason string.
    """
    loc = profile.get("location", {})
    center_str: str | None = loc.get("center")
    radius_km: float | None = loc.get("radius_km")

    if not center_str or radius_km is None:
        return None

    location = (listing.get("location") or "").strip()
    if not location or _is_remote_location(location):
        return None

    listing_coords = geocache.get(location)
    if listing_coords is None:
        fallback = (loc.get("geocode_fallback") or "pass").lower()
        if fallback == "discard":
            return f'geo_filter: location "{location}" could not be geocoded (fallback=discard)'
        return None

    center_coords = geocache.get(center_str)
    if center_coords is None:
        return None  # Can't filter without center coords — skip silently.

    distance_km = geodesic(center_coords, listing_coords).km
    if distance_km > radius_km:
        return (
            f'geo_filter: "{location}" is {distance_km:.0f} km from '
            f'"{center_str}" (radius {radius_km} km)'
        )
    return None


# ---------------------------------------------------------------------------
# Scraper
# ---------------------------------------------------------------------------

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
_SCRAPE_TIMEOUT = 10
_SCRAPE_MIN_LENGTH = 100
_NOISE_TAGS = ["script", "style", "nav", "header", "footer"]


def scrape_description(url: str, fallback: str = "") -> tuple[str, bool]:
    """GET a job listing page and extract its visible text.

    Removes noise tags (script, style, nav, header, footer) and collapses
    whitespace before returning. Falls back to ``fallback`` if the request
    fails, the status code is not 200, or the extracted text is under
    100 characters.

    Args:
        url: The redirect URL to scrape.
        fallback: Text to return if scraping fails (typically the API snippet).

    Returns:
        ``(description_text, scraped_ok)`` where ``scraped_ok`` is True on
        success and False when the fallback was used.
    """
    # NOTE: No inter-request delay or robots.txt check. This is acceptable for
    # personal use at low volume (~50-250 listings/run), but would need rate
    # limiting and robots.txt compliance for any higher-volume or production use.
    try:
        response = requests.get(
            url,
            headers={"User-Agent": _USER_AGENT},
            timeout=_SCRAPE_TIMEOUT,
        )
    except requests.RequestException as exc:
        logger.warning("Scrape request failed for %s: %s", url, exc)
        return fallback, False

    if response.status_code != 200:
        logger.warning(
            "Scrape returned HTTP %d for %s", response.status_code, url
        )
        return fallback, False

    soup = BeautifulSoup(response.text, "html.parser")

    for tag in _NOISE_TAGS:
        for element in soup.find_all(tag):
            element.decompose()

    raw_text = soup.get_text(separator=" ", strip=True)
    # Collapse runs of whitespace to single spaces.
    cleaned = re.sub(r"\s+", " ", raw_text).strip()

    if len(cleaned) < _SCRAPE_MIN_LENGTH:
        logger.warning(
            "Scraped text too short (%d chars) for %s; using fallback",
            len(cleaned),
            url,
        )
        return fallback, False

    return cleaned, True


# ---------------------------------------------------------------------------
# Location notes helper
# ---------------------------------------------------------------------------


def _generate_location_notes(center: str | None, radius_km: float | None) -> str | None:
    """Auto-generate a human-readable location hint for the LLM scoring prompt.

    Used when ``profile["location"]["notes"]`` is absent.  Returns ``None``
    when neither *center* nor *radius_km* is set so callers can omit the
    field entirely.

    Args:
        center:    Geocodable center string (e.g. ``"Miami, FL"``), or None.
        radius_km: Filter radius in km, or None.

    Returns:
        A short description string, or ``None`` if no location config is set.
    """
    if center and radius_km is not None:
        return f"Within {radius_km:.0f} km of {center}"
    if center:
        return f"Near {center}"
    return None


# ---------------------------------------------------------------------------
# Scorer
# ---------------------------------------------------------------------------

_SCORE_KEYS = {"score", "matched_skills", "missing_skills", "concerns", "verdict"}

_PROMPT_TEMPLATE = """\
You are evaluating a job listing for a candidate. Score how well the role matches their profile.

CANDIDATE PROFILE:
{profile_json}

JOB DESCRIPTION:
{description}

Respond with ONLY a JSON object. No explanation, no markdown, no code fences. The object must have exactly these keys:
- "score": integer from 0 to 10 (10 = perfect match)
- "matched_skills": array of strings (skills from the profile that this role uses)
- "missing_skills": array of strings (skills this role requires that the candidate lacks or has little experience in)
- "concerns": array of strings (red flags or mismatches, e.g. seniority mismatch, wrong industry, anti-preferences violated)
- "verdict": one sentence summarising the match

JSON only:\
"""


def score_listing(
    description: str,
    profile: dict,
    provider: LLMProvider,
) -> dict | None:
    """Score a job description against a candidate profile using an LLM provider.

    Builds a structured prompt and delegates to ``provider.complete()``.
    The provider is responsible for retries, JSON parsing, and token counting.
    Returns ``None`` if the provider raises ``RuntimeError`` (both attempts
    failed), so the listing can be stored unscored for later re-scoring.

    Args:
        description: Full (or snippet) job description text.
        profile:     Candidate profile dict loaded from profile.json.
        provider:    An initialised ``LLMProvider`` instance.

    Returns:
        Dict with keys ``score``, ``matched_skills``, ``missing_skills``,
        ``concerns``, ``verdict``, ``tokens_input``, ``tokens_output``;
        or ``None`` on persistent failure.
    """
    prompt = _PROMPT_TEMPLATE.format(
        profile_json=json.dumps(profile, indent=2),
        description=description,
    )

    try:
        return provider.complete(prompt)
    except RuntimeError:
        logger.warning("Scoring failed after 2 attempts; listing will be stored unscored")
        return None


# ---------------------------------------------------------------------------
# Provider-chain fallback scorer
# ---------------------------------------------------------------------------

_AUTH_MARKERS = ("401", "403", "unauthorized", "authentication")


def _provider_name(provider: LLMProvider) -> str:
    """Derive a short provider name from a provider instance's class name.

    Strips the ``"provider"`` suffix (case-insensitive) so that
    ``AnthropicProvider`` → ``"anthropic"``.

    Args:
        provider: Any ``LLMProvider`` instance.

    Returns:
        Lowercase name string, e.g. ``"anthropic"``, ``"openai"``, ``"gemini"``.
    """
    return type(provider).__name__.lower().replace("provider", "")


def _provider_model(provider: LLMProvider) -> str:
    """Return the model string stored on a provider instance.

    Concrete providers store the model as ``_model`` or ``_model_name``
    (private attributes); this helper tries both and falls back to
    ``"unknown"`` so callers never receive an AttributeError.

    Args:
        provider: Any ``LLMProvider`` instance.

    Returns:
        Model identifier string.
    """
    return (
        getattr(provider, "_model", None)
        or getattr(provider, "_model_name", None)
        or "unknown"
    )


def _is_auth_error(exc: RuntimeError) -> bool:
    """Return True if the error message indicates an auth failure (401/403).

    Auth errors mean the API key is bad for the entire run; transient errors
    (rate limits, 5xx, network blips) only skip the current listing.

    Args:
        exc: The ``RuntimeError`` raised by ``provider.complete()``.

    Returns:
        True when the message contains an auth/credential-related marker.
    """
    msg = str(exc).lower()
    return any(marker in msg for marker in _AUTH_MARKERS)


def format_skills_for_prompt(profile: dict) -> dict:
    """Convert structured skill objects to LLM-optimised strings for the scoring prompt.

    If ``primary_skills`` is already a list of strings (old flat format), it is
    passed through unchanged for backward compatibility.

    Args:
        profile: Candidate profile dict.  A shallow copy is returned — the
                 original is never mutated.

    Returns:
        A new dict identical to *profile* except that ``primary_skills`` is
        replaced with a list of human-readable strings when the input contains
        structured skill objects.
    """
    profile = dict(profile)  # shallow copy — do not mutate caller's dict
    skills = profile.get("primary_skills", [])
    if skills and all(isinstance(s, dict) for s in skills):
        formatted = []
        for s in skills:
            status = "active" if s.get("active", True) else "dormant"
            years = s.get("years_active", 0)
            year_label = "year" if years == 1 else "years"
            formatted.append(f"{s['description']} ({years} {year_label}, {status})")
        profile["primary_skills"] = formatted
    return profile


def format_education_for_prompt(profile: dict) -> dict:
    """Convert structured education objects to LLM-readable strings for the scoring prompt.

    If ``education`` is already a list of strings (old flat format), it is
    passed through unchanged for backward compatibility.

    Args:
        profile: Candidate profile dict.  A shallow copy is returned — the
                 original is never mutated.

    Returns:
        A new dict identical to *profile* except that ``education`` is
        replaced with a list of human-readable strings when the input contains
        structured education objects.
    """
    profile = dict(profile)  # shallow copy — do not mutate caller's dict
    education = profile.get("education", [])
    if education and all(isinstance(e, dict) for e in education):
        formatted = []
        for e in education:
            deg_type = e.get("degree_type", "")
            deg_field = e.get("degree_field", "")
            school = e.get("school", "")
            year = e.get("graduation_year", "")
            base = f"{deg_type} in {deg_field}" if deg_field else deg_type
            if school and year:
                suffix = f" — {school} ({year})"
            elif school:
                suffix = f" — {school}"
            elif year:
                suffix = f" ({year})"
            else:
                suffix = ""
            formatted.append(f"{base}{suffix}")
        profile["education"] = formatted
    return profile


def score_listing_with_fallback(
    listing: dict,
    profile: dict,
    chain: list,
    dead_providers: set,
) -> dict | None:
    """Score a listing, falling back through the provider chain on failure.

    Builds the scoring prompt once and then iterates the ordered provider
    chain, calling ``provider.complete()`` directly so that ``RuntimeError``
    exceptions propagate here for auth-vs-transient classification.
    (``score_listing()`` wraps ``provider.complete()`` but swallows
    ``RuntimeError``; going directly gives us the error detail we need.)

    Failure modes:

    * **Auth error (401/403)**: permanently adds the provider to
      ``dead_providers`` for the rest of the run — the key is bad.
    * **Transient error (rate-limit, 5xx, etc.)**: logs a warning and moves
      to the next provider, keeping this one available for future listings.
    * **Success**: injects ``"model_used"`` as ``"{provider_name}/{model}"``
      into the result dict and returns it.

    Args:
        listing:        Normalised listing dict.  Uses ``listing["description"]``
                        for the prompt.
        profile:        Candidate profile dict loaded from ``profile.json``.
        chain:          Ordered list of ``LLMProvider`` instances.
        dead_providers: Set of provider name strings (mutated in-place).
                        Providers in this set are skipped entirely.

    Returns:
        Scoring result dict with an added ``"model_used"`` key, or ``None``
        if every provider in the chain failed.
    """
    # Build the profile dict that is serialised into the LLM prompt.
    # The nested ``location`` block is replaced with a flat ``location_notes``
    # string so the model receives a clean, readable hint.  Internal fields
    # (center, radius_km, geocode_fallback) that are irrelevant to scoring are
    # stripped to avoid confusing the model.
    loc = profile.get("location", {})
    location_notes = loc.get("notes") or _generate_location_notes(
        loc.get("center"), loc.get("radius_km")
    )
    scoring_profile = {k: v for k, v in profile.items() if k != "location"}
    if location_notes:
        scoring_profile["location_notes"] = location_notes

    # Convert structured skill objects to LLM-readable strings before serialising.
    scoring_profile = format_skills_for_prompt(scoring_profile)
    # Convert structured education objects to LLM-readable strings before serialising.
    scoring_profile = format_education_for_prompt(scoring_profile)

    prompt = _PROMPT_TEMPLATE.format(
        profile_json=json.dumps(scoring_profile, indent=2),
        description=listing["description"],
    )

    for provider in chain:
        name = _provider_name(provider)
        if name in dead_providers:
            logger.debug("Skipping dead provider: %s", name)
            continue

        try:
            result = provider.complete(prompt)
            result["model_used"] = f"{name}/{_provider_model(provider)}"
            return result

        except RuntimeError as exc:
            if _is_auth_error(exc):
                dead_providers.add(name)
                remaining = [_provider_name(p) for p in chain if _provider_name(p) not in dead_providers]
                logger.warning(
                    "Provider %s disabled for this run (auth error: %s). Remaining: %s",
                    name, exc, remaining or ["none"],
                )
            else:
                logger.warning(
                    "Transient error from provider %s — trying next provider: %s",
                    name, exc,
                )

    logger.warning(
        "All providers in chain failed for listing: %s",
        listing.get("title", "(no title)"),
    )
    return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _inject_env_var_credentials(providers: dict) -> None:
    """Inject ``ADZUNA_APP_ID`` / ``ADZUNA_APP_KEY`` env vars into *providers*.

    When set, each env var is written into
    ``providers["job_sources"]["adzuna"]["app_id"]`` /
    ``providers["job_sources"]["adzuna"]["app_key"]`` using ``setdefault`` so
    that an existing value in providers.json is never overwritten.

    This allows containerised deployments to supply Adzuna credentials via the
    environment without modifying providers.json, while still respecting any
    value already present in the file.

    Args:
        providers: The providers dict loaded by ``load_providers()``.  Modified
                   in-place; missing intermediate keys are created as needed.
    """
    adzuna_env_id  = os.environ.get("ADZUNA_APP_ID",  "")
    adzuna_env_key = os.environ.get("ADZUNA_APP_KEY", "")
    if adzuna_env_id or adzuna_env_key:
        adzuna_src = providers.setdefault("job_sources", {}).setdefault("adzuna", {})
        if adzuna_env_id:
            adzuna_src.setdefault("app_id", adzuna_env_id)
        if adzuna_env_key:
            adzuna_src.setdefault("app_key", adzuna_env_key)


# ---------------------------------------------------------------------------
# Plugin isolation helper
# ---------------------------------------------------------------------------

def _safe_pages(client):
    """Wrap ``client.pages()`` so an unhandled exception from a plugin aborts
    only that source, not the entire ingest run.

    Any ``Exception`` that escapes the plugin's ``pages()`` generator is caught
    here, logged at ERROR level with a full traceback, and then the generator
    simply returns — leaving the outer ``for client in sources:`` loop free to
    continue with the next source.

    ``GeneratorExit`` is explicitly re-raised (PEP 479) so Python's generator
    close protocol works correctly when the consumer stops iterating early.
    ``SystemExit`` is caught by its own branch so the ingest run survives a
    plugin that calls ``sys.exit()``.  ``KeyboardInterrupt`` is deliberately NOT
    caught — Ctrl-C must still exit the process.

    Args:
        client: A :class:`JobSource` instance whose ``pages()`` iterator is
                to be consumed safely.

    Yields:
        Lists of normalised listing dicts, exactly as ``client.pages()`` would.
    """
    _log = logging.getLogger("ingest")
    try:
        yield from client.pages()
    except SystemExit as exc:  # noqa: BLE001
        # SystemExit is a BaseException, not an Exception — it bypasses a plain
        # ``except Exception:`` clause and would otherwise silently kill the
        # entire process with no traceback.  Catch it here, log the exit code,
        # and let the outer sources loop continue to the next plugin.
        _log.error(
            "Plugin %r called sys.exit(%r) — aborting this source only. "
            "Full traceback follows.",
            client.SOURCE,
            exc.code,
            exc_info=True,
        )
    except GeneratorExit:
        # PEP 479: generators must re-raise GeneratorExit, not swallow it.
        # This exception is only raised when the *consumer* closes the generator
        # early (e.g. a ``break`` in the outer loop) — it is NOT a plugin error.
        raise
    except Exception:  # noqa: BLE001
        _log.error(
            "Plugin %r raised an unhandled exception — skipping source. "
            "Full traceback follows.",
            client.SOURCE,
            exc_info=True,
        )


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def run(
    config_path: str = _DEFAULT_CONFIG_PATH,
    profile_path: str = _DEFAULT_PROFILE_PATH,
    hours: int | None = None,
    keys_path: str = _DEFAULT_KEYS_PATH,
    providers_path: str = _DEFAULT_PROVIDERS_PATH,
) -> None:
    """Run the full ingestion pipeline.

    Loads config and profile, initialises the DB, pages through Adzuna
    results, pre-filters, deduplicates, scrapes, scores, and persists each
    listing.  Prints a summary line when complete.

    After ``load_providers()`` returns, the env vars ``ADZUNA_APP_ID`` and
    ``ADZUNA_APP_KEY`` are injected into the providers dict via
    ``_inject_env_var_credentials()`` so that containerised deployments can
    supply Adzuna credentials without modifying ``providers.json``.  Existing
    values already present in the file are not overwritten (``setdefault`` is
    used, not direct assignment).

    Args:
        config_path:    Path to config.json (default ``"config/config.json"``).
        profile_path:   Path to profile.json (default ``"config/profile.json"``).
        hours:          If provided, only process listings whose ``created_at``
                        timestamp is within the last N hours. Overrides
                        ``search.max_days_old`` in config with ``ceil(hours/24)``.
        keys_path:      Path to legacy ``keys.json`` passed to
                        :func:`credentials.load_providers` for one-time migration;
                        not used when ``providers.json`` is already present.
                        Default: ``"config/keys.json"``.
        providers_path: Path to ``providers.json`` — the unified credential store
                        for LLM providers and job source credentials.
                        Default: ``"config/providers.json"``.
                        Override in tests to inject a temp file.
    """
    # ---------------------------------------------------------------------- #
    # Pre-pipeline setup — errors here are caught and logged before the main #
    # try block so they appear in the log file, not silently on stderr only. #
    # ---------------------------------------------------------------------- #
    try:
        config = load_config(config_path)
        profile = load_profile(profile_path)
    except SystemExit as exc:
        # load_config/load_profile raise SystemExit, which bypasses a plain
        # except Exception.  Extract the message and log it so the error
        # appears in the log file, then re-raise to preserve the exit code.
        logger.error("Startup error: %s", exc)
        raise

    job_type = config["search"].get("what", "").strip()

    if hours is not None:
        config["search"]["max_days_old"] = math.ceil(hours / 24)

    try:
        db.init_db()
    except Exception as exc:  # noqa: BLE001
        logger.error("Database initialisation failed: %s", exc)
        sys.exit(1)

    # Record this run in the ingest_runs table so the Admin UI can show history.
    # If this fails (e.g. ingest_runs table missing), run_id is set to None and
    # the run continues without admin-UI tracking.
    run_id: int | None = None
    _log_name = Path(_current_log_file).name if _current_log_file else None
    try:
        run_id = db.create_ingest_run(
            trigger_source=_detect_trigger_source(),
            log_filename=_log_name,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Could not create ingest_runs record (admin UI will not show this run): %s", exc
        )

    # _pipeline_error stores any unhandled exception so the finally block can
    # record it in ingest_runs without swallowing the exception.
    _pipeline_error: BaseException | None = None

    try:
        # ------------------------------------------------------------------ #
        # Pipeline body (geo filter → load providers → source loop → summary) #
        # ------------------------------------------------------------------ #

        # Initialise the geospatial filter.  Geocodes location.center up-front if
        # location.center and location.radius_km are both set in the profile.
        geo = GeoFilter(profile=profile)
        if geo.is_active:
            _loc = profile.get("location", {})
            logger.info(
                "  Geo filter: center=%r radius=%s km fallback=%s",
                _loc.get("center"),
                _loc.get("radius_km"),
                _loc.get("geocode_fallback", "pass"),
            )

        try:
            providers = load_providers(
                providers_path=providers_path,
                keys_path=keys_path,
                config_path=config_path,
            )
        except CredentialError as exc:
            logger.warning(
                "No credentials found — skipping ingest run. %s", exc
            )
            if run_id is not None:
                db.finish_ingest_run(run_id, status="failed", error_message=str(exc))
            return

        _inject_env_var_credentials(providers)

        from job_sources.auto_register import ensure_plugins_registered
        ensure_plugins_registered(providers_path)

        sources = make_enabled_sources(providers, config)
        if not sources:
            logger.warning(
                "No job sources are enabled. Enable at least one source in Settings > Sources."
            )
            logger.info("Run complete: 0 fetched | no sources enabled")
            if run_id is not None:
                db.finish_ingest_run(run_id, status="success", counts={"fetched": 0})
            return

        chain = build_provider_chain(providers)
        dead_providers: set[str] = set()

        # --- Run start banner ---
        logger.info("=" * 60)
        logger.info("INGEST RUN STARTED")
        logger.info(
            "  Search: '%s' | max_pages: %d | max_days_old: %d",
            config["search"].get("what", ""),
            config["search"].get("max_pages", 0),
            config["search"].get("max_days_old", 0),
        )
        pf = config.get("prefilter", {})
        if pf:
            logger.info(
                "  Prefilter: title_include=%s | salary_floor=%s",
                pf.get("title_include", []),
                pf.get("salary_min") or config.get("search", {}).get("salary_min"),
            )
        logger.info(
            "  Sources: %s",
            ", ".join(c.SOURCE for c in sources),
        )
        if chain:
            logger.info(
                "  LLM providers: %s",
                " | ".join(f"{_provider_name(p)}/{_provider_model(p)}" for p in chain),
            )
        else:
            logger.warning("  No LLM providers configured — scoring will fail for all listings")
        logger.info("=" * 60)

        # Counters.
        fetched = 0
        prefiltered = 0
        deduped = 0
        scraped_ok = 0
        scraped_fallback = 0
        scraped_skipped = 0
        scored = 0
        score_failed = 0
        listing_failed = 0
        total_tokens_input = 0
        total_tokens_output = 0
        # Per-provider cost tracking: {provider_name: {input, output, cost}}
        provider_costs: dict[str, dict] = {}
        source_fetch_counts: dict[str, int] = {}

        # Cutoff used for --hours filtering.
        hours_cutoff: datetime | None = None
        if hours is not None:
            from datetime import timedelta
            hours_cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

        for client in sources:
            logger.info("Fetching from source: %s", client.SOURCE)
            for page in _safe_pages(client):
                # Emit fetched event before per-listing loop to maintain ordering invariant
                # for SSE clients (prevents filtered > fetched in drawer). See issue #200.
                logger.info("Fetched %d listing(s) from %s", len(page), client.SOURCE)
                for listing in page:
                    fetched += 1
                    src_name = listing.get("source", client.SOURCE)
                    source_fetch_counts[src_name] = source_fetch_counts.get(src_name, 0) + 1
                    title = listing.get("title", "(no title)")
                    try:
                        # --- Hours filter (created_at) ---
                        if hours_cutoff is not None:
                            created_raw = listing.get("created_at", "")
                            if created_raw:
                                try:
                                    created_dt = datetime.fromisoformat(
                                        created_raw.replace("Z", "+00:00")
                                    )
                                    if created_dt < hours_cutoff:
                                        prefiltered += 1
                                        logger.info(
                                            "FILTERED  [%s] %s — created_at older than %d hours",
                                            src_name, title, hours,
                                        )
                                        continue
                                except (ValueError, TypeError):
                                    pass  # Unparseable date — let the listing through.

                        # --- Pre-filter ---
                        reason = prefilter(listing, config)
                        if reason is not None:
                            prefiltered += 1
                            logger.info("FILTERED  [%s] %s — %s", src_name, title, reason)
                            continue

                        # --- Geospatial filter ---
                        geo_reason = geo.check(listing)
                        if geo_reason is not None:
                            prefiltered += 1
                            logger.info("FILTERED  [%s] %s — %s", src_name, title, geo_reason)
                            continue

                        # --- Dedup ---
                        # Open one connection and reuse it for both dedup checks to avoid
                        # two open/close round-trips per listing.
                        with db.get_connection() as _dedup_conn:
                            _is_dupe = db.listing_exists(
                                _dedup_conn, listing["source"], listing["source_id"]
                            )
                            if not _is_dupe:
                                redirect_url = listing.get("redirect_url", "")
                                if redirect_url:
                                    _is_dupe = db.listing_exists_by_url(_dedup_conn, redirect_url)
                        if _is_dupe:
                            deduped += 1
                            logger.info("DUPE      [%s] %s", src_name, title)
                            continue

                        # --- Scrape ---
                        # Sources that set skip_scrape=True provide the description via
                        # API.  If the source also sets description_is_full=True AND the
                        # description is long enough (>= _SCRAPE_MIN_LENGTH), classify
                        # as "full"; otherwise fall back to "snippet".
                        if listing.get("skip_scrape"):
                            scraped_skipped += 1
                            if (listing.get("description_is_full")
                                    and len(listing.get("description", "")) >= _SCRAPE_MIN_LENGTH):
                                listing["description_source"] = "full"
                                logger.info("SCRAPE SKIP (full) [%s] %s", src_name, title)
                            else:
                                listing["description_source"] = "snippet"
                                logger.info("SCRAPE SKIP (snippet) [%s] %s", src_name, title)
                        else:
                            description, ok = scrape_description(
                                listing["redirect_url"],
                                fallback=listing["description"],
                            )
                            if ok:
                                scraped_ok += 1
                                listing["description_source"] = "full"
                            else:
                                scraped_fallback += 1
                                listing["description_source"] = "snippet"
                                logger.info("SCRAPE FALLBACK  [%s] %s", src_name, title)
                            listing["description"] = description

                        # --- Score ---
                        score_result = score_listing_with_fallback(
                            listing=listing,
                            profile=profile,
                            chain=chain,
                            dead_providers=dead_providers,
                        )

                        if score_result is None:
                            score_failed += 1
                            logger.warning("SCORE FAILED  [%s] %s", src_name, title)
                            listing.update(
                                {
                                    "score": None,
                                    "matched_skills": [],
                                    "missing_skills": [],
                                    "concerns": [],
                                    "verdict": None,
                                    "seen": 0,
                                }
                            )
                        else:
                            scored += 1
                            logger.info(
                                "SCORED %d/10  [%s] %s",
                                score_result.get("score", 0),
                                src_name,
                                title,
                            )
                            if _verbose:
                                logger.info(
                                    "  verdict: %s\n  matched: %s\n  missing: %s\n  concerns: %s",
                                    score_result.get("verdict", ""),
                                    ", ".join(score_result.get("matched_skills") or []) or "none",
                                    ", ".join(score_result.get("missing_skills") or []) or "none",
                                    ", ".join(score_result.get("concerns") or []) or "none",
                                )
                            else:
                                logger.debug(
                                    "  verdict: %s | matched: %s | missing: %s",
                                    score_result.get("verdict", ""),
                                    ", ".join(score_result.get("matched_skills") or []) or "none",
                                    ", ".join(score_result.get("missing_skills") or []) or "none",
                                )
                            tok_in = score_result.get("tokens_input") or 0
                            tok_out = score_result.get("tokens_output") or 0
                            total_tokens_input += tok_in
                            total_tokens_output += tok_out

                            # Accumulate per-provider cost for the run summary.
                            used_provider = score_result.get("model_used", "").split("/")[0] or "unknown"
                            if used_provider not in provider_costs:
                                # Retrieve the matching provider to get its pricing rates.
                                matched = next(
                                    (p for p in chain if _provider_name(p) == used_provider),
                                    None,
                                )
                                provider_costs[used_provider] = {
                                    "input": 0,
                                    "output": 0,
                                    "cost": 0.0,
                                    "_in_rate": matched.input_cost_per_mtok if matched else 0.0,
                                    "_out_rate": matched.output_cost_per_mtok if matched else 0.0,
                                }
                            bucket = provider_costs[used_provider]
                            bucket["input"] += tok_in
                            bucket["output"] += tok_out
                            bucket["cost"] += (
                                tok_in  / 1_000_000 * bucket["_in_rate"]
                                + tok_out / 1_000_000 * bucket["_out_rate"]
                            )

                            listing.update(score_result)
                            listing["seen"] = 1

                        # --- Persist ---
                        listing["fetched_at"] = datetime.now(timezone.utc).isoformat()
                        listing["bookmarked"] = 0
                        listing["dismissed"] = 0
                        listing["job_type"] = job_type or None

                        # Populate posted_at from created_at when the source's normalise()
                        # does not set it directly (non-Adzuna sources).  db.insert_listing()
                        # defaults posted_at to NULL via setdefault — setting it here ensures
                        # date-sort works correctly for all sources.
                        if not listing.get("posted_at"):
                            listing["posted_at"] = listing.get("created_at") or None

                        try:
                            db.insert_listing(listing)
                        except Exception as exc:  # noqa: BLE001
                            logger.warning("DB insert failed  [%s] %s: %s", src_name, title, exc)

                    except Exception:  # noqa: BLE001
                        listing_failed += 1
                        logger.exception(
                            "LISTING FAILED  [%s] %s — unhandled exception in per-listing pipeline",
                            src_name,
                            title,
                        )

        total_tokens = total_tokens_input + total_tokens_output
        run_cost = sum(b["cost"] for b in provider_costs.values())
        logger.info(
            "Run complete: %d source(s) | %d fetched | %d pre-filtered | "
            "%d dupes skipped | %d scored (%d failed) | "
            "%d listing error(s) | "
            "%d scrape skipped | %d scrape fallbacks | ~%s tok | ~$%.4f",
            len(sources), fetched, prefiltered,
            deduped, scored, score_failed,
            listing_failed,
            scraped_skipped, scraped_fallback, f"{total_tokens:,}", run_cost,
        )
        if len(provider_costs) > 1:
            breakdown = " | ".join(
                f"{name}: ~{b['input'] + b['output']:,} tok ~${b['cost']:.4f}"
                for name, b in provider_costs.items()
            )
            logger.info("  Cost breakdown: %s", breakdown)
        if source_fetch_counts:
            logger.info("  Sources: %s", " | ".join(f"{src}: {cnt}" for src, cnt in source_fetch_counts.items()))
        if geo.is_active:
            logger.info(
                "  Geocache: %d hit(s) | %d miss(es) | %d failed | %d discarded by radius",
                geo.hits, geo.misses, geo.failed, geo.geo_discarded,
            )
        logger.info("=" * 60)
        logger.info("INGEST RUN COMPLETE")
        logger.info("=" * 60)

        if run_id is not None:
            db.finish_ingest_run(
                run_id,
                status="success",
                counts={
                    "fetched": fetched,
                    "filtered": prefiltered,
                    "scored": scored,
                    "failed": score_failed,
                },
                cost_usd=run_cost,
            )
    except Exception as exc:  # noqa: BLE001
        if run_id is not None:
            try:
                db.finish_ingest_run(
                    run_id,
                    status="failed",
                    error_message=str(exc),
                )
            except Exception:  # noqa: BLE001
                logger.warning("Could not record run failure in database")
        raise


# ---------------------------------------------------------------------------
# Rescorer
# ---------------------------------------------------------------------------

def rescore(
    config_path: str = _DEFAULT_CONFIG_PATH,
    profile_path: str = _DEFAULT_PROFILE_PATH,
    keys_path: str = _DEFAULT_KEYS_PATH,
    providers_path: str = _DEFAULT_PROVIDERS_PATH,
) -> None:
    """Re-score all previously scored listings against the current profile.

    Loads config and profile, fetches every listing with seen = 1 from the
    database, and re-runs scoring on each one via the provider chain.
    Does not fetch new listings from Adzuna.

    Args:
        config_path:    Path to config.json (default ``"config/config.json"``).
        profile_path:   Path to profile.json (default ``"config/profile.json"``).
        keys_path:      Path to legacy ``keys.json`` passed to
                        :func:`credentials.load_providers` for one-time migration;
                        not used when ``providers.json`` is already present.
                        Default: ``"config/keys.json"``.
        providers_path: Path to ``providers.json`` — the unified credential store
                        for LLM providers and job source credentials.
                        Default: ``"config/providers.json"``.
                        Override in tests to inject a temp file.
    """
    try:
        profile = load_profile(profile_path)
    except SystemExit as exc:
        logger.error("Startup error: %s", exc)
        raise

    try:
        providers = load_providers(
            providers_path=providers_path,
            keys_path=keys_path,
            config_path=config_path,
        )
    except CredentialError as exc:
        logger.warning(
            "No credentials found — skipping rescore run. %s", exc
        )
        return

    chain = build_provider_chain(providers)
    dead_providers: set[str] = set()

    try:
        listings = db.get_all_scored()
    except Exception as exc:  # noqa: BLE001
        logger.error("Could not fetch listings from database: %s", exc)
        sys.exit(1)

    if not listings:
        logger.info("No scored listings to rescore.")
        return

    total = len(listings)
    logger.info("=" * 60)
    logger.info("RESCORE RUN STARTED  (re-scoring existing listings — no new fetch)")
    logger.info("  Listings to rescore: %d", total)
    if chain:
        logger.info(
            "  LLM providers: %s",
            " | ".join(f"{_provider_name(p)}/{_provider_model(p)}" for p in chain),
        )
    else:
        logger.warning("  No LLM providers configured — all rescores will fail")
    logger.info("=" * 60)
    rescored = 0
    failed = 0
    tokens_input = 0
    tokens_output = 0
    provider_costs: dict[str, dict] = {}

    for listing in listings:
        title = listing.get("title", "(no title)")
        result = score_listing_with_fallback(
            listing=listing,
            profile=profile,
            chain=chain,
            dead_providers=dead_providers,
        )

        if result is not None:
            db.update_score(listing["source"], listing["source_id"], result)
            rescored += 1
            tok_in = result.get("tokens_input") or 0
            tok_out = result.get("tokens_output") or 0
            tokens_input += tok_in
            tokens_output += tok_out
            logger.info("RESCORED %d/10  %s", result.get("score", 0), title)
            if _verbose:
                logger.info(
                    "  verdict: %s\n  matched: %s\n  missing: %s\n  concerns: %s",
                    result.get("verdict", ""),
                    ", ".join(result.get("matched_skills") or []) or "none",
                    ", ".join(result.get("missing_skills") or []) or "none",
                    ", ".join(result.get("concerns") or []) or "none",
                )

            used_provider = result.get("model_used", "").split("/")[0] or "unknown"
            if used_provider not in provider_costs:
                matched = next(
                    (p for p in chain if _provider_name(p) == used_provider),
                    None,
                )
                provider_costs[used_provider] = {
                    "input": 0,
                    "output": 0,
                    "cost": 0.0,
                    "_in_rate": matched.input_cost_per_mtok if matched else 0.0,
                    "_out_rate": matched.output_cost_per_mtok if matched else 0.0,
                }
            bucket = provider_costs[used_provider]
            bucket["input"] += tok_in
            bucket["output"] += tok_out
            bucket["cost"] += (
                tok_in  / 1_000_000 * bucket["_in_rate"]
                + tok_out / 1_000_000 * bucket["_out_rate"]
            )
        else:
            failed += 1
            logger.warning("RESCORE FAILED  %s", title)

    total_tokens = tokens_input + tokens_output
    cost = sum(b["cost"] for b in provider_costs.values())
    logger.info(
        "Rescore complete: %d listings | %d rescored (%d failed) | ~%s tok | ~$%.4f",
        total, rescored, failed, f"{total_tokens:,}", cost,
    )
    if len(provider_costs) > 1:
        breakdown = " | ".join(
            f"{name}: ~{b['input'] + b['output']:,} tok ~${b['cost']:.4f}"
            for name, b in provider_costs.items()
        )
        logger.info("  Cost breakdown: %s", breakdown)
    logger.info("=" * 60)
    logger.info("RESCORE RUN COMPLETE")
    logger.info("=" * 60)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Job Matcher ingestion pipeline")
    parser.add_argument(
        "--rescore",
        action="store_true",
        help="Re-score all previously scored listings against the current profile. "
             "Does not fetch new listings.",
    )
    parser.add_argument("--config", default=_DEFAULT_CONFIG_PATH, help="Path to config.json")
    parser.add_argument("--profile", default=_DEFAULT_PROFILE_PATH, help="Path to profile.json")
    parser.add_argument(
        "--hours",
        type=int,
        default=None,
        help=(
            "Only process listings fetched within the last N hours. "
            "Overrides max_days_old in config."
        ),
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Log the full scoring breakdown (verdict, matched/missing skills, concerns) "
             "for every listing at INFO level.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable DEBUG level logging (verbose output for troubleshooting).",
    )
    args = parser.parse_args()

    _configure_file_logging()

    if args.verbose:
        _verbose = True

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    if args.rescore:
        rescore(config_path=args.config, profile_path=args.profile)
    else:
        run(config_path=args.config, profile_path=args.profile, hours=args.hours)
