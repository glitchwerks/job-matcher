"""
job_sources/arbeitnow.py — Backward-compatibility shim.

The ArbeitnowClient implementation has moved to plugins/sources/arbeitnow/plugin.py
and is loaded into the SOURCES registry via the plugin loader.
This module re-exports the class and private helpers, and ensures the mock patch
target ``patch("job_sources.arbeitnow.requests.get")`` resolves correctly.
"""

# NOTE: keep `import requests` at the top of this file.
# Test mocks use patch("job_sources.arbeitnow.requests.get") — if this import is removed,
# those mock targets will stop resolving silently.
import requests  # noqa: F401 — kept so patch("job_sources.arbeitnow.requests.get") resolves

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
from job_sources._plugin_arbeitnow import (  # noqa: F401
    _CONTRACT_TIME_MAP,
    _strip_html,
    _unix_to_iso,
)

__all__ = ["ArbeitnowClient", "_CONTRACT_TIME_MAP", "_strip_html", "_unix_to_iso"]
