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
import time
from datetime import datetime, timezone
from typing import Iterator

import requests
from bs4 import BeautifulSoup

import db
from providers import build_provider_chain, LLMProvider

_DB_PATH: str = os.environ.get("DB_PATH", "jobs.db")

# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("ingest")

# ---------------------------------------------------------------------------
# Config and profile loading
# ---------------------------------------------------------------------------

_REQUIRED_TOP_LEVEL = ("adzuna_app_id", "adzuna_app_key")
_REQUIRED_SEARCH = ("country", "what", "results_per_page", "max_pages")
_REQUIRED_SCORING = ("threshold",)

# Default models used when constructing a keys dict from env vars.
_ENV_VAR_DEFAULTS: tuple[tuple[str, str, str], ...] = (
    ("ANTHROPIC_API_KEY", "anthropic", "claude-haiku-4-5-20251001"),
    ("OPENAI_API_KEY",    "openai",    "gpt-4o-mini"),
    ("GOOGLE_API_KEY",    "gemini",    "gemini-1.5-flash"),
)


def load_config(path: str = "config.json") -> dict:
    """Load and validate config.json.

    Raises SystemExit with a descriptive message if the file cannot be read
    or any required key is missing.

    LLM provider API keys are no longer validated here — they have moved to
    ``keys.json`` and are loaded by :func:`load_keys`.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            config = json.load(fh)
    except FileNotFoundError:
        raise SystemExit(f"Config file not found: {path}")
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Config file is not valid JSON: {exc}")

    # Environment variables override config.json values for containerised deployments.
    # Applied before the missing-key check so env vars can satisfy required key validation.
    # LLM provider keys (ANTHROPIC_API_KEY etc.) are intentionally excluded here —
    # they are handled by load_keys().
    for env_var, config_key in (
        ("ADZUNA_APP_ID",  "adzuna_app_id"),
        ("ADZUNA_APP_KEY", "adzuna_app_key"),
    ):
        val = os.environ.get(env_var)
        if val:
            config[config_key] = val

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


def load_keys(path: str = "keys.json") -> dict:
    """Load LLM provider API keys and preferred provider from ``keys.json``.

    Supports two paths:

    * **keys.json present** — load and return it directly. The file must have
      a ``providers`` key whose value is a non-empty dict.
    * **keys.json absent** — construct an equivalent dict from environment
      variables (``ANTHROPIC_API_KEY``, ``OPENAI_API_KEY``, ``GOOGLE_API_KEY``).
      Only providers whose env var is non-empty are included.
      ``preferred_provider`` is set to the first provider found, or
      ``"anthropic"`` if none are found (which also triggers ``SystemExit``).

    Raises:
        SystemExit: If neither ``keys.json`` nor any env var provides at least
            one API key.

    Returns:
        Dict matching the ``keys.example.json`` structure::

            {
                "providers": {
                    "anthropic": {"api_key": "...", "model": "..."},
                    ...
                },
                "preferred_provider": "anthropic"
            }
    """
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                keys = json.load(fh)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"keys.json is not valid JSON: {exc}")

        providers = keys.get("providers")
        if not isinstance(providers, dict) or not providers:
            raise SystemExit(
                f"keys.json must have a non-empty 'providers' dict. "
                f"Copy keys.example.json to {path} and fill in your API keys."
            )

        logger.info("Loaded keys.json")
        return keys

    # keys.json not present — fall back to environment variables.
    logger.info("keys.json not found — using env var fallback")

    providers: dict[str, dict[str, str]] = {}
    preferred_provider: str = "anthropic"
    first_found = True

    for env_var, provider_name, default_model in _ENV_VAR_DEFAULTS:
        api_key = os.environ.get(env_var, "")
        if api_key:
            providers[provider_name] = {"api_key": api_key, "model": default_model}
            if first_found:
                preferred_provider = provider_name
                first_found = False

    if not providers:
        raise SystemExit(
            "No LLM API keys found. Either:\n"
            "  1. Copy keys.example.json to keys.json and fill in your API keys, or\n"
            "  2. Set at least one of ANTHROPIC_API_KEY, OPENAI_API_KEY, GOOGLE_API_KEY "
            "as an environment variable."
        )

    return {"providers": providers, "preferred_provider": preferred_provider}


def load_profile(path: str = "profile.json") -> dict:
    """Load profile.json and return the parsed dict.

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

