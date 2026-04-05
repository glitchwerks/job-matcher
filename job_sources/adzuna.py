"""
job_sources/adzuna.py — Backward-compatibility shim.

The AdzunaClient implementation has moved to plugins/sources/adzuna/plugin.py
and is loaded into the SOURCES registry via the plugin loader.
This module re-exports the class and ensures the mock patch target
``patch("job_sources.adzuna.requests.get")`` resolves correctly.
"""

# NOTE: keep `import requests` and `import time` at the top of this file.
# Test mocks use patch("job_sources.adzuna.requests.get") — if this import is removed,
# those mock targets will stop resolving silently.
import requests  # noqa: F401 — kept so patch("job_sources.adzuna.requests.get") resolves
import time  # noqa: F401 — kept so patch("job_sources.adzuna.time.sleep") resolves

from job_sources import SOURCES as _SOURCES

AdzunaClient = _SOURCES.get("adzuna")
if AdzunaClient is None:
    import logging as _logging
    _logging.getLogger(__name__).warning(
        "job_sources.adzuna: plugin failed to load — AdzunaClient is None; "
        "any code that instantiates it will raise TypeError."
    )

__all__ = ["AdzunaClient"]
