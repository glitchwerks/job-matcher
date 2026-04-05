"""
job_sources/the_muse.py — Backward-compatibility shim.

The TheMuseClient implementation has moved to plugins/sources/the_muse/plugin.py
and is loaded into the SOURCES registry via the plugin loader.
This module re-exports the class and ensures the mock patch target
``patch("job_sources.the_muse.requests.get")`` resolves correctly.
"""

# NOTE: keep `import requests` at the top of this file.
# Test mocks use patch("job_sources.the_muse.requests.get") — if this import is removed,
# those mock targets will stop resolving silently.
import requests  # noqa: F401 — kept so patch("job_sources.the_muse.requests.get") resolves

from job_sources import SOURCES as _SOURCES

TheMuseClient = _SOURCES.get("the_muse")
if TheMuseClient is None:
    import logging as _logging
    _logging.getLogger(__name__).warning(
        "job_sources.the_muse: plugin failed to load — TheMuseClient is None; "
        "any code that instantiates it will raise TypeError."
    )

__all__ = ["TheMuseClient"]
