"""
tests/test_profile_import.py — Tests for POST /profile/import-pdf (issue #41).

Covered cases
-------------

_extract_pdf_text():
* Returns concatenated text from a multi-page PDF
* Raises ValueError when PdfReader cannot parse the bytes
* Returns empty string for a PDF with no extractable text

_build_import_prompt():
* Always uses _IMPORT_PROMPT_FRESH regardless of mode — LLM only extracts
* Contains resume text in both fresh and merge scenarios
* Does not inject existing profile JSON into the prompt

_parse_import_response():
* Parses valid JSON string correctly
* Strips markdown code fences before parsing
* Returns None for malformed JSON
* Fills default values for missing fields
* Preserves null for location_center when absent

_merge_import_result():
* New skills are appended; existing skills are preserved
* Duplicate skills (case-insensitive) are not added twice
* New education entries are appended; duplicates are skipped
* Seniority is preserved from current when set; filled from import when empty
* Industries are merged without duplicates
* Location is preserved from current when set; filled from import when empty
* Handles missing keys in current profile gracefully

POST /profile/import-pdf:
* Returns 400 when no file is provided
* Returns 400 when uploaded file is not a PDF
* Returns 400 when PDF text cannot be extracted
* Returns 422 when extracted text is too short (< 50 chars)
* Returns 503 when no LLM provider is configured
* Returns 502 when all LLM providers fail
* Returns 502 when LLM returns unparseable response
* Returns 200 with profile JSON on fresh import success
* Returns 200 with profile JSON on merge import success — extraction prompt used, _merge_import_result() merges
* Does NOT write to profile.json (returns JSON for client-side pre-fill only)
* Defaults to fresh mode when mode param is absent
* Uses merge mode when mode=merge is posted
"""

from __future__ import annotations

import io
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest
from pypdf.errors import PdfReadError

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app as app_module
from app import app as flask_app


# ===========================================================================
# Fixtures
# ===========================================================================


@pytest.fixture()
def client():
    flask_app.config["TESTING"] = True
    with flask_app.test_client() as c:
        yield c


@pytest.fixture()
def tmp_profile_path(tmp_path, monkeypatch):
    """Point _PROFILE_PATH at a temp file for isolation."""
    path = str(tmp_path / "profile.json")
    monkeypatch.setattr(app_module, "_PROFILE_PATH", path)
    return path


@pytest.fixture()
def tmp_providers_path(tmp_path, monkeypatch):
    """Point _PROVIDERS_PATH at a temp file so no real credentials are read."""
    path = str(tmp_path / "providers.json")
    monkeypatch.setattr(app_module, "_PROVIDERS_PATH", path)
    return path


@pytest.fixture()
def tmp_keys_path(tmp_path, monkeypatch):
    """Prevent legacy key migration from triggering during tests."""
    path = str(tmp_path / "keys.json")
    monkeypatch.setattr(app_module, "_KEYS_PATH", path)
    return path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_pdf_bytes() -> bytes:
    """Return something that looks like a PDF header but is not real."""
    return b"%PDF-1.4 fake content"


def _make_pdf_upload(content: bytes = b"fake pdf bytes", filename: str = "resume.pdf"):
    """Return a BytesIO suitable for use as a test file upload."""
    return (io.BytesIO(content), filename)


# ===========================================================================
# TestPdfExtraction
# ===========================================================================


class TestPdfExtraction:
    """Tests for _extract_pdf_text()."""

    def test_returns_concatenated_text_from_pages(self):
        """Text from all pages is concatenated into a single string."""
        page1 = MagicMock()
        page1.extract_text.return_value = "Page one text. "
        page2 = MagicMock()
        page2.extract_text.return_value = "Page two text."
        mock_reader = MagicMock()
        mock_reader.pages = [page1, page2]

        with patch("app.PdfReader", return_value=mock_reader):
            result = app_module._extract_pdf_text(b"fake")

        assert result == "Page one text. Page two text."

    def test_raises_value_error_when_pdf_unreadable(self):
        """ValueError is raised (not a raw exception) when PdfReader fails."""
        with patch("app.PdfReader", side_effect=PdfReadError("corrupt")):
            with pytest.raises(ValueError, match="Could not read PDF"):
                app_module._extract_pdf_text(b"not a pdf")

    def test_returns_empty_string_for_pages_with_no_text(self):
        """Pages returning None from extract_text are treated as empty strings."""
        page = MagicMock()
        page.extract_text.return_value = None
        mock_reader = MagicMock()
        mock_reader.pages = [page]

        with patch("app.PdfReader", return_value=mock_reader):
            result = app_module._extract_pdf_text(b"fake")

        assert result == ""


