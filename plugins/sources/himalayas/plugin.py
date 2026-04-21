"""
plugins/sources/himalayas/plugin.py — Himalayas API implementation of the JobSource protocol.

Wraps the Himalayas Jobs REST API: offset-based pagination, HTML stripping,
and normalisation to the canonical listing schema.

API: GET https://himalayas.app/jobs/api?limit=<n>&offset=<n>
Response: {"jobs": [...], "total": N}
"""

from __future__ import annotations

import logging
from datetime import datetime, UTC
from math import ceil
from typing import Iterator, Optional

import requests
from bs4 import BeautifulSoup

from job_sources.base import JobSource

logger = logging.getLogger("ingest.himalayas")

_HIMALAYAS_URL = "https://himalayas.app/jobs/api"

_JOB_TYPE_MAP: dict[str, str] = {
    # Underscore-separated variants (standard API form)
    "FULL_TIME": "full_time",
    "PART_TIME": "part_time",
    "CONTRACT": "contract",
    "FREELANCE": "freelance",
    "INTERNSHIP": "internship",
    # Space-separated variants (issue #239 — some API responses use spaces)
    "FULL TIME": "full_time",
    "PART TIME": "part_time",
    # "CONTRACT" and "FREELANCE" are single-word; no space variant needed.
    # "INTERNSHIP" is also single-word.
}


def _strip_html(text: str) -> str:
    """Strip HTML tags from *text* using BeautifulSoup.

    If no HTML tags are detected the input is returned unchanged so that
    plain-text and Markdown descriptions are not mangled.

    Args:
        text: Raw description string, possibly containing HTML markup.

    Returns:
        Plain-text string with HTML tags removed, or the original string
        if no tags were present.
    """
    if "<" not in text:
        return text
    return BeautifulSoup(text, "html.parser").get_text(separator=" ", strip=True)


