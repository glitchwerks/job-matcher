"""
job_sources/jooble.py — Backward-compatibility shim.

The JoobleClient implementation has moved to plugins/sources/jooble/plugin.py
and is loaded into the SOURCES registry via the plugin loader.
This module re-exports the class and private helpers for backward-compatible imports.
"""

# NOTE: tests now patch the real module directly:
#   patch("job_sources._plugin_jooble.requests.post")
# The import below is kept only to preserve the attribute chain for any
# external code that may still reference job_sources.jooble.requests directly.
import requests  # noqa: F401, E402

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
from job_sources._plugin_jooble import (  # noqa: F401, E402
    _CONTRACT_TIME_MAP,
    _normalise_contract_time,
)

__all__ = ["JoobleClient", "_CONTRACT_TIME_MAP", "_normalise_contract_time"]
