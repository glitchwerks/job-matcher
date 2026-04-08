"""
tests/test_pdf_async.py — Tests for async/background PDF import (issue #68).

Covered cases
-------------

POST /profile/import-pdf — sync path (small PDF):
* Returns 200 with success payload when text ≤ threshold
* Does NOT include an "async" field in the response

POST /profile/import-pdf — async path (large PDF):
* Returns 202 with {"async": True, "job_id": "..."} when text > threshold
* job_id is a valid UUID string

GET /profile/import-pdf/status/<job_id>:
* Returns 404 for an unknown job_id
* Returns {"status": "pending"} for a pending job
* Returns {"status": "running"} for a running job
* Returns {"status": "complete", "result": {...}} for a complete job
* Returns {"status": "failed", "error": "..."} for a failed job

Job cleanup:
* _prune_pdf_jobs removes completed jobs older than TTL
* _prune_pdf_jobs keeps completed jobs newer than TTL
* _prune_pdf_jobs keeps running jobs regardless of age
"""

from __future__ import annotations

import io
import json
import os
import sys
import time
import threading
import unittest.mock as mock
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app as app_module
from app import app as flask_app


# ===========================================================================
# Helpers
# ===========================================================================

def _make_minimal_pdf_bytes() -> bytes:
    """Return a small but valid PDF byte string (no pypdf dependency needed —
    we mock _extract_pdf_text so the bytes themselves do not matter)."""
    return b"%PDF-1.4\n%%EOF"


def _make_fake_llm_response() -> dict:
    """Return a realistic parsed LLM response dict."""
    return {
        "primary_skills": [{"skill": "Python", "years": 5, "status": "active"}],
        "education": ["B.S. CS, MIT, 2015"],
        "seniority": "Senior",
        "preferred_industries": ["fintech"],
        "location_center": "Miami, FL",
    }


# ===========================================================================
# Fixtures
# ===========================================================================

@pytest.fixture(autouse=True)
def _clear_pdf_jobs():
    """Ensure _pdf_jobs is empty before and after each test."""
    with app_module._pdf_jobs_lock:
        app_module._pdf_jobs.clear()
    yield
    with app_module._pdf_jobs_lock:
        app_module._pdf_jobs.clear()


@pytest.fixture()
def client():
    flask_app.config["TESTING"] = True
    with flask_app.test_client() as c:
        yield c


@pytest.fixture()
def tmp_providers_path(tmp_path, monkeypatch):
    path = str(tmp_path / "providers.json")
    monkeypatch.setattr(app_module, "_PROVIDERS_PATH", path)
    return path


@pytest.fixture()
def tmp_keys_path(tmp_path, monkeypatch):
    path = str(tmp_path / "keys.json")
    monkeypatch.setattr(app_module, "_KEYS_PATH", path)
    return path


@pytest.fixture()
def tmp_profile_path(tmp_path, monkeypatch):
    path = str(tmp_path / "profile.json")
    monkeypatch.setattr(app_module, "_PROFILE_PATH", path)
    return path


# ===========================================================================
# POST /profile/import-pdf — synchronous path (small PDF)
# ===========================================================================


class TestImportPdfSync:
    """Small PDFs (≤ threshold) must use the synchronous path."""

    def _post(self, client, text: str, mode: str = "fresh"):
        data = {
            "mode": mode,
            "file": (io.BytesIO(_make_minimal_pdf_bytes()), "resume.pdf"),
        }
        with (
            mock.patch.object(app_module, "_extract_pdf_text", return_value=text),
            mock.patch.object(app_module, "build_provider_chain", return_value=["stub"]),
            mock.patch.object(
                app_module,
                "generate_with_fallback",
                return_value=(json.dumps(_make_fake_llm_response()), "anthropic/claude-haiku"),
            ),
            mock.patch.object(app_module, "_load_providers_safe", return_value={}),
        ):
            return client.post(
                "/profile/import-pdf",
                data=data,
                content_type="multipart/form-data",
            )

    def test_small_pdf_returns_200(
        self, client, tmp_providers_path, tmp_keys_path, tmp_profile_path
    ):
        # Text well under threshold
        text = "x" * 100
        resp = self._post(client, text)
        assert resp.status_code == 200

    def test_small_pdf_returns_success_payload(
        self, client, tmp_providers_path, tmp_keys_path, tmp_profile_path
    ):
        text = "x" * 100
        resp = self._post(client, text)
        data = resp.get_json()
        assert data["success"] is True
        assert "profile" in data
        assert "model_used" in data

    def test_small_pdf_has_no_async_field(
        self, client, tmp_providers_path, tmp_keys_path, tmp_profile_path
    ):
        text = "x" * 100
        resp = self._post(client, text)
        data = resp.get_json()
        assert "async" not in data

    def test_text_at_threshold_is_sync(
        self, client, tmp_providers_path, tmp_keys_path, tmp_profile_path
    ):
        """Exactly at the threshold boundary must still use the sync path."""
        text = "x" * app_module._PDF_ASYNC_THRESHOLD
        resp = self._post(client, text)
        assert resp.status_code == 200
        data = resp.get_json()
        assert "async" not in data


