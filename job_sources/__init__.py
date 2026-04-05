"""
job_sources/ — Pluggable job source provider package for Job Matcher.

Public API
----------
* ``JobSource``             — abstract base class; import from here or ``job_sources.base``
* ``get_sources()``         — lazy registry accessor; returns mapping of source name → class
                              (populated automatically from ``plugins/sources/`` on first call)
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
import sys

from .base import JobSource
from .loader import load_plugins

__all__ = [
    "JobSource",
    "get_sources",
    "make_source",
    "make_enabled_sources",
]

# ---------------------------------------------------------------------------
# Module-level __getattr__ — enables lazy ``SOURCES`` attribute access.
# ``SOURCES`` is kept for backward compatibility with existing callers; it
# delegates to ``get_sources()`` so the plugin scan is deferred until first
# use rather than happening at import time.
# ---------------------------------------------------------------------------


def __getattr__(name: str):  # type: ignore[return]
    if name == "SOURCES":
        return get_sources()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

# ---------------------------------------------------------------------------
# Source registry — lazy-loaded on first access, cached thereafter.
# ---------------------------------------------------------------------------

_sources_cache: dict[str, type[JobSource]] | None = None


def get_sources() -> dict[str, type[JobSource]]:
    """Return the source registry, loading plugins on first call.

    The result is cached for the lifetime of the process.  Call
    ``get_sources()`` wherever ``SOURCES`` was previously used.

    If ``SOURCES`` has been directly set on this module (e.g. by
    ``monkeypatch.setattr`` in tests), that overriding value is returned
    instead of the cache.

    Returns:
        Mapping of ``source_key`` strings to their ``JobSource`` subclasses.
    """
    global _sources_cache
    # Allow tests (and callers) to override by setting job_sources.SOURCES directly.
    _override = vars(sys.modules[__name__]).get("SOURCES")
    if _override is not None:
        return _override
    if _sources_cache is None:
        _sources_cache = load_plugins()
    return _sources_cache


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
    sources = get_sources()
    source_name: str = config.get("job_source", "adzuna")

    cls = sources.get(source_name)
    if cls is None:
        raise ValueError(
            f"Unknown job source: {source_name!r}. "
            f"Registered sources: {list(sources)}."
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
        List of instantiated ``JobSource`` objects, in registry order.
    """
    _log = logging.getLogger("ingest.sources")

    sources = get_sources()
    sources_cfg: dict = providers_data.get("job_sources") or {}
    result: list[JobSource] = []

    # Warn about sources enabled in providers.json that are not in the loaded registry.
    loaded_keys = set(sources.keys())
    for key, src_cfg in sources_cfg.items():
        if src_cfg.get("enabled") and key not in loaded_keys:
            _log.warning(
                "Source %r is enabled in providers.json but was not loaded "
                "(plugin missing or failed to load) — skipping",
                key,
            )

    for key, cls in sources.items():
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
