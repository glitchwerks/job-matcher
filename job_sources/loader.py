"""
job_sources/loader.py — Dynamic plugin loader for external job source plugins.

Scans a ``plugins/sources/`` directory for subdirectories that each contain
a ``plugin.py`` (the source implementation) and a ``source.json`` (metadata
and settings schema).  Valid plugins are loaded via ``importlib`` and
registered by their ``source_key``.

Plugin folder layout
--------------------
::

    plugins/sources/
        my_source/
            plugin.py    — must contain exactly one JobSource subclass
            source.json  — metadata: source_key, display_name, description,
                           home_url, fields

Public API
----------
``load_plugins(plugins_dir=None) -> dict[str, type[JobSource]]``
    Discover and load all valid plugins.  Returns a mapping of
    ``source_key -> JobSource subclass``.

NOTE: load_plugins() is called at import time from job_sources/__init__.py
(SOURCES = load_plugins()). This means the plugins/ directory is scanned once
when the package is first imported. Plugins loaded after that point will not
appear in SOURCES until the process restarts.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import sys
from pathlib import Path

from .base import JobSource

_log = logging.getLogger(__name__)

# Required top-level keys every source.json must have.
_REQUIRED_SCHEMA_KEYS = {"source_key", "display_name", "description", "home_url", "fields"}

# Field names that are reserved by the framework and cannot be used in source.json.
_RESERVED_FIELD_NAMES = {"enabled"}


def load_plugins(plugins_dir: Path | str | None = None) -> dict[str, type[JobSource]]:
    """Discover and load all job source plugins from *plugins_dir*.

    Args:
        plugins_dir: Directory to scan for plugin sub-folders.  Defaults to
            ``<repo_root>/plugins/sources/`` relative to this file.

    Returns:
        Mapping of ``source_key`` strings to their ``JobSource`` subclasses.
        Returns an empty dict if *plugins_dir* does not exist or is empty.
        Individual plugin failures never crash the loader — they are skipped
        with a ``logging.warning`` and the next plugin is attempted.
    """
    if plugins_dir is None:
        # Resolve default: repo_root/plugins/sources/
        # This file lives at job_sources/loader.py, so parent.parent == repo root.
        plugins_dir = Path(__file__).parent.parent / "plugins" / "sources"

    plugins_dir = Path(plugins_dir)

    if not plugins_dir.exists():
        return {}

    result: dict[str, type[JobSource]] = {}

    try:
        entries = sorted(os.scandir(plugins_dir), key=lambda e: e.name)
    except OSError as exc:
        _log.warning("Could not scan plugins directory %s: %s", plugins_dir, exc)
        return {}

    for entry in entries:
        # Skip hidden/private folders (underscore prefix) and non-directories.
        if entry.name.startswith("_"):
            _log.debug("Plugin loader: skipping %s (underscore prefix)", entry.name)
            continue
        if not entry.is_dir():
            _log.debug("Plugin loader: skipping %s (not a directory)", entry.name)
            continue

        plugin_dir = Path(entry.path)
        plugin_py = plugin_dir / "plugin.py"
        source_json = plugin_dir / "source.json"

        # --- Validate required files ---
        if not plugin_py.exists():
            _log.warning("Plugin %s: missing plugin.py — skipping", entry.name)
            continue
        if not source_json.exists():
            _log.warning("Plugin %s: missing source.json — skipping", entry.name)
            continue

        # --- Parse source.json ---
        try:
            schema: dict = json.loads(source_json.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            _log.warning("Plugin %s: invalid source.json (%s) — skipping", entry.name, exc)
            continue

        # --- Validate required schema keys ---
        missing_keys = _REQUIRED_SCHEMA_KEYS - set(schema.keys())
        if missing_keys:
            _log.warning(
                "Plugin %s: source.json missing required keys %s — skipping",
                entry.name,
                sorted(missing_keys),
            )
            continue

        # --- Validate source_key is a non-empty string ---
        source_key_raw = schema.get("source_key")
        if not isinstance(source_key_raw, str) or not source_key_raw.strip():
            _log.warning(
                "Plugin %s: source.json 'source_key' must be a non-empty string — skipping",
                entry.name,
            )
            continue

        # --- Enforce source_key matches folder name ---
        if schema["source_key"] != entry.name:
            _log.warning(
                "Plugin %s: source_key %r does not match folder name — skipping",
                entry.name, schema["source_key"],
            )
            continue

        # --- Validate fields type ---
        if not isinstance(schema.get("fields"), list):
            _log.warning("Plugin %s: 'fields' must be a list — skipping", entry.name)
            continue

        # --- Validate each field entry ---
        fields_valid = True
        for field in schema["fields"]:
            if not isinstance(field, dict):
                _log.warning("Plugin %s: each field must be a dict — skipping", entry.name)
                fields_valid = False
                break
            if not isinstance(field.get("name"), str):
                _log.warning(
                    "Plugin %s: each field must have a string 'name' key — skipping",
                    entry.name,
                )
                fields_valid = False
                break
            if field.get("name") in _RESERVED_FIELD_NAMES:
                _log.warning(
                    "Plugin %s: field name %r is reserved — skipping",
                    entry.name, field["name"],
                )
                fields_valid = False
                break

        if not fields_valid:
            continue

        # --- Load plugin.py module ---
        module_name = f"job_sources._plugin_{entry.name}"
        try:
            spec = importlib.util.spec_from_file_location(
                module_name, plugin_py
            )
            module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
            # Register in sys.modules before exec so that the shim modules
            # (job_sources/<name>.py) can import helpers from the plugin module
            # by its registered name.
            sys.modules[module_name] = module
            spec.loader.exec_module(module)  # type: ignore[union-attr]
        except Exception as exc:
            sys.modules.pop(module_name, None)
            _log.warning("Plugin %s: %s", entry.name, exc)
            continue

        # --- Find exactly one JobSource subclass ---
        try:
            subclasses = [
                obj
                for obj in module.__dict__.values()
                if (
                    isinstance(obj, type)
                    and issubclass(obj, JobSource)
                    and obj is not JobSource
                )
            ]

            if len(subclasses) == 0:
                _log.warning(
                    "Plugin %s: plugin.py defines no JobSource subclass — skipping",
                    entry.name,
                )
                continue
            if len(subclasses) > 1:
                _log.warning(
                    "Plugin %s: plugin.py defines %d JobSource subclasses (expected exactly 1) — skipping",
                    entry.name,
                    len(subclasses),
                )
                continue

            cls = subclasses[0]
        except Exception as exc:
            _log.warning("Plugin %s: %s", entry.name, exc)
            continue

        # --- Check for duplicate source_key before registering ---
        source_key: str = schema["source_key"]
        if source_key in result:
            _log.warning(
                "Plugin %s: source_key %r already registered by another plugin — skipping",
                entry.name, source_key,
            )
            continue

        # --- Attach schema and install settings_schema shim ---
        cls._plugin_schema = schema  # type: ignore[attr-defined]

        # The shim replaces the abstract settings_schema() classmethod with one
        # derived from source.json, excluding the internal source_key field.
        cls.settings_schema = classmethod(  # type: ignore[assignment]
            lambda c, _schema=schema: {k: v for k, v in _schema.items() if k != "source_key"}
        )

        result[source_key] = cls
        _log.debug("Loaded plugin %r as source %r", entry.name, source_key)

    return result
