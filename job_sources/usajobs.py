"""
job_sources/usajobs.py — Backward-compatibility shim.

The USAJobsClient implementation has moved to plugins/sources/usajobs/plugin.py
and is loaded into the SOURCES registry via the plugin loader.
This module re-exports the class and module-level helpers for backward-compatible imports.
"""

# NOTE: tests now patch the real module directly:
#   patch("job_sources._plugin_usajobs.requests.get")
# The import below is kept only to preserve the attribute chain for any
# external code that may still reference job_sources.usajobs.requests directly.
import requests  # noqa: F401, E402

from job_sources import SOURCES as _SOURCES

USAJobsClient = _SOURCES.get("usajobs")
if USAJobsClient is None:
    import logging as _logging
    _logging.getLogger(__name__).warning(
        "job_sources.usajobs: plugin failed to load — USAJobsClient is None; "
        "any code that instantiates it will raise TypeError."
    )

# Re-export module-level helper from the plugin for tests that import it directly.
from job_sources._plugin_usajobs import _parse_float  # noqa: F401, E402

__all__ = ["USAJobsClient", "_parse_float"]