_ADZUNA_BASE = "https://api.adzuna.com/v1/api/jobs/{country}/search/{page}"


class AdzunaClient:
    """Wraps the Adzuna Jobs REST API.

    Handles pagination, rate-limit retry, and result normalisation.
    """

    def __init__(self, app_id: str, app_key: str, config: dict) -> None:
        """Store credentials and the search section of config.

        Args:
            app_id: Adzuna application ID.
            app_key: Adzuna application key.
            config: Full config dict; the ``search`` sub-dict is used for
                query parameters.
        """
        self._app_id = app_id
        self._app_key = app_key
        self._search = config["search"]

    def fetch_page(self, page: int) -> list[dict]:
        """Fetch a single page of Adzuna results.

        On HTTP 429 retries up to three times with exponential back-off
        (2 s, 4 s, 8 s).  Any other non-200 response is logged and returns
        an empty list.  Missing ``results`` key in the response also returns
        an empty list.

        Args:
            page: 1-based page number.

        Returns:
            List of normalised listing dicts.
        """
        country = self._search["country"]
        url = _ADZUNA_BASE.format(country=country, page=page)

        params: dict[str, str | int] = {
            "app_id": self._app_id,
            "app_key": self._app_key,
            "what": self._search["what"],
            "results_per_page": self._search["results_per_page"],
            "content-type": "application/json",
        }

        # Optional params — only add if present and non-empty/non-zero.
        where = self._search.get("where", "")
        if where:
            params["where"] = where

        salary_min = self._search.get("salary_min", 0)
        if salary_min:
            params["salary_min"] = salary_min

        distance = self._search.get("distance", 0)
        if distance:
            params["distance"] = distance

        max_days_old = self._search.get("max_days_old", 0)
        if max_days_old:
            params["max_days_old"] = max_days_old

        backoff_delays = [2, 4, 8]
        response: requests.Response | None = None

        for attempt, delay in enumerate([0] + backoff_delays):
            if delay:
                logger.warning(
                    "Rate-limited by Adzuna (429); retrying in %d s (attempt %d/3)",
                    delay,
                    attempt,
                )
                time.sleep(delay)

            try:
                response = requests.get(url, params=params, timeout=15)
            except requests.RequestException as exc:
                logger.warning("Adzuna request failed: %s", exc)
                return []

            if response.status_code == 200:
                break
            if response.status_code == 429:
                if attempt < len(backoff_delays):
                    continue
                # Exhausted retries.
                logger.warning(
                    "Adzuna rate limit not resolved after %d retries; page %d skipped",
                    len(backoff_delays),
                    page,
                )
                return []
            else:
                logger.warning(
                    "Adzuna returned HTTP %d for page %d; skipping",
                    response.status_code,
                    page,
                )
                return []

        if response is None:
            return []

        try:
            data = response.json()
        except ValueError as exc:
            logger.warning("Adzuna response is not valid JSON: %s", exc)
            return []

        raw_results: list[dict] = data.get("results", [])
        if not raw_results:
            return []

        return [self._normalise(r) for r in raw_results]

    @staticmethod
    def _normalise(raw: dict) -> dict:
        """Map an Adzuna result dict to the canonical listing shape.

        Args:
            raw: A single entry from the Adzuna ``results`` array.

        Returns:
            Dict with keys matching the ``listings`` table columns.
        """
        company_obj = raw.get("company") or {}
        location_obj = raw.get("location") or {}

        salary_is_predicted_raw = raw.get("salary_is_predicted", 0)
        # Adzuna returns this as "1"/"0" or bool; coerce to int.
        try:
            salary_is_predicted = int(salary_is_predicted_raw)
        except (TypeError, ValueError):
            salary_is_predicted = 0

        return {
            "adzuna_id": raw.get("id", ""),
            "source": "adzuna",
            "source_id": str(raw.get("id", "")),
            "title": raw.get("title", ""),
            "company": company_obj.get("display_name", "") if isinstance(company_obj, dict) else "",
            "location": location_obj.get("display_name", "") if isinstance(location_obj, dict) else "",
            "salary_min": raw.get("salary_min"),
            "salary_max": raw.get("salary_max"),
            "salary_is_predicted": salary_is_predicted,
            "contract_type": raw.get("contract_type", "") or "",
            "contract_time": raw.get("contract_time", "") or "",
            "description": raw.get("description", "") or "",
            "redirect_url": raw.get("redirect_url", "") or "",
            "created_at": raw.get("created", "") or "",
            "posted_at": raw.get("created") or None,
        }

    def pages(self) -> Iterator[list[dict]]:
        """Yield normalised listing lists, one per page.

        Iterates from page 1 up to ``max_pages`` (inclusive). Stops early
        if a page returns zero results.

        Yields:
            Lists of normalised listing dicts.
        """
        max_pages: int = self._search["max_pages"]
        for page in range(1, max_pages + 1):
            results = self.fetch_page(page)
            if not results:
                logger.info("Page %d returned 0 results; stopping early", page)
                return
            yield results


