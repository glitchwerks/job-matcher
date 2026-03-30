"""
job_sources/ — Pluggable job source provider package for Job Matcher.

Public API
----------
* ``JobSource``        — abstract base class; import from here or ``job_sources.base``
* ``AdzunaClient``     — Adzuna Jobs API backend
* ``ArbeitnowClient``  — Arbeitnow job board API backend
* ``HimalayasClient``  — Himalayas Jobs API backend
* ``RemoteOKClient``   — RemoteOK jobs API backend
* ``USAJobsClient``    — USAJobs API backend
* ``TheMuseClient``    — The Muse API backend
* ``RemotiveClient``   — Remotive remote-jobs API backend
* ``SOURCES``          — registry mapping source name strings to their classes
* ``make_source()``    — factory that reads ``config["job_source"]`` and returns
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
from .himalayas import HimalayasClient
from .remoteok import RemoteOKClient
from .usajobs import USAJobsClient
from .the_muse import TheMuseClient
from .remotive import RemotiveClient

__all__ = [
    "JobSource",
    "AdzunaClient",
    "ArbeitnowClient",
    "HimalayasClient",
    "RemoteOKClient",
    "USAJobsClient",
    "TheMuseClient",
    "RemotiveClient",
    "SOURCES",
    "make_source",
]

# ---------------------------------------------------------------------------
# Source registry — maps source name string → class
# ---------------------------------------------------------------------------

SOURCES: dict[str, type[JobSource]] = {
    "adzuna": AdzunaClient,
    "arbeitnow": ArbeitnowClient,
    "himalayas": HimalayasClient,
    "remoteok": RemoteOKClient,
    "usajobs": USAJobsClient,
    "the_muse": TheMuseClient,
    "remotive": RemotiveClient,
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

    return cls(config=config)
