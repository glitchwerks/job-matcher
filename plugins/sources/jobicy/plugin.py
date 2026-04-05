"""
plugins/sources/jobicy/plugin.py — Jobicy API implementation of the JobSource protocol.

Wraps the Jobicy remote jobs API (https://jobicy.com/api/v2/remote-jobs).
Single-page response; no API key required.  HTML stripped from descriptions;
salary mapped from structured annual min/max fields.
"""

from __future__ import annotations

import logging
from typing import Iterator

import requests

from job_sources.base import JobSource
from job_sources.utils import strip_html

logger = logging.getLogger("ingest.jobicy")

_JOBICY_URL = "https://jobicy.com/api/v2/remote-jobs"


class JobicyClient(JobSource):
    """JobSource implementation for the Jobicy remote jobs API.

    The Jobicy API is single-page (no pagination). ``total_pages()`` always
    returns 1.  No API key is required.
    """

    SOURCE = "jobicy"

    def __init__(self, config: dict, credentials: dict | None = None) -> None:
        """Read Jobicy-specific settings from config.

        Args:
            config:      Full config dict.  Reads ``config["jobicy"]["tag"]``
                         (default ``"software engineer"``), ``config["jobicy"]["geo"]``
                         (default ``"usa"``), and ``config["jobicy"]["count"]``
                         (default ``50``).  The ``"jobicy"`` key is optional — if
                         absent, defaults are used.
            credentials: Unused — Jobicy requires no credentials.  Accepted
                         so the factory can pass ``credentials=src_cfg`` uniformly.
        """
        jobicy_cfg: dict = config.get("jobicy") or {}
        self._tag: str = jobicy_cfg.get("tag", "software engineer")
        self._geo: str = jobicy_cfg.get("geo", "usa")
        self._count: int = max(1, min(int(jobicy_cfg.get("count", 50)), 100))

    # ------------------------------------------------------------------
    # JobSource interface
    # ------------------------------------------------------------------

    def fetch_page(self, page: int) -> list[dict]:
        """Fetch listings from the Jobicy API.

        Jobicy returns a single page of results regardless of the ``page``
        argument.  Any non-1 ``page`` value is ignored because the API does
        not support pagination.

        Args:
            page: 1-based page number (only page 1 is meaningful here).

        Returns:
            List of normalised listing dicts (via ``normalise()``), or an
            empty list on any error.
        """
        params: dict[str, str | int] = {
            "count": self._count,
            "geo": self._geo,
            "tag": self._tag,
        }

        try:
            response = requests.get(_JOBICY_URL, params=params, timeout=15)
        except requests.RequestException as exc:
            logger.warning("Jobicy request failed: %s", exc)
            return []

        if response.status_code != 200:
            logger.warning(
                "Jobicy returned HTTP %d; skipping", response.status_code
            )
            return []

        try:
            data = response.json()
        except ValueError as exc:
            logger.warning("Jobicy response is not valid JSON: %s", exc)
            return []

        raw_jobs: list[dict] = data.get("jobs", [])
        if not raw_jobs:
            return []

        return [self.normalise(job) for job in raw_jobs]

    def total_pages(self) -> int:
        """Return 1 — Jobicy API is single-page.

        Returns:
            Always ``1``.
        """
        return 1

    def pages(self) -> Iterator[list[dict]]:
        """Yield the single page of Jobicy listings.

        Jobicy is a single-page API, so this yields at most one list.
        Yields nothing if the fetch returns no results.

        Yields:
            A single list of normalised listing dicts.
        """
        results = self.fetch_page(1)
        if results:
            yield results

    def normalise(self, raw: dict) -> dict:
        """Map a Jobicy job dict to the canonical listing schema.

        Salary is populated from the structured ``annualSalaryMin`` /
        ``annualSalaryMax`` fields when either is non-null; ``salary_period``
        is set to ``"annual"`` in that case.

        Args:
            raw: A single entry from the Jobicy ``jobs`` array.

        Returns:
            Dict conforming to the canonical listing schema defined in
            ``job_sources.base``.
        """
        salary_min_raw = raw.get("annualSalaryMin")
        salary_max_raw = raw.get("annualSalaryMax")

        salary_min: float | None = None
        salary_max: float | None = None
        salary_period: str | None = None

        if salary_min_raw is not None or salary_max_raw is not None:
            salary_period = "annual"
            try:
                salary_min = float(salary_min_raw) if salary_min_raw is not None else None
            except (TypeError, ValueError):
                salary_min = None
            try:
                salary_max = float(salary_max_raw) if salary_max_raw is not None else None
            except (TypeError, ValueError):
                salary_max = None

        return {
            "source": self.SOURCE,
            "source_id": str(raw.get("id", "")),
            "title": raw.get("jobTitle", "") or "",
            "company": raw.get("companyName", "") or "",
            "location": raw.get("jobGeo", "") or "",
            "salary_min": salary_min,
            "salary_max": salary_max,
            "salary_period": salary_period,
            "contract_type": None,
            "contract_time": raw.get("jobType", "") or "",
            "description": strip_html(raw.get("jobDescription", "") or ""),
            "redirect_url": raw.get("url", "") or "",
            "created_at": raw.get("pubDate", "") or "",
        }