# ---------------------------------------------------------------------------
# Pre-filter
# ---------------------------------------------------------------------------

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
    salary_max = listing.get("salary_max")
    configured_floor = (
        pf.get("salary_min")
        or config.get("search", {}).get("salary_min")
        or 0
    )
    if configured_floor and salary_max is not None:
        try:
            if float(salary_max) < float(configured_floor):
                return f"salary: max {salary_max} below floor {configured_floor}"
        except (TypeError, ValueError):
            pass  # If we can't compare, let it through.

    # Contract time.
    require_time: str | None = pf.get("require_contract_time")
    if require_time is not None:
        actual_time = listing.get("contract_time", "")
        if actual_time.lower() != require_time.lower():
            return f'contract_time: got "{actual_time}" expected "{require_time}"'

    # Contract type.
    require_type: str | None = pf.get("require_contract_type")
    if require_type is not None:
        actual_type = listing.get("contract_type", "")
        if actual_type.lower() != require_type.lower():
            return f'contract_type: got "{actual_type}" expected "{require_type}"'

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
    prompt = _PROMPT_TEMPLATE.format(
        profile_json=json.dumps(profile, indent=2),
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
                logger.warning(
                    "Auth error from provider %s — removing from chain for this run: %s",
                    name,
                    exc,
                )
                dead_providers.add(name)
            else:
                logger.warning(
                    "Transient error from provider %s — trying next provider: %s",
                    name,
                    exc,
                )

    logger.warning(
        "All providers in chain failed for listing: %s",
        listing.get("title", "(no title)"),
    )
    return None


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def run(
    config_path: str = "config.json",
    profile_path: str = "profile.json",
    hours: int | None = None,
    keys_path: str = "keys.json",
) -> None:
    """Run the full ingestion pipeline.

    Loads config and profile, initialises the DB, pages through Adzuna
    results, pre-filters, deduplicates, scrapes, scores, and persists each
    listing.  Prints a summary line when complete.

    Args:
        config_path:  Path to config.json (default ``"config.json"``).
        profile_path: Path to profile.json (default ``"profile.json"``).
        hours:        If provided, only process listings whose ``created_at``
                      timestamp is within the last N hours. Overrides
                      ``search.max_days_old`` in config with ``ceil(hours/24)``.
        keys_path:    Path to keys.json (default ``"keys.json"``).  Override
                      in tests to inject a temp file.
    """
    config = load_config(config_path)
    profile = load_profile(profile_path)
    job_type = config["search"].get("what", "").strip()

    if hours is not None:
        config["search"]["max_days_old"] = math.ceil(hours / 24)

    db.init_db(db_path=_DB_PATH)

    client = AdzunaClient(
        app_id=config["adzuna_app_id"],
        app_key=config["adzuna_app_key"],
        config=config,
    )

    keys = load_keys(keys_path)
    chain = build_provider_chain(keys)
    dead_providers: set[str] = set()

    # Counters.
    fetched = 0
    prefiltered = 0
    deduped = 0
    scraped_ok = 0
    scraped_fallback = 0
    scored = 0
    score_failed = 0
    total_tokens_input = 0
    total_tokens_output = 0
    # Per-provider cost tracking: {provider_name: {input, output, cost}}
    provider_costs: dict[str, dict] = {}

    # Cutoff used for --hours filtering.
    hours_cutoff: datetime | None = None
    if hours is not None:
        from datetime import timedelta
        hours_cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

    for page in client.pages():
        for listing in page:
            fetched += 1
            title = listing.get("title", "(no title)")

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
                                "FILTERED  %s — created_at older than %d hours", title, hours
                            )
                            continue
                    except (ValueError, TypeError):
                        pass  # Unparseable date — let the listing through.

            # --- Pre-filter ---
            reason = prefilter(listing, config)
            if reason is not None:
                prefiltered += 1
                logger.info("FILTERED  %s — %s", title, reason)
                continue

            # --- Dedup ---
            # Open one connection and reuse it for both dedup checks to avoid
            # two open/close round-trips per listing.
            with db.get_connection(_DB_PATH) as _dedup_conn:
                _is_dupe = db.listing_exists(
                    _dedup_conn, listing["source"], listing["source_id"]
                )
                if not _is_dupe:
                    redirect_url = listing.get("redirect_url", "")
                    if redirect_url:
                        _is_dupe = db.listing_exists_by_url(_dedup_conn, redirect_url)
            if _is_dupe:
                deduped += 1
                logger.info("DUPE      %s", title)
                continue

            # --- Scrape ---
            description, ok = scrape_description(
                listing["redirect_url"],
                fallback=listing["description"],
            )
            if ok:
                scraped_ok += 1
            else:
                scraped_fallback += 1
                logger.warning("SCRAPE FALLBACK  %s", title)

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
                logger.warning("SCORE FAILED  %s", title)
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
                    "SCORED %d/10  %s",
                    score_result.get("score", 0),
                    title,
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

            try:
                db.insert_listing(listing, db_path=_DB_PATH)
            except Exception as exc:  # noqa: BLE001
                logger.warning("DB insert failed for %s: %s", title, exc)

    total_tokens = total_tokens_input + total_tokens_output
    run_cost = sum(b["cost"] for b in provider_costs.values())
    print(
        f"Run complete: {fetched} fetched | {prefiltered} pre-filtered | "
        f"{deduped} dupes skipped | {scored} scored ({score_failed} failed) | "
        f"{scraped_fallback} scrape fallbacks | "
        f"~{total_tokens:,} tok | ~${run_cost:.4f}"
    )
    if len(provider_costs) > 1:
        breakdown = " | ".join(
            f"{name}: ~{b['input'] + b['output']:,} tok ~${b['cost']:.4f}"
            for name, b in provider_costs.items()
        )
        print(f"  Cost breakdown: {breakdown}")


