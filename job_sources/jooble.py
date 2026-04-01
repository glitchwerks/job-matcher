"""
job_sources/jooble.py — Jooble API implementation of the JobSource protocol.

Wraps the Jooble job-search API (https://jooble.org/api/{api_key}):
page-number pagination, HTML stripping from snippets, best-effort salary
parsing from free-text, and normalisation to the canonical listing schema.

Config keys (under ``config["jooble"]``):
    api_key          str  — Jooble API key (required)
    keywords         str  — search keywords (default: "software engineer")
    location         str  — location filter (default: "")
    results_per_page int  — controls page size passed to the API (default: 20)
    max_pages        int  — upper cap on pages fetched per run (default: 5)
"""

from __future__ import annotations

import logging
import math
from typing import Iterator

import requests

from .base import JobSource
from .utils import parse_salary, strip_html

logger = logging.getLogger("ingest.jooble")

_JOOBLE_BASE_URL = "https://jooble.org/api/{api_key}"

# Mapping of Jooble contract type strings to canonical values.
_CONTRACT_TIME_MAP: dict[str, str] = {
    "full-time": "full_time",
    "part-time": "part_time",
    "contract": "contract",
}


def _normalise_contract_time(raw_type: str) -> str:
    """Map a Jooble job type string to the canonical contract_time value.

    Known values such as ``"Full-time"`` and ``"Part-time"`` are mapped
    to their canonical equivalents.  Unmapped values are passed through
    unchanged so that the prefilter can still reject or accept them.

    Args:
        raw_type: Raw type string from the Jooble API (e.g. ``"Full-time"``).

    Returns:
        Canonical contract_time string, or the original value if no mapping
        exists.
    """
    return _CONTRACT_TIME_MAP.get(raw_type.lower(), raw_type)


