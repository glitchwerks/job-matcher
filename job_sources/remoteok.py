"""
job_sources/remoteok.py — RemoteOK API implementation of the JobSource protocol.

RemoteOK exposes a single endpoint (https://remoteok.com/api) that returns all
current remote listings as one JSON array.  The first element in the array is an
API metadata object — it is skipped during normalisation.

No API key is required, but the server blocks requests that omit a ``User-Agent``
header.  The default user-agent string can be overridden via
``config["remoteok"]["user_agent"]``.
"""

from __future__ import annotations

import logging
from typing import Iterator

import requests
from bs4 import BeautifulSoup

from .base import JobSource

logger = logging.getLogger("ingest.remoteok")

_REMOTEOK_API = "https://remoteok.com/api"
_DEFAULT_USER_AGENT = "job-matcher-ui/1.0"


class RemoteOKClient(JobSource):
    """JobSource implementation for the RemoteOK jobs API.

    RemoteOK returns all listings in a single request so pagination is trivial:
    ``total_pages()`` always returns ``1`` and ``fetch_page()`` ignores the
    ``page`` argument.

    HTML is stripped from the ``description`` field using BeautifulSoup so that
    downstream scoring sees plain text.
    """

    SOURCE = "remoteok"

    def __init__(self, config: dict) -> None:
        """Initialise the client from the application config.

        Args:
            config: Full config dict.  If a ``"remoteok"`` sub-dict is present
                    its ``"user_agent"`` key is used; otherwise the default
                    user-agent string is used.
        """
        remoteok_cfg: dict = config.get("remoteok") or {}
        self._user_agent: str = remoteok_cfg.get("user_agent", _DEFAULT_USER_AGENT)
        self._raw_cache: list[dict] | None = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _fetch_all(self) -> list[dict]:
        """Fetch and cache the full RemoteOK listing array.

        Skips the first element of the response array (API metadata).

        Returns:
            List of raw job listing dicts.  Returns an empty list on any error.
        """
        if self._raw_cache is not None:
            return self._raw_cache

        headers = {"User-Agent": self._user_agent}
        try:
            response = requests.get(_REMOTEOK_API, headers=headers, timeout=15)
        except requests.RequestException as exc:
            logger.warning("RemoteOK request failed: %s", exc)
            return []

        if response.status_code != 200:
            logger.warning(
                "RemoteOK returned HTTP %d; skipping fetch", response.status_code
            )
            return []

        try:
            data: list = response.json()
        except ValueError as exc:
            logger.warning("RemoteOK response is not valid JSON: %s", exc)
            return []

        if not isinstance(data, list):
            logger.warning("RemoteOK response is not a JSON array; skipping")
            return []

        # Filter to items that look like job listings (have both "id" and "position").
        # This naturally skips the metadata object at data[0] which lacks these fields.
        raw_jobs = [
            item
            for item in data
            if isinstance(item, dict) and "id" in item and "position" in item
        ]

        self._raw_cache = [self.normalise(job) for job in raw_jobs]
        return self._raw_cache

    # ------------------------------------------------------------------
    # JobSource interface
    # ------------------------------------------------------------------

    @classmethod
    def settings_schema(cls) -> dict:
        """Return the settings schema for Remote OK.

        RemoteOK requires no credentials — the public API is key-free.

        Returns:
            Schema dict with ``display_name`` and an empty ``fields`` list.
        """
        return {
            "display_name": "Remote OK",
            "fields": [],
        }

    def fetch_page(self, page: int) -> list[dict]:
        """Fetch all RemoteOK listings (the API has no pagination).

        The ``page`` argument is accepted for interface compatibility but is
        ignored — RemoteOK returns all listings in a single request.

        Args:
            page: Ignored.

        Returns:
            List of raw job listing dicts.
        """
        return self._fetch_all()

    def total_pages(self) -> int:
        """Return the number of pages available.

        RemoteOK is a single-page API so this always returns ``1``.

        Returns:
            Always ``1``.
        """
        return 1

    def pages(self) -> Iterator[list[dict]]:
        """Yield the single page of RemoteOK listings.

        RemoteOK returns all listings in one request, so this yields at most
        one list.  Yields nothing if the fetch returns no results.

        Yields:
            A single list of normalised listing dicts.
        """
        results = self.fetch_page(1)
        if results:
            yield results

    def normalise(self, raw: dict) -> dict:
        """Map a RemoteOK raw listing dict to the canonical listing schema.

        Salary fields set to ``0`` by RemoteOK (meaning "not specified") are
        converted to ``None``.  HTML is stripped from the ``description`` field.
        An empty ``location`` field is replaced with ``"Remote"``.

        Args:
            raw: A single job dict from the RemoteOK API response.

        Returns:
            Dict conforming to the canonical listing schema defined in
            ``job_sources.base``.
        """
        # Salary: 0 or absent → None.
        raw_salary_min = raw.get("salary_min")
        raw_salary_max = raw.get("salary_max")
        salary_min = int(raw_salary_min) if raw_salary_min else None
        salary_max = int(raw_salary_max) if raw_salary_max else None
        if salary_min == 0:
            salary_min = None
        if salary_max == 0:
            salary_max = None

        # Location: fall back to "Remote" when empty.
        location = (raw.get("location") or "").strip() or "Remote"

        # Description: strip HTML tags.
        raw_description = raw.get("description") or ""
        if raw_description:
            description = BeautifulSoup(
                raw_description, "html.parser"
            ).get_text(separator=" ", strip=True)
        else:
            description = ""

        return {
            "source": self.SOURCE,
            "source_id": str(raw.get("id", "")),
            "title": raw.get("position", "") or "",
            "company": raw.get("company", "") or "",
            "location": location,
            "salary_min": salary_min,
            "salary_max": salary_max,
            "contract_type": None,
            "contract_time": None,
            "description": description,
            "redirect_url": raw.get("url", "") or "",
            "created_at": raw.get("date", "") or "",
        }
