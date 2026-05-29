"""config_io.py — Cross-platform atomic config-file I/O for Job Matcher.

All writes to the three mutable config files (``config/config.json``,
``config/providers.json``, ``config/profile.json``) must go through
:func:`atomic_config_write`.  **Never call** ``json.dump`` on a config
path directly — doing so is racy under multi-worker Gunicorn (the
Docker prod deployment) and can produce last-write-wins data loss.

Contract
--------
* Acquires an advisory lock on ``<path>.lock`` before reading.
* Re-reads the file under the lock so the caller always mutates the
  freshest on-disk state (prevents TOCTOU races).
* Writes atomically: serialises to ``<path>.tmp``, then renames with
  :func:`os.replace` (which is atomic on both POSIX and Windows NTFS).
* On clean exit the lock is released and the ``.tmp`` sibling is gone
  (consumed by the rename).
* On exception the lock is released, the ``.tmp`` sibling is removed,
  and the original file is left unchanged.

Locking strategy
----------------
:class:`filelock.FileLock` (``pip install filelock``) is used rather
than a hand-rolled ``fcntl``/``msvcrt`` branch because:

* It is cross-platform — works on both the Windows dev machine and the
  Linux prod/CI containers without conditional imports.
* It handles stale-lock recovery automatically.
* It is well-maintained, has no transitive dependencies, and is already
  the standard choice in tools like pip, uv, and hatch.

The previous hand-rolled Windows ``O_EXCL`` spin-lock in
``credentials.save_providers()`` had two problems: it was POSIX-absent
(guarded by ``sys.platform == "win32"``) and it used a separate lock
file path that was not the same convention as ``filelock`` would use.
This module replaces all of that.

ADR
---
See ``docs/adr/adr-001-atomic-config-writes.md`` for the full decision
record.
"""
from __future__ import annotations

import json
import os
from contextlib import contextmanager
from typing import Any, Generator

from filelock import FileLock

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

#: Timeout in seconds to wait for the advisory lock before giving up.
#: Five seconds is generous for a web-request context; background jobs
#: (ingest.py) also use this path and share the same budget.
LOCK_TIMEOUT: float = 5.0


@contextmanager
def atomic_config_write(
    path: str,
    lock_timeout: float = LOCK_TIMEOUT,
) -> Generator[dict[str, Any], None, None]:
    """Read, yield, and atomically write a JSON config file.

    Acquires an advisory file lock on ``<path>.lock``, reads the current
    file contents (or an empty dict when the file is absent), yields the
    parsed dict to the caller for in-place mutation, then writes the
    mutated dict back atomically via a ``<path>.tmp`` → ``os.replace``
    rename.  The lock and any temp file are always cleaned up, even on
    exception.

    Args:
        path: Absolute or relative path to the target JSON config file.
            The file does not need to exist; a missing file is treated as
            ``{}``.
        lock_timeout: Seconds to wait for the advisory lock.  Defaults
            to :data:`LOCK_TIMEOUT` (5 s).  Raise :class:`Timeout
            <filelock.Timeout>` when the budget is exhausted.

    Yields:
        Mutable dict containing the current contents of *path*.  Mutate
        this dict in-place; the changes are written when the ``with``
        block exits cleanly.

    Raises:
        filelock.Timeout: If the lock cannot be acquired within
            *lock_timeout* seconds.
        OSError: If reading or writing the config file fails.

    Example::

        from config_io import atomic_config_write

        with atomic_config_write("config/config.json") as cfg:
            cfg.setdefault("prefilter", {})
            cfg["prefilter"]["title_exclude"].append("intern")
    """
    lock_path = path + ".lock"
    tmp_path = path + ".tmp"
    lock = FileLock(lock_path, timeout=lock_timeout)

    with lock:
        # Re-read under the lock — freshest on-disk state.
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as fh:
                    data: dict[str, Any] = json.load(fh)
            except (json.JSONDecodeError, OSError):
                data = {}
        else:
            data = {}

        _write_ok = False
        try:
            yield data

            # --- Atomic write ---
            with open(tmp_path, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2)
            os.replace(tmp_path, path)
            _write_ok = True
        finally:
            if not _write_ok:
                # Exception path: clean up partial temp file.
                try:
                    if os.path.exists(tmp_path):
                        os.remove(tmp_path)
                except OSError:
                    pass
