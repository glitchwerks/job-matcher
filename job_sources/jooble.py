"""
job_sources/jooble.py — Backward-compatibility shim.

The JoobleClient implementation has moved to plugins/sources/jooble/plugin.py
and is loaded into the SOURCES registry via the plugin loader.
This module re-exports the class and private helpers, and ensures the mock patch
target ``patch("job_sources.jooble.requests.post")`` resolves correctly.
"""

# NOTE: keep `import requests` at the top of this file.
# Test mocks use patch("job_sources.jooble.requests.post") — if this import is removed,
# those mock targets will stop resolving silently.
import requests  # noqa: F401 — kept so patch("job_sources.jooble.requests.post") resolves

from job_sources import SOURCES as _SOURCES

JoobleClient = _SOURCES.get("jooble")
if JoobleClient is None:
    import logging as _logging
    _logging.getLogger(__name__).warning(
        "job_sources.jooble: plugin failed to load — JoobleClient is None; "
        "any code that instantiates it will raise TypeError."
    )

# Re-export module-level helpers from the plugin for tests that import them directly.
# The loader registers plugin modules in sys.modules as job_sources._plugin_<name>.
from job_sources._plugin_jooble import (  # noqa: F401
    _CONTRACT_TIME_MAP,
    _normalise_contract_time,
)

__all__ = ["JoobleClient", "_CONTRACT_TIME_MAP", "_normalise_contract_time"]