# ===========================================================================
# TestImportPromptConstruction
# ===========================================================================


class TestImportPromptConstruction:
    """Tests for _build_import_prompt()."""

    def test_contains_resume_text(self):
        """Prompt embeds the resume text."""
        prompt = app_module._build_import_prompt("my resume content")
        assert "my resume content" in prompt

    def test_never_includes_existing_profile(self):
        """The extraction-only prompt never injects existing profile data, regardless of mode.

        The LLM's job is to extract from the resume only; _merge_import_result()
        handles merging deterministically in code after parsing.
        """
        prompt = app_module._build_import_prompt("resume here")
        assert "EXISTING PROFILE" not in prompt

    def test_uses_fresh_prompt_template(self):
        """_build_import_prompt always uses _IMPORT_PROMPT_FRESH."""
        prompt = app_module._build_import_prompt("some resume text")
        # The fresh prompt asks for JSON only — verify the sentinel phrase is present
        assert "JSON only:" in prompt


# ===========================================================================
# TestImportResponseParsing
# ===========================================================================


class TestImportResponseParsing:
    """Tests for _parse_import_response()."""

    def test_parses_valid_json(self):
        """A well-formed JSON string is parsed into a dict."""
        raw = json.dumps({
            "primary_skills": [{"skill": "Python", "years": 5, "status": "active"}],
            "education": [
                {"degree_type": "B.S.", "degree_field": "CS", "school": "MIT", "graduation_year": "2015"}
            ],
            "seniority": "Senior",
            "preferred_industries": ["fintech"],
            "location_center": "Miami, FL",
        })
        result = app_module._parse_import_response(raw)
        assert result is not None
        assert result["seniority"] == "Senior"
        assert result["location_center"] == "Miami, FL"

    def test_strips_markdown_fences_before_parsing(self):
        """JSON wrapped in ```json ... ``` code fences is parsed correctly."""
        raw = "```json\n{\"seniority\": \"Mid-level\"}\n```"
        result = app_module._parse_import_response(raw)
        assert result is not None
        assert result["seniority"] == "Mid-level"

    def test_returns_none_for_malformed_json(self):
        """Non-JSON response returns None without raising."""
        result = app_module._parse_import_response("sorry, I cannot extract that.")
        assert result is None

    def test_fills_defaults_for_missing_fields(self):
        """Missing keys are filled with empty defaults rather than raising KeyError."""
        raw = json.dumps({"seniority": "Junior"})
        result = app_module._parse_import_response(raw)
        assert result is not None
        assert result["primary_skills"] == []
        assert result["education"] == []
        assert result["preferred_industries"] == []
        assert result["location_center"] is None

    def test_null_location_center_preserved(self):
        """Explicit null in the JSON is preserved as None in Python."""
        raw = json.dumps({"location_center": None})
        result = app_module._parse_import_response(raw)
        assert result is not None
        assert result["location_center"] is None


# ===========================================================================
# TestImportMergeLogic
# ===========================================================================


