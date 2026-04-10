"""
plugins/sources/jsearch/plugin.py — JSearch (RapidAPI) implementation of the JobSource protocol.

Wraps the JSearch API (https://jsearch.p.rapidapi.com/search), which aggregates
job listings from Google for Jobs and returns full plaintext descriptions in the
API response — no scraping step required.

Free tier: 200 requests/month.  Each call to ``fetch_page()`` makes **two** API
requests: one local query (``"{what} in {where}"`` with a ``radius`` refinement)
and one remote-only query (``remote_jobs_only=true``).  At the configured default
of ``max_pages=5``, each run makes 10 API calls — roughly 20 runs/month on the
free tier.  Consider reducing ``max_pages`` to 3 for more conservative usage (~30
runs/month).
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

# Reverse mapping: canonical contract_time → JSearch employment_types API value.
# Used to translate prefilter.require_contract_time back to the native filter param.
_REVERSE_CONTRACT_TIME_MAP: dict[str, str] = {
    "full_time": "FULLTIME",
    "part_time": "PARTTIME",
    "contract": "CONTRACTOR",
    "intern": "INTERN",
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
        self._prefilter: dict = config.get("prefilter") or {}

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

        Makes **two** API calls per invocation to capture both local and remote
        results:

        1. **Local query** — ``query="{what} in {where}"`` with a ``radius``
           parameter (from ``config["search"]["distance"]``, in km).  Skipped
           when ``where`` is not configured.
        2. **Remote query** — ``query="{what}"`` with ``remote_jobs_only=true``.

        Results from both calls are combined and deduplicated by ``job_id``
        before being normalised.  With ``max_pages=5`` the default, that is
        10 API calls per run — roughly 20 runs/month on the 200-req/month free
        tier.

        Optional native filters added when configured:

        * ``radius`` — added to the local query when
          ``config["search"]["distance"]`` is non-zero.
        * ``employment_types`` — added to both queries when
          ``config["prefilter"]["require_contract_time"]`` is set and maps to a
          known JSearch employment type (via ``_REVERSE_CONTRACT_TIME_MAP``).
        * ``date_posted`` — added to both queries when
          ``config["search"]["max_days_old"]`` is non-zero.

        On HTTP 429 retries up to three times with exponential back-off
        (2 s, 4 s, 8 s) per individual request, identical to the Adzuna
        implementation.  Any other non-200 response, network error, or bad JSON
        is logged and that sub-query returns an empty list; results from the
        other sub-query are still returned.

        An additional envelope check is performed: if the HTTP status is 200
        but the response body contains ``status != "OK"`` (a RapidAPI error
        envelope pattern), the method logs a warning and treats that sub-query
        as returning ``[]``.

        Args:
            page: 1-based page number.

        Returns:
            List of normalised listing dicts (via ``normalise()``),
            deduplicated by ``job_id``.  Returns ``[]`` on total failure.
        """
        what: str = self._search.get("what", "")
        where: str = self._search.get("where", "")
        distance: int = int(self._search.get("distance") or 0)

        date_posted = _map_date_posted(self._search.get("max_days_old", 0))

        # Resolve optional employment_types filter from prefilter config.
        require_contract_time: str | None = self._prefilter.get("require_contract_time")
        employment_types: str | None = (
            _REVERSE_CONTRACT_TIME_MAP.get(require_contract_time)
            if require_contract_time
            else None
        )

        headers: dict[str, str] = {
            "X-RapidAPI-Key": self._api_key,
            "X-RapidAPI-Host": _JSEARCH_HOST,
        }

        # --- Build base params shared by both queries ---
        base_params: dict[str, str | int] = {"page": page, "num_pages": 1}
        if date_posted is not None:
            base_params["date_posted"] = date_posted
        if employment_types is not None:
            base_params["employment_types"] = employment_types

        # --- Local query (geo-matched) ---
        local_raw: list[dict] = []
        if where:
            local_params = {
                **base_params,
                "query": f"{what} in {where}",
            }
            if distance:
                local_params["radius"] = distance
            local_raw = self._fetch_raw(local_params, headers, page, label="local")

        # --- Remote query ---
        remote_params = {
            **base_params,
            "query": what,
            "remote_jobs_only": "true",
        }
        remote_raw = self._fetch_raw(remote_params, headers, page, label="remote")

        # Deduplicate by job_id and normalise.
        seen_ids: set[str] = set()
        results: list[dict] = []
        for job in local_raw + remote_raw:
            job_id = str(job.get("job_id", ""))
            if job_id in seen_ids:
                continue
            seen_ids.add(job_id)
            results.append(self.normalise(job))
        return results

    def _fetch_raw(
        self,
        params: dict,
        headers: dict[str, str],
        page: int,
        label: str = "",
    ) -> list[dict]:
        """Execute a single API request with retry/back-off, returning raw job dicts.

        This is the low-level helper used by ``fetch_page()`` for each of its two
        sub-queries (local and remote).  Error handling mirrors the original
        single-request implementation: 429 triggers exponential back-off up to
        three retries; any other non-200 status, network error, or bad JSON
        returns ``[]`` after logging a warning.

        Args:
            params:  Query parameters dict to send with the request.
            headers: HTTP headers (API key, host) to send.
            page:    Page number, used only for log messages.
            label:   Short string identifying the sub-query type (``"local"`` or
                     ``"remote"``), used in log messages.

        Returns:
            Raw list of job dicts from the ``data`` key of the API response, or
            ``[]`` on any error.
        """
        prefix = f"JSearch [{label}]" if label else "JSearch"
        backoff_delays = [2, 4, 8]
        response: requests.Response | None = None

        for attempt, delay in enumerate([0] + backoff_delays):
            if delay:
                logger.warning(
                    "%s rate-limited (429); retrying in %d s (attempt %d/3)",
                    prefix,
                    delay,
                    attempt,
                )
                time.sleep(delay)

            try:
                response = requests.get(
                    _JSEARCH_URL, headers=headers, params=params, timeout=20
                )
            except requests.RequestException as exc:
                logger.warning("%s request failed: %s", prefix, exc)
                return []

            if response.status_code == 200:
                break
            if response.status_code == 429:
                if attempt < len(backoff_delays):
                    continue
                logger.warning(
                    "%s rate limit not resolved after %d retries; page %d skipped",
                    prefix,
                    len(backoff_delays),
                    page,
                )
                return []
            else:
                logger.warning(
                    "%s returned HTTP %d for page %d; skipping",
                    prefix,
                    response.status_code,
                    page,
                )
                return []

        if response is None:
            return []

        try:
            data = response.json()
        except ValueError as exc:
            logger.warning("%s response is not valid JSON: %s", prefix, exc)
            return []

        # Guard against HTTP 200 error envelopes from RapidAPI.
        if data.get("status") != "OK":
            logger.warning(
                "%s response status is not 'OK' (got %r); page %d skipped",
                prefix,
                data.get("status"),
                page,
            )
            return []

        return list(data.get("data", []))

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
            "skip_scrape": True,           # Apply links are ATS portals, not scrapable
            "description_is_full": True,   # API provides complete job descriptions
        }
