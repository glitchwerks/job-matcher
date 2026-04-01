"""
job_sources/adzuna.py — Adzuna API implementation of the JobSource protocol.

Wraps the Adzuna Jobs REST API: pagination, rate-limit retry, and
normalisation to the canonical listing schema.
"""

from __future__ import annotations

import logging
import time
from typing import Iterator

import requests

from .base import JobSource

logger = logging.getLogger("ingest.adzuna")

_ADZUNA_BASE = "https://api.adzuna.com/v1/api/jobs/{country}/search/{page}"


class AdzunaClient(JobSource):
    """JobSource implementation for the Adzuna Jobs REST API.

    Handles pagination, rate-limit retry with exponential back-off, and
    normalisation of raw Adzuna response dicts to the canonical listing schema.
    """

    SOURCE = "adzuna"

    def __init__(
        self,
        config: dict,
        app_id: str | None = None,
        app_key: str | None = None,
    ) -> None:
        """Extract credentials and search parameters from config.

        Reads ``adzuna_app_id`` and ``adzuna_app_key`` from *config* so that
        the factory can pass ``config=config`` uniformly for all sources.
        The optional *app_id* and *app_key* parameters are accepted for
        backward compatibility but config is the canonical source.

        Args:
            config:  Full config dict.  Must contain ``adzuna_app_id``,
                     ``adzuna_app_key``, and a ``search`` sub-dict.
            app_id:  Deprecated — pass credentials via config instead.
            app_key: Deprecated — pass credentials via config instead.
        """
        self._app_id: str = app_id if app_id is not None else config["adzuna_app_id"]
        self._app_key: str = app_key if app_key is not None else config["adzuna_app_key"]
        self._search = config["search"]

    # ------------------------------------------------------------------
    # JobSource interface
    # ------------------------------------------------------------------

    @classmethod
    def settings_schema(cls) -> dict:
        """Return the settings schema for Adzuna.

        Returns:
            Schema dict with ``display_name`` and credential ``fields``
            for the Adzuna App ID and App Key.
        """
        return {
            "display_name": "Adzuna",
            "home_url": "https://www.adzuna.com",
            "fields": [
                {
                    "name": "app_id",
                    "label": "App ID",
                    "type": "password",
                    "required": True,
                },
                {
                    "name": "app_key",
                    "label": "App Key",
                    "type": "password",
                    "required": True,
                },
            ],
        }

    def fetch_page(self, page: int) -> list[dict]:
        """Fetch a single page of raw Adzuna results.

        On HTTP 429 retries up to three times with exponential back-off
        (2 s, 4 s, 8 s).  Any other non-200 response is logged and returns
        an empty list.  Missing ``results`` key in the response also returns
        an empty list.

        Args:
            page: 1-based page number.

        Returns:
            List of normalised listing dicts (via ``normalise()``).
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

        return [self.normalise(r) for r in raw_results]

    def total_pages(self) -> int:
        """Return the configured maximum number of pages.

        Adzuna does not expose a total-page count without an additional API
        call, so the configured ``max_pages`` value is used as the upper bound.

        Returns:
            ``search.max_pages`` from the config dict.
        """
        return self._search["max_pages"]

    def normalise(self, raw: dict) -> dict:
        """Map an Adzuna result dict to the canonical listing schema.

        Args:
            raw: A single entry from the Adzuna ``results`` array.

        Returns:
            Dict conforming to the canonical listing schema defined in
            ``job_sources.base``.
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
            "source": self.SOURCE,
            "source_id": str(raw.get("id", "")),
            "title": raw.get("title", ""),
            "company": company_obj.get("display_name", "") if isinstance(company_obj, dict) else "",
            "location": location_obj.get("display_name", "") if isinstance(location_obj, dict) else "",
            "salary_min": raw.get("salary_min"),
            "salary_max": raw.get("salary_max"),
            "salary_period": None,  # Adzuna does not expose a pay-period field
            "salary_is_predicted": salary_is_predicted,
            "contract_type": raw.get("contract_type", "") or "",
            "contract_time": raw.get("contract_time", "") or "",
            "description": raw.get("description", "") or "",
            "redirect_url": raw.get("redirect_url", "") or "",
            "created_at": raw.get("created", "") or "",
            "posted_at": raw.get("created") or None,
        }

    # ------------------------------------------------------------------
    # Convenience iterator
    # ------------------------------------------------------------------

    def pages(self) -> Iterator[list[dict]]:
        """Yield normalised listing lists, one per page.

        Iterates from page 1 up to ``total_pages()`` (inclusive). Stops
        early if a page returns zero results.

        Yields:
            Lists of normalised listing dicts.
        """
        for page in range(1, self.total_pages() + 1):
            results = self.fetch_page(page)
            if not results:
                logger.info("Page %d returned 0 results; stopping early", page)
                return
            yield results