# ===========================================================================
# POST /profile/import-pdf — async path (large PDF)
# ===========================================================================


class TestImportPdfAsync:
    """Large PDFs (> threshold) must be dispatched asynchronously."""

    def _post(self, client, text: str, mode: str = "fresh"):
        data = {
            "mode": mode,
            "file": (io.BytesIO(_make_minimal_pdf_bytes()), "big_resume.pdf"),
        }
        with (
            mock.patch.object(app_module, "_extract_pdf_text", return_value=text),
            mock.patch.object(app_module, "_load_providers_safe", return_value={}),
            mock.patch.object(
                app_module,
                "_run_pdf_import_job",
                return_value=None,  # stub — don't actually start a thread
            ),
        ):
            # Also patch Thread so we don't spawn real threads in most tests
            with mock.patch("threading.Thread") as mock_thread:
                mock_thread.return_value.start = mock.MagicMock()
                return client.post(
                    "/profile/import-pdf",
                    data=data,
                    content_type="multipart/form-data",
                ), mock_thread

    def test_large_pdf_returns_202(
        self, client, tmp_providers_path, tmp_keys_path, tmp_profile_path
    ):
        text = "x" * (app_module._PDF_ASYNC_THRESHOLD + 1)
        resp, _ = self._post(client, text)
        assert resp.status_code == 202

    def test_large_pdf_response_has_async_true(
        self, client, tmp_providers_path, tmp_keys_path, tmp_profile_path
    ):
        text = "x" * (app_module._PDF_ASYNC_THRESHOLD + 1)
        resp, _ = self._post(client, text)
        data = resp.get_json()
        assert data.get("async") is True

    def test_large_pdf_response_has_job_id(
        self, client, tmp_providers_path, tmp_keys_path, tmp_profile_path
    ):
        import uuid as _uuid

        text = "x" * (app_module._PDF_ASYNC_THRESHOLD + 1)
        resp, _ = self._post(client, text)
        data = resp.get_json()
        assert "job_id" in data
        # Must be a valid UUID
        _uuid.UUID(data["job_id"])  # raises ValueError if invalid

    def test_large_pdf_job_registered_as_pending(
        self, client, tmp_providers_path, tmp_keys_path, tmp_profile_path
    ):
        """After the POST the job must be visible in _pdf_jobs as 'pending'."""
        text = "x" * (app_module._PDF_ASYNC_THRESHOLD + 1)
        resp, _ = self._post(client, text)
        job_id = resp.get_json()["job_id"]
        with app_module._pdf_jobs_lock:
            assert job_id in app_module._pdf_jobs
            assert app_module._pdf_jobs[job_id]["status"] == "pending"

    def test_large_pdf_spawns_daemon_thread(
        self, client, tmp_providers_path, tmp_keys_path, tmp_profile_path
    ):
        text = "x" * (app_module._PDF_ASYNC_THRESHOLD + 1)
        _, mock_thread_cls = self._post(client, text)
        # Thread constructor must have been called with daemon=True
        call_kwargs = mock_thread_cls.call_args[1]
        assert call_kwargs.get("daemon") is True


# ===========================================================================
# GET /profile/import-pdf/status/<job_id>
# ===========================================================================


