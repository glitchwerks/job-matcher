"""
plugins/sources/jsearch/plugin.py — JSearch (RapidAPI) implementation of the JobSource protocol.

Wraps the JSearch API (https://jsearch.p.rapidapi.com/search), which aggregates
job listings from Google for Jobs and returns full plaintext descriptions in the
API response — no scraping step required.

Free tier: 200 requests/month.  The configured ``max_pages`` default of 3 is
intentionally conservative to stay within that budget.
"""

from __future__ import annotations

import logging
import time

import requests

from job_sources.base import JobSource

logger = logging.getLogger("ingest.jsearch")

_JSEARCH_URL = "https://jsearch.p.rapidapi.com/search"
_JSEARCH_HOST = "jsearch.p.rapidapi.com"

# Mapping of JSearch employment-type strings (uppercase) to canonical contract_time values.
_CONTRACT_TIME_MAP: dict[str, str] = {
    "FULLTIME": "full_time",
    "PARTTIME": "part_time",
    "CONTRACTOR": "contract",
    "INTERN": "intern",
}

# Mapping of JSearch salary-period strings (uppercase) to canonical salary_period values.
# MONTH and WEEK are not in the canonical list but are passed through to avoid data loss.
_SALARY_PERIOD_MAP: dict[str, str] = {
    "YEAR": "annual",
    "DAY": "daily",
    "HOUR": "hourly",
    "MONTH": "month",
    "WEEK": "week",
}


def _normalise_contract_time(raw: str | None) -> str | None:
    """Map a JSearch employment-type string to the canonical contract_time value.

    Hyphens and spaces are stripped before lookup, so ``"full-time"``,
    ``"FULL-TIME"``, and ``"full time"`` all resolve to the same canonical
    value.  Lookup is case-insensitive.  Known values such as ``"FULLTIME"``
    are mapped to their canonical equivalents.  Unknown values are lowercased
    and passed through so that the prefilter can still act on them.  ``None``
    or empty string returns ``None``.

    Args:
        raw: Raw employment-type string from the JSearch API (e.g. ``"FULLTIME"``).

    Returns:
        Canonical contract_time string, the lowercased original for unknown values,
        or ``None`` if *raw* is absent.
    """
    if not raw:
        return None
    key = raw.upper().replace("-", "").replace(" ", "")
    return _CONTRACT_TIME_MAP.get(key, raw.lower())


def _normalise_salary_period(raw: str | None) -> str | None:
    """Map a JSearch salary-period string to the canonical salary_period value.

    Lookup is case-insensitive.  Unknown period codes are discarded (return
    ``None``) rather than passed through, because an unrecognised period code
    would be meaningless downstream.  ``None`` or empty string returns ``None``.

    Args:
        raw: Raw salary-period string from the JSearch API (e.g. ``"YEAR"``).

    Returns:
        Canonical salary_period string, or ``None`` if *raw* is absent or
        unrecognised.
    """
    if not raw:
        return None
    return _SALARY_PERIOD_MAP.get(raw.upper())


def _map_date_posted(max_days_old: int) -> str | None:
    """Convert a ``max_days_old`` integer to a JSearch ``date_posted`` param value.

    JSearch accepts a small set of named intervals rather than an arbitrary day
    count.  Values are bucketed to the nearest supported interval.  Zero means
    no filter — the param should be omitted entirely.

    Args:
        max_days_old: Maximum listing age in days, as configured in
            ``config["search"]["max_days_old"]``.

    Returns:
        One of ``"today"``, ``"3days"``, ``"week"``, ``"month"``, or ``None``
        (omit the parameter) for a value of 0.
    """
    if max_days_old == 0:
        return None
    if max_days_old == 1:
        return "today"
    if max_days_old <= 3:
        return "3days"
    if max_days_old <= 7:
        return "week"
    return "month"


