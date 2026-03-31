"""
tests/test_reorder.py — TDD tests for POST /api/providers/reorder.

Covered cases:
- Valid order returns 200 and writes provider_order to providers.json
- Response body contains provider display names in submitted order
- Unknown provider key in order returns 400
- Missing 'order' key in JSON body returns 400
- Non-JSON body returns 400
- Empty order list returns 200 (valid — falls back to registry order at runtime)
- Subset of providers (not all listed) returns 200
- Write failure (OSError) returns 500
"""
from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app as app_module
from app import app as flask_app


@pytest.fixture()
def tmp_providers_path(tmp_path, monkeypatch):
    """Point _PROVIDERS_PATH at a temp file for full isolation."""
    path = str(tmp_path / "providers.json")
    monkeypatch.setattr(app_module, "_PROVIDERS_PATH", path)
    return path


@pytest.fixture()
def client():
    flask_app.config["TESTING"] = True
    with flask_app.test_client() as c:
        yield c


class TestReorderEndpoint:
    def test_valid_order_returns_200(self, client, tmp_providers_path):
        from providers import _PROVIDER_CLASS_MAP
        valid_order = list(_PROVIDER_CLASS_MAP.keys())
        resp = client.post(
            "/api/providers/reorder",
            data=json.dumps({"order": valid_order}),
            content_type="application/json",
        )
        assert resp.status_code == 200

    def test_valid_order_writes_provider_order_to_file(self, client, tmp_providers_path):
        from providers import _PROVIDER_CLASS_MAP
        keys = list(_PROVIDER_CLASS_MAP.keys())
        new_order = list(reversed(keys))
        client.post(
            "/api/providers/reorder",
            data=json.dumps({"order": new_order}),
            content_type="application/json",
        )
        with open(tmp_providers_path, encoding="utf-8") as fh:
            saved = json.load(fh)
        assert saved["provider_order"] == new_order

    def test_response_contains_provider_names_in_order(self, client, tmp_providers_path):
        from providers import _PROVIDER_CLASS_MAP
        keys = list(_PROVIDER_CLASS_MAP.keys())
        new_order = list(reversed(keys))
        resp = client.post(
            "/api/providers/reorder",
            data=json.dumps({"order": new_order}),
            content_type="application/json",
        )
        html = resp.data.decode()
        positions = []
        for key in new_order:
            cls = _PROVIDER_CLASS_MAP[key]
            name = cls.settings_schema()["display_name"]
            positions.append(html.index(name))
        assert positions == sorted(positions), "Provider names not in submitted order in response HTML"

    def test_unknown_provider_key_returns_400(self, client, tmp_providers_path):
        resp = client.post(
            "/api/providers/reorder",
            data=json.dumps({"order": ["anthropic", "not_a_real_provider"]}),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_unknown_provider_does_not_write_to_file(self, client, tmp_providers_path):
        client.post(
            "/api/providers/reorder",
            data=json.dumps({"order": ["unknown_llm"]}),
            content_type="application/json",
        )
        assert not os.path.exists(tmp_providers_path)

    def test_missing_order_key_returns_400(self, client, tmp_providers_path):
        resp = client.post(
            "/api/providers/reorder",
            data=json.dumps({"wrong_key": []}),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_non_json_body_returns_400(self, client, tmp_providers_path):
        resp = client.post(
            "/api/providers/reorder",
            data="not json at all",
            content_type="text/plain",
        )
        assert resp.status_code == 400

    def test_empty_order_returns_200(self, client, tmp_providers_path):
        resp = client.post(
            "/api/providers/reorder",
            data=json.dumps({"order": []}),
            content_type="application/json",
        )
        assert resp.status_code == 200

    def test_empty_order_writes_empty_list_to_file(self, client, tmp_providers_path):
        client.post(
            "/api/providers/reorder",
            data=json.dumps({"order": []}),
            content_type="application/json",
        )
        with open(tmp_providers_path, encoding="utf-8") as fh:
            saved = json.load(fh)
        assert saved["provider_order"] == []

    def test_subset_order_returns_200(self, client, tmp_providers_path):
        from providers import _PROVIDER_CLASS_MAP
        keys = list(_PROVIDER_CLASS_MAP.keys())
        resp = client.post(
            "/api/providers/reorder",
            data=json.dumps({"order": [keys[0]]}),
            content_type="application/json",
        )
        assert resp.status_code == 200

    def test_write_failure_returns_500(self, client, tmp_providers_path, monkeypatch):
        from providers import _PROVIDER_CLASS_MAP
        valid_order = list(_PROVIDER_CLASS_MAP.keys())

        def _failing_save(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(app_module, "save_providers", _failing_save)
        resp = client.post(
            "/api/providers/reorder",
            data=json.dumps({"order": valid_order}),
            content_type="application/json",
        )
        assert resp.status_code == 500

    def test_duplicate_keys_in_order_returns_400(self, client, tmp_providers_path):
        resp = client.post(
            "/api/providers/reorder",
            data=json.dumps({"order": ["anthropic", "anthropic", "gemini"]}),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_duplicate_keys_do_not_write_to_file(self, client, tmp_providers_path):
        client.post(
            "/api/providers/reorder",
            data=json.dumps({"order": ["anthropic", "anthropic"]}),
            content_type="application/json",
        )
        assert not os.path.exists(tmp_providers_path)

    def test_non_list_order_value_returns_400(self, client, tmp_providers_path):
        resp = client.post(
            "/api/providers/reorder",
            data=json.dumps({"order": "anthropic"}),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_non_string_entries_in_order_returns_400(self, client, tmp_providers_path):
        resp = client.post(
            "/api/providers/reorder",
            data=json.dumps({"order": [1, 2, 3]}),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_response_fragment_contains_data_id_attributes(self, client, tmp_providers_path):
        """data-id attributes are required by SortableJS.toArray() — verify they survive the round-trip."""
        from providers import _PROVIDER_CLASS_MAP
        keys = list(_PROVIDER_CLASS_MAP.keys())
        resp = client.post(
            "/api/providers/reorder",
            data=json.dumps({"order": keys}),
            content_type="application/json",
        )
        assert resp.status_code == 200
        html = resp.data.decode()
        for key in keys:
            assert f'data-id="{key}"' in html, f"Missing data-id for provider '{key}' in response fragment"
