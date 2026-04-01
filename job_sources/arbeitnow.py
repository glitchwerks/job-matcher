"""
job_sources/arbeitnow.py — Arbeitnow API implementation of the JobSource protocol.

Wraps the Arbeitnow job-board REST API: page-number pagination via meta.last_page,
HTML stripping from descriptions, and normalisation to the canonical listing schema.

API docs: https://www.arbeitnow.com/api/job-board-api
No API key required.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Iterator

import requests
from bs4 import BeautifulSoup

from .base import JobSource

logger = logging.getLogger("ingest.arbeitnow")

_BASE_URL = "https://www.arbeitnow.com/api/job-board-api"

# Arbeitnow uses inconsistent strings for full-time employment — some in English,
# some in German.  This map normalises them to the canonical "full_time" value so
# the prefilter's contract_time check can match them.  Lookup is case-insensitive.
# Unmapped values are passed through unchanged so genuinely part-time or contract
# roles are still rejected by the prefilter.
_CONTRACT_TIME_MAP: dict[str, str] = {
    "full-time permanent": "full_time",
    "berufserfahren": "full_time",        # German: "experienced professional"
    "professional / experienced": "full_time",
}


def _strip_html(html: str) -> str:
    """Remove HTML tags and return plain text.

    Uses BeautifulSoup so that character entities (``&amp;``, ``&nbsp;``, etc.)
    are decoded correctly.  Words that were separated only by tags are joined
    with a single space so the result is readable prose.

    Args:
        html: Raw HTML string.

    Returns:
        Plain-text string with tags removed.
    """
    return BeautifulSoup(html, "html.parser").get_text(separator=" ", strip=True)


def _unix_to_iso(ts: Any) -> str | None:
    """Convert a Unix timestamp integer to an ISO 8601 UTC string.

    Args:
        ts: Unix timestamp (int or float).  ``None`` or non-numeric values
            return ``None``.

    Returns:
        ISO 8601 string of the form ``"YYYY-MM-DDTHH:MM:SSZ"``, or ``None``
        when the input cannot be converted.
    """
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError, OSError):
        return None


class ArbeitnowClient(JobSource):
    """JobSource implementation for the Arbeitnow job-board API.

    The Arbeitnow API returns paginated results via ``?page=N``.  ``total_pages()``
    fetches page 1 to read ``meta.last_page``.  ``fetch_page()`` then retrieves
    any subsequent page.

    No API key or authentication is required.
    """

    SOURCE = "arbeitnow"

    def __init__(self, config: dict | None = None) -> None:
        """Initialise the client.

        Args:
            config: Full application config dict.  The optional
                    ``config["arbeitnow"]`` sub-dict can carry future overrides;
                    no keys are currently required.
        """
        self._config: dict = (config or {}).get("arbeitnow", {})
        self._cached_total_pages: int | None = None

    # ------------------------------------------------------------------
    # JobSource interface
    # ------------------------------------------------------------------

    @classmethod
    def settings_schema(cls) -> dict:
        """Return the settings schema for Arbeitnow.

        Arbeitnow requires no credentials — the public API is key-free.

        Returns:
            Schema dict with ``display_name`` and an empty ``fields`` list.
        """
        return {
            "display_name": "Arbeitnow",
            "home_url": "https://www.arbeitnow.com",
            "fields": [],
        }

    def fetch_page(self, page: int) -> list[dict]:
        """Fetch a single page of raw Arbeitnow listings.

        On any non-200 HTTP status or network/JSON error the method logs a
        warning and returns an empty list so the caller can continue without
        crashing.

        Args:
            page: 1-based page number.

        Returns:
            List of raw listing dicts as returned by the ``data`` array in
            the Arbeitnow API response.  Returns ``[]`` on any error.
        """
        try:
            response = requests.get(_BASE_URL, params={"page": page}, timeout=15)
        except requests.RequestException as exc:
            logger.warning("Arbeitnow request failed (page %d): %s", page, exc)
            return []

        if response.status_code != 200:
            logger.warning(
                "Arbeitnow returned HTTP %d for page %d; skipping",
                response.status_code,
                page,
            )
            return []

        try:
            data = response.json()
        except ValueError as exc:
            logger.warning("Arbeitnow response is not valid JSON (page %d): %s", page, exc)
            return []

        return data.get("data", [])

    def total_pages(self) -> int:
        """Return the total number of available pages.

        Fetches page 1 on the first call and reads ``meta.last_page`` from the
        response.  The result is cached for the lifetime of the instance so
        subsequent calls do not make additional HTTP requests.

        Returns:
            ``meta.last_page`` from the API, or ``1`` as a safe fallback when
            the ``meta`` key is absent or the request fails.
        """
        if self._cached_total_pages is not None:
            return self._cached_total_pages

        try:
            response = requests.get(_BASE_URL, params={"page": 1}, timeout=15)
            response.raise_for_status()
            data = response.json()
        except (requests.RequestException, ValueError) as exc:
            logger.warning("Arbeitnow total_pages() request failed: %s", exc)
            self._cached_total_pages = 1
            return 1

        meta = data.get("meta", {})
        last_page = meta.get("last_page", 1) if isinstance(meta, dict) else 1

        try:
            self._cached_total_pages = int(last_page)
        except (TypeError, ValueError):
            self._cached_total_pages = 1

        return self._cached_total_pages

    def pages(self) -> Iterator[list[dict]]:
        """Yield normalised listing lists, one per page.

        Iterates from page 1 up to ``total_pages()`` (inclusive).  Stops
        early if a page returns zero results.

        Yields:
            Lists of normalised listing dicts (after ``normalise()``).
        """
        for page in range(1, self.total_pages() + 1):
            results = self.fetch_page(page)
            if not results:
                logger.info("Arbeitnow page %d returned 0 results; stopping early", page)
                return
            yield [self.normalise(r) for r in results]

    def normalise(self, raw: dict) -> dict:
        """Map an Arbeitnow listing dict to the canonical listing schema.

        Field mapping:
        - ``slug``         → ``source_id``
        - ``company_name`` → ``company``
        - ``location`` / ``remote`` → ``location`` (``"Remote"`` when remote=True
          and location is blank)
        - ``job_types[0]`` → ``contract_time`` (``None`` when list is empty)
        - ``description``  → ``description`` (HTML tags stripped)
        - ``url``          → ``redirect_url``
        - ``created_at``   → ``created_at`` (Unix timestamp → ISO 8601)
        - ``salary_min``, ``salary_max``, ``contract_type`` are always ``None``
          because Arbeitnow does not expose them.

        Args:
            raw: A single entry from the Arbeitnow ``data`` array.

        Returns:
            Dict conforming to the canonical listing schema defined in
            ``job_sources.base``.
        """
        # Resolve location: prefer the location string; fall back to "Remote"
        # when the remote flag is set and no location string is provided.
        location_raw: str = raw.get("location", "") or ""
        remote: bool = bool(raw.get("remote", False))
        if not location_raw and remote:
            location = "Remote"
        else:
            location = location_raw

        # contract_time: first element of job_types list, or None.
        # Normalise non-standard Arbeitnow strings to the canonical value so that
        # the prefilter's contract_time check can match full-time roles reliably.
        job_types: list = raw.get("job_types") or []
        raw_contract_time: str | None = job_types[0] if job_types else None
        contract_time: str | None = (
            _CONTRACT_TIME_MAP.get(raw_contract_time.lower(), raw_contract_time)
            if raw_contract_time is not None
            else None
        )

        # Strip HTML from description.
        raw_description: str = raw.get("description", "") or ""
        description = _strip_html(raw_description) if raw_description else None

        return {
            "source": self.SOURCE,
            "source_id": str(raw.get("slug", "")),
            "title": raw.get("title", "") or "",
            "company": raw.get("company_name", "") or "",
            "location": location,
            "salary_min": None,
            "salary_max": None,
            "salary_period": None,  # Arbeitnow does not expose salary data
            "contract_type": None,
            "contract_time": contract_time,
            "description": description,
            "redirect_url": raw.get("url", "") or "",
            "created_at": _unix_to_iso(raw.get("created_at")),
        }
