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

import hashlib
import logging
import os
import sys
import tempfile

from job_sources import SOURCES  # module-level so tests can monkeypatch it
from credentials import load_providers, save_providers

logger = logging.getLogger(__name__)


def _resolve_lock_path(lock_path: str) -> str:
    """Return a writable lock path, falling back to the system temp directory.

    The preferred location is *lock_path* itself (alongside providers.json).
    If that directory is not writable — e.g. in a Docker container where
    ``/app/config/`` is a read-only image layer — we derive a deterministic
    fallback path in ``tempfile.gettempdir()`` so the lock still provides
    cross-process coordination for any processes that share the same temp dir.
    """
    lock_dir = os.path.dirname(lock_path) or "."
    if os.access(lock_dir, os.W_OK):
        return lock_path

    # Fallback: name the temp lock file after a hash of the original path so
    # different providers.json files get different locks.
    name_hash = hashlib.sha1(lock_path.encode()).hexdigest()[:12]
    fallback = os.path.join(tempfile.gettempdir(), f"providers_{name_hash}.lock")
    logger.debug(
        "Config directory %r is not writable; using temp lock file %r",
        lock_dir,
        fallback,
    )
    return fallback


def _acquire_lock(lock_path: str):
    """Return an open file handle locked for exclusive access (cross-platform).

    If the preferred lock path is in a non-writable directory, a fallback path
    under ``tempfile.gettempdir()`` is used automatically.

    Returns None if the lock cannot be acquired (another process holds it).
    """
    resolved = _resolve_lock_path(lock_path)
    try:
        fh = open(resolved, "w")
    except OSError as exc:
        logger.warning(
            "Could not open lock file %r (%s); skipping file lock.",
            resolved,
            exc,
        )
        return None
    try:
        if sys.platform == "win32":
            import msvcrt
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh


def _release_lock(fh) -> None:
    """Release the file lock and close the handle."""
    if fh is None:
        return
    try:
        if sys.platform == "win32":
            import msvcrt
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh, fcntl.LOCK_UN)
    finally:
        fh.close()


def ensure_plugins_registered(providers_path: str) -> None:
    """Add any newly-discovered plugin sources to providers.json if absent.

    Idempotent — calling multiple times produces the same result.
    Keyless sources (fields == []) are added with ``enabled: True``.
    Keyed sources are added with ``enabled: False`` and blank credential
    fields so the user can fill them in via the Settings UI.
    Existing entries are never modified.

    Uses a file lock to prevent TOCTOU races when app.py and ingest.py start
    simultaneously.  If the lock cannot be acquired, a warning is logged and
    the function returns without modifying providers.json — it is safer to
    skip auto-registration than to risk corrupting the file.

    Args:
        providers_path: Path to ``providers.json`` (the unified credential
                        store managed by :mod:`credentials`).
    """
    lock_path = providers_path + ".lock"
    lock_fh = _acquire_lock(lock_path)
    if lock_fh is None:
        logger.warning(
            "Could not acquire lock on %s — another process may be updating "
            "providers.json; skipping auto-registration this run.",
            lock_path,
        )
        return

    try:
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
    finally:
        _release_lock(lock_fh)
