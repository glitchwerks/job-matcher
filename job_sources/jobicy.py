"""
job_sources/jobicy.py — Backward-compatibility shim.

The JobicyClient implementation has moved to plugins/sources/jobicy/plugin.py
and is loaded into the SOURCES registry via the plugin loader.
This module re-exports the class and ensures the mock patch target
``patch("job_sources.jobicy.requests.get")`` resolves correctly.
"""

# NOTE: keep `import requests` at the top of this file.
# Test mocks use patch("job_sources.jobicy.requests.get") — if this import is removed,
# those mock targets will stop resolving silently.
import requests  # noqa: F401 — kept so patch("job_sources.jobicy.requests.get") resolves

from job_sources import SOURCES as _SOURCES

JobicyClient = _SOURCES.get("jobicy")
if JobicyClient is None:
    import logging as _logging
    _logging.getLogger(__name__).warning(
        "job_sources.jobicy: plugin failed to load — JobicyClient is None; "
        "any code that instantiates it will raise TypeError."
    )

__all__ = ["JobicyClient"]
