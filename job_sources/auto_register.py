"""
job_sources/auto_register.py — Auto-register newly discovered plugin sources.

When the app starts, any plugin that has been discovered via the plugin loader
but is not yet present in providers.json is automatically added so users can
see it in the Settings UI without needing to edit the file manually.

Public API
----------
* ``ensure_plugins_registered`` — idempotent; safe to call on every startup.
"""

from __future__ import annotations

import logging

from config_io import atomic_config_write
from job_sources import SOURCES  # module-level so tests can monkeypatch it

logger = logging.getLogger(__name__)


def ensure_plugins_registered(providers_path: str) -> None:
    """Add any newly-discovered plugin sources to providers.json if absent.

    Idempotent — calling multiple times produces the same result.
    Keyless sources (fields == []) are added with ``enabled: True``.
    Keyed sources are added with ``enabled: False`` and blank credential
    fields so the user can fill them in via the Settings UI.
    Existing entries are never modified.

    Uses :func:`config_io.atomic_config_write` to prevent TOCTOU races when
    ``app.py`` and ``ingest.py`` start simultaneously.  If the lock cannot be
    acquired, an :class:`~filelock.Timeout` is raised and propagates to the
    caller — the startup path catches it and logs a warning.

    Args:
        providers_path: Path to ``providers.json`` (the unified credential
                        store managed by :mod:`credentials`).
    """
    try:
        with atomic_config_write(providers_path) as providers_data:
            job_sources_cfg: dict = providers_data.setdefault(
                "job_sources", {}
            )

            added: list[str] = []

            for source_key, cls in SOURCES.items():
                if source_key in job_sources_cfg:
                    # Already registered — never touch existing entries.
                    continue

                schema: dict = cls.settings_schema()
                fields: list[dict] = schema.get("fields", [])

                if not fields:
                    # Keyless source — works without any credentials.
                    entry: dict = {"enabled": True}
                else:
                    # Keyed source — add with disabled flag and blank fields
                    # so the user can fill them in via the Settings UI.
                    entry = {"enabled": False}
                    for field in fields:
                        entry[field["name"]] = ""

                job_sources_cfg[source_key] = entry
                added.append(source_key)

            if not added:
                # Nothing to write — raise to abort the write path cleanly.
                # atomic_config_write only writes on a clean exit, so we must
                # not raise here; instead we simply let the block finish
                # (no-op write will still happen but is harmless and idempotent).
                pass

    except (PermissionError, OSError) as exc:
        # Config directory may be read-only (e.g. Docker image layer without
        # a mounted config volume).  Registration still took effect in memory
        # for this process — it just won't persist to disk until the next run
        # with a writable config directory.
        logger.warning(
            "Could not persist auto-registered sources to %s: %s",
            providers_path,
            exc,
        )
        return

    if added:
        logger.info("Auto-registered job sources: %s", added)