# ---------------------------------------------------------------------------
# Rescorer
# ---------------------------------------------------------------------------

def rescore(
    config_path: str = "config.json",
    profile_path: str = "profile.json",
    keys_path: str = "keys.json",
) -> None:
    """Re-score all previously scored listings against the current profile.

    Loads config and profile, fetches every listing with seen = 1 from the
    database, and re-runs scoring on each one via the provider chain.
    Does not fetch new listings from Adzuna.

    Args:
        config_path:  Path to config.json (default ``"config.json"``).
        profile_path: Path to profile.json (default ``"profile.json"``).
        keys_path:    Path to keys.json (default ``"keys.json"``).  Override
                      in tests to inject a temp file.
    """
    profile = load_profile(profile_path)

    keys = load_keys(keys_path)
    chain = build_provider_chain(keys)
    dead_providers: set[str] = set()

    listings = db.get_all_scored(db_path=_DB_PATH)
    if not listings:
        print("No scored listings to rescore.")
        return

    total = len(listings)
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
            db.update_score(listing["source"], listing["source_id"], result, db_path=_DB_PATH)
            rescored += 1
            tok_in = result.get("tokens_input") or 0
            tok_out = result.get("tokens_output") or 0
            tokens_input += tok_in
            tokens_output += tok_out
            logger.info("RESCORED %d/10  %s", result.get("score", 0), title)

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
    print(
        f"Rescore complete: {total} listings | {rescored} rescored ({failed} failed) | "
        f"~{total_tokens:,} tok | ~${cost:.4f}"
    )
    if len(provider_costs) > 1:
        breakdown = " | ".join(
            f"{name}: ~{b['input'] + b['output']:,} tok ~${b['cost']:.4f}"
            for name, b in provider_costs.items()
        )
        print(f"  Cost breakdown: {breakdown}")


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
    parser.add_argument("--config", default="config.json", help="Path to config.json")
    parser.add_argument("--profile", default="profile.json", help="Path to profile.json")
    parser.add_argument(
        "--hours",
        type=int,
        default=None,
        help=(
            "Only process listings fetched within the last N hours. "
            "Overrides max_days_old in config."
        ),
    )
    args = parser.parse_args()

    if args.rescore:
        rescore(config_path=args.config, profile_path=args.profile)
    else:
        run(config_path=args.config, profile_path=args.profile, hours=args.hours)
