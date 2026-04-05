"""
job_sources/himalayas.py — Backward-compatibility shim.

The HimalayasClient implementation has moved to plugins/sources/himalayas/plugin.py
and is loaded into the SOURCES registry via the plugin loader.
This module re-exports the class and private helpers for backward-compatible imports.
"""

# NOTE: tests now patch the real module directly:
#   patch("job_sources._plugin_himalayas.requests.get")
# The import below is kept only to preserve the attribute chain for any
# external code that may still reference job_sources.himalayas.requests directly.
import requests  # noqa: F401, E402

from job_sources import SOURCES as _SOURCES

HimalayasClient = _SOURCES.get("himalayas")
if HimalayasClient is None:
    import logging as _logging
    _logging.getLogger(__name__).warning(
        "job_sources.himalayas: plugin failed to load — HimalayasClient is None; "
        "any code that instantiates it will raise TypeError."
    )

# Re-export module-level helpers from the plugin for tests that import them directly.
# The loader registers plugin modules in sys.modules as job_sources._plugin_<name>.
from job_sources._plugin_himalayas import (  # noqa: F401, E402
    _parse_created_at,
    _strip_html,
    _map_job_type,
)

__all__ = ["HimalayasClient", "_parse_created_at", "_strip_html", "_map_job_type"]
