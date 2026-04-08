"""Tests for _configure_file_logging() graceful fallback on PermissionError / OSError.

These tests run without a real filesystem write — they mock logging.FileHandler
to simulate the container scenario where the log directory is owned by root and
the ingest process runs as appuser (uid 1000).
"""

import logging
from unittest.mock import patch

import pytest

from ingest import _configure_file_logging


@pytest.fixture(autouse=True)
def reset_root_logger():
    """Ensure root logger is clean before and after each test."""
    root = logging.getLogger()
    handlers_before = list(root.handlers)
    yield
    for h in list(root.handlers):
        if h not in handlers_before:
            root.removeHandler(h)


def test_no_raise_on_permission_error(tmp_path, monkeypatch):
    """Function must not raise when FileHandler raises PermissionError."""
    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    with patch("logging.FileHandler", side_effect=PermissionError("Permission denied")):
        # Should complete silently — no exception propagated.
        _configure_file_logging()


def test_warning_logged_on_permission_error(tmp_path, monkeypatch, caplog):
    """A WARNING containing 'File logging unavailable' must be emitted."""
    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    with patch("logging.FileHandler", side_effect=PermissionError("Permission denied")):
        with caplog.at_level(logging.WARNING):
            _configure_file_logging()

    assert any(
        "File logging unavailable" in record.message
        for record in caplog.records
        if record.levelno == logging.WARNING
    ), "Expected a WARNING containing 'File logging unavailable' but none was found."


def test_no_file_handler_added_on_permission_error(tmp_path, monkeypatch):
    """No FileHandler should be attached to the root logger when creation fails."""
    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    root_logger = logging.getLogger()
    handlers_before = list(root_logger.handlers)

    with patch("logging.FileHandler", side_effect=PermissionError("Permission denied")):
        _configure_file_logging()

    file_handlers_after = [
        h for h in root_logger.handlers
        if isinstance(h, logging.FileHandler) and h not in handlers_before
    ]
    assert file_handlers_after == [], (
        f"Expected no new FileHandler on root logger, but found: {file_handlers_after}"
    )


def test_no_raise_on_oserror(tmp_path, monkeypatch):
    """Function must not raise when FileHandler raises OSError."""
    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    with patch("logging.FileHandler", side_effect=OSError("Read-only file system")):
        _configure_file_logging()


def test_no_raise_on_makedirs_permission_error(monkeypatch):
    """Function must not raise when os.makedirs raises PermissionError."""
    monkeypatch.setenv("LOG_DIR", "/root/logs")
    with patch("os.makedirs", side_effect=PermissionError("Permission denied")):
        _configure_file_logging()
