"""
tests/test_log_pruning.py — Tests for the log file pruning logic in
_configure_file_logging().

The function creates a new timestamped log file and then prunes the oldest
files in the log directory so that at most MAX_LOG_FILES (30) remain.

These tests exercise that pruning behaviour by pointing DB_PATH at a tmp
directory, pre-populating it with fake log files, and asserting the correct
files survive after the call.
"""

import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ingest import _configure_file_logging


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_fake_logs(log_dir: str, count: int) -> list[str]:
    """Create *count* empty ingest_*.log files with incrementing timestamps.

    Filenames follow the pattern ingest_20260101_HHMMSS.log where HHMMSS
    encodes the index (zero-padded to 6 digits).  Because names are fixed-
    width and zero-padded, lexicographic sort equals chronological order.

    Returns the list of filenames (basename only) in creation order
    (oldest first).
    """
    names = []
    for i in range(count):
        # Encode index as HHMMSS: i // 3600, (i % 3600) // 60, i % 60
        hh = i // 3600
        mm = (i % 3600) // 60
        ss = i % 60
        name = f"ingest_20260101_{hh:02d}{mm:02d}{ss:02d}.log"
        open(os.path.join(log_dir, name), "w").close()
        names.append(name)
    return names


def _cleanup_handler(handler: logging.Handler) -> None:
    """Close and remove a handler from the root logger."""
    root = logging.getLogger()
    root.removeHandler(handler)
    handler.close()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestLogPruning:
    """Verify that _configure_file_logging() prunes old log files correctly."""

    def test_prunes_to_30_files(self, tmp_path, monkeypatch):
        """Starting with 35 pre-existing files, the call should leave 30 total.

        Timeline:
          - 35 fake files created before the call
          - _configure_file_logging() creates 1 new file → 36 exist
          - Pruning removes 36 - 30 = 6 oldest
          - Final count: 30
        """
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        _make_fake_logs(str(log_dir), 35)

        # Point LOG_DIR at the tmp log directory so _configure_file_logging()
        # writes into the directory we pre-populated above.
        monkeypatch.setenv("LOG_DIR", str(log_dir))

        # Track handlers added to root logger so we can clean up afterward.
        root = logging.getLogger()
        handlers_before = set(root.handlers)

        try:
            _configure_file_logging()
        finally:
            # Remove any handler added by the call to avoid polluting other tests.
            for h in list(root.handlers):
                if h not in handlers_before:
                    _cleanup_handler(h)

        remaining = [
            f for f in os.listdir(str(log_dir))
            if f.startswith("ingest_") and f.endswith(".log")
        ]
        assert len(remaining) == 30, (
            f"Expected 30 log files after pruning, got {len(remaining)}: {sorted(remaining)}"
        )

    def test_oldest_files_are_removed(self, tmp_path, monkeypatch):
        """The 6 oldest files (by lexicographic filename order) must be gone.

        With 35 pre-existing files + 1 new = 36, the 6 with the smallest
        names (ingest_20260101_000000.log … ingest_20260101_000005.log)
        should be deleted.
        """
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        fake_names = _make_fake_logs(str(log_dir), 35)

        monkeypatch.setenv("LOG_DIR", str(log_dir))

        root = logging.getLogger()
        handlers_before = set(root.handlers)

        try:
            _configure_file_logging()
        finally:
            for h in list(root.handlers):
                if h not in handlers_before:
                    _cleanup_handler(h)

        remaining = set(os.listdir(str(log_dir)))

        # The 6 oldest pre-existing files should be gone.
        expected_deleted = fake_names[:6]
        for name in expected_deleted:
            assert name not in remaining, (
                f"Expected '{name}' to be pruned but it still exists"
            )

    def test_newest_files_are_kept(self, tmp_path, monkeypatch):
        """The 29 newest pre-existing files plus the new file must all survive.

        After pruning, the 29 youngest fake files and the newly created
        ingest_<current-ts>.log should all be present.
        """
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        fake_names = _make_fake_logs(str(log_dir), 35)

        monkeypatch.setenv("LOG_DIR", str(log_dir))

        root = logging.getLogger()
        handlers_before = set(root.handlers)

        try:
            _configure_file_logging()
        finally:
            for h in list(root.handlers):
                if h not in handlers_before:
                    _cleanup_handler(h)

        remaining = set(os.listdir(str(log_dir)))

        # The 29 youngest fake files must still be there.
        expected_kept = fake_names[6:]  # indices 6–34, i.e. 29 files
        for name in expected_kept:
            assert name in remaining, (
                f"Expected '{name}' to be kept but it was removed"
            )

    def test_no_pruning_when_under_limit(self, tmp_path, monkeypatch):
        """When fewer than 30 files exist, no pruning should occur.

        Starting with 10 files + 1 new = 11 total — all 10 originals survive.
        """
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        fake_names = _make_fake_logs(str(log_dir), 10)

        monkeypatch.setenv("LOG_DIR", str(log_dir))

        root = logging.getLogger()
        handlers_before = set(root.handlers)

        try:
            _configure_file_logging()
        finally:
            for h in list(root.handlers):
                if h not in handlers_before:
                    _cleanup_handler(h)

        remaining = set(os.listdir(str(log_dir)))

        for name in fake_names:
            assert name in remaining, (
                f"Expected '{name}' to be kept (under limit) but it was removed"
            )
        # Total should be 11: 10 originals + 1 new.
        assert len(remaining) == 11

    def test_handler_added_to_root_logger(self, tmp_path, monkeypatch):
        """_configure_file_logging() must attach a FileHandler to the root logger."""
        log_dir = tmp_path / "logs"
        log_dir.mkdir()

        monkeypatch.setenv("LOG_DIR", str(log_dir))

        root = logging.getLogger()
        handlers_before = set(root.handlers)
        new_handlers = []

        try:
            _configure_file_logging()
            new_handlers = [h for h in root.handlers if h not in handlers_before]
            assert len(new_handlers) == 1
            assert isinstance(new_handlers[0], logging.FileHandler)
        finally:
            for h in new_handlers:
                _cleanup_handler(h)
