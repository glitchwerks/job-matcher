"""
job_sources/the_muse.py — The Muse API implementation of the JobSource protocol.

Wraps The Muse public jobs API (https://www.themuse.com/api/public/jobs):
0-indexed pagination, optional API key, HTML stripping from the description
field, and normalisation to the canonical listing schema.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

import requests
from bs4 import BeautifulSoup

from .base import JobSource

logger = logging.getLogger("ingest.the_muse")

_THE_MUSE_BASE = "https://www.themuse.com/api/public/jobs"

_DEFAULT_CATEGORY = "Software Engineer"
_DEFAULT_RESULTS_PER_PAGE = 20


class TheMuseClient(JobSource):
    """JobSource implementation for The Muse public jobs API.

    Uses 0-indexed pagination (page 0 = first page).  The API key is optional
    for basic usage but reduces rate-limiting.  HTML markup in the ``contents``
    field is stripped to plain text before being stored as ``description``.
    """

    SOURCE = "the_muse"

    def __init__(self, config: dict) -> None:
        """Extract The Muse credentials and search parameters from config.

        Args:
            config: Full config dict.  Reads from the ``the_muse`` sub-dict
                    if present; all keys within that sub-dict are optional.
        """
        muse_cfg: dict = config.get("the_muse") or {}
        self._api_key: str | None = muse_cfg.get("api_key") or None
        self._category: str = muse_cfg.get("category") or _DEFAULT_CATEGORY
        self._results_per_page: int = int(
            muse_cfg.get("results_per_page") or _DEFAULT_RESULTS_PER_PAGE
        )
        # Cache the total page count after the first call to total_pages().
        self._page_count: int | None = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_params(self, page: int) -> dict:
        """Build the query-parameter dict for a single API request.

        Args:
            page: 0-based page number.

        Returns:
            Dict of query parameters suitable for ``requests.get(params=...)``.
        """
        params: dict[str, str | int] = {
            "category": self._category,
            "page": page,
            "results_per_page": self._results_per_page,
        }
        if self._api_key:
            params["api_key"] = self._api_key
        return params

    @staticmethod
    def _strip_html(html: str | None) -> str:
        """Convert an HTML string to plain text.

        Uses BeautifulSoup with the built-in ``html.parser`` so that no
        external parser (lxml, html5lib) is required.

        Args:
            html: Raw HTML string, or ``None`` / empty.

        Returns:
            Plain-text string with leading/trailing whitespace removed.
            Returns an empty string when *html* is falsy.
        """
        if not html:
            return ""
        return BeautifulSoup(html, "html.parser").get_text(separator=" ", strip=True)

    def _get_page(self, page: int) -> dict:
        """Perform a single GET request and return the parsed JSON body.

        Args:
            page: 0-based page number.

        Returns:
            Parsed JSON dict.  Returns an empty dict on any error (network
            failure, non-200 status, or invalid JSON).
        """
        params = self._build_params(page)
        try:
            response = requests.get(_THE_MUSE_BASE, params=params, timeout=15)
        except requests.RequestException as exc:
            logger.warning("The Muse request failed: %s", exc)
            return {}

        if response.status_code != 200:
            logger.warning(
                "The Muse returned HTTP %d for page %d; skipping",
                response.status_code,
                page,
            )
            return {}

        try:
            return response.json()
        except ValueError as exc:
            logger.warning("The Muse response is not valid JSON: %s", exc)
            return {}

    # ------------------------------------------------------------------
    # JobSource interface
    # ------------------------------------------------------------------

    @classmethod
    def settings_schema(cls) -> dict:
        """Return the settings schema for The Muse.

        The Muse's public API is key-free; an optional key exists for
        higher rate limits but is not required for basic ingestion.

        Returns:
            Schema dict with ``display_name`` and an empty ``fields`` list.
        """
        return {
            "display_name": "The Muse",
            "home_url": "https://www.themuse.com",
            "fields": [],
        }

    def fetch_page(self, page: int) -> list[dict]:
        """Fetch a single page of raw The Muse results.

        The Muse API uses 0-indexed pagination internally, but this method
        follows the ``JobSource`` interface convention: ``page=1`` returns
        the first page.  Each raw result is passed through ``normalise()``
        before being returned so that callers always receive canonical
        listing dicts.

        Args:
            page: 1-based page number.

        Returns:
            List of normalised listing dicts.  Returns an empty list on any
            HTTP or parsing error, or when the page contains no results.
        """
        api_page = page - 1
        data = self._get_page(api_page)
        raw_results: list[dict] = data.get("results", [])
        if not raw_results:
            return []
        return [self.normalise(r) for r in raw_results]

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

    def total_pages(self) -> int:
        """Return the total number of result pages for the current query.

        Calls page 0 of the API and reads the ``page_count`` field from the
        response.  The result is cached so that subsequent calls do not make
        additional HTTP requests.

        Returns:
            Total page count as reported by the API.  Returns 0 if the
            initial request fails.
        """
        if self._page_count is not None:
            return self._page_count

        data = self._get_page(0)
        self._page_count = int(data.get("page_count", 0))
        return self._page_count

    def normalise(self, raw: dict) -> dict:
        """Map a raw The Muse listing dict to the canonical listing schema.

        The Muse does not expose salary information, so ``salary_min``,
        ``salary_max``, and ``contract_type`` are always ``None``.  HTML
        markup in the ``contents`` field is stripped to plain text.

        Args:
            raw: A single entry from The Muse ``results`` array.

        Returns:
            Dict conforming to the canonical listing schema defined in
            ``job_sources.base``.
        """
        # Nested objects — guard against None / missing keys.
        company: str = (raw.get("company") or {}).get("name") or ""
        locations: list = raw.get("locations") or []
        location: str | None = locations[0].get("name") if locations else None

        return {
            "source": self.SOURCE,
            "source_id": str(raw.get("id", "")),
            "title": raw.get("name") or "",
            "company": company,
            "location": location,
            "salary_min": None,
            "salary_max": None,
            "salary_period": None,  # The Muse does not expose salary data
            "contract_type": None,
            "contract_time": raw.get("type") or None,
            "description": self._strip_html(raw.get("contents")),
            "redirect_url": (raw.get("refs") or {}).get("landing_page") or "",
            "created_at": raw.get("publication_date") or None,
        }
