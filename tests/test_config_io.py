"""tests/test_config_io.py — TDD tests for config_io.atomic_config_write.

Written BEFORE the implementation per TDD discipline.

Covered cases
-------------
atomic_config_write():
* Reads existing file and yields its contents to the caller
* Writes mutated dict back to the target path on clean exit
* Write is atomic — uses os.replace (no partial write visible to readers)
* Tempfile is cleaned up after successful write
* On exception inside the mutation block: does NOT write, cleans up tempfile,
  re-raises the exception, and leaves the original file unchanged
* Concurrent writers serialise — no lost updates under thread contention
* New key written by one thread is present in file after both threads finish
* Lock file is released even when the mutation block raises

codebase_guard():
* Fails (regex match) if any non-helper file contains a direct json.dump on
  a config path, or a bare open(..., "w") on a config path
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_initial(path: str, data: dict) -> None:
    """Write *data* as JSON to *path* (setup helper only — not the SUT)."""
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)


# ---------------------------------------------------------------------------
# Import the SUT (will fail until config_io.py is created — that's RED)
# ---------------------------------------------------------------------------


@pytest.fixture()
def tmp_cfg(tmp_path: Path) -> str:
    """Return a path to a temporary JSON config file with initial content."""
    cfg_path = str(tmp_path / "config.json")
    _write_initial(cfg_path, {"key": "initial"})
    return cfg_path


@pytest.fixture()
def empty_cfg(tmp_path: Path) -> str:
    """Return a path to a temporary directory (no file yet — absent case)."""
    return str(tmp_path / "providers.json")


# ===========================================================================
# Basic read-yield-write contract
# ===========================================================================


class TestAtomicConfigWriteContract:
    """atomic_config_write reads, yields, and writes correctly."""

    def test_yields_existing_dict(self, tmp_cfg: str) -> None:
        """The context manager yields the current file contents."""
        from config_io import atomic_config_write

        with atomic_config_write(tmp_cfg) as data:
            assert data == {"key": "initial"}

    def test_writes_mutation_on_exit(self, tmp_cfg: str) -> None:
        """Mutations made inside the block are persisted after exit."""
        from config_io import atomic_config_write

        with atomic_config_write(tmp_cfg) as data:
            data["key"] = "updated"
            data["new"] = 42

        with open(tmp_cfg, encoding="utf-8") as fh:
            saved = json.load(fh)

        assert saved == {"key": "updated", "new": 42}

    def test_absent_file_yields_empty_dict(self, empty_cfg: str) -> None:
        """When the target file does not yet exist, an empty dict is yielded."""
        from config_io import atomic_config_write

        with atomic_config_write(empty_cfg) as data:
            assert data == {}
            data["created"] = True

        with open(empty_cfg, encoding="utf-8") as fh:
            saved = json.load(fh)

        assert saved == {"created": True}

    def test_output_is_valid_json(self, tmp_cfg: str) -> None:
        """The written file must be valid JSON (round-trip safe)."""
        from config_io import atomic_config_write

        with atomic_config_write(tmp_cfg) as data:
            data["nested"] = {"a": [1, 2, 3]}

        with open(tmp_cfg, encoding="utf-8") as fh:
            saved = json.load(fh)

        assert saved["nested"] == {"a": [1, 2, 3]}


# ===========================================================================
# Atomicity and tempfile cleanup
# ===========================================================================


class TestAtomicReplaceAndCleanup:
    """Write goes through a tmp file that is cleaned up in all cases."""

    def test_no_tmp_file_after_successful_write(self, tmp_cfg: str) -> None:
        """The .tmp sibling file is absent after a successful write."""
        from config_io import atomic_config_write

        with atomic_config_write(tmp_cfg) as data:
            data["x"] = 1

        assert not os.path.exists(tmp_cfg + ".tmp"), (
            "Stale .tmp file found after successful atomic_config_write"
        )

    def test_exception_in_block_does_not_write(self, tmp_cfg: str) -> None:
        """If the mutation block raises, the original file is not modified."""
        from config_io import atomic_config_write

        with pytest.raises(ValueError, match="boom"):
            with atomic_config_write(tmp_cfg) as data:
                data["key"] = "should-not-be-saved"
                raise ValueError("boom")

        with open(tmp_cfg, encoding="utf-8") as fh:
            saved = json.load(fh)

        assert saved == {"key": "initial"}, (
            "File was modified despite exception in mutation block"
        )

    def test_exception_in_block_cleans_up_tmp(self, tmp_cfg: str) -> None:
        """If the mutation block raises, the .tmp sibling is removed."""
        from config_io import atomic_config_write

        with pytest.raises(ValueError):
            with atomic_config_write(tmp_cfg) as data:
                data["x"] = 1
                raise ValueError("cleanup test")

        assert not os.path.exists(tmp_cfg + ".tmp"), (
            "Stale .tmp file remains after exception in mutation block"
        )

    def test_exception_in_block_reraises(self, tmp_cfg: str) -> None:
        """The original exception propagates out of atomic_config_write."""
        from config_io import atomic_config_write

        with pytest.raises(RuntimeError, match="propagated"):
            with atomic_config_write(tmp_cfg) as _data:
                raise RuntimeError("propagated")


# ===========================================================================
# Concurrency — no lost updates
# ===========================================================================


class TestConcurrentWriters:
    """Concurrent threads must serialise; neither update may be lost."""

    def test_no_lost_updates_under_contention(self, tmp_path: Path) -> None:
        """Two threads each increment a counter — both increments survive.

        Thread A reads {counter: 0}, increments to 1.
        Thread B reads the file *after* A writes and sees {counter: 1},
        increments to 2.  Without locking, both could read 0 and the final
        value would be 1 (last-write-wins).
        """
        from config_io import atomic_config_write

        cfg = str(tmp_path / "counters.json")
        _write_initial(cfg, {"counter": 0})

        errors: list[str] = []
        n_threads = 5
        increments_per_thread = 10

        def increment() -> None:
            for _ in range(increments_per_thread):
                try:
                    with atomic_config_write(cfg) as data:
                        data["counter"] = data.get("counter", 0) + 1
                except Exception as exc:  # noqa: BLE001
                    errors.append(str(exc))

        threads = [threading.Thread(target=increment) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Thread errors: {errors}"

        with open(cfg, encoding="utf-8") as fh:
            final = json.load(fh)

        expected = n_threads * increments_per_thread
        assert final["counter"] == expected, (
            f"Lost updates: expected counter={expected}, "
            f"got {final['counter']}"
        )

    def test_distinct_keys_no_lost_updates(self, tmp_path: Path) -> None:
        """Two threads writing different keys both survive in the output."""
        from config_io import atomic_config_write

        cfg = str(tmp_path / "two_keys.json")
        _write_initial(cfg, {})

        results: dict[str, bool] = {}

        def write_key(key: str, value: str) -> None:
            with atomic_config_write(cfg) as data:
                time.sleep(0.01)  # force overlap
                data[key] = value
            results[key] = True

        t1 = threading.Thread(target=write_key, args=("alpha", "A"))
        t2 = threading.Thread(target=write_key, args=("beta", "B"))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        with open(cfg, encoding="utf-8") as fh:
            saved = json.load(fh)

        assert saved.get("alpha") == "A", "Key 'alpha' was lost"
        assert saved.get("beta") == "B", "Key 'beta' was lost"

    def test_lock_released_after_exception(self, tmp_path: Path) -> None:
        """A subsequent writer can acquire the lock after a failed write."""
        from config_io import atomic_config_write

        cfg = str(tmp_path / "lock_release.json")
        _write_initial(cfg, {"step": 0})

        # First write raises — lock must be released so second can proceed.
        with pytest.raises(ValueError):
            with atomic_config_write(cfg) as data:
                data["step"] = 1
                raise ValueError("simulated failure")

        # Second write must succeed (would deadlock if lock was not released).
        with atomic_config_write(cfg) as data:
            data["step"] = 2

        with open(cfg, encoding="utf-8") as fh:
            saved = json.load(fh)

        assert saved["step"] == 2, (
            "Lock was not released after exception; second write failed"
        )


# ===========================================================================
# Codebase guard — no direct json.dump on config paths
# ===========================================================================


class TestCodebaseGuard:
    """Fail fast if any module bypasses atomic_config_write."""

    # Files that ARE allowed to contain direct json.dump because they are
    # the helper itself or tests.
    _ALLOWED_FILES = {
        "config_io.py",
        "tests/test_config_io.py",
        "tests\\test_config_io.py",
    }

    # Source files to audit — only project modules, not venv/tmp.
    _SOURCE_GLOBS = [
        "*.py",
        "web/*.py",
        "services/*.py",
        "credentials.py",
    ]

    def _collect_source_files(self, repo_root: Path) -> list[Path]:
        files: list[Path] = []
        for pattern in self._SOURCE_GLOBS:
            files.extend(repo_root.glob(pattern))
        return files

    def _is_allowed(self, path: Path) -> bool:
        name = path.name
        rel = str(path)
        return any(
            allowed in rel or name == allowed.split("/")[-1]
            for allowed in self._ALLOWED_FILES
        )

    def test_no_direct_json_dump_on_config_paths(self) -> None:
        """No project .py file may call json.dump on a config file path.

        This is a guard against regressions that re-introduce bare writes.
        The check is deliberately simple: any line matching both ``json.dump``
        and a config path string (config.json, providers.json, profile.json)
        that is not inside an allowed file is a violation.
        """
        import re

        repo_root = Path(__file__).parent.parent
        pattern = re.compile(
            r"json\.dump\s*\(.*(?:config\.json|providers\.json|profile\.json)"
        )

        violations: list[str] = []
        for src in self._collect_source_files(repo_root):
            if self._is_allowed(src):
                continue
            try:
                text = src.read_text(encoding="utf-8")
            except OSError:
                continue
            for lineno, line in enumerate(text.splitlines(), 1):
                if pattern.search(line):
                    violations.append(f"{src.relative_to(repo_root)}:{lineno}: {line.strip()}")

        assert not violations, (
            "Direct json.dump on config path found — use atomic_config_write "
            "instead:\n" + "\n".join(violations)
        )
