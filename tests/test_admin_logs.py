"""
tests/test_admin_logs.py — Tests for /admin/logs list and download routes.

Covers happy paths, timestamp parsing, download behaviour, path traversal
protection, malformed filenames, empty directory, and retention race.
"""

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import app as flask_app


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def client():
    flask_app.config["TESTING"] = True
    with flask_app.test_client() as c:
        yield c


@pytest.fixture()
def log_dir(tmp_path, monkeypatch):
    """Redirect LOG_DIR in paths module and web.admin to a temp directory."""
    import paths
    import web.admin as admin_module  # noqa: PLC0415
    monkeypatch.setattr(paths, "LOG_DIR", tmp_path)
    monkeypatch.setattr(admin_module, "LOG_DIR", tmp_path)
    return tmp_path


def _make_log(log_dir: Path, name: str, content: str = "log content") -> Path:
    """Create a fake log file in log_dir with the given name and content."""
    p = log_dir / name
    p.write_text(content, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# GET /admin/logs — list route
# ---------------------------------------------------------------------------


class TestAdminLogsList:
    def test_happy_path_three_files(self, client, log_dir):
        """Three valid log files are listed, newest first."""
        _make_log(log_dir, "ingest_20260411_143022.log", "run1")
        _make_log(log_dir, "ingest_20260410_090000.log", "run2")
        _make_log(log_dir, "ingest_20260412_010101.log", "run3")

        with patch("db.get_listing_count", return_value=0):
            resp = client.get("/admin/logs")

        assert resp.status_code == 200
        body = resp.data.decode()

        # All three timestamps should appear
        assert "2026-04-11" in body
        assert "2026-04-10" in body
        assert "2026-04-12" in body

        # Newest first — 20260412 should appear before 20260411 and 20260410
        idx_newest = body.index("2026-04-12")
        idx_middle = body.index("2026-04-11")
        idx_oldest = body.index("2026-04-10")
        assert idx_newest < idx_middle < idx_oldest

    def test_timestamp_parsing(self, client, log_dir):
        """File ingest_20260411_143022.log is displayed as '2026-04-11 14:30:22'."""
        _make_log(log_dir, "ingest_20260411_143022.log")

        with patch("db.get_listing_count", return_value=0):
            resp = client.get("/admin/logs")

        body = resp.data.decode()
        assert "2026-04-11 14:30:22" in body

    def test_non_matching_files_excluded(self, client, log_dir):
        """Files that don't match the ingest_*.log pattern are not listed."""
        _make_log(log_dir, "ingest_20260411_143022.log")
        _make_log(log_dir, "app.log")
        _make_log(log_dir, "ingest_bad.log")
        _make_log(log_dir, "README.txt")

        with patch("db.get_listing_count", return_value=0):
            resp = client.get("/admin/logs")

        body = resp.data.decode()
        assert "app.log" not in body
        assert "ingest_bad.log" not in body
        assert "README.txt" not in body

    def test_empty_dir_shows_no_logs_message(self, client, log_dir):
        """When the log directory has no matching files, 'No logs yet' is shown."""
        with patch("db.get_listing_count", return_value=0):
            resp = client.get("/admin/logs")

        body = resp.data.decode()
        assert "No logs yet" in body

    def test_missing_log_dir_returns_empty(self, client, tmp_path, monkeypatch):
        """When LOG_DIR does not exist, the response contains 'No logs yet' (no error)."""
        import paths
        import web.admin as admin_module  # noqa: PLC0415
        nonexistent = tmp_path / "does_not_exist"
        monkeypatch.setattr(paths, "LOG_DIR", nonexistent)
        monkeypatch.setattr(admin_module, "LOG_DIR", nonexistent)

        with patch("db.get_listing_count", return_value=0):
            resp = client.get("/admin/logs")

        assert resp.status_code == 200
        assert "No logs yet" in resp.data.decode()

    def test_size_bytes(self, client, log_dir):
        """Small files show size in bytes."""
        _make_log(log_dir, "ingest_20260411_143022.log", "x" * 512)

        with patch("db.get_listing_count", return_value=0):
            resp = client.get("/admin/logs")

        assert "512 B" in resp.data.decode()

    def test_size_kilobytes(self, client, log_dir):
        """Files >= 1024 bytes show size in KB."""
        _make_log(log_dir, "ingest_20260411_143022.log", "x" * 2048)

        with patch("db.get_listing_count", return_value=0):
            resp = client.get("/admin/logs")

        assert "KB" in resp.data.decode()

    def test_size_megabytes(self, client, log_dir):
        """Files >= 1 MB show size in MB."""
        _make_log(log_dir, "ingest_20260411_143022.log", "x" * (2 * 1024 * 1024))

        with patch("db.get_listing_count", return_value=0):
            resp = client.get("/admin/logs")

        assert "MB" in resp.data.decode()

    def test_download_link_present(self, client, log_dir):
        """Each log row contains a Download link pointing at the correct URL."""
        _make_log(log_dir, "ingest_20260411_143022.log")

        with patch("db.get_listing_count", return_value=0):
            resp = client.get("/admin/logs")

        body = resp.data.decode()
        assert "/admin/logs/ingest_20260411_143022.log/download" in body


# ---------------------------------------------------------------------------
# GET /admin/logs/<filename>/download — download route
# ---------------------------------------------------------------------------


class TestAdminLogDownload:
    def test_download_happy_path(self, client, log_dir):
        """Valid log file is served as an attachment with correct content."""
        expected = "hello from the log file"
        _make_log(log_dir, "ingest_20260411_143022.log", expected)

        resp = client.get("/admin/logs/ingest_20260411_143022.log/download")

        assert resp.status_code == 200
        assert expected in resp.data.decode()
        # Content-Disposition must mark this as an attachment
        disposition = resp.headers.get("Content-Disposition", "")
        assert "attachment" in disposition

    def test_download_content_type(self, client, log_dir):
        """Download response has a text/plain content type."""
        _make_log(log_dir, "ingest_20260411_143022.log", "data")

        resp = client.get("/admin/logs/ingest_20260411_143022.log/download")

        assert resp.status_code == 200
        assert "text/plain" in resp.content_type

    def test_path_traversal_returns_404(self, client, log_dir):
        """Path traversal sequences in the filename return 404."""
        resp = client.get("/admin/logs/..%2F..%2Fetc%2Fpasswd/download")
        assert resp.status_code == 404

    def test_malformed_filename_returns_404(self, client, log_dir):
        """Filenames that don't match the strict pattern return 404."""
        resp = client.get("/admin/logs/ingest_bad.log/download")
        assert resp.status_code == 404

    def test_nonexistent_file_returns_404(self, client, log_dir):
        """A well-formed filename that doesn't exist on disk returns 404."""
        resp = client.get("/admin/logs/ingest_20260411_143022.log/download")
        assert resp.status_code == 404

    def test_retention_race_returns_404(self, client, log_dir):
        """File listed then deleted before download returns 404 (not 500)."""
        p = _make_log(log_dir, "ingest_20260411_143022.log", "data")
        p.unlink()  # Delete between list and download

        resp = client.get("/admin/logs/ingest_20260411_143022.log/download")
        assert resp.status_code == 404

    @pytest.mark.skipif(os.name == "nt", reason="Symlinks require admin on Windows")
    def test_symlink_escape_returns_404(self, client, log_dir, tmp_path):
        """A symlink pointing outside LOG_DIR is rejected with 404."""
        # Create a file strictly outside log_dir in a sibling directory.
        # (log_dir IS tmp_path, so tmp_path itself is inside log_dir — use a
        # subdirectory of tmp_path.parent that is not log_dir.)
        outside_dir = tmp_path.parent / "outside_secret"
        outside_dir.mkdir(exist_ok=True)
        secret = outside_dir / "secret.txt"
        secret.write_text("secret content", encoding="utf-8")

        # Create a symlink inside log_dir that points to the outside file,
        # named to match the log filename pattern
        symlink = log_dir / "ingest_20260411_143022.log"
        symlink.symlink_to(secret)

        resp = client.get("/admin/logs/ingest_20260411_143022.log/download")
        assert resp.status_code == 404
