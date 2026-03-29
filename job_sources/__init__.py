"""
job_sources/ — Pluggable job source provider package for Job Matcher.

Public API
----------
* ``JobSource``      — abstract base class; import from here or ``job_sources.base``
* ``AdzunaClient``   — Adzuna Jobs API backend
* ``SOURCES``        — registry mapping source name strings to their classes
* ``make_source()``  — factory that reads ``config["job_source"]`` and returns
                       the right ``JobSource`` instance

Usage
-----
    from job_sources import make_source

    source = make_source(config)          # reads config["job_source"], defaults to "adzuna"
    for page in source.pages():           # AdzunaClient.pages() iterator
        for raw in source.fetch_page(n):  # or fetch page-by-page
            listing = source.normalise(raw)
"""

from __future__ import annotations

from .base import JobSource
from .adzuna import AdzunaClient
from .arbeitnow import ArbeitnowClient

__all__ = [
    "JobSource",
    "AdzunaClient",
    "ArbeitnowClient",
    "SOURCES",
    "make_source",
]

# ---------------------------------------------------------------------------
# Source registry — maps source name string → class
# ---------------------------------------------------------------------------

SOURCES: dict[str, type[JobSource]] = {
    "adzuna": AdzunaClient,
    "arbeitnow": ArbeitnowClient,
}


def make_source(config: dict) -> JobSource:
    """Instantiate and return the correct ``JobSource`` for *config*.

    Reads ``config["job_source"]`` (default: ``"adzuna"``) to select the
    backend, then passes the full config dict to the constructor so each
    source can extract whatever credentials and search parameters it needs.

    Args:
        config: Full config dict as returned by ``ingest.load_config()``.
                Must contain all keys required by the selected source
                (e.g. ``adzuna_app_id`` and ``adzuna_app_key`` for Adzuna).

    Returns:
        An initialised ``JobSource`` instance.

    Raises:
        ValueError: If ``job_source`` names an unregistered backend.
    """
    source_name: str = config.get("job_source", "adzuna")

    cls = SOURCES.get(source_name)
    if cls is None:
        raise ValueError(
            f"Unknown job source: {source_name!r}. "
            f"Registered sources: {list(SOURCES)}."
        )

    # Adzuna needs app_id / app_key pulled from the top-level config.
    # Each source class is responsible for extracting what it needs from
    # the full config dict — the factory just passes it through unchanged.
    if source_name == "adzuna":
        return cls(
            app_id=config["adzuna_app_id"],
            app_key=config["adzuna_app_key"],
            config=config,
        )

    # Generic fallback for future sources that accept (config,) only.
    return cls(config=config)  # type: ignore[call-arg]
