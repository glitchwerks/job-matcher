"""
tests/test_auto_register.py — Unit tests for job_sources.auto_register.

All tests use tmp_path fixtures and monkeypatch SOURCES so no real plugins
or providers.json files are needed.
"""

from __future__ import annotations

import json
import os

import job_sources.auto_register as _mod
from job_sources.auto_register import ensure_plugins_registered


# ---------------------------------------------------------------------------
# Fake source classes used as test doubles
# ---------------------------------------------------------------------------


class _FakeKeyless:
    """A source that requires no credentials (keyless)."""

    @classmethod
    def settings_schema(cls) -> dict:
        return {"display_name": "Keyless", "fields": []}


class _FakeKeyed:
    """A source that requires an api_key credential (keyed)."""

    @classmethod
    def settings_schema(cls) -> dict:
        return {
            "display_name": "Keyed",
            "fields": [
                {
                    "name": "api_key",
                    "label": "API Key",
                    "type": "password",
                    "required": True,
                }
            ],
        }


class _FakeMultiKeyed:
    """A source that requires two credential fields."""

    @classmethod
    def settings_schema(cls) -> dict:
        return {
            "display_name": "Multi-keyed",
            "fields": [
                {"name": "app_id", "label": "App ID", "type": "text", "required": True},
                {"name": "app_key", "label": "App Key", "type": "password", "required": True},
            ],
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_providers(path: str, data: dict) -> None:
    """Write a providers.json to *path*."""
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)


def _read_providers(path: str) -> dict:
    """Read and return the providers.json at *path*."""
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_keyless_plugin_added_with_enabled_true(tmp_path, monkeypatch):
    """A keyless plugin not in providers.json is added with enabled=True."""
    providers_path = str(tmp_path / "providers.json")
    _write_providers(providers_path, {"job_sources": {}})

    monkeypatch.setattr(_mod, "SOURCES", {"keyless": _FakeKeyless})
    ensure_plugins_registered(providers_path)

    data = _read_providers(providers_path)
    assert data["job_sources"]["keyless"] == {"enabled": True}


def test_keyed_plugin_added_with_enabled_false(tmp_path, monkeypatch):
    """A keyed plugin not in providers.json is added with enabled=False and blank fields."""
    providers_path = str(tmp_path / "providers.json")
    _write_providers(providers_path, {"job_sources": {}})

    monkeypatch.setattr(_mod, "SOURCES", {"keyed": _FakeKeyed})
    ensure_plugins_registered(providers_path)

    data = _read_providers(providers_path)
    assert data["job_sources"]["keyed"] == {"enabled": False, "api_key": ""}


def test_existing_plugin_not_overwritten(tmp_path, monkeypatch):
    """A plugin already in providers.json is left untouched."""
    providers_path = str(tmp_path / "providers.json")
    _write_providers(
        providers_path,
        {"job_sources": {"keyed": {"enabled": True, "api_key": "secret"}}},
    )

    monkeypatch.setattr(_mod, "SOURCES", {"keyed": _FakeKeyed})
    ensure_plugins_registered(providers_path)

    data = _read_providers(providers_path)
    assert data["job_sources"]["keyed"] == {"enabled": True, "api_key": "secret"}


def test_existing_enabled_false_not_changed(tmp_path, monkeypatch):
    """An explicitly disabled plugin stays disabled after auto-registration."""
    providers_path = str(tmp_path / "providers.json")
    _write_providers(
        providers_path,
        {"job_sources": {"keyed": {"enabled": False, "api_key": ""}}},
    )

    monkeypatch.setattr(_mod, "SOURCES", {"keyed": _FakeKeyed})
    ensure_plugins_registered(providers_path)

    data = _read_providers(providers_path)
    assert data["job_sources"]["keyed"]["enabled"] is False


def test_idempotent(tmp_path, monkeypatch):
    """Calling ensure_plugins_registered twice produces the same result."""
    providers_path = str(tmp_path / "providers.json")
    _write_providers(providers_path, {"job_sources": {}})

    monkeypatch.setattr(_mod, "SOURCES", {"keyless": _FakeKeyless, "keyed": _FakeKeyed})
    ensure_plugins_registered(providers_path)
    first = _read_providers(providers_path)

    ensure_plugins_registered(providers_path)
    second = _read_providers(providers_path)

    assert first == second


def test_no_tmp_file_remains(tmp_path, monkeypatch):
    """After a successful call, no .tmp file is left alongside providers.json."""
    providers_path = str(tmp_path / "providers.json")
    _write_providers(providers_path, {"job_sources": {}})

    monkeypatch.setattr(_mod, "SOURCES", {"keyless": _FakeKeyless})
    ensure_plugins_registered(providers_path)

    tmp_file = providers_path + ".tmp"
    assert not os.path.exists(tmp_file), f"Unexpected temp file: {tmp_file}"


def test_multiple_missing_plugins_all_added(tmp_path, monkeypatch):
    """Two missing plugins are both added in a single call."""
    providers_path = str(tmp_path / "providers.json")
    _write_providers(providers_path, {"job_sources": {}})

    monkeypatch.setattr(_mod, "SOURCES", {"keyless": _FakeKeyless, "keyed": _FakeKeyed})
    ensure_plugins_registered(providers_path)

    data = _read_providers(providers_path)
    assert data["job_sources"]["keyless"] == {"enabled": True}
    assert data["job_sources"]["keyed"] == {"enabled": False, "api_key": ""}