class JSearchClient(JobSource):
    """JobSource implementation for the JSearch (RapidAPI) job-search API.

    JSearch aggregates from Google for Jobs and returns full plaintext job
    descriptions in the API response, so ``skip_scrape=True`` is set on all
    normalised listings — the ingestion pipeline uses the API description
    directly and skips the HTTP scrape step.

    Pagination is page-number based.  ``total_pages()`` returns the configured
    ``max_pages`` value without making an API call, mirroring the Adzuna pattern.

    Note:
        ``results_per_page`` from ``config["search"]`` is intentionally ignored
        because JSearch fixes its own page size (~10 results).  A DEBUG message
        is logged in ``__init__`` if ``results_per_page`` is present in config.
    """

    SOURCE = "jsearch"

    def __init__(self, config: dict, credentials: dict | None = None) -> None:
        """Extract JSearch credentials and search parameters from config.

        Credentials are read from *credentials* first (the providers.json entry
        passed by ``make_enabled_sources``).  As a backward-compat fallback the
        constructor also accepts a ``config["jsearch"]`` sub-dict — installs that
        have not yet migrated to providers.json will continue to work.

        Args:
            config:      Full config dict.  Must contain a ``"search"`` sub-dict.
            credentials: Per-source credentials dict from providers.json.
                         Expected key: ``api_key``.

        Raises:
            ValueError: If ``api_key`` cannot be resolved from either source.
        """
        creds: dict = credentials or {}
        legacy_cfg: dict = config.get("jsearch") or {}

        api_key: str = str(creds.get("api_key") or legacy_cfg.get("api_key") or "")
        if not api_key:
            raise ValueError(
                "JSearch 'api_key' is required but was not found in credentials "
                "or config['jsearch']."
            )

        self._api_key: str = api_key
        self._search: dict = config["search"]

        if "results_per_page" in self._search:
            logger.debug(
                "JSearch: 'results_per_page' from config is ignored — "
                "JSearch fixes its own page size (~10 results/page)."
            )

    # ------------------------------------------------------------------
    # JobSource interface
    # ------------------------------------------------------------------

    def fetch_page(self, page: int) -> list[dict]:
        """Fetch and normalise a single page of JSearch listings.

        On HTTP 429 retries up to three times with exponential back-off
        (2 s, 4 s, 8 s), identical to the Adzuna implementation.  Any other
        non-200 response, network error, or bad JSON is logged and returns an
        empty list.

        An additional envelope check is performed: if the HTTP status is 200
        but the response body contains ``status != "OK"`` (a RapidAPI error
        envelope pattern), the method logs a warning and returns ``[]``.

        Args:
            page: 1-based page number.

        Returns:
            List of normalised listing dicts (via ``normalise()``).
            Returns ``[]`` on any error.
        """
        what: str = self._search.get("what", "")
        where: str = self._search.get("where", "")
        query: str = f"{what} in {where}" if where else what

        params: dict[str, str | int] = {
            "query": query,
            "page": page,
            "num_pages": 1,
        }

        date_posted = _map_date_posted(self._search.get("max_days_old", 0))
        if date_posted is not None:
            params["date_posted"] = date_posted

        headers: dict[str, str] = {
            "X-RapidAPI-Key": self._api_key,
            "X-RapidAPI-Host": _JSEARCH_HOST,
        }

        backoff_delays = [2, 4, 8]
        response: requests.Response | None = None

        for attempt, delay in enumerate([0] + backoff_delays):
            if delay:
                logger.warning(
                    "Rate-limited by JSearch (429); retrying in %d s (attempt %d/3)",
                    delay,
                    attempt,
                )
                time.sleep(delay)

            try:
                response = requests.get(
                    _JSEARCH_URL, headers=headers, params=params, timeout=20
                )
            except requests.RequestException as exc:
                logger.warning("JSearch request failed: %s", exc)
                return []

            if response.status_code == 200:
                break
            if response.status_code == 429:
                if attempt < len(backoff_delays):
                    continue
                logger.warning(
                    "JSearch rate limit not resolved after %d retries; page %d skipped",
                    len(backoff_delays),
                    page,
                )
                return []
            else:
                logger.warning(
                    "JSearch returned HTTP %d for page %d; skipping",
                    response.status_code,
                    page,
                )
                return []

        if response is None:
            return []

        try:
            data = response.json()
        except ValueError as exc:
            logger.warning("JSearch response is not valid JSON: %s", exc)
            return []

        # Guard against HTTP 200 error envelopes from RapidAPI.
        if data.get("status") != "OK":
            logger.warning(
                "JSearch response status is not 'OK' (got %r); page %d skipped",
                data.get("status"),
                page,
            )
            return []

        return [self.normalise(job) for job in data.get("data", [])]

    def total_pages(self) -> int:
        """Return the configured maximum number of pages.

        JSearch does not expose a total-result count in its response, so the
        configured ``max_pages`` value is used as the upper bound — identical
        to the Adzuna pattern.

        Returns:
            ``search.max_pages`` from the config dict.
        """
        return self._search["max_pages"]

    def normalise(self, raw: dict) -> dict:
        """Map a JSearch listing dict to the canonical listing schema.

        Location is assembled from structured city/state/country fields when
        available, falling back to ``job_location``.  Salary fields are numeric
        and require no parsing.  ``skip_scrape`` is always ``True`` because
        JSearch provides the full description in the API response and apply
        links point to ATS portals that do not yield useful scraped content.

        Args:
            raw: A single entry from the JSearch ``data`` array.

        Returns:
            Dict conforming to the canonical listing schema defined in
            ``job_sources.base``.
        """
        # Assemble location from structured parts; fall back to job_location.
        location_parts = [
            raw.get("job_city") or "",
            raw.get("job_state") or "",
            raw.get("job_country") or "",
        ]
        location = ", ".join(p for p in location_parts if p)
        if not location:
            location = raw.get("job_location", "") or ""

        redirect_url = (
            raw.get("job_apply_link") or raw.get("job_google_link") or ""
        )

        return {
            "source": self.SOURCE,
            "source_id": str(raw.get("job_id", "")),
            "title": raw.get("job_title", "") or "",
            "company": raw.get("employer_name", "") or "",
            "location": location,
            "salary_min": raw.get("job_min_salary"),
            "salary_max": raw.get("job_max_salary"),
            "salary_period": _normalise_salary_period(raw.get("job_salary_period")),
            "contract_type": None,  # JSearch does not expose permanent/contract distinction
            "contract_time": _normalise_contract_time(raw.get("job_employment_type")),
            "description": raw.get("job_description", "") or "",
            "redirect_url": redirect_url,
            "created_at": raw.get("job_posted_at_datetime_utc") or None,
            "skip_scrape": True,  # Full description provided; apply links are ATS portals
        }