class TestImportPdfStatus:

    def test_unknown_job_returns_404(
        self, client, tmp_providers_path, tmp_keys_path
    ):
        resp = client.get("/profile/import-pdf/status/nonexistent-id")
        assert resp.status_code == 404

    def test_pending_job_returns_status_pending(
        self, client, tmp_providers_path, tmp_keys_path
    ):
        job_id = "test-pending-job"
        with app_module._pdf_jobs_lock:
            app_module._pdf_jobs[job_id] = {
                "id": job_id,
                "status": "pending",
                "result": None,
                "error": None,
                "created_at": datetime.now(timezone.utc).timestamp(),
            }
        resp = client.get(f"/profile/import-pdf/status/{job_id}")
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "pending"

    def test_running_job_returns_status_running(
        self, client, tmp_providers_path, tmp_keys_path
    ):
        job_id = "test-running-job"
        with app_module._pdf_jobs_lock:
            app_module._pdf_jobs[job_id] = {
                "id": job_id,
                "status": "running",
                "result": None,
                "error": None,
                "created_at": datetime.now(timezone.utc).timestamp(),
            }
        resp = client.get(f"/profile/import-pdf/status/{job_id}")
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "running"

    def test_complete_job_returns_result(
        self, client, tmp_providers_path, tmp_keys_path
    ):
        job_id = "test-complete-job"
        payload = {"success": True, "profile": {"primary_skills": []}, "model_used": "x/y"}
        with app_module._pdf_jobs_lock:
            app_module._pdf_jobs[job_id] = {
                "id": job_id,
                "status": "complete",
                "result": payload,
                "error": None,
                "created_at": datetime.now(timezone.utc).timestamp(),
            }
        resp = client.get(f"/profile/import-pdf/status/{job_id}")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "complete"
        assert data["result"] == payload

    def test_failed_job_returns_error(
        self, client, tmp_providers_path, tmp_keys_path
    ):
        job_id = "test-failed-job"
        with app_module._pdf_jobs_lock:
            app_module._pdf_jobs[job_id] = {
                "id": job_id,
                "status": "failed",
                "result": None,
                "error": "All LLM providers failed.",
                "created_at": datetime.now(timezone.utc).timestamp(),
            }
        resp = client.get(f"/profile/import-pdf/status/{job_id}")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "failed"
        assert data["error"] == "All LLM providers failed."


# ===========================================================================
# Job cleanup — _prune_pdf_jobs
# ===========================================================================


class TestPrunePdfJobs:

    def test_prune_removes_old_complete_job(self):
        """Complete jobs created more than TTL seconds ago must be pruned."""
        job_id = "old-complete"
        with app_module._pdf_jobs_lock:
            app_module._pdf_jobs[job_id] = {
                "id": job_id,
                "status": "complete",
                "result": {},
                "error": None,
                "created_at": datetime.now(timezone.utc).timestamp() - app_module._PDF_JOB_TTL_SECONDS - 1,
            }
        app_module._prune_pdf_jobs()
        with app_module._pdf_jobs_lock:
            assert job_id not in app_module._pdf_jobs

    def test_prune_removes_old_failed_job(self):
        """Failed jobs older than TTL must also be pruned."""
        job_id = "old-failed"
        with app_module._pdf_jobs_lock:
            app_module._pdf_jobs[job_id] = {
                "id": job_id,
                "status": "failed",
                "result": None,
                "error": "boom",
                "created_at": datetime.now(timezone.utc).timestamp() - app_module._PDF_JOB_TTL_SECONDS - 1,
            }
        app_module._prune_pdf_jobs()
        with app_module._pdf_jobs_lock:
            assert job_id not in app_module._pdf_jobs

    def test_prune_keeps_recent_complete_job(self):
        """Complete jobs within TTL must not be pruned."""
        job_id = "recent-complete"
        with app_module._pdf_jobs_lock:
            app_module._pdf_jobs[job_id] = {
                "id": job_id,
                "status": "complete",
                "result": {},
                "error": None,
                "created_at": datetime.now(timezone.utc).timestamp() - 10,  # 10 s ago
            }
        app_module._prune_pdf_jobs()
        with app_module._pdf_jobs_lock:
            assert job_id in app_module._pdf_jobs

    def test_prune_keeps_running_job_regardless_of_age(self):
        """Running jobs must never be pruned even if they are very old."""
        job_id = "old-running"
        with app_module._pdf_jobs_lock:
            app_module._pdf_jobs[job_id] = {
                "id": job_id,
                "status": "running",
                "result": None,
                "error": None,
                "created_at": 0.0,  # epoch — extremely old
            }
        app_module._prune_pdf_jobs()
        with app_module._pdf_jobs_lock:
            assert job_id in app_module._pdf_jobs

    def test_prune_keeps_pending_job_regardless_of_age(self):
        """Pending jobs must not be pruned (they haven't started yet)."""
        job_id = "old-pending"
        with app_module._pdf_jobs_lock:
            app_module._pdf_jobs[job_id] = {
                "id": job_id,
                "status": "pending",
                "result": None,
                "error": None,
                "created_at": 0.0,
            }
        app_module._prune_pdf_jobs()
        with app_module._pdf_jobs_lock:
            assert job_id in app_module._pdf_jobs