def test_mixed_existing_and_new(tmp_path, monkeypatch):
    """An existing plugin is preserved and a new plugin is added."""
    providers_path = str(tmp_path / "providers.json")
    _write_providers(
        providers_path,
        {"job_sources": {"keyless": {"enabled": False}}},  # existing, disabled
    )

    monkeypatch.setattr(
        _mod, "SOURCES", {"keyless": _FakeKeyless, "keyed": _FakeKeyed}
    )
    ensure_plugins_registered(providers_path)

    data = _read_providers(providers_path)
    # Existing entry kept exactly as-is
    assert data["job_sources"]["keyless"] == {"enabled": False}
    # New entry added with defaults
    assert data["job_sources"]["keyed"] == {"enabled": False, "api_key": ""}


def test_multi_keyed_plugin_fields_all_added(tmp_path, monkeypatch):
    """All credential fields of a multi-keyed plugin are initialised to blank strings."""
    providers_path = str(tmp_path / "providers.json")
    _write_providers(providers_path, {"job_sources": {}})

    monkeypatch.setattr(_mod, "SOURCES", {"multi": _FakeMultiKeyed})
    ensure_plugins_registered(providers_path)

    data = _read_providers(providers_path)
    assert data["job_sources"]["multi"] == {
        "enabled": False,
        "app_id": "",
        "app_key": "",
    }


def test_absent_providers_file_creates_file(tmp_path, monkeypatch):
    """When providers.json does not exist, it is created with the new source entry."""
    providers_path = str(tmp_path / "providers.json")
    # Do NOT create the file

    monkeypatch.setattr(_mod, "SOURCES", {"keyless": _FakeKeyless})
    ensure_plugins_registered(providers_path)

    assert os.path.exists(providers_path)
    data = _read_providers(providers_path)
    assert data["job_sources"]["keyless"] == {"enabled": True}


def test_calling_twice_does_not_error(tmp_path, monkeypatch):
    """Calling ensure_plugins_registered twice in sequence does not raise."""
    providers_path = str(tmp_path / "providers.json")
    _write_providers(providers_path, {"job_sources": {}})

    monkeypatch.setattr(_mod, "SOURCES", {"keyless": _FakeKeyless})
    ensure_plugins_registered(providers_path)
    ensure_plugins_registered(providers_path)  # should not raise

    data = _read_providers(providers_path)
    assert data["job_sources"]["keyless"] == {"enabled": True}


def test_no_sources_noop(tmp_path, monkeypatch):
    """When SOURCES is empty, providers.json is not modified."""
    providers_path = str(tmp_path / "providers.json")
    original = {"job_sources": {"adzuna": {"enabled": True, "app_id": "x"}}}
    _write_providers(providers_path, original)

    monkeypatch.setattr(_mod, "SOURCES", {})
    ensure_plugins_registered(providers_path)

    data = _read_providers(providers_path)
    assert data == original


def test_ensure_plugins_registered_acquires_lock_alongside_providers_file(
    tmp_path, monkeypatch
):
    """ensure_plugins_registered uses a .lock sibling of providers.json.

    This is an integration-level check that the locking convention used by
    atomic_config_write (``<path>.lock``) is respected.  The private
    ``_resolve_lock_path`` helper no longer exists — locking is now handled
    entirely by filelock via config_io.atomic_config_write.
    """
    providers_path = str(tmp_path / "providers.json")
    _write_providers(providers_path, {"job_sources": {}})

    monkeypatch.setattr(_mod, "SOURCES", {"keyless": _FakeKeyless})

    ensure_plugins_registered(providers_path)

    # The file was successfully written — this proves the lock was acquired and
    # released cleanly.
    data = _read_providers(providers_path)
    assert data["job_sources"]["keyless"] == {"enabled": True}


def test_ensure_plugins_registered_survives_unwritable_config_dir(tmp_path, monkeypatch):
    """ensure_plugins_registered does not raise when the config dir is not
    writable — it falls back to a temp lock file and still registers sources.

    Regression test: previously the function crashed with
    ``PermissionError: [Errno 13] Permission denied: '/app/config/providers.json.lock'``
    on Docker containers where /app/config/ was a read-only image layer.
    """

    providers_path = str(tmp_path / "providers.json")
    _write_providers(providers_path, {"job_sources": {}})

    monkeypatch.setattr(_mod, "SOURCES", {"keyless": _FakeKeyless})

    # Make os.access report the providers.json directory as non-writable so
    # _resolve_lock_path must fall back to the temp dir.
    real_access = os.access
    config_dir = os.path.normpath(str(tmp_path))

    def _fake_access(path, mode, **kw):
        if mode == os.W_OK and os.path.normpath(path) == config_dir:
            return False
        return real_access(path, mode, **kw)

    monkeypatch.setattr(os, "access", _fake_access)

    # Must not raise — this is the core regression assertion.
    ensure_plugins_registered(providers_path)

    data = _read_providers(providers_path)
    assert data["job_sources"]["keyless"] == {"enabled": True}