def _parse_created_at(value: Optional[int | str]) -> Optional[str]:
    """Convert a Himalayas ``pubDate`` value to an ISO 8601 string.

    Himalayas returns ``pubDate`` as either an ISO 8601 string or a Unix
    timestamp integer.  The integer may be in **seconds** (10-digit, e.g.
    ``1_775_944_840``) or **milliseconds** (13-digit, e.g.
    ``1_700_000_000_000``).  Values below ``10_000_000_000`` are treated as
    seconds; values at or above that threshold are treated as milliseconds.
    ``None`` is passed through as ``None``.

    Args:
        value: ISO 8601 string, Unix seconds int, Unix milliseconds int,
               or ``None``.

    Returns:
        ISO 8601 string (e.g. ``"2026-01-02T12:34:56Z"``) or ``None``.
    """
    if value is None:
        return None
    if isinstance(value, int):
        # Distinguish Unix seconds from Unix milliseconds by magnitude.
        # Seconds-range timestamps (year ~2001–2286) are < 10_000_000_000.
        # Milliseconds-range timestamps for the same period are >= 10^12.
        _MS_THRESHOLD = 10_000_000_000
        ts_seconds = value / 1000 if value >= _MS_THRESHOLD else value
        return datetime.fromtimestamp(ts_seconds, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    # Assume string — pass through unchanged.
    return str(value)


def _map_job_type(job_type: Optional[str]) -> Optional[str]:
    """Map a Himalayas ``jobType`` value to the canonical ``contract_time`` string.

    Known values are mapped via ``_JOB_TYPE_MAP`` (which covers both the
    standard underscore form ``"FULL_TIME"`` and the space-separated form
    ``"FULL TIME"`` seen in some API responses — issue #239).  Anything else
    is lower-cased and has spaces replaced with underscores so that future
    API values degrade gracefully to a usable snake_case token rather than
    producing a string with spaces that fails downstream normalization.

    Args:
        job_type: Himalayas job type string, e.g. ``"FULL_TIME"`` or
                  ``"Full Time"``.

    Returns:
        Canonical contract-time string, or ``None`` if *job_type* is falsy.
    """
    if not job_type:
        return None
    return _JOB_TYPE_MAP.get(job_type, job_type.lower().replace(" ", "_"))


class HimalayasClient(JobSource):
    """JobSource implementation for the Himalayas Jobs REST API.

    Handles offset-based pagination, HTML description stripping, and
    normalisation of raw Himalayas response dicts to the canonical listing
    schema.
    """

    SOURCE = "himalayas"

    def __init__(self, config: dict, credentials: dict | None = None) -> None:
        """Store pagination settings from config.

        Args:
            config:      Full config dict. Reads ``config["himalayas"]["limit"]``
                         (default 100, max 100) to control page size.
            credentials: Unused — Himalayas requires no credentials.  Accepted
                         so the factory can pass ``credentials=src_cfg`` uniformly.
        """
        himalayas_cfg = config.get("himalayas") or {}
        self._limit: int = int(himalayas_cfg.get("limit", 100))
        self._total: Optional[int] = None  # cached after first API call

    # ------------------------------------------------------------------
    # JobSource interface
    # ------------------------------------------------------------------

    def fetch_page(self, page: int) -> list[dict]:
        """Fetch a single page of raw Himalayas listings.

        Converts the 1-based *page* argument to an offset
        (``offset = (page - 1) * limit``) before calling the API.

        Any non-200 HTTP response or network error is logged and returns an
        empty list.  The ``total`` field in the response is cached so that
        ``total_pages()`` can be called after the first ``fetch_page()``
        without a separate request.

        Args:
            page: 1-based page number.

        Returns:
            List of normalised listing dicts (via ``normalise()``).
        """
        offset = (page - 1) * self._limit
        params: dict[str, int] = {"limit": self._limit, "offset": offset}

        try:
            response = requests.get(_HIMALAYAS_URL, params=params, timeout=15)
        except requests.RequestException as exc:
            logger.warning("Himalayas request failed: %s", exc)
            return []

        if response.status_code != 200:
            logger.warning(
                "Himalayas returned HTTP %d for page %d (offset %d); skipping",
                response.status_code,
                page,
                offset,
            )
            return []

        try:
            data = response.json()
        except ValueError as exc:
            logger.warning("Himalayas response is not valid JSON: %s", exc)
            return []

        # Cache total so total_pages() can use it without a second request.
        if "total" in data:
            self._total = int(data["total"])

        raw_jobs: list[dict] = data.get("jobs", [])
        if not raw_jobs:
            return []

        results: list[dict] = []
        for raw in raw_jobs:
            listing = self.normalise(raw)
            if not listing.get("redirect_url"):
                title = listing.get("title") or ""
                source_id = raw.get("guid") or ""
                identifier = (
                    f"{title} (ID: {source_id})" if title and source_id
                    else title or source_id or "<unknown>"
                )
                logger.warning(
                    "Himalayas: skipping listing with no redirect_url — %s", identifier
                )
                continue
            results.append(listing)
        return results

    def total_pages(self) -> int:
        """Return the total number of pages for the current search.

        If the total has already been fetched (via a prior ``fetch_page()``
        call), it is used directly.  Otherwise a lightweight request is made
        to the API (offset=0, same limit) to obtain the ``total`` field.

        Returns:
            ``ceil(total / limit)``.
        """
        if self._total is not None:
            return ceil(self._total / self._limit)

        # Fetch the first page solely to read the total count.
        params: dict[str, int] = {"limit": self._limit, "offset": 0}
        try:
            response = requests.get(_HIMALAYAS_URL, params=params, timeout=15)
            response.raise_for_status()
            data = response.json()
            self._total = int(data.get("total", 0))
        except (requests.RequestException, ValueError, KeyError) as exc:
            logger.warning("Himalayas total_pages() request failed: %s", exc)
            return 1

        return ceil(self._total / self._limit) if self._total else 1

    def pages(self) -> Iterator[list[dict]]:
        """Yield normalised listing lists, one per page.

        Iterates from page 1 up to ``total_pages()`` (inclusive).  Stops
        early if a page returns zero results.

        Yields:
            Lists of normalised listing dicts.
        """
        for page in range(1, self.total_pages() + 1):
            results = self.fetch_page(page)
            if not results:
                logger.info("Himalayas page %d returned 0 results; stopping early", page)
                return
            yield results

    def normalise(self, raw: dict) -> dict:
        """Map a Himalayas job dict to the canonical listing schema.

        Args:
            raw: A single entry from the Himalayas ``jobs`` array.

        Returns:
            Dict conforming to the canonical listing schema defined in
            ``job_sources.base``.
        """
        location_restrictions: list[str] = raw.get("locationRestrictions") or []
        location = ", ".join(location_restrictions) if location_restrictions else "Worldwide"

        description_raw: str = raw.get("description") or ""
        description = _strip_html(description_raw) if description_raw else ""

        return {
            "source": self.SOURCE,
            "source_id": str(raw.get("guid", "")),
            "title": raw.get("title", "") or "",
            "company": raw.get("companyName", "") or "",
            "location": location,
            "salary_min": raw.get("minSalary"),
            "salary_max": raw.get("maxSalary"),
            "salary_period": None,  # Himalayas API does not expose a pay-period field
            "contract_type": None,
            "contract_time": _map_job_type(raw.get("employmentType")),
            "description": description,
            "redirect_url": raw.get("applicationLink") or "",
            "created_at": _parse_created_at(raw.get("pubDate")),
        }
