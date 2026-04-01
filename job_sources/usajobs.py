"""
job_sources/usajobs.py — USAJobs API implementation of the JobSource protocol.

Wraps the USAJobs REST API (https://developer.usajobs.gov/API-Reference):
pagination, authentication headers, and normalisation to the canonical
listing schema.

Config keys (under ``config["usajobs"]``):
    api_key          str  — USAJobs authorization key (required)
    user_agent       str  — contact email required by USAJobs (required)
    keyword          str  — search keyword (default: "software engineer")
    results_per_page int  — results per page (default: 25)
"""

from __future__ import annotations

import logging
from typing import Iterator

import requests

from .base import JobSource

logger = logging.getLogger("ingest.usajobs")

_USAJOBS_SEARCH_URL = "https://data.usajobs.gov/api/search"

# Only map salary values when pay is expressed as an annual rate.
_ANNUAL_RATE_CODE = "PA"


class USAJobsClient(JobSource):
    """JobSource implementation for the USAJobs REST API.

    Handles pagination, authentication headers, and normalisation of raw
    USAJobs response dicts to the canonical listing schema.
    """

    SOURCE = "usajobs"

    def __init__(self, config: dict) -> None:
        """Extract USAJobs credentials and search params from config.

        Args:
            config: Full config dict.  Must contain a ``"usajobs"`` sub-dict
                    with at least ``api_key`` and ``user_agent``.

        Raises:
            ValueError: If ``config["usajobs"]`` is missing, or if
                        ``api_key`` or ``user_agent`` are absent within it.
        """
        usajobs_cfg: dict | None = config.get("usajobs")
        if not usajobs_cfg:
            raise ValueError(
                "USAJobs config block is absent. "
                "Add a 'usajobs' section to config.json with 'api_key' and 'user_agent'."
            )

        api_key: str | None = usajobs_cfg.get("api_key")
        if not api_key:
            raise ValueError(
                "USAJobs 'api_key' is required but missing from config['usajobs']."
            )

        user_agent: str | None = usajobs_cfg.get("user_agent")
        if not user_agent:
            raise ValueError(
                "USAJobs 'user_agent' (contact email) is required but missing "
                "from config['usajobs']."
            )

        self._api_key = api_key
        self._user_agent = user_agent
        self._keyword: str = usajobs_cfg.get("keyword", "software engineer")
        self._results_per_page: int = int(usajobs_cfg.get("results_per_page", 25))

    # ------------------------------------------------------------------
    # JobSource interface
    # ------------------------------------------------------------------

    @classmethod
    def settings_schema(cls) -> dict:
        """Return the settings schema for USAJobs.

        USAJobs requires an API key and a contact email (User-Agent) as
        mandated by the USAJobs developer agreement.

        Returns:
            Schema dict with ``display_name`` and credential ``fields``
            for the USAJobs API key and user-agent contact email.
        """
        return {
            "display_name": "USAJobs",
            "description": "Official US federal government job board. Requires a free API key and contact email from usajobs.gov. Best for government or contractor roles.",
            "home_url": "https://www.usajobs.gov",
            "fields": [
                {
                    "name": "api_key",
                    "label": "API Key",
                    "type": "password",
                    "required": True,
                },
                {
                    "name": "user_agent",
                    "label": "Contact Email (User-Agent)",
                    "type": "text",
                    "required": True,
                },
            ],
        }

    def fetch_page(self, page: int) -> list[dict]:
        """Fetch a single page of raw USAJobs search results.

        Non-200 responses are logged and return an empty list.  The raw
        ``SearchResultItems`` entries are returned without normalisation —
        callers should call ``normalise()`` on each item.

        Args:
            page: 1-based page number.

        Returns:
            List of raw ``SearchResultItems`` dicts as returned by the API.
        """
        params: dict[str, str | int] = {
            "Keyword": self._keyword,
            "Page": page,
            "ResultsPerPage": self._results_per_page,
        }

        headers = {
            "Authorization-Key": self._api_key,
            "User-Agent": self._user_agent,
            "Host": "data.usajobs.gov",
        }

        try:
            response = requests.get(
                _USAJOBS_SEARCH_URL,
                params=params,
                headers=headers,
                timeout=15,
            )
        except requests.RequestException as exc:
            logger.warning("USAJobs request failed: %s", exc)
            return []

        if response.status_code != 200:
            logger.warning(
                "USAJobs returned HTTP %d for page %d; skipping",
                response.status_code,
                page,
            )
            return []

        try:
            data = response.json()
        except ValueError as exc:
            logger.warning("USAJobs response is not valid JSON: %s", exc)
            return []

        items: list[dict] = (
            data.get("SearchResult", {})
            .get("SearchResultItems", [])
        )
        return items

    def total_pages(self) -> int:
        """Query page 1 and return the total number of pages available.

        Returns:
            ``NumberOfPages`` from the USAJobs search metadata.

        Raises:
            RuntimeError: If the API request fails or the response cannot
                          be parsed.
        """
        params: dict[str, str | int] = {
            "Keyword": self._keyword,
            "Page": 1,
            "ResultsPerPage": self._results_per_page,
        }

        headers = {
            "Authorization-Key": self._api_key,
            "User-Agent": self._user_agent,
            "Host": "data.usajobs.gov",
        }

        try:
            response = requests.get(
                _USAJOBS_SEARCH_URL,
                params=params,
                headers=headers,
                timeout=15,
            )
        except requests.RequestException as exc:
            raise RuntimeError(f"USAJobs total_pages() request failed: {exc}") from exc

        if response.status_code != 200:
            raise RuntimeError(
                f"USAJobs total_pages() got HTTP {response.status_code}"
            )

        try:
            data = response.json()
        except ValueError as exc:
            raise RuntimeError(f"USAJobs total_pages() response is not valid JSON: {exc}") from exc

        try:
            pages = int(
                data["SearchResult"]["UserArea"]["NumberOfPages"]
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(
                f"USAJobs total_pages() could not read NumberOfPages: {exc}"
            ) from exc

        return pages

    def normalise(self, raw: dict) -> dict:
        """Map a raw USAJobs ``SearchResultItems`` entry to the canonical schema.

        Salary fields are only populated when the rate interval is annual
        (``RateIntervalCode == "PA"``).  String salary values are cast to
        float; unparseable values become ``None``.

        Args:
            raw: A single entry from ``SearchResultItems``.

        Returns:
            Dict conforming to the canonical listing schema defined in
            ``job_sources.base``.
        """
        descriptor: dict = raw.get("MatchedObjectDescriptor") or {}

        # --- salary ---
        salary_min: float | None = None
        salary_max: float | None = None
        remuneration_list = descriptor.get("PositionRemuneration") or []
        if remuneration_list:
            pay = remuneration_list[0]
            if pay.get("RateIntervalCode") == _ANNUAL_RATE_CODE:
                salary_min = _parse_float(pay.get("MinimumRange"))
                salary_max = _parse_float(pay.get("MaximumRange"))

        # --- contract type / time ---
        offering_types = descriptor.get("PositionOfferingType") or []
        contract_type: str | None = (
            offering_types[0].get("Name") if offering_types else None
        )
        contract_time: str | None = descriptor.get("ScheduleTypeName") or None

        return {
            "source": self.SOURCE,
            "source_id": str(raw.get("MatchedObjectId", "")),
            "title": descriptor.get("PositionTitle", "") or "",
            "company": descriptor.get("OrganizationName", "") or "",
            "location": descriptor.get("PositionLocationDisplay", "") or "",
            "salary_min": salary_min,
            "salary_max": salary_max,
            "salary_period": "annual" if salary_max is not None else None,
            "contract_type": contract_type,
            "contract_time": contract_time,
            "description": descriptor.get("QualificationSummary", "") or "",
            "redirect_url": descriptor.get("PositionURI", "") or "",
            "created_at": descriptor.get("PublicationStartDate", "") or "",
        }

    # ------------------------------------------------------------------
    # Convenience iterator
    # ------------------------------------------------------------------

    def pages(self) -> Iterator[list[dict]]:
        """Yield raw item lists, one per page, up to ``total_pages()``.

        Stops early if a page returns zero results.

        Yields:
            Lists of raw ``SearchResultItems`` dicts.
        """
        for page in range(1, self.total_pages() + 1):
            results = self.fetch_page(page)
            if not results:
                logger.info("USAJobs page %d returned 0 results; stopping early", page)
                return
            yield results


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_float(value: object) -> float | None:
    """Cast *value* to float, returning ``None`` on failure or if absent."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
