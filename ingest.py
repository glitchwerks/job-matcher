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
from datetime import datetime, timezone
import requests
from bs4 import BeautifulSoup

import db
from job_sources import make_source, AdzunaClient, make_enabled_sources  # noqa: F401 — AdzunaClient re-exported for backward compat
from providers import build_provider_chain, LLMProvider
from credentials import CredentialError, load_providers

_DB_PATH: str = os.environ.get("DB_PATH", "jobs.db")

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


def _configure_file_logging() -> None:
    """Attach a FileHandler writing to a timestamped per-run log file.

    Files are named  ingest_YYYYMMDD_HHMMSS.log  and stored in the same
    logs/ directory as before.  Old files are pruned to keep at most
    MAX_LOG_FILES runs (default 30).

    Called only from the __main__ entry point so that importing this module
    during tests does not create directories or open file handles.
    """
    MAX_LOG_FILES = 30

    db_abs  = os.path.abspath(os.environ.get("DB_PATH", "jobs.db"))
    log_dir = os.path.join(os.path.dirname(db_abs), "logs")
    os.makedirs(log_dir, exist_ok=True)

    ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = os.path.join(log_dir, f"ingest_{ts}.log")

    handler = logging.FileHandler(log_file, encoding="utf-8")
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    logging.getLogger().addHandler(handler)
    logger.info("Logging to file: %s", log_file)

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
        keys_path:      Path to legacy keys.json (used by migration; default
                        ``"config/keys.json"``).
        providers_path: Path to providers.json (default ``"config/providers.json"``).
                        Override in tests to inject a temp file.
    """
    config = load_config(config_path)
    profile = load_profile(profile_path)
    job_type = config["search"].get("what", "").strip()

    if hours is not None:
        config["search"]["max_days_old"] = math.ceil(hours / 24)

    db.init_db(db_path=_DB_PATH)

    try:
        providers = load_providers(
            providers_path=providers_path,
            keys_path=keys_path,
            config_path=config_path,
        )
    except CredentialError as exc:
        logger.error("Credential error: %s", exc)
        import sys as _sys
        _sys.exit(1)

    _inject_env_var_credentials(providers)

    sources = make_enabled_sources(providers, config)
    if not sources:
        logger.warning(
            "No job sources are enabled. Enable at least one source in Settings > Sources."
        )
        logger.info("Run complete: 0 fetched | no sources enabled")
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
    scored = 0
    score_failed = 0
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
        for page in client.pages():
            for listing in page:
                fetched += 1
                src_name = listing.get("source", client.SOURCE)
                source_fetch_counts[src_name] = source_fetch_counts.get(src_name, 0) + 1
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
                    logger.info("DUPE      [%s] %s", src_name, title)
                    continue

                # --- Scrape ---
                # Sources that set skip_scrape=True (e.g. Jooble, whose /jdp/
                # pages return HTTP 403 to cold requests) have already provided
                # the best available description via the API.  Skip the HTTP
                # round-trip and use the API description directly.
                if listing.get("skip_scrape"):
                    scraped_fallback += 1
                    logger.info("SCRAPE SKIP      [%s] %s", src_name, title)
                else:
                    description, ok = scrape_description(
                        listing["redirect_url"],
                        fallback=listing["description"],
                    )
                    if ok:
                        scraped_ok += 1
                    else:
                        scraped_fallback += 1
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
                    db.insert_listing(listing, db_path=_DB_PATH)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("DB insert failed  [%s] %s: %s", src_name, title, exc)

        source_count = source_fetch_counts.get(client.SOURCE, 0)
        logger.info("Fetched %d listing(s) from %s", source_count, client.SOURCE)

    total_tokens = total_tokens_input + total_tokens_output
    run_cost = sum(b["cost"] for b in provider_costs.values())
    logger.info(
        "Run complete: %d source(s) | %d fetched | %d pre-filtered | "
        "%d dupes skipped | %d scored (%d failed) | "
        "%d scrape fallbacks | ~%s tok | ~$%.4f",
        len(sources), fetched, prefiltered,
        deduped, scored, score_failed,
        scraped_fallback, f"{total_tokens:,}", run_cost,
    )
    if len(provider_costs) > 1:
        breakdown = " | ".join(
            f"{name}: ~{b['input'] + b['output']:,} tok ~${b['cost']:.4f}"
            for name, b in provider_costs.items()
        )
        logger.info("  Cost breakdown: %s", breakdown)
    if source_fetch_counts:
        logger.info("  Sources: %s", " | ".join(f"{src}: {cnt}" for src, cnt in source_fetch_counts.items()))
    logger.info("=" * 60)
    logger.info("INGEST RUN COMPLETE")
    logger.info("=" * 60)


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
        keys_path:      Path to legacy keys.json (used by migration; default
                        ``"config/keys.json"``).
        providers_path: Path to providers.json (default ``"config/providers.json"``).
                        Override in tests to inject a temp file.
    """
    profile = load_profile(profile_path)

    try:
        providers = load_providers(
            providers_path=providers_path,
            keys_path=keys_path,
            config_path=config_path,
        )
    except CredentialError as exc:
        logger.error("Credential error: %s", exc)
        import sys as _sys
        _sys.exit(1)

    chain = build_provider_chain(providers)
    dead_providers: set[str] = set()

    listings = db.get_all_scored(db_path=_DB_PATH)
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
        "--debug",
        action="store_true",
        help="Enable DEBUG level logging (verbose output for troubleshooting).",
    )
    args = parser.parse_args()

    _configure_file_logging()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    if args.rescore:
        rescore(config_path=args.config, profile_path=args.profile)
    else:
        run(config_path=args.config, profile_path=args.profile, hours=args.hours)
