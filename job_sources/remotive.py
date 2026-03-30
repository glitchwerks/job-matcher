"""
job_sources/remotive.py — Remotive API implementation of the JobSource protocol.

Wraps the Remotive remote jobs API (https://remotive.com/api/remote-jobs).
Single-page response; no API key required. HTML stripped from descriptions;
salary parsed best-effort from free-text strings.
"""

from __future__ import annotations

import logging
import re
from typing import Iterator

import requests
from bs4 import BeautifulSoup

from .base import JobSource

logger = logging.getLogger("ingest.remotive")

_REMOTIVE_URL = "https://remotive.com/api/remote-jobs"

# Regex to find numbers with optional k-suffix, e.g. "80,000", "120k", "50K"
_SALARY_NUMBER_RE = re.compile(r"[\d,]+(?:\.\d+)?[kK]?")


def _parse_salary(raw: str) -> tuple[float | None, float | None]:
    """Parse a free-text salary string into (salary_min, salary_max).

    Handles patterns like:
      - "$80,000 - $120,000"
      - "€50k"
      - "100K-150K"
      - "" (empty → both None)

    Args:
        raw: Free-text salary string from the Remotive API.

    Returns:
        A (salary_min, salary_max) tuple of floats, or (None, None) if the
        string is empty or no numeric values can be extracted.
    """
    if not raw or not raw.strip():
        return None, None

    matches = _SALARY_NUMBER_RE.findall(raw)
    if not matches:
        return None, None

    values: list[float] = []
    for m in matches:
        # Strip commas used as thousands separators.
        cleaned = m.replace(",", "")
        lower = cleaned.lower()
        if lower.endswith("k"):
            try:
                values.append(float(lower[:-1]) * 1000)
            except ValueError:
                continue
        else:
            try:
                values.append(float(cleaned))
            except ValueError:
                continue

    if not values:
        return None, None

    salary_min = values[0]
    salary_max = values[1] if len(values) >= 2 else salary_min
    return salary_min, salary_max


def _strip_html(html: str) -> str:
    """Strip HTML tags from a string using BeautifulSoup.

    Args:
        html: HTML string to strip.

    Returns:
        Plain text with tags removed and whitespace normalised.
    """
    if not html:
        return ""
    return BeautifulSoup(html, "html.parser").get_text(separator=" ", strip=True)


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
        salary_min, salary_max = _parse_salary(raw.get("salary") or "")

        return {
            "source": self.SOURCE,
            "source_id": str(raw.get("id", "")),
            "title": raw.get("title", "") or "",
            "company": raw.get("company_name", "") or "",
            "location": raw.get("candidate_required_location", "") or "",
            "salary_min": salary_min,
            "salary_max": salary_max,
            "contract_type": None,
            "contract_time": raw.get("job_type", "") or "",
            "description": _strip_html(raw.get("description", "") or ""),
            "redirect_url": raw.get("url", "") or "",
            "created_at": raw.get("publication_date", "") or "",
        }
