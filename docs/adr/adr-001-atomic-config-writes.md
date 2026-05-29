# ADR-001: Atomic Config-File Writes via `config_io.atomic_config_write`

**Status:** Accepted  
**Date:** 2026-05-29  
**Issue:** [#610](https://github.com/glitchwerks/job-matcher/issues/610)

---

## Context

The application manages three mutable JSON config files:

- `config/config.json` — search parameters and prefilter rules
- `config/providers.json` — LLM and job-source credentials
- `config/profile.json` — candidate skills and preferences

In the Docker production deployment, Gunicorn runs with multiple workers. Under
concurrent requests (e.g. two browser tabs saving settings simultaneously, or an
ingest run touching `providers.json` while the UI saves it), two workers can each
read the same file, mutate different keys, and write back independently. The
second write silently overwrites the first — a classic last-write-wins lost-update
race (TOCTOU).

Prior state:

- `credentials.save_providers()` had a hand-rolled Windows-only `O_EXCL` spin-lock
  plus a manual `tmp` → `os.replace` rename. The lock was absent on Linux/POSIX.
- `services.profile_store._write_json_atomic()` had the atomic rename but **no**
  lock — two workers could still race on the read.
- `web.profile.apply_prefilter_suggestions()` used a bare `open(..., "w")` + 
  `json.dump` with no atomicity and no lock.

---

## Decision

Introduce `config_io.py` at the repo root. It exports one public symbol:

```python
@contextmanager
def atomic_config_write(path: str, lock_timeout: float = 5.0) -> Generator[dict, None, None]:
    ...
```

**Contract:**

1. Acquires an advisory file lock on `<path>.lock` before doing anything.
2. Re-reads the file under the lock (freshest on-disk state).
3. Yields the parsed dict to the caller for in-place mutation.
4. On clean exit: writes atomically via `<path>.tmp` → `os.replace`, then
   releases the lock.
5. On exception: does NOT write, removes the `<path>.tmp` if it exists, releases
   the lock, re-raises the exception.

**Locking mechanism: `filelock.FileLock`**

`filelock` (PyPI: `filelock>=3.13.0,<4`) was chosen over a stdlib
`fcntl`/`msvcrt` branch because:

- It is cross-platform: works on Windows (dev machine) and Linux (CI + prod)
  with a single code path.
- It handles stale-lock recovery automatically (via the `timeout` parameter and
  a platform-appropriate strategy).
- It is a tiny library (single file, zero transitive deps) maintained by the same
  ecosystem as pip, uv, and hatch — well-exercised and stable.
- The alternative — conditional `fcntl.flock` on POSIX and `msvcrt.locking` on
  Windows — requires platform branches, is tricky to test, and the previous
  Windows-only spin-lock in `save_providers()` demonstrates how hand-rolled
  solutions are left incomplete.

---

## Consequences

### Positive

- All config writes are serialised at the OS level: no lost updates under
  multi-worker Gunicorn.
- The lock + atomic-rename combination means readers always see either the old
  complete file or the new complete file — never a partial write.
- The Windows dev machine and Linux prod deployment share the same code path.
- One new dependency (`filelock`), pinned in `requirements.txt`.

### Constraints

- **All config writes MUST go through `atomic_config_write`.** Never call
  `json.dump` directly on a config file path. Never use `open(..., "w")` on a
  config path.
- A regression-guard test in `tests/test_config_io.py::TestCodebaseGuard` greps
  the source tree and will fail CI if any module introduces a direct `json.dump`
  on a config path.
- The `filelock` lock file (`<path>.lock`) is a sibling of the config file.
  It is created automatically and should be gitignored (all three config files
  are already gitignored).

---

## Migrated Write Sites

| File | Location | Previous mechanism | After |
|---|---|---|---|
| `credentials.py` | `migrate_from_legacy()` | manual tmp+rename, no lock | `atomic_config_write` |
| `credentials.py` | `save_providers()` | Windows-only O_EXCL spin-lock + tmp+rename | `atomic_config_write` |
| `web/profile.py` | `apply_prefilter_suggestions()` | bare `open(..., "w")` | `atomic_config_write` |
| `services/profile_store.py` | `_write_json_atomic()` | tmp+rename, no lock | thin wrapper over `atomic_config_write` |