class JoobleClient(JobSource):
    """JobSource implementation for the Jooble job-search API.

    Jooble uses page-number pagination with a POST body.  ``total_pages()``
    fetches page 1 to read ``totalCount`` and computes the page ceiling.
    Results are capped at ``max_pages`` (default 5) to avoid excessive API
    usage.

    The first-page response from ``total_pages()`` is cached so that
    ``pages()`` does not repeat the same HTTP request.
    """

    SOURCE = "jooble"

    def __init__(self, config: dict) -> None:
        """Extract Jooble credentials and search params from config.

        Args:
            config: Full config dict.  Must contain a ``"jooble"`` sub-dict
                    with at least ``api_key``.

        Raises:
            ValueError: If ``config["jooble"]`` is absent or ``api_key`` is
                        missing within it.
        """
        jooble_cfg: dict | None = config.get("jooble")
        if not jooble_cfg:
            raise ValueError(
                "Jooble config block is absent. "
                "Add a 'jooble' section to config.json with 'api_key'."
            )

        api_key: str | None = jooble_cfg.get("api_key")
        if not api_key:
            raise ValueError(
                "Jooble 'api_key' is required but missing from config['jooble']."
            )

        self._api_key: str = api_key
        self._keywords: str = jooble_cfg.get("keywords", "software engineer")
        self._location: str = jooble_cfg.get("location", "")
        self._results_per_page: int = max(1, int(jooble_cfg.get("results_per_page", 20)))
        self._max_pages: int = max(1, int(jooble_cfg.get("max_pages", 5)))
        self._url: str = _JOOBLE_BASE_URL.format(api_key=self._api_key)

        # Cache for total_pages() / first-page results to avoid duplicate requests.
        self._cached_total_pages: int | None = None
        self._cached_first_page: list[dict] | None = None  # raw jobs from page 1

    # ------------------------------------------------------------------
    # JobSource interface
    # ------------------------------------------------------------------

    @classmethod
    def settings_schema(cls) -> dict:
        """Return the settings schema for Jooble.

        Jooble requires an API key obtained from https://jooble.org/api/about.

        Returns:
            Schema dict with ``display_name`` and a ``fields`` list containing
            the required ``api_key`` field.
        """
        return {
            "display_name": "Jooble",
            "description": "Aggregates listings from hundreds of boards worldwide. Free API key required (register at jooble.org). Broad coverage; description quality varies.",
            "fields": [
                {
                    "name": "api_key",
                    "label": "API Key",
                    "type": "password",
                    "required": True,
                },
            ],
        }

    def fetch_page(self, page: int) -> list[dict]:
        """Fetch and normalise a single page of Jooble listings.

        On any non-200 HTTP status or network/JSON error the method logs a
        warning and returns an empty list so the caller can continue without
        crashing.

        Args:
            page: 1-based page number.

        Returns:
            List of normalised listing dicts (via ``normalise()``).
            Returns ``[]`` on any error.
        """
        payload: dict[str, str | int] = {
            "keywords": self._keywords,
            "location": self._location,
            "page": page,
        }

        try:
            response = requests.post(self._url, json=payload, timeout=15)
            response.raise_for_status()
            data = response.json()
        except requests.RequestException as exc:
            logger.warning("Jooble request failed (page %d): %s", page, exc)
            return []
        except ValueError as exc:
            logger.warning("Jooble response is not valid JSON (page %d): %s", page, exc)
            return []

        raw_jobs: list[dict] = data.get("jobs", [])
        return [self.normalise(job) for job in raw_jobs]

    def total_pages(self) -> int:
        """Return the number of available pages, capped at ``max_pages``.

        Fetches page 1 on the first call to read ``totalCount``.  The result
        is cached for the lifetime of the instance so subsequent calls do not
        make additional HTTP requests.  The raw page-1 jobs are also cached
        so that ``pages()`` can reuse them without a second request.

        Returns:
            ``math.ceil(totalCount / results_per_page)``, capped at
            ``max_pages``.  Returns ``1`` as a safe fallback on any error.
        """
        if self._cached_total_pages is not None:
            return self._cached_total_pages

        payload: dict[str, str | int] = {
            "keywords": self._keywords,
            "location": self._location,
            "page": 1,
        }

        try:
            response = requests.post(self._url, json=payload, timeout=15)
            response.raise_for_status()
            data = response.json()
        except (requests.RequestException, ValueError) as exc:
            logger.warning("Jooble total_pages() request failed: %s", exc)
            self._cached_total_pages = 1
            return 1

        # Cache raw page-1 results so pages() doesn't re-fetch.
        self._cached_first_page = data.get("jobs", [])

        total_count: int = 0
        try:
            total_count = int(data.get("totalCount", 0))
        except (TypeError, ValueError):
            total_count = 0

        if total_count <= 0:
            self._cached_total_pages = 1
            return 1

        pages = math.ceil(total_count / self._results_per_page)
        self._cached_total_pages = min(pages, self._max_pages)
        return self._cached_total_pages

    def pages(self) -> Iterator[list[dict]]:
        """Yield normalised listing lists, one per page.

        Reuses the page-1 response cached by ``total_pages()`` to avoid a
        duplicate API call, then iterates from page 2 up to ``total_pages()``
        (inclusive).  Stops early if a page returns zero results.

        Yields:
            Lists of normalised listing dicts (after ``normalise()``).
        """
        total = self.total_pages()  # populates _cached_first_page

        if self._cached_first_page is not None:
            yield [self.normalise(r) for r in self._cached_first_page]
            start_page = 2
        else:
            start_page = 1

        for page in range(start_page, total + 1):
            results = self.fetch_page(page)
            if not results:
                logger.info("Jooble page %d returned 0 results; stopping early", page)
                return
            yield results

    def normalise(self, raw: dict) -> dict:
        """Map a Jooble listing dict to the canonical listing schema.

        HTML is stripped from the ``snippet`` field.  Salary is parsed
        best-effort from the free-text ``salary`` field; ``salary_period``
        is always ``None`` because the period cannot be reliably determined
        from the Jooble API.  The ``type`` field is mapped to the canonical
        ``contract_time`` value where possible.

        Args:
            raw: A single entry from the Jooble ``jobs`` array.

        Returns:
            Dict conforming to the canonical listing schema defined in
            ``job_sources.base``.
        """
        salary_min, salary_max = parse_salary(raw.get("salary") or "")

        raw_type: str = raw.get("type", "") or ""
        contract_time: str = _normalise_contract_time(raw_type) if raw_type else ""

        return {
            "source": self.SOURCE,
            "source_id": str(raw.get("id", "")),
            "title": raw.get("title", "") or "",
            "company": raw.get("company", "") or "",
            "location": raw.get("location", "") or "",
            "salary_min": salary_min,
            "salary_max": salary_max,
            "salary_period": None,  # Jooble salary is free-text; period cannot be reliably inferred
            "contract_type": None,
            "contract_time": contract_time,
            "description": strip_html(raw.get("snippet", "") or ""),
            "redirect_url": raw.get("link", "") or "",
            "created_at": raw.get("updated", "") or "",
        }