class TestImportMergeLogic:
    """Tests for _merge_import_result()."""

    def test_new_skills_are_appended(self):
        """Skills in the import that are absent from current are added."""
        current = {"primary_skills": [{"description": "Python", "years_active": 5, "active": True}]}
        imported = {
            "primary_skills": [{"skill": "Go", "years": 2, "status": "active"}],
            "education": [],
            "seniority": "",
            "preferred_industries": [],
            "location_center": None,
        }
        result = app_module._merge_import_result(current, imported)
        assert any(s.get("description") == "Go" for s in result["primary_skills"] if isinstance(s, dict))

    def test_existing_skills_are_preserved(self):
        """Skills already in the current profile are kept intact."""
        current = {"primary_skills": [{"description": "Python", "years_active": 5, "active": True}]}
        imported = {
            "primary_skills": [{"skill": "Go", "years": 2, "status": "active"}],
            "education": [],
            "seniority": "",
            "preferred_industries": [],
            "location_center": None,
        }
        result = app_module._merge_import_result(current, imported)
        assert any(s.get("description") == "Python" for s in result["primary_skills"] if isinstance(s, dict))

    def test_duplicate_skills_are_not_added(self):
        """A skill already in current is not added again even if case differs."""
        current = {"primary_skills": [{"description": "Python", "years_active": 5, "active": True}]}
        imported = {
            "primary_skills": [{"skill": "python", "years": 3, "status": "active"}],
            "education": [],
            "seniority": "",
            "preferred_industries": [],
            "location_center": None,
        }
        result = app_module._merge_import_result(current, imported)
        python_entries = [s for s in result["primary_skills"] if isinstance(s, dict) and "python" in s.get("description", "").lower()]
        assert len(python_entries) == 1

    def test_new_education_entries_are_appended(self):
        """Structured education entries in the import that are not in current are added."""
        current = {
            "education": [
                {"degree_type": "B.S.", "degree_field": "CS", "school": "MIT", "graduation_year": "2015"}
            ]
        }
        imported = {
            "primary_skills": [],
            "education": [
                {"degree_type": "M.S.", "degree_field": "ML", "school": "Stanford", "graduation_year": "2017"}
            ],
            "seniority": "",
            "preferred_industries": [],
            "location_center": None,
        }
        result = app_module._merge_import_result(current, imported)
        edu_schools = [e["school"] for e in result["education"] if isinstance(e, dict)]
        assert "Stanford" in edu_schools
        assert "MIT" in edu_schools

    def test_duplicate_education_entries_are_skipped(self):
        """Identical structured education entries (all four fields match) are not duplicated."""
        entry = {"degree_type": "B.S.", "degree_field": "CS", "school": "MIT", "graduation_year": "2015"}
        current = {"education": [entry]}
        imported = {
            "primary_skills": [],
            "education": [entry.copy()],
            "seniority": "",
            "preferred_industries": [],
            "location_center": None,
        }
        result = app_module._merge_import_result(current, imported)
        mit_entries = [e for e in result["education"] if isinstance(e, dict) and e.get("school") == "MIT"]
        assert len(mit_entries) == 1

    def test_seniority_is_preserved_from_current_when_set(self):
        """If current profile has a seniority value, it is not overwritten."""
        current = {"seniority": "Staff"}
        imported = {
            "primary_skills": [],
            "education": [],
            "seniority": "Junior",
            "preferred_industries": [],
            "location_center": None,
        }
        result = app_module._merge_import_result(current, imported)
        assert result["seniority"] == "Staff"

    def test_seniority_filled_from_import_when_empty(self):
        """If current seniority is empty or absent, import value is used."""
        current = {"seniority": ""}
        imported = {
            "primary_skills": [],
            "education": [],
            "seniority": "Senior",
            "preferred_industries": [],
            "location_center": None,
        }
        result = app_module._merge_import_result(current, imported)
        assert result["seniority"] == "Senior"

    def test_industries_are_merged_without_duplicates(self):
        """Industries from both current and import appear once each."""
        current = {"preferred_industries": ["fintech"]}
        imported = {
            "primary_skills": [],
            "education": [],
            "seniority": "",
            "preferred_industries": ["fintech", "healthtech"],
            "location_center": None,
        }
        result = app_module._merge_import_result(current, imported)
        assert result["preferred_industries"].count("fintech") == 1
        assert "healthtech" in result["preferred_industries"]

    def test_location_preserved_from_current_when_set(self):
        """If current profile has a location center, it is kept."""
        current = {"location": {"center": "New York, NY"}}
        imported = {
            "primary_skills": [],
            "education": [],
            "seniority": "",
            "preferred_industries": [],
            "location_center": "Austin, TX",
        }
        result = app_module._merge_import_result(current, imported)
        assert result["location_center"] == "New York, NY"

    def test_location_filled_from_import_when_absent(self):
        """If current has no location, import location_center is used."""
        current = {}
        imported = {
            "primary_skills": [],
            "education": [],
            "seniority": "",
            "preferred_industries": [],
            "location_center": "Austin, TX",
        }
        result = app_module._merge_import_result(current, imported)
        assert result["location_center"] == "Austin, TX"

    def test_handles_missing_keys_in_current_profile(self):
        """Empty current profile dict does not raise KeyError."""
        current = {}
        imported = {
            "primary_skills": [{"skill": "Rust", "years": 1, "status": "active"}],
            "education": [
                {"degree_type": "B.S.", "degree_field": "CS", "school": "CMU", "graduation_year": "2020"}
            ],
            "seniority": "Mid-level",
            "preferred_industries": ["systems"],
            "location_center": None,
        }
        result = app_module._merge_import_result(current, imported)
        assert any(s.get("description") == "Rust" for s in result["primary_skills"] if isinstance(s, dict))
        edu_schools = [e["school"] for e in result["education"] if isinstance(e, dict)]
        assert "CMU" in edu_schools


