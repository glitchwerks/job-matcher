"""
job_sources/ — Pluggable job source provider package for Job Matcher.

Public API
----------
* ``JobSource``             — abstract base class; import from here or ``job_sources.base``
* ``AdzunaClient``          — Adzuna Jobs API backend
* ``ArbeitnowClient``       — Arbeitnow job board API backend
* ``HimalayasClient``       — Himalayas Jobs API backend
* ``RemoteOKClient``        — RemoteOK jobs API backend
* ``USAJobsClient``         — USAJobs API backend
* ``TheMuseClient``         — The Muse API backend
* ``RemotiveClient``        — Remotive remote-jobs API backend
* ``SOURCES``               — registry mapping source name strings to their classes
* ``make_source()``         — factory that reads ``config["job_source"]`` and returns
                              the right ``JobSource`` instance
* ``make_enabled_sources()``— factory that returns all enabled ``JobSource`` instances
                              based on the ``enabled`` flag in providers_data

Usage
-----
    from job_sources import make_source, make_enabled_sources

    source = make_source(config)          # reads config["job_source"], defaults to "adzuna"
    for page in source.pages():           # AdzunaClient.pages() iterator
        for raw in source.fetch_page(n):  # or fetch page-by-page
            listing = source.normalise(raw)

    sources = make_enabled_sources(providers_data, config)  # all enabled sources
    for source in sources:
        for page in source.pages():
            for listing in page:
                ...
"""

from __future__ import annotations

import logging

from .base import JobSource
from .adzuna import AdzunaClient
from .arbeitnow import ArbeitnowClient
from .himalayas import HimalayasClient
from .remoteok import RemoteOKClient
from .usajobs import USAJobsClient
from .the_muse import TheMuseClient
from .remotive import RemotiveClient
from .jobicy import JobicyClient
from .jooble import JoobleClient

__all__ = [
    "JobSource",
    "AdzunaClient",
    "ArbeitnowClient",
    "HimalayasClient",
    "RemoteOKClient",
    "USAJobsClient",
    "TheMuseClient",
    "RemotiveClient",
    "JobicyClient",
    "JoobleClient",
    "SOURCES",
    "make_source",
    "make_enabled_sources",
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
    "jobicy": JobicyClient,
    "jooble": JoobleClient,
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


def make_enabled_sources(providers_data: dict, config: dict) -> list[JobSource]:
    """Return a list of instantiated ``JobSource`` objects for all enabled sources.

    Reads ``providers_data["job_sources"][key]["enabled"]`` for each registered
    source.  Sources without an entry default to disabled.  Keyed sources that
    are enabled but missing required credentials are skipped with a warning.

    Args:
        providers_data: Dict as returned by ``credentials.load_providers()``.
        config:         Full config dict (passed to each source constructor).

    Returns:
        List of instantiated ``JobSource`` objects, in ``SOURCES`` registry order.
    """
    _log = logging.getLogger("ingest.sources")

    sources_cfg: dict = providers_data.get("job_sources") or {}
    result: list[JobSource] = []

    for key, cls in SOURCES.items():
        src_cfg = sources_cfg.get(key) or {}

        # Determine the default enabled state.
        # Keyless sources (no required credentials) default to enabled=True when
        # absent from providers_data — they work without any configuration.
        # Keyed sources (with required credentials) default to enabled=False when
        # absent — activating them silently without credentials would only produce
        # errors, so we skip them with a warning instead.
        schema = cls.settings_schema()
        required = [f["name"] for f in schema.get("fields", []) if f.get("required")]
        default_enabled = len(required) == 0  # keyless → True, keyed → False

        if key not in sources_cfg:
            # Source has no entry at all in providers_data.
            if required:
                # Keyed source with no entry — credentials are always absent.
                _log.warning(
                    "Source '%s' has no entry in providers_data and requires credentials (%s) — skipping.",
                    key, ", ".join(required),
                )
                continue
            # Keyless source with no entry — treat as enabled.
        else:
            if not src_cfg.get("enabled", default_enabled):
                continue

        # For keyed sources, check required credentials are present.
        if required:
            missing = [f for f in required if not str(src_cfg.get(f, "")).strip()]
            if missing:
                _log.warning(
                    "Source '%s' is enabled but missing required credentials (%s) — skipping.",
                    key, ", ".join(missing),
                )
                continue

        try:
            result.append(cls(config=config, credentials=src_cfg))
        except ValueError as exc:
            _log.warning(
                "Source '%s' failed to initialise — skipping: %s",
                key, exc,
            )

    return result
