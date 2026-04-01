"""
job_sources/remotive.py — Remotive API implementation of the JobSource protocol.

Wraps the Remotive remote jobs API (https://remotive.com/api/remote-jobs).
Single-page response; no API key required. HTML stripped from descriptions;
salary parsed best-effort from free-text strings.
"""

from __future__ import annotations

import logging
from typing import Iterator

import requests

from .base import JobSource
from .utils import parse_salary, strip_html

logger = logging.getLogger("ingest.remotive")

_REMOTIVE_URL = "https://remotive.com/api/remote-jobs"


class RemotiveClient(JobSource):
    """JobSource implementation for the Remotive remote jobs API.

    The Remotive API is single-page (no pagination). ``total_pages()`` always
    returns 1. No API key is required.
    """

    SOURCE = "remotive"

    def __init__(self, config: dict) -> None:
        """Read Remotive-specific settings from config.

        Args:
            config: Full config dict. Reads ``config["remotive"]["category"]``
                    (default ``"software-dev"``) and ``config["remotive"]["limit"]``
                    (default 100). The ``"remotive"`` key is optional — if absent,
                    defaults are used.
        """
        remotive_cfg = config.get("remotive") or {}
        self._category: str = remotive_cfg.get("category", "software-dev")
        self._limit: int = int(remotive_cfg.get("limit", 100))

    # ------------------------------------------------------------------
    # JobSource interface
    # ------------------------------------------------------------------

    @classmethod
    def settings_schema(cls) -> dict:
        """Return the settings schema for Remotive.

        Remotive requires no credentials — the public API is key-free.

        Returns:
            Schema dict with ``display_name`` and an empty ``fields`` list.
        """
        return {
            "display_name": "Remotive",
            "description": "Curated remote tech jobs across software, design, and marketing. Free API with no authentication. Smaller volume but strong signal-to-noise.",
            "home_url": "https://remotive.com",
            "fields": [],
        }

    def fetch_page(self, page: int) -> list[dict]:
        """Fetch listings from the Remotive API.

        Remotive returns a single page of results regardless of the ``page``
        argument. Any non-1 ``page`` value is ignored because the API does
        not support pagination.

        Args:
            page: 1-based page number (only page 1 is meaningful here).

        Returns:
            List of normalised listing dicts (via ``normalise()``), or an
            empty list on any error.
        """
        params: dict[str, str | int] = {
            "category": self._category,
            "limit": self._limit,
        }

        try:
            response = requests.get(_REMOTIVE_URL, params=params, timeout=15)
        except requests.RequestException as exc:
            logger.warning("Remotive request failed: %s", exc)
            return []

        if response.status_code != 200:
            logger.warning(
                "Remotive returned HTTP %d; skipping", response.status_code
            )
            return []

        try:
            data = response.json()
        except ValueError as exc:
            logger.warning("Remotive response is not valid JSON: %s", exc)
            return []

        raw_jobs: list[dict] = data.get("jobs", [])
        if not raw_jobs:
            return []

        return [self.normalise(job) for job in raw_jobs]

    def total_pages(self) -> int:
        """Return 1 — Remotive API is single-page.

        Returns:
            Always ``1``.
        """
        return 1

    def pages(self) -> Iterator[list[dict]]:
        """Yield the single page of Remotive listings.

        Remotive is a single-page API, so this yields at most one list.
        Yields nothing if the fetch returns no results.

        Yields:
            A single list of normalised listing dicts.
        """
        results = self.fetch_page(1)
        if results:
            yield results

    def normalise(self, raw: dict) -> dict:
        """Map a Remotive job dict to the canonical listing schema.

        Args:
            raw: A single entry from the Remotive ``jobs`` array.

        Returns:
            Dict conforming to the canonical listing schema defined in
            ``job_sources.base``.
        """
        salary_min, salary_max = parse_salary(raw.get("salary") or "")

        return {
            "source": self.SOURCE,
            "source_id": str(raw.get("id", "")),
            "title": raw.get("title", "") or "",
            "company": raw.get("company_name", "") or "",
            "location": raw.get("candidate_required_location", "") or "",
            "salary_min": salary_min,
            "salary_max": salary_max,
            "salary_period": None,  # Remotive salary is free-text; period cannot be reliably inferred
            "contract_type": None,
            "contract_time": raw.get("job_type", "") or "",
            "description": strip_html(raw.get("description", "") or ""),
            "redirect_url": raw.get("url", "") or "",
            "created_at": raw.get("publication_date", "") or "",
        }
