"""
tests/test_source_json.py — Validates every plugin's source.json.

Checks:
  - source.json exists in each plugin directory
  - It is valid JSON
  - It contains all required top-level keys
  - source_key matches the plugin folder name
  - Every field entry has the required keys
"""

import json
from pathlib import Path
import pytest

PLUGINS_DIR = Path(__file__).parent.parent / "plugins" / "sources"
REQUIRED_KEYS = {"source_key", "display_name", "description", "home_url", "fields"}
REQUIRED_FIELD_KEYS = {"name", "label", "type", "required"}


def _plugin_dirs():
    return [p for p in sorted(PLUGINS_DIR.iterdir())
            if p.is_dir() and not p.name.startswith("_")]


@pytest.mark.parametrize("plugin_dir", _plugin_dirs(), ids=lambda p: p.name)
def test_source_json_exists(plugin_dir):
    assert (plugin_dir / "source.json").exists()


@pytest.mark.parametrize("plugin_dir", _plugin_dirs(), ids=lambda p: p.name)
def test_source_json_valid_json(plugin_dir):
    data = json.loads((plugin_dir / "source.json").read_text())
    assert isinstance(data, dict)


@pytest.mark.parametrize("plugin_dir", _plugin_dirs(), ids=lambda p: p.name)
def test_source_json_required_keys(plugin_dir):
    data = json.loads((plugin_dir / "source.json").read_text())
    assert REQUIRED_KEYS <= data.keys()


@pytest.mark.parametrize("plugin_dir", _plugin_dirs(), ids=lambda p: p.name)
def test_source_key_matches_folder(plugin_dir):
    data = json.loads((plugin_dir / "source.json").read_text())
    assert data["source_key"] == plugin_dir.name


@pytest.mark.parametrize("plugin_dir", _plugin_dirs(), ids=lambda p: p.name)
def test_fields_have_required_keys(plugin_dir):
    data = json.loads((plugin_dir / "source.json").read_text())
    for field in data["fields"]:
        assert REQUIRED_FIELD_KEYS <= field.keys(), f"Field {field} missing keys"
