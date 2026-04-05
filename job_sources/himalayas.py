"""
job_sources/himalayas.py — Backward-compatibility shim.

The HimalayasClient implementation has moved to plugins/sources/himalayas/plugin.py
and is loaded into the SOURCES registry via the plugin loader.
This module re-exports the class and private helpers, and ensures the mock patch
target ``patch("job_sources.himalayas.requests.get")`` resolves correctly.
"""

# NOTE: keep `import requests` at the top of this file.
# Test mocks use patch("job_sources.himalayas.requests.get") — if this import is removed,
# those mock targets will stop resolving silently.
import requests  # noqa: F401 — kept so patch("job_sources.himalayas.requests.get") resolves

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
from job_sources._plugin_himalayas import (  # noqa: F401
    _parse_created_at,
    _strip_html,
    _map_job_type,
)

__all__ = ["HimalayasClient", "_parse_created_at", "_strip_html", "_map_job_type"]
