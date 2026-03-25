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
import re
import time
from datetime import datetime, timezone
from typing import Iterator

import anthropic
import requests
from bs4 import BeautifulSoup

import db

# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("ingest")

# ---------------------------------------------------------------------------
# Config and profile loading
# ---------------------------------------------------------------------------

_REQUIRED_TOP_LEVEL = ("adzuna_app_id", "adzuna_app_key", "anthropic_api_key")
_REQUIRED_SEARCH = ("country", "what", "results_per_page", "max_pages")
_REQUIRED_SCORING = ("threshold", "model")


def load_config(path: str = "config.json") -> dict:
    """Load and validate config.json.

    Raises SystemExit with a descriptive message if the file cannot be read
    or any required key is missing.
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
    model: str,
    api_key: str,
) -> dict | None:
    """Score a job description against a candidate profile using Claude.

    Builds a structured prompt, calls the Anthropic Messages API, and parses
    the JSON response.  Retries once on failure (API error or malformed JSON)
    after a 2-second delay.  Returns None if both attempts fail.

    Args:
        description: Full (or snippet) job description text.
        profile: Candidate profile dict loaded from profile.json.
        model: Anthropic model ID (e.g. ``claude-haiku-4-5-20251001``).
        api_key: Anthropic API key.

    Returns:
        Dict with keys ``score``, ``matched_skills``, ``missing_skills``,
        ``concerns``, ``verdict``; or None on persistent failure.
    """
    prompt = _PROMPT_TEMPLATE.format(
        profile_json=json.dumps(profile, indent=2),
        description=description,
    )

    client = anthropic.Anthropic(api_key=api_key)

    for attempt in range(2):
        if attempt > 0:
            time.sleep(2)

        try:
            message = client.messages.create(
                model=model,
                max_tokens=1024,
                messages=[{"role": "user", "content": prompt}],
            )
        except anthropic.APIError as exc:
            logger.warning("Anthropic API error (attempt %d/2): %s", attempt + 1, exc)
            continue

        # Extract the text content from the first content block.
        try:
            raw_content = message.content[0].text
        except (IndexError, AttributeError) as exc:
            logger.warning(
                "Unexpected Anthropic response structure (attempt %d/2): %s",
                attempt + 1,
                exc,
            )
            continue

        # Strip markdown code fences that the model sometimes wraps around JSON
        # despite being instructed not to (e.g. ```json ... ```).
        stripped = raw_content.strip()
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()

        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError as exc:
            logger.warning(
                "Anthropic returned non-JSON (attempt %d/2): %s — raw: %.200s",
                attempt + 1,
                exc,
                raw_content,
            )
            continue

        if not isinstance(parsed, dict) or not _SCORE_KEYS.issubset(parsed.keys()):
            missing_keys = _SCORE_KEYS - set(parsed.keys())
            logger.warning(
                "Score response missing keys %s (attempt %d/2)",
                missing_keys,
                attempt + 1,
            )
            continue

        # Attach token usage from the API response, if available.
        try:
            parsed["tokens_input"] = message.usage.input_tokens
            parsed["tokens_output"] = message.usage.output_tokens
        except AttributeError:
            parsed["tokens_input"] = None
            parsed["tokens_output"] = None

        return parsed

    logger.warning("Scoring failed after 2 attempts; listing will be stored unscored")
    return None


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def run(config_path: str = "config.json", profile_path: str = "profile.json") -> None:
    """Run the full ingestion pipeline.

    Loads config and profile, initialises the DB, pages through Adzuna
    results, pre-filters, deduplicates, scrapes, scores, and persists each
    listing.  Prints a summary line when complete.

    Args:
        config_path: Path to config.json (default ``"config.json"``).
        profile_path: Path to profile.json (default ``"profile.json"``).
    """
    config = load_config(config_path)
    profile = load_profile(profile_path)
    job_type = config["search"].get("what", "").strip()

    db.init_db()

    client = AdzunaClient(
        app_id=config["adzuna_app_id"],
        app_key=config["adzuna_app_key"],
        config=config,
    )

    anthropic_api_key: str = config["anthropic_api_key"]
    scoring_model: str = config["scoring"]["model"]

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

    for page in client.pages():
        for listing in page:
            fetched += 1
            title = listing.get("title", "(no title)")

            # --- Pre-filter ---
            reason = prefilter(listing, config)
            if reason is not None:
                prefiltered += 1
                logger.info("FILTERED  %s — %s", title, reason)
                continue

            # --- Dedup ---
            if db.listing_exists(listing["adzuna_id"]):
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
            score_result = score_listing(
                description=description,
                profile=profile,
                model=scoring_model,
                api_key=anthropic_api_key,
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
                total_tokens_input += score_result.get("tokens_input") or 0
                total_tokens_output += score_result.get("tokens_output") or 0
                listing.update(score_result)
                listing["seen"] = 1

            # --- Persist ---
            listing["fetched_at"] = datetime.now(timezone.utc).isoformat()
            listing["bookmarked"] = 0
            listing["dismissed"] = 0
            listing["job_type"] = job_type or None

            try:
                db.insert_listing(listing)
            except Exception as exc:  # noqa: BLE001
                logger.warning("DB insert failed for %s: %s", title, exc)

    total_tokens = total_tokens_input + total_tokens_output
    run_cost = (
        total_tokens_input / 1_000_000 * 0.80
        + total_tokens_output / 1_000_000 * 4.00
    )
    print(
        f"Run complete: {fetched} fetched | {prefiltered} pre-filtered | "
        f"{deduped} dupes skipped | {scored} scored ({score_failed} failed) | "
        f"{scraped_fallback} scrape fallbacks | "
        f"~{total_tokens:,} tok | ~${run_cost:.4f}"
    )


# ---------------------------------------------------------------------------
# Rescorer
# ---------------------------------------------------------------------------

def rescore(config_path: str = "config.json", profile_path: str = "profile.json") -> None:
    """Re-score all previously scored listings against the current profile.

    Loads config and profile, fetches every listing with seen = 1 from the
    database, and re-runs Claude scoring on each one.  Does not fetch new
    listings from Adzuna.

    Args:
        config_path:  Path to config.json (default ``"config.json"``).
        profile_path: Path to profile.json (default ``"profile.json"``).
    """
    config = load_config(config_path)
    profile = load_profile(profile_path)

    api_key: str = config["anthropic_api_key"]
    model: str = config["scoring"]["model"]

    listings = db.get_all_scored()
    if not listings:
        print("No scored listings to rescore.")
        return

    total = len(listings)
    rescored = 0
    failed = 0
    tokens_input = 0
    tokens_output = 0

    for listing in listings:
        title = listing.get("title", "(no title)")
        result = score_listing(listing["description"], profile, model, api_key)

        if result is not None:
            db.update_score(listing["adzuna_id"], result)
            rescored += 1
            tokens_input += result.get("tokens_input") or 0
            tokens_output += result.get("tokens_output") or 0
            logger.info("RESCORED %d/10  %s", result.get("score", 0), title)
        else:
            failed += 1
            logger.warning("RESCORE FAILED  %s", title)

    total_tokens = tokens_input + tokens_output
    cost = (
        tokens_input / 1_000_000 * 0.80
        + tokens_output / 1_000_000 * 4.00
    )
    print(
        f"Rescore complete: {total} listings | {rescored} rescored ({failed} failed) | "
        f"~{total_tokens:,} tok | ~${cost:.4f}"
    )


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
    args = parser.parse_args()

    if args.rescore:
        rescore(config_path=args.config, profile_path=args.profile)
    else:
        run(config_path=args.config, profile_path=args.profile)