# ===========================================================================
# Integration — _run_pdf_import_job (end-to-end worker logic, no real LLM)
# ===========================================================================


class TestRunPdfImportJob:
    """Test the worker function itself with mocked LLM, without spawning threads."""

    def _setup_job(self, job_id: str):
        """Insert a pending job into _pdf_jobs."""
        with app_module._pdf_jobs_lock:
            app_module._pdf_jobs[job_id] = {
                "id": job_id,
                "status": "pending",
                "result": None,
                "error": None,
                "created_at": datetime.now(timezone.utc).timestamp(),
            }

    def test_worker_sets_complete_on_success(self, tmp_profile_path):
        job_id = "worker-success"
        self._setup_job(job_id)
        llm_resp = json.dumps(_make_fake_llm_response())

        with (
            mock.patch.object(app_module, "build_provider_chain", return_value=["stub"]),
            mock.patch.object(
                app_module, "generate_with_fallback",
                return_value=(llm_resp, "anthropic/claude-haiku"),
            ),
        ):
            app_module._run_pdf_import_job(
                job_id, "resume text", "fresh", {}, tmp_profile_path
            )

        with app_module._pdf_jobs_lock:
            job = app_module._pdf_jobs[job_id]
        assert job["status"] == "complete"
        assert job["result"]["success"] is True
        assert "profile" in job["result"]
        assert job["result"]["model_used"] == "anthropic/claude-haiku"

    def test_worker_sets_failed_when_no_providers(self, tmp_profile_path):
        job_id = "worker-no-providers"
        self._setup_job(job_id)

        with mock.patch.object(app_module, "build_provider_chain", return_value=[]):
            app_module._run_pdf_import_job(
                job_id, "resume text", "fresh", {}, tmp_profile_path
            )

        with app_module._pdf_jobs_lock:
            job = app_module._pdf_jobs[job_id]
        assert job["status"] == "failed"
        assert "No LLM provider" in job["error"]

    def test_worker_sets_failed_when_llm_returns_none(self, tmp_profile_path):
        job_id = "worker-llm-none"
        self._setup_job(job_id)

        with (
            mock.patch.object(app_module, "build_provider_chain", return_value=["stub"]),
            mock.patch.object(app_module, "generate_with_fallback", return_value=None),
        ):
            app_module._run_pdf_import_job(
                job_id, "resume text", "fresh", {}, tmp_profile_path
            )

        with app_module._pdf_jobs_lock:
            job = app_module._pdf_jobs[job_id]
        assert job["status"] == "failed"
        assert "providers failed" in job["error"]

    def test_worker_sets_failed_when_response_unparseable(self, tmp_profile_path):
        job_id = "worker-unparse"
        self._setup_job(job_id)

        with (
            mock.patch.object(app_module, "build_provider_chain", return_value=["stub"]),
            mock.patch.object(
                app_module, "generate_with_fallback",
                return_value=("not valid json {{{{", "x/y"),
            ),
        ):
            app_module._run_pdf_import_job(
                job_id, "resume text", "fresh", {}, tmp_profile_path
            )

        with app_module._pdf_jobs_lock:
            job = app_module._pdf_jobs[job_id]
        assert job["status"] == "failed"
        assert "unparseable" in job["error"]

    def test_worker_transitions_through_running_status(self, tmp_profile_path):
        """The worker must set status=running before calling the LLM."""
        job_id = "worker-running-check"
        self._setup_job(job_id)
        observed_statuses = []

        def fake_generate(prompt, chain, failures):
            with app_module._pdf_jobs_lock:
                observed_statuses.append(app_module._pdf_jobs[job_id]["status"])
            return (json.dumps(_make_fake_llm_response()), "x/y")

        with (
            mock.patch.object(app_module, "build_provider_chain", return_value=["stub"]),
            mock.patch.object(app_module, "generate_with_fallback", side_effect=fake_generate),
        ):
            app_module._run_pdf_import_job(
                job_id, "resume text", "fresh", {}, tmp_profile_path
            )

        assert "running" in observed_statuses
