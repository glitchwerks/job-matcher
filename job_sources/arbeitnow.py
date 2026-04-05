"""
job_sources/arbeitnow.py — Backward-compatibility shim.

The ArbeitnowClient implementation has moved to plugins/sources/arbeitnow/plugin.py
and is loaded into the SOURCES registry via the plugin loader.
This module re-exports the class and private helpers for backward-compatible imports.
"""

# NOTE: tests now patch the real module directly:
#   patch("job_sources._plugin_arbeitnow.requests.get")
# The import below is kept only to preserve the attribute chain for any
# external code that may still reference job_sources.arbeitnow.requests directly.
import requests  # noqa: F401, E402

from job_sources import SOURCES as _SOURCES

ArbeitnowClient = _SOURCES.get("arbeitnow")
if ArbeitnowClient is None:
    import logging as _logging
    _logging.getLogger(__name__).warning(
        "job_sources.arbeitnow: plugin failed to load — ArbeitnowClient is None; "
        "any code that instantiates it will raise TypeError."
    )

# Re-export module-level helpers from the plugin for tests that import them directly.
# The loader registers plugin modules in sys.modules as job_sources._plugin_<name>.
from job_sources._plugin_arbeitnow import (  # noqa: F401, E402
    _CONTRACT_TIME_MAP,
    _strip_html,
    _unix_to_iso,
)

__all__ = ["ArbeitnowClient", "_CONTRACT_TIME_MAP", "_strip_html", "_unix_to_iso"]
