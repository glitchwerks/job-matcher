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

from job_sources import SOURCES  # module-level so tests can monkeypatch it
from credentials import load_providers, save_providers

logger = logging.getLogger(__name__)


def ensure_plugins_registered(providers_path: str) -> None:
    """Add any newly-discovered plugin sources to providers.json if absent.

    Idempotent — calling multiple times produces the same result.
    Keyless sources (fields == []) are added with ``enabled: True``.
    Keyed sources are added with ``enabled: False`` and blank credential
    fields so the user can fill them in via the Settings UI.
    Existing entries are never modified.

    Args:
        providers_path: Path to ``providers.json`` (the unified credential
                        store managed by :mod:`credentials`).
    """

    # --- Load existing providers data (start from skeleton on any failure) ---
    try:
        providers_data = load_providers(providers_path)
    except Exception:
        providers_data = {"job_sources": {}}

    job_sources_cfg: dict = providers_data.setdefault("job_sources", {})

    added: list[str] = []

    for source_key, cls in SOURCES.items():
        if source_key in job_sources_cfg:
            # Already registered — never touch existing entries.
            continue

        schema: dict = cls.settings_schema()
        fields: list[dict] = schema.get("fields", [])

        if not fields:
            # Keyless source — works without any credentials; enable immediately.
            entry: dict = {"enabled": True}
        else:
            # Keyed source — add with disabled flag and blank credential fields
            # so the user can see and fill them in via the Settings UI.
            entry = {"enabled": False}
            for field in fields:
                entry[field["name"]] = ""

        job_sources_cfg[source_key] = entry
        added.append(source_key)

    if added:
        # Pass only the newly-added entries so save_providers deep-merges them
        # without touching any existing keys in providers.json.
        updates = {"job_sources": {key: job_sources_cfg[key] for key in added}}
        save_providers(updates, providers_path)
        logger.info("Auto-registered job sources: %s", added)
