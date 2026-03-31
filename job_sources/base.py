"""
job_sources/base.py — Abstract base class for job source providers.

All concrete job sources must implement ``fetch_page()``, ``total_pages()``,
and ``normalise()`` so that the ingestion pipeline can work with any source
without source-specific branching.

Canonical listing schema
------------------------
``normalise()`` must return a dict with exactly these keys:

    source          str          — source identifier, e.g. "adzuna"
    source_id       str          — source-specific listing ID
    title           str
    company         str
    location        str
    salary_min      float|None
    salary_max      float|None
    salary_period   str|None     — pay period: "annual", "daily", "hourly", or None (unknown)
    contract_type   str|None
    contract_time   str|None
    description     str|None     — snippet or None; full JD scraped later
    redirect_url    str
    created_at      str|None     — ISO 8601 string, e.g. "2026-01-02T12:34:56Z"
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Iterator


class JobSource(ABC):
    """Interface that every job source backend must satisfy.

    Concrete sub-classes encapsulate source-specific API calls, pagination,
    and field normalisation so that ``ingest.py`` remains source-agnostic.
    """

    @abstractmethod
    def fetch_page(self, page: int) -> list[dict]:
        """Fetch a single page of raw listings from the source.

        Args:
            page: 1-based page number.

        Returns:
            List of raw listing dicts as returned by the source API.
            Returns an empty list when no results are available or on error.
        """
        ...

    @abstractmethod
    def total_pages(self) -> int:
        """Return the number of pages available for the current search.

        Returns:
            Integer page count. Implementations may return a configured
            maximum rather than querying the API for the true total.
        """
        ...

    @abstractmethod
    def normalise(self, raw: dict) -> dict:
        """Convert a source-specific raw listing dict to the canonical schema.

        Args:
            raw: A single raw listing dict as returned by ``fetch_page()``.

        Returns:
            Dict conforming to the canonical listing schema documented in
            this module's docstring.  All keys must be present; unknown
            source fields are silently dropped.
        """
        ...

    @classmethod
    @abstractmethod
    def settings_schema(cls) -> dict:
        """Return the settings schema for this job source.

        The returned dict describes the credentials and configuration fields
        that the Settings UI should render for this source.

        Returns:
            Dict with exactly two keys:

            * ``display_name`` — str, human-readable name shown in the UI.
            * ``fields``       — list of field dicts.  Each field dict must
              have: ``name`` (str), ``label`` (str), ``type`` (``"text"``
              or ``"password"``), ``required`` (bool).  Sources that
              require no credentials return an empty list so the UI can
              render them as a status-only card.
        """
        ...

    def pages(self) -> Iterator[list[dict]]:
        """Yield normalised listing lists, one per page.

        Default implementation iterates from page 1 up to ``total_pages()``
        (inclusive) using ``fetch_page()`` and stops early when a page returns
        zero results.  Subclasses may override this to apply source-specific
        logic (e.g. 0-based page numbering, caching).

        Yields:
            Lists of normalised listing dicts (one list per page).
        """
        import logging as _logging
        _log = _logging.getLogger(__name__)
        for page in range(1, self.total_pages() + 1):
            results = self.fetch_page(page)
            if not results:
                _log.info("Page %d returned 0 results; stopping early", page)
                return
            yield results