# ===========================================================================
# TestImportEndpoint
# ===========================================================================


class TestImportEndpoint:
    """Tests for POST /profile/import-pdf."""

    def test_returns_400_when_no_file_provided(self, client, tmp_providers_path, tmp_keys_path):
        """Missing file field returns 400."""
        resp = client.post("/profile/import-pdf", data={})
        assert resp.status_code == 400
        body = resp.get_json()
        assert body["success"] is False

    def test_returns_400_when_file_is_not_pdf(self, client, tmp_providers_path, tmp_keys_path):
        """Non-PDF file extension returns 400."""
        data = {"file": (io.BytesIO(b"hello"), "resume.docx")}
        resp = client.post("/profile/import-pdf", data=data, content_type="multipart/form-data")
        assert resp.status_code == 400
        body = resp.get_json()
        assert body["success"] is False
        assert "PDF" in body["error"]

    def test_returns_400_when_pdf_unreadable(self, client, tmp_providers_path, tmp_keys_path):
        """When _extract_pdf_text raises ValueError, endpoint returns 400."""
        data = {"file": (io.BytesIO(b"garbage"), "resume.pdf")}
        with patch("app._extract_pdf_text", side_effect=ValueError("Could not read PDF: bad")):
            resp = client.post("/profile/import-pdf", data=data, content_type="multipart/form-data")
        assert resp.status_code == 400
        body = resp.get_json()
        assert body["success"] is False

    def test_returns_422_when_extracted_text_too_short(self, client, tmp_providers_path, tmp_keys_path):
        """Fewer than 50 meaningful characters after extraction returns 422."""
        data = {"file": (io.BytesIO(b"fake pdf"), "resume.pdf")}
        with patch("app._extract_pdf_text", return_value="too short"):
            resp = client.post("/profile/import-pdf", data=data, content_type="multipart/form-data")
        assert resp.status_code == 422
        body = resp.get_json()
        assert body["success"] is False

    def test_returns_503_when_no_provider_configured(self, client, tmp_providers_path, tmp_keys_path):
        """Empty provider chain returns 503."""
        data = {"file": (io.BytesIO(b"fake"), "resume.pdf")}
        long_text = "x" * 200
        with patch("app._extract_pdf_text", return_value=long_text), \
             patch("app.build_provider_chain", return_value=[]):
            resp = client.post("/profile/import-pdf", data=data, content_type="multipart/form-data")
        assert resp.status_code == 503
        body = resp.get_json()
        assert body["success"] is False

    def test_returns_502_when_all_providers_fail(self, client, tmp_providers_path, tmp_keys_path):
        """generate_with_fallback returning None yields 502."""
        data = {"file": (io.BytesIO(b"fake"), "resume.pdf")}
        long_text = "x" * 200
        mock_provider = MagicMock()
        with patch("app._extract_pdf_text", return_value=long_text), \
             patch("app.build_provider_chain", return_value=[mock_provider]), \
             patch("app.generate_with_fallback", return_value=None):
            resp = client.post("/profile/import-pdf", data=data, content_type="multipart/form-data")
        assert resp.status_code == 502
        body = resp.get_json()
        assert body["success"] is False

    def test_returns_502_when_llm_response_unparseable(self, client, tmp_providers_path, tmp_keys_path):
        """An unparseable LLM response returns 502."""
        data = {"file": (io.BytesIO(b"fake"), "resume.pdf")}
        long_text = "x" * 200
        mock_provider = MagicMock()
        with patch("app._extract_pdf_text", return_value=long_text), \
             patch("app.build_provider_chain", return_value=[mock_provider]), \
             patch("app.generate_with_fallback", return_value=("not json at all", "anthropic/haiku")), \
             patch("app._parse_import_response", return_value=None):
            resp = client.post("/profile/import-pdf", data=data, content_type="multipart/form-data")
        assert resp.status_code == 502
        body = resp.get_json()
        assert body["success"] is False

    def test_returns_200_with_profile_on_fresh_import(self, client, tmp_providers_path, tmp_keys_path):
        """Happy path: fresh import returns 200 with structured profile and model_used."""
        data = {"file": (io.BytesIO(b"fake"), "resume.pdf"), "mode": "fresh"}
        long_text = "x" * 200
        mock_provider = MagicMock()
        parsed_response = {
            "primary_skills": [{"skill": "Python", "years": 5, "status": "active"}],
            "education": [
                {"degree_type": "B.S.", "degree_field": "CS", "school": "MIT", "graduation_year": "2015"}
            ],
            "seniority": "Senior",
            "preferred_industries": ["fintech"],
            "location_center": "Miami, FL",
        }
        with patch("app._extract_pdf_text", return_value=long_text), \
             patch("app.build_provider_chain", return_value=[mock_provider]), \
             patch("app.generate_with_fallback", return_value=(json.dumps(parsed_response), "anthropic/claude-haiku")), \
             patch("app._parse_import_response", return_value=parsed_response):
            resp = client.post("/profile/import-pdf", data=data, content_type="multipart/form-data")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["success"] is True
        assert "profile" in body
        assert body["model_used"] == "anthropic/claude-haiku"
        assert body["profile"]["seniority"] == "Senior"
        assert any(s.get("description") == "Python" for s in body["profile"]["primary_skills"] if isinstance(s, dict))

    def test_returns_200_with_profile_on_merge_import(self, client, tmp_profile_path, tmp_providers_path, tmp_keys_path):
        """Merge mode loads current profile and merges imported data."""
        # Write an existing profile
        existing = {
            "primary_skills": [{"description": "Java", "years_active": 8, "active": True}],
            "education": [
                {"degree_type": "B.S.", "degree_field": "CS", "school": "MIT", "graduation_year": "2015"}
            ],
            "seniority": "Staff",
            "preferred_industries": ["fintech"],
            "location": {"center": "New York, NY"},
        }
        with open(tmp_profile_path, "w") as f:
            json.dump(existing, f)

        data = {"file": (io.BytesIO(b"fake"), "resume.pdf"), "mode": "merge"}
        long_text = "x" * 200
        mock_provider = MagicMock()
        parsed_response = {
            "primary_skills": [{"skill": "Go", "years": 2, "status": "active"}],
            "education": [
                {"degree_type": "M.S.", "degree_field": "ML", "school": "Stanford", "graduation_year": "2017"}
            ],
            "seniority": "Junior",  # should be ignored since current has "Staff"
            "preferred_industries": ["healthtech"],
            "location_center": "Austin, TX",  # should be ignored since current has location
        }
        with patch("app._extract_pdf_text", return_value=long_text), \
             patch("app.build_provider_chain", return_value=[mock_provider]), \
             patch("app.generate_with_fallback", return_value=(json.dumps(parsed_response), "openai/gpt-4o")), \
             patch("app._parse_import_response", return_value=parsed_response):
            resp = client.post("/profile/import-pdf", data=data, content_type="multipart/form-data")

        assert resp.status_code == 200
        body = resp.get_json()
        assert body["success"] is True
        profile = body["profile"]
        # Existing seniority preserved
        assert profile["seniority"] == "Staff"
        # New skill added
        assert any(s.get("description") == "Go" for s in profile["primary_skills"] if isinstance(s, dict))
        # Existing skill preserved
        assert any(s.get("description") == "Java" for s in profile["primary_skills"] if isinstance(s, dict))

    def test_merge_mode_uses_extraction_prompt_not_merge_prompt(
        self, client, tmp_profile_path, tmp_providers_path, tmp_keys_path
    ):
        """Merge mode must send the extraction-only prompt to the LLM (not a merge prompt).

        Regression test for issue #161: the old _IMPORT_PROMPT_MERGE injected the
        full current profile into the LLM prompt, which produced unparseable responses.
        The fix is to always extract via _IMPORT_PROMPT_FRESH and merge in code via
        _merge_import_result().  This test verifies:
        1. _build_import_prompt() is called without profile data (extraction-only).
        2. _merge_import_result() is called to combine the result with the existing profile.
        """
        existing = {
            "primary_skills": [{"description": "Java", "years_active": 8, "active": True}],
            "education": [],
            "seniority": "Staff",
            "preferred_industries": [],
            "location": {"center": "New York, NY"},
        }
        with open(tmp_profile_path, "w") as f:
            json.dump(existing, f)

        data = {"file": (io.BytesIO(b"fake"), "resume.pdf"), "mode": "merge"}
        long_text = "x" * 200
        mock_provider = MagicMock()
        parsed_response = {
            "primary_skills": [{"skill": "Go", "years": 2, "status": "active"}],
            "education": [],
            "seniority": "Junior",
            "preferred_industries": [],
            "location_center": "Austin, TX",
        }

        captured_prompts: list[str] = []

        original_build = app_module._build_import_prompt

        def spy_build(resume_text: str) -> str:
            prompt = original_build(resume_text)
            captured_prompts.append(prompt)
            return prompt

        with patch("app._extract_pdf_text", return_value=long_text), \
             patch("app.build_provider_chain", return_value=[mock_provider]), \
             patch("app.generate_with_fallback", return_value=(json.dumps(parsed_response), "openai/gpt-4o")), \
             patch("app._parse_import_response", return_value=parsed_response), \
             patch("app._build_import_prompt", side_effect=spy_build):
            resp = client.post("/profile/import-pdf", data=data, content_type="multipart/form-data")

        assert resp.status_code == 200
        # The prompt sent to the LLM must not contain existing profile data
        assert len(captured_prompts) == 1, "Expected exactly one prompt to be built"
        assert "EXISTING PROFILE" not in captured_prompts[0]
        # The merge result should still incorporate the existing profile via _merge_import_result()
        profile = resp.get_json()["profile"]
        assert profile["seniority"] == "Staff"  # existing value preserved by code merge
        assert any(s.get("description") == "Java" for s in profile["primary_skills"] if isinstance(s, dict))

    def test_does_not_write_profile_json(self, client, tmp_profile_path, tmp_providers_path, tmp_keys_path):
        """The endpoint must NOT write to profile.json — it returns JSON for client pre-fill only."""
        data = {"file": (io.BytesIO(b"fake"), "resume.pdf"), "mode": "fresh"}
        long_text = "x" * 200
        mock_provider = MagicMock()
        parsed_response = {
            "primary_skills": [{"skill": "Python", "years": 5, "status": "active"}],
            "education": [],
            "seniority": "Senior",
            "preferred_industries": [],
            "location_center": None,
        }
        with patch("app._extract_pdf_text", return_value=long_text), \
             patch("app.build_provider_chain", return_value=[mock_provider]), \
             patch("app.generate_with_fallback", return_value=(json.dumps(parsed_response), "anthropic/haiku")), \
             patch("app._parse_import_response", return_value=parsed_response):
            resp = client.post("/profile/import-pdf", data=data, content_type="multipart/form-data")

        assert resp.status_code == 200
        # profile.json must not exist (nothing was written)
        assert not os.path.exists(tmp_profile_path), "profile.json should NOT be written by import endpoint"

    def test_defaults_to_fresh_mode_when_mode_absent(self, client, tmp_providers_path, tmp_keys_path):
        """Omitting mode parameter defaults to fresh mode (no current profile loaded)."""
        data = {"file": (io.BytesIO(b"fake"), "resume.pdf")}  # no mode field
        long_text = "x" * 200
        mock_provider = MagicMock()
        parsed_response = {
            "primary_skills": [],
            "education": [],
            "seniority": "Mid-level",
            "preferred_industries": [],
            "location_center": None,
        }
        with patch("app._extract_pdf_text", return_value=long_text), \
             patch("app.build_provider_chain", return_value=[mock_provider]), \
             patch("app.generate_with_fallback", return_value=(json.dumps(parsed_response), "anthropic/haiku")), \
             patch("app._parse_import_response", return_value=parsed_response):
            resp = client.post("/profile/import-pdf", data=data, content_type="multipart/form-data")

        assert resp.status_code == 200
        body = resp.get_json()
        assert body["success"] is True

    def test_invalid_mode_treated_as_fresh(self, client, tmp_providers_path, tmp_keys_path):
        """An unrecognized mode value is treated as fresh rather than raising."""
        data = {"file": (io.BytesIO(b"fake"), "resume.pdf"), "mode": "invalid_mode"}
        long_text = "x" * 200
        mock_provider = MagicMock()
        parsed_response = {
            "primary_skills": [],
            "education": [],
            "seniority": "",
            "preferred_industries": [],
            "location_center": None,
        }
        with patch("app._extract_pdf_text", return_value=long_text), \
             patch("app.build_provider_chain", return_value=[mock_provider]), \
             patch("app.generate_with_fallback", return_value=(json.dumps(parsed_response), "anthropic/haiku")), \
             patch("app._parse_import_response", return_value=parsed_response):
            resp = client.post("/profile/import-pdf", data=data, content_type="multipart/form-data")

        assert resp.status_code == 200
