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

_build_import_prompt() with suggest_filters (issue #251):
* Toggle off — prompt byte-for-byte identical to base (no prefilter section)
* Toggle off — prompt does not contain prefilter_suggestions key
* Toggle on — prompt contains prefilter_suggestions section
* Toggle on — prompt still ends with JSON-only sentinel
* Toggle on — prompt explicitly names title_include and title_exclude
* Toggle on — resume text still injected
* Toggle on — prompt is longer than base prompt
* Toggle on — does NOT mention require_contract_time or require_contract_type

_parse_import_response() prefilter validation (issue #251):
* Disjoint include/exclude arrays accepted and normalised to lowercase
* Overlapping terms cause the whole response to be rejected (returns None)
* Overlap detection is case-insensitive
* Non-dict prefilter_suggestions value is silently dropped
* Empty arrays are valid
* Absent key is fine (response accepted without error)

_merge_prefilter_suggestions() (issue #251):
* Empty existing prefilter uses suggestions directly
* New include terms appended; duplicates skipped (case-insensitive)
* New exclude terms appended
* Other prefilter keys (require_contract_*) preserved unchanged
* User-added terms never removed
* Empty suggestions leave existing unchanged

POST /profile/import-pdf — suggest_filters toggle (issue #251):
* Toggle absent → prefilter_suggestions absent from response
* Toggle on → prefilter_suggestions present in response
* Toggle on → _build_import_prompt called with suggest_filters=True
* Toggle absent → _build_import_prompt called with suggest_filters=False
* Toggle on but LLM returns no suggestions → key absent from response

POST /api/apply-prefilter-suggestions (issue #251):
* Returns 400 for missing arrays
* Returns 400 for non-JSON body
* Returns 400 when include/exclude overlap (disjoint-set guard)
* Fresh config: writes new prefilter block
* Existing prefilter: unions without removing user terms
* Other prefilter keys preserved
* Missing config.json returns 500
* Duplicate terms on apply are deduplicated
"""

from __future__ import annotations

import io
import json
import os
import re
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

        with patch("services.pdf_import.PdfReader", return_value=mock_reader):
            result = app_module._extract_pdf_text(b"fake")

        assert result == "Page one text. Page two text."

    def test_raises_value_error_when_pdf_unreadable(self):
        """ValueError is raised (not a raw exception) when PdfReader fails."""
        with patch("services.pdf_import.PdfReader", side_effect=PdfReadError("corrupt")):
            with pytest.raises(ValueError, match="Could not read PDF"):
                app_module._extract_pdf_text(b"not a pdf")

    def test_returns_empty_string_for_pages_with_no_text(self):
        """Pages returning None from extract_text are treated as empty strings."""
        page = MagicMock()
        page.extract_text.return_value = None
        mock_reader = MagicMock()
        mock_reader.pages = [page]

        with patch("services.pdf_import.PdfReader", return_value=mock_reader):
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

        def spy_build(resume_text: str, suggest_filters: bool = False) -> str:
            prompt = original_build(resume_text, suggest_filters=suggest_filters)
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


# ===========================================================================
# TestNormaliseEducation
# ===========================================================================


class TestNormaliseEducation:
    """Unit tests for _normalise_education().

    Covered cases
    -------------
    * Flat string with short degree abbreviation and year
    * Flat string with dotted degree abbreviation (B.S.)
    * Flat string with long-form degree type (Master of Science in ...)
    * Flat string with no year — graduation_year defaults to ""
    * Flat string that is entirely unparseable — degree_field gets whole string
    * Dict with missing keys — absent keys filled with ""
    * Well-formed dict — passed through unchanged
    * Mixed list of strings and dicts — all normalised to structured objects
    """

    def test_flat_string_with_degree_school_year(self):
        """'BS Computer Engineering, Georgia Institute of Technology, 2016' parses fully."""
        result = app_module._normalise_education(
            ["BS Computer Engineering, Georgia Institute of Technology, 2016"]
        )
        assert len(result) == 1
        entry = result[0]
        assert entry["degree_type"].upper() in {"BS", "B.S."}
        assert "Computer Engineering" in entry["degree_field"]
        assert "Georgia Institute of Technology" in entry["school"]
        assert entry["graduation_year"] == "2016"

    def test_flat_string_with_dotted_degree(self):
        """'B.S. Computer Science, MIT, 2016' handles dots in degree abbreviation."""
        result = app_module._normalise_education(["B.S. Computer Science, MIT, 2016"])
        assert len(result) == 1
        entry = result[0]
        assert "B.S." in entry["degree_type"]
        assert "Computer Science" in entry["degree_field"]
        assert entry["school"] == "MIT"
        assert entry["graduation_year"] == "2016"

    def test_flat_string_masters(self):
        """'Master of Science in Data Science, Stanford University, 2020' handles long-form type."""
        result = app_module._normalise_education(
            ["Master of Science in Data Science, Stanford University, 2020"]
        )
        assert len(result) == 1
        entry = result[0]
        assert "Master of Science" in entry["degree_type"]
        assert "Data Science" in entry["degree_field"]
        assert "Stanford University" in entry["school"]
        assert entry["graduation_year"] == "2020"

    def test_flat_string_no_year(self):
        """Flat string without a year leaves graduation_year as empty string."""
        result = app_module._normalise_education(["BS Computer Science, Some University"])
        assert len(result) == 1
        assert result[0]["graduation_year"] == ""
        assert result[0]["school"] == "Some University"

    def test_flat_string_unparseable(self):
        """Completely unparseable string falls back: degree_field gets the whole text."""
        result = app_module._normalise_education(["some random text"])
        assert len(result) == 1
        entry = result[0]
        assert entry["degree_field"] == "some random text"
        assert entry["degree_type"] == ""
        assert entry["school"] == ""
        assert entry["graduation_year"] == ""

    def test_dict_missing_keys(self):
        """Dict with only some keys present — missing keys filled with empty strings."""
        result = app_module._normalise_education([{"degree_field": "CS", "school": "MIT"}])
        assert len(result) == 1
        entry = result[0]
        assert entry["degree_field"] == "CS"
        assert entry["school"] == "MIT"
        assert entry["degree_type"] == ""
        assert entry["graduation_year"] == ""

    def test_dict_well_formed(self):
        """Fully-specified dict is passed through unchanged."""
        edu = {
            "degree_type": "B.S.",
            "degree_field": "Computer Science",
            "school": "MIT",
            "graduation_year": "2015",
        }
        result = app_module._normalise_education([edu])
        assert len(result) == 1
        assert result[0] == edu

    def test_mixed_list(self):
        """List containing both strings and dicts — all normalised to structured objects."""
        entries = [
            "BS Computer Engineering, Georgia Tech, 2016",
            {"degree_type": "M.S.", "degree_field": "ML", "school": "Stanford", "graduation_year": "2018"},
        ]
        result = app_module._normalise_education(entries)
        assert len(result) == 2
        # Both entries must have all four keys
        for entry in result:
            assert "degree_type" in entry
            assert "degree_field" in entry
            assert "school" in entry
            assert "graduation_year" in entry
        # String entry should be parsed
        assert result[0]["graduation_year"] == "2016"
        # Dict entry should pass through intact
        assert result[1]["school"] == "Stanford"

    def test_flat_string_year_at_beginning(self):
        """Year appearing at the start of the string is still extracted correctly."""
        result = app_module._normalise_education(["2016 BS Computer Science, MIT"])
        assert result[0]["graduation_year"] == "2016"
        assert result[0]["degree_type"] == "BS"
        assert result[0]["school"] == "MIT"


# ===========================================================================
# TestBuildImportPromptFilters — issue #251
# ===========================================================================


class TestBuildImportPromptFilters:
    """Tests for _build_import_prompt() with suggest_filters toggle (issue #251)."""

    def test_toggle_off_prompt_identical_to_base(self):
        """When suggest_filters=False the prompt is byte-for-byte the base prompt."""
        default_prompt = app_module._build_import_prompt("resume text")
        explicit_off = app_module._build_import_prompt(
            "resume text", suggest_filters=False
        )
        assert default_prompt == explicit_off

    def test_toggle_off_does_not_contain_prefilter_section(self):
        """Prompt without toggle contains no mention of prefilter_suggestions."""
        prompt = app_module._build_import_prompt("some resume", suggest_filters=False)
        assert "prefilter_suggestions" not in prompt

    def test_toggle_on_contains_prefilter_section(self):
        """Prompt with suggest_filters=True mentions prefilter_suggestions."""
        prompt = app_module._build_import_prompt("some resume", suggest_filters=True)
        assert "prefilter_suggestions" in prompt

    def test_toggle_on_still_ends_with_json_only_sentinel(self):
        """Prompt with toggle still ends with the 'JSON only:' sentinel."""
        prompt = app_module._build_import_prompt("some resume", suggest_filters=True)
        assert prompt.rstrip().endswith("JSON only:")

    def test_toggle_on_contains_title_include_and_exclude(self):
        """Extended prompt explicitly names title_include and title_exclude."""
        prompt = app_module._build_import_prompt("some resume", suggest_filters=True)
        assert "title_include" in prompt
        assert "title_exclude" in prompt

    def test_toggle_on_still_contains_resume_text(self):
        """Resume text is injected into the extended prompt."""
        prompt = app_module._build_import_prompt(
            "my special resume", suggest_filters=True
        )
        assert "my special resume" in prompt

    def test_toggle_on_prompt_differs_from_base(self):
        """The extended prompt is longer than the base prompt."""
        base = app_module._build_import_prompt("resume text", suggest_filters=False)
        extended = app_module._build_import_prompt(
            "resume text", suggest_filters=True
        )
        assert len(extended) > len(base)

    def test_toggle_on_explicitly_excludes_contract_fields(self):
        """Extended prompt tells the LLM not to generate require_contract_* fields.

        The spec says these are user preferences, not resume-derived.  The
        prompt extension must mention them in a negative/exclusion context
        (e.g. "Do NOT include ...") rather than as fields to extract.
        """
        prompt = app_module._build_import_prompt("some resume", suggest_filters=True)
        # The prompt must contain a "Do NOT" or equivalent exclusion instruction
        # covering the contract fields — not a positive extraction request.
        prompt_lower = prompt.lower()
        # Verify the exclusion instruction is present.
        assert "do not" in prompt_lower or "not include" in prompt_lower
        # Verify neither field appears as a top-level JSON key to extract
        # (i.e., not listed with a leading dash-space like "- \"require_...\"").
        assert '- "require_contract_time"' not in prompt
        assert '- "require_contract_type"' not in prompt


# ===========================================================================
# TestParseImportResponsePrefilter — issue #251
# ===========================================================================


class TestParseImportResponsePrefilter:
    """Tests for _parse_import_response() prefilter_suggestions validation."""

    def _base_response(self, **extra) -> str:
        """Return a JSON string with base fields plus any extras."""
        data = {
            "primary_skills": [],
            "education": [],
            "seniority": "Senior",
            "preferred_industries": [],
            "location_center": None,
        }
        data.update(extra)
        return json.dumps(data)

    def test_valid_disjoint_suggestions_accepted(self):
        """Disjoint title_include / title_exclude are kept in the result."""
        raw = self._base_response(
            prefilter_suggestions={
                "title_include": ["engineer", "developer"],
                "title_exclude": ["manager", "director"],
            }
        )
        result = app_module._parse_import_response(raw)
        assert result is not None
        pf = result["prefilter_suggestions"]
        assert pf["title_include"] == ["engineer", "developer"]
        assert pf["title_exclude"] == ["manager", "director"]

    def test_overlapping_suggestions_dropped_profile_preserved(self):
        """Overlapping include/exclude terms drop prefilter_suggestions; profile is returned."""
        raw = self._base_response(
            prefilter_suggestions={
                "title_include": ["engineer", "manager"],
                "title_exclude": ["manager", "director"],
            }
        )
        result = app_module._parse_import_response(raw)
        # Core profile must be returned — not None — despite bad suggestions.
        assert result is not None
        assert result["seniority"] == "Senior"
        # The malformed suggestions section must be absent.
        assert "prefilter_suggestions" not in result

    def test_overlap_is_case_insensitive(self):
        """Case-insensitive overlap still drops suggestions but preserves profile."""
        raw = self._base_response(
            prefilter_suggestions={
                "title_include": ["Engineer"],
                "title_exclude": ["engineer"],
            }
        )
        result = app_module._parse_import_response(raw)
        assert result is not None
        assert result["seniority"] == "Senior"
        assert "prefilter_suggestions" not in result

    def test_suggestions_normalised_to_lowercase(self):
        """Returned title_include / title_exclude are always lowercase."""
        raw = self._base_response(
            prefilter_suggestions={
                "title_include": ["ENGINEER", "Developer"],
                "title_exclude": ["Manager"],
            }
        )
        result = app_module._parse_import_response(raw)
        assert result is not None
        pf = result["prefilter_suggestions"]
        assert pf["title_include"] == ["engineer", "developer"]
        assert pf["title_exclude"] == ["manager"]

    def test_empty_suggestions_accepted(self):
        """Empty include and exclude arrays are valid (no overlap possible)."""
        raw = self._base_response(
            prefilter_suggestions={"title_include": [], "title_exclude": []}
        )
        result = app_module._parse_import_response(raw)
        assert result is not None
        pf = result["prefilter_suggestions"]
        assert pf["title_include"] == []
        assert pf["title_exclude"] == []

    def test_non_dict_suggestions_key_is_dropped(self):
        """A non-dict prefilter_suggestions value is silently dropped."""
        raw = self._base_response(prefilter_suggestions=["bad", "value"])
        result = app_module._parse_import_response(raw)
        assert result is not None
        assert "prefilter_suggestions" not in result

    def test_absent_suggestions_key_is_fine(self):
        """Response without prefilter_suggestions is accepted without error."""
        raw = self._base_response()
        result = app_module._parse_import_response(raw)
        assert result is not None
        assert "prefilter_suggestions" not in result

    def test_list_too_long_drops_suggestions_profile_preserved(self):
        """title_include list exceeding _MAX_PATTERNS_PER_LIST drops suggestions only."""
        too_many = [f"term{i}" for i in range(app_module._MAX_PATTERNS_PER_LIST + 1)]
        raw = self._base_response(
            prefilter_suggestions={"title_include": too_many, "title_exclude": []}
        )
        result = app_module._parse_import_response(raw)
        assert result is not None
        assert result["seniority"] == "Senior"
        assert "prefilter_suggestions" not in result

    def test_pattern_too_long_drops_suggestions_profile_preserved(self):
        """A pattern string exceeding _MAX_PATTERN_LEN drops suggestions only."""
        long_pattern = "a" * (app_module._MAX_PATTERN_LEN + 1)
        raw = self._base_response(
            prefilter_suggestions={"title_include": [long_pattern], "title_exclude": []}
        )
        result = app_module._parse_import_response(raw)
        assert result is not None
        assert result["seniority"] == "Senior"
        assert "prefilter_suggestions" not in result

    def test_fence_wrapped_response_parsed_correctly(self):
        """JSON wrapped in ```json fences is parsed; profile and suggestions both returned."""
        payload = {
            "primary_skills": [],
            "education": [],
            "seniority": "Mid-level",
            "preferred_industries": [],
            "location_center": None,
            "prefilter_suggestions": {
                "title_include": ["engineer"],
                "title_exclude": ["intern"],
            },
        }
        raw = "```json\n" + json.dumps(payload) + "\n```"
        result = app_module._parse_import_response(raw)
        assert result is not None
        assert result["seniority"] == "Mid-level"
        pf = result["prefilter_suggestions"]
        assert pf["title_include"] == ["engineer"]
        assert pf["title_exclude"] == ["intern"]

    def test_truncated_json_returns_none(self):
        """Truncated (unparseable) JSON returns None — core parse failure."""
        raw = '{"seniority": "Senior", "prefilter_suggestions": {"title_include": ["eng'
        result = app_module._parse_import_response(raw)
        assert result is None

    def test_missing_prefilter_suggestions_key_not_an_error(self):
        """LLM response that omits prefilter_suggestions entirely is accepted."""
        raw = json.dumps({
            "primary_skills": [{"skill": "Python", "years": 3, "status": "active"}],
            "education": [],
            "seniority": "Senior",
            "preferred_industries": [],
            "location_center": None,
        })
        result = app_module._parse_import_response(raw)
        assert result is not None
        assert "prefilter_suggestions" not in result
        assert result["seniority"] == "Senior"

    def test_endpoint_returns_200_with_profile_when_suggestions_malformed(
        self, client, tmp_providers_path, tmp_keys_path
    ):
        """When prefilter_suggestions is malformed the endpoint still returns 200 with profile.

        Regression test for issue #271: validation failures in the optional
        prefilter_suggestions block must NOT 502 the entire response.
        """
        long_text = "x" * 200
        mock_provider = MagicMock()
        # Simulate LLM returning overlapping suggestions (a common LLM variance).
        raw_llm = json.dumps({
            "primary_skills": [],
            "education": [],
            "seniority": "Senior",
            "preferred_industries": [],
            "location_center": None,
            "prefilter_suggestions": {
                "title_include": ["engineer", "manager"],
                "title_exclude": ["manager"],  # overlap — would have caused old 502
            },
        })
        with patch("app._extract_pdf_text", return_value=long_text), \
             patch("app.build_provider_chain", return_value=[mock_provider]), \
             patch("app.generate_with_fallback", return_value=(raw_llm, "anthropic/haiku")):
            resp = client.post(
                "/profile/import-pdf",
                data={
                    "file": (io.BytesIO(b"fake"), "resume.pdf"),
                    "suggest_filters": "1",
                },
                content_type="multipart/form-data",
            )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["success"] is True
        assert body["profile"]["seniority"] == "Senior"
        # Malformed suggestions must be absent; profile is intact.
        assert "prefilter_suggestions" not in body


# ===========================================================================
# TestMergePrefilterSuggestions — issue #251
# ===========================================================================


class TestMergePrefilterSuggestions:
    """Tests for _merge_prefilter_suggestions() (issue #251)."""

    def test_fresh_empty_prefilter_uses_suggestions_directly(self):
        """When existing prefilter is empty the suggestions become the result."""
        result = app_module._merge_prefilter_suggestions(
            {},
            {"title_include": ["engineer"], "title_exclude": ["director"]},
        )
        assert result["title_include"] == ["engineer"]
        assert result["title_exclude"] == ["director"]

    def test_merge_adds_new_include_terms(self):
        """New include terms from suggestions are appended to existing ones."""
        result = app_module._merge_prefilter_suggestions(
            {"title_include": ["engineer"], "title_exclude": []},
            {"title_include": ["developer"], "title_exclude": []},
        )
        assert "engineer" in result["title_include"]
        assert "developer" in result["title_include"]

    def test_merge_does_not_duplicate_existing_include_terms(self):
        """Include terms already present are not added a second time."""
        result = app_module._merge_prefilter_suggestions(
            {"title_include": ["engineer"], "title_exclude": []},
            {"title_include": ["engineer", "developer"], "title_exclude": []},
        )
        assert result["title_include"].count("engineer") == 1
        assert "developer" in result["title_include"]

    def test_dedup_is_case_insensitive(self):
        """Duplicate detection is case-insensitive; output is always lowercase."""
        result = app_module._merge_prefilter_suggestions(
            {"title_include": ["Engineer"], "title_exclude": []},
            {"title_include": ["engineer"], "title_exclude": []},
        )
        # Existing "Engineer" is normalised to lowercase; suggested duplicate not added.
        assert len(result["title_include"]) == 1
        assert result["title_include"][0] == "engineer"

    def test_merge_adds_new_exclude_terms(self):
        """New exclude terms from suggestions are appended."""
        result = app_module._merge_prefilter_suggestions(
            {"title_include": [], "title_exclude": ["director"]},
            {"title_include": [], "title_exclude": ["manager"]},
        )
        assert "director" in result["title_exclude"]
        assert "manager" in result["title_exclude"]

    def test_other_prefilter_keys_preserved(self):
        """Keys other than title_include/exclude are passed through unchanged."""
        result = app_module._merge_prefilter_suggestions(
            {
                "title_include": [],
                "title_exclude": [],
                "require_contract_time": "full_time",
                "require_contract_type": "permanent",
            },
            {"title_include": ["engineer"], "title_exclude": []},
        )
        assert result["require_contract_time"] == "full_time"
        assert result["require_contract_type"] == "permanent"

    def test_fresh_mode_replaces_include_with_suggestions(self):
        """In fresh mode (empty existing prefilter) suggestions become the lists."""
        result = app_module._merge_prefilter_suggestions(
            {},
            {"title_include": ["engineer", "developer"], "title_exclude": ["intern"]},
        )
        assert result["title_include"] == ["engineer", "developer"]
        assert result["title_exclude"] == ["intern"]

    def test_user_added_terms_never_removed(self):
        """User-added patterns are preserved even if absent from suggestions."""
        result = app_module._merge_prefilter_suggestions(
            {"title_include": ["staff", "principal"], "title_exclude": ["intern"]},
            {"title_include": ["engineer"], "title_exclude": ["manager"]},
        )
        # All four user-added terms must still be present.
        assert "staff" in result["title_include"]
        assert "principal" in result["title_include"]
        assert "intern" in result["title_exclude"]
        # Suggested terms also present.
        assert "engineer" in result["title_include"]
        assert "manager" in result["title_exclude"]

    def test_empty_suggestions_leaves_existing_unchanged(self):
        """Empty suggestion arrays leave the existing prefilter intact."""
        result = app_module._merge_prefilter_suggestions(
            {"title_include": ["engineer"], "title_exclude": ["intern"]},
            {"title_include": [], "title_exclude": []},
        )
        assert result["title_include"] == ["engineer"]
        assert result["title_exclude"] == ["intern"]


# ===========================================================================
# TestImportEndpointSuggestFilters — issue #251
# ===========================================================================


class TestImportEndpointSuggestFilters:
    """Tests for suggest_filters toggle on POST /profile/import-pdf (issue #251)."""

    def _llm_response_with_suggestions(self, include=None, exclude=None) -> dict:
        """Build a parsed LLM response dict that includes prefilter_suggestions."""
        return {
            "primary_skills": [],
            "education": [],
            "seniority": "Senior",
            "preferred_industries": [],
            "location_center": None,
            "prefilter_suggestions": {
                "title_include": include if include is not None else ["engineer"],
                "title_exclude": exclude if exclude is not None else ["manager"],
            },
        }

    def test_suggest_filters_off_by_default(
        self, client, tmp_providers_path, tmp_keys_path
    ):
        """When suggest_filters is not sent the response has no prefilter_suggestions."""
        long_text = "x" * 200
        mock_provider = MagicMock()
        parsed = self._llm_response_with_suggestions()
        with patch("app._extract_pdf_text", return_value=long_text), \
             patch("app.build_provider_chain", return_value=[mock_provider]), \
             patch("app.generate_with_fallback", return_value=(json.dumps(parsed), "anthropic/haiku")), \
             patch("app._parse_import_response", return_value=parsed):
            resp = client.post(
                "/profile/import-pdf",
                data={"file": (io.BytesIO(b"fake"), "resume.pdf")},
                content_type="multipart/form-data",
            )
        assert resp.status_code == 200
        body = resp.get_json()
        assert "prefilter_suggestions" not in body

    def test_suggest_filters_on_returns_suggestions(
        self, client, tmp_providers_path, tmp_keys_path
    ):
        """When suggest_filters=1 is posted, the response includes prefilter_suggestions."""
        long_text = "x" * 200
        mock_provider = MagicMock()
        parsed = self._llm_response_with_suggestions(
            include=["engineer", "developer"],
            exclude=["manager", "director"],
        )
        with patch("app._extract_pdf_text", return_value=long_text), \
             patch("app.build_provider_chain", return_value=[mock_provider]), \
             patch("app.generate_with_fallback", return_value=(json.dumps(parsed), "anthropic/haiku")), \
             patch("app._parse_import_response", return_value=parsed):
            resp = client.post(
                "/profile/import-pdf",
                data={
                    "file": (io.BytesIO(b"fake"), "resume.pdf"),
                    "suggest_filters": "1",
                },
                content_type="multipart/form-data",
            )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["success"] is True
        assert "prefilter_suggestions" in body
        pf = body["prefilter_suggestions"]
        assert "engineer" in pf["title_include"]
        assert "manager" in pf["title_exclude"]

    def test_suggest_filters_on_sends_extended_prompt(
        self, client, tmp_providers_path, tmp_keys_path
    ):
        """When suggest_filters=1, _build_import_prompt is called with suggest_filters=True."""
        long_text = "x" * 200
        mock_provider = MagicMock()
        parsed = self._llm_response_with_suggestions()
        captured: list[dict] = []

        original_build = app_module._build_import_prompt

        def spy_build(resume_text: str, suggest_filters: bool = False) -> str:
            captured.append({"suggest_filters": suggest_filters})
            return original_build(resume_text, suggest_filters=suggest_filters)

        with patch("app._extract_pdf_text", return_value=long_text), \
             patch("app.build_provider_chain", return_value=[mock_provider]), \
             patch("app.generate_with_fallback", return_value=(json.dumps(parsed), "anthropic/haiku")), \
             patch("app._parse_import_response", return_value=parsed), \
             patch("app._build_import_prompt", side_effect=spy_build):
            client.post(
                "/profile/import-pdf",
                data={
                    "file": (io.BytesIO(b"fake"), "resume.pdf"),
                    "suggest_filters": "1",
                },
                content_type="multipart/form-data",
            )
        assert len(captured) == 1
        assert captured[0]["suggest_filters"] is True

    def test_suggest_filters_off_sends_base_prompt(
        self, client, tmp_providers_path, tmp_keys_path
    ):
        """When suggest_filters is absent, _build_import_prompt is called with False."""
        long_text = "x" * 200
        mock_provider = MagicMock()
        parsed = {
            "primary_skills": [],
            "education": [],
            "seniority": "",
            "preferred_industries": [],
            "location_center": None,
        }
        captured: list[dict] = []

        original_build = app_module._build_import_prompt

        def spy_build(resume_text: str, suggest_filters: bool = False) -> str:
            captured.append({"suggest_filters": suggest_filters})
            return original_build(resume_text, suggest_filters=suggest_filters)

        with patch("app._extract_pdf_text", return_value=long_text), \
             patch("app.build_provider_chain", return_value=[mock_provider]), \
             patch("app.generate_with_fallback", return_value=(json.dumps(parsed), "anthropic/haiku")), \
             patch("app._parse_import_response", return_value=parsed), \
             patch("app._build_import_prompt", side_effect=spy_build):
            client.post(
                "/profile/import-pdf",
                data={"file": (io.BytesIO(b"fake"), "resume.pdf")},
                content_type="multipart/form-data",
            )
        assert len(captured) == 1
        assert captured[0]["suggest_filters"] is False

    def test_suggest_filters_absent_when_llm_returns_no_suggestions(
        self, client, tmp_providers_path, tmp_keys_path
    ):
        """Even with suggest_filters=1, if LLM gives no suggestions the key is absent."""
        long_text = "x" * 200
        mock_provider = MagicMock()
        parsed = {
            "primary_skills": [],
            "education": [],
            "seniority": "Senior",
            "preferred_industries": [],
            "location_center": None,
            # no prefilter_suggestions key
        }
        with patch("app._extract_pdf_text", return_value=long_text), \
             patch("app.build_provider_chain", return_value=[mock_provider]), \
             patch("app.generate_with_fallback", return_value=(json.dumps(parsed), "anthropic/haiku")), \
             patch("app._parse_import_response", return_value=parsed):
            resp = client.post(
                "/profile/import-pdf",
                data={
                    "file": (io.BytesIO(b"fake"), "resume.pdf"),
                    "suggest_filters": "1",
                },
                content_type="multipart/form-data",
            )
        assert resp.status_code == 200
        body = resp.get_json()
        assert "prefilter_suggestions" not in body


# ===========================================================================
# TestApplyPrefilterSuggestionsEndpoint — issue #251
# ===========================================================================


class TestApplyPrefilterSuggestionsEndpoint:
    """Tests for POST /api/apply-prefilter-suggestions (issue #251)."""

    @pytest.fixture()
    def tmp_config_path(self, tmp_path, monkeypatch):
        """Point _CONFIG_PATH at a temp file for isolation."""
        path = str(tmp_path / "config.json")
        monkeypatch.setattr(app_module, "_CONFIG_PATH", path)
        return path

    def _write_config(self, path: str, cfg: dict) -> None:
        """Write cfg as JSON to path."""
        with open(path, "w") as fh:
            json.dump(cfg, fh)

    def _post_suggestions(
        self,
        client,
        include: list,
        exclude: list,
        csrf_token: str = "valid-token",
    ):
        """POST to /api/apply-prefilter-suggestions using form-data encoding.

        Mirrors the JS fetch in profile.html: title_include / title_exclude are
        JSON-encoded strings inside a multipart form, alongside the CSRF token.
        """
        return client.post(
            "/api/apply-prefilter-suggestions",
            data={
                "csrf_token": csrf_token,
                "title_include": json.dumps(include),
                "title_exclude": json.dumps(exclude),
            },
        )

    @pytest.fixture()
    def client_with_csrf(self, client):
        """Return (test_client, csrf_token) with the token pre-seeded in the session."""
        with client.session_transaction() as sess:
            sess["csrf_token"] = "valid-token"
        return client, "valid-token"

    # ------------------------------------------------------------------
    # CSRF tests (new — review item 1)
    # ------------------------------------------------------------------

    def test_returns_403_without_csrf_token(self, client, tmp_config_path):
        """Request with no CSRF token is rejected with 403."""
        self._write_config(tmp_config_path, {})
        resp = client.post(
            "/api/apply-prefilter-suggestions",
            data={
                "title_include": json.dumps(["engineer"]),
                "title_exclude": json.dumps([]),
                # no csrf_token field
            },
        )
        assert resp.status_code == 403
        assert resp.get_json()["success"] is False

    def test_returns_403_with_wrong_csrf_token(self, client, tmp_config_path):
        """Request with an incorrect CSRF token is rejected with 403."""
        self._write_config(tmp_config_path, {})
        with client.session_transaction() as sess:
            sess["csrf_token"] = "correct-token"
        resp = client.post(
            "/api/apply-prefilter-suggestions",
            data={
                "csrf_token": "wrong-token",
                "title_include": json.dumps(["engineer"]),
                "title_exclude": json.dumps([]),
            },
        )
        assert resp.status_code == 403
        assert resp.get_json()["success"] is False

    def test_accepts_request_with_valid_csrf_token(self, client_with_csrf, tmp_path, monkeypatch):
        """Request with a matching CSRF token succeeds (200)."""
        client, token = client_with_csrf
        path = str(tmp_path / "config.json")
        monkeypatch.setattr(app_module, "_CONFIG_PATH", path)
        self._write_config(path, {})
        resp = self._post_suggestions(client, ["engineer"], [], csrf_token=token)
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True

    # ------------------------------------------------------------------
    # Input validation: missing / malformed arrays
    # ------------------------------------------------------------------

    def test_returns_400_when_body_missing_arrays(self, client_with_csrf):
        """Missing title_exclude array returns 400."""
        client, token = client_with_csrf
        resp = client.post(
            "/api/apply-prefilter-suggestions",
            data={
                "csrf_token": token,
                "title_include": json.dumps(["engineer"]),
                # missing title_exclude
            },
        )
        assert resp.status_code == 400
        assert resp.get_json()["success"] is False

    def test_returns_400_for_non_json_arrays(self, client_with_csrf):
        """Non-JSON-encoded array fields return 400."""
        client, token = client_with_csrf
        resp = client.post(
            "/api/apply-prefilter-suggestions",
            data={
                "csrf_token": token,
                "title_include": "not json",
                "title_exclude": "[]",
            },
        )
        assert resp.status_code == 400

    def test_returns_400_when_include_and_exclude_overlap(
        self, client_with_csrf, tmp_path, monkeypatch
    ):
        """Overlapping include/exclude terms return 400 (disjoint-set guard)."""
        client, token = client_with_csrf
        path = str(tmp_path / "config.json")
        monkeypatch.setattr(app_module, "_CONFIG_PATH", path)
        self._write_config(path, {})
        resp = self._post_suggestions(
            client,
            ["engineer", "manager"],
            ["manager", "director"],
            csrf_token=token,
        )
        assert resp.status_code == 400
        body = resp.get_json()
        assert body["success"] is False
        assert "manager" in body["error"]

    # ------------------------------------------------------------------
    # Input validation: length / count limits (new — review item 2)
    # ------------------------------------------------------------------

    def test_parse_drops_suggestions_when_pattern_over_max_length(self):
        """_parse_import_response drops prefilter_suggestions (not the whole response) when
        any pattern exceeds _MAX_PATTERN_LEN; core profile is preserved."""
        over_long = "x" * (app_module._MAX_PATTERN_LEN + 1)
        raw = json.dumps({
            "primary_skills": [],
            "education": [],
            "seniority": "",
            "preferred_industries": [],
            "location_center": None,
            "prefilter_suggestions": {
                "title_include": [over_long],
                "title_exclude": [],
            },
        })
        result = app_module._parse_import_response(raw)
        assert result is not None
        assert "prefilter_suggestions" not in result

    def test_parse_accepts_pattern_at_exact_max_length(self):
        """_parse_import_response accepts a pattern that is exactly _MAX_PATTERN_LEN chars."""
        exact = "x" * app_module._MAX_PATTERN_LEN
        raw = json.dumps({
            "primary_skills": [],
            "education": [],
            "seniority": "",
            "preferred_industries": [],
            "location_center": None,
            "prefilter_suggestions": {
                "title_include": [exact],
                "title_exclude": [],
            },
        })
        result = app_module._parse_import_response(raw)
        assert result is not None
        assert exact in result["prefilter_suggestions"]["title_include"]

    def test_parse_drops_suggestions_when_list_over_max_count(self):
        """_parse_import_response drops prefilter_suggestions (not the whole response) when
        a list exceeds _MAX_PATTERNS_PER_LIST; core profile is preserved."""
        too_many = [f"term{i}" for i in range(app_module._MAX_PATTERNS_PER_LIST + 1)]
        raw = json.dumps({
            "primary_skills": [],
            "education": [],
            "seniority": "",
            "preferred_industries": [],
            "location_center": None,
            "prefilter_suggestions": {
                "title_include": too_many,
                "title_exclude": [],
            },
        })
        result = app_module._parse_import_response(raw)
        assert result is not None
        assert "prefilter_suggestions" not in result

    def test_parse_accepts_list_at_exact_max_count(self):
        """_parse_import_response accepts a list with exactly _MAX_PATTERNS_PER_LIST items."""
        exact_count = [f"term{i}" for i in range(app_module._MAX_PATTERNS_PER_LIST)]
        raw = json.dumps({
            "primary_skills": [],
            "education": [],
            "seniority": "",
            "preferred_industries": [],
            "location_center": None,
            "prefilter_suggestions": {
                "title_include": exact_count,
                "title_exclude": [],
            },
        })
        result = app_module._parse_import_response(raw)
        assert result is not None
        assert len(result["prefilter_suggestions"]["title_include"]) == app_module._MAX_PATTERNS_PER_LIST

    # ------------------------------------------------------------------
    # Lowercase normalisation on merge (new — review item 4)
    # ------------------------------------------------------------------

    def test_merge_normalises_existing_and_new_to_lowercase(
        self, client_with_csrf, tmp_path, monkeypatch
    ):
        """Existing mixed-case patterns and new suggestions are all lowercased on merge."""
        client, token = client_with_csrf
        path = str(tmp_path / "config.json")
        monkeypatch.setattr(app_module, "_CONFIG_PATH", path)
        self._write_config(
            path,
            {"prefilter": {"title_include": ["Engineer"], "title_exclude": []}},
        )
        resp = self._post_suggestions(
            client, ["engineer", "developer"], [], csrf_token=token
        )
        assert resp.status_code == 200
        with open(path) as fh:
            cfg = json.load(fh)
        inc = cfg["prefilter"]["title_include"]
        # All entries must be lowercase.
        assert all(s == s.lower() for s in inc), f"Mixed-case entries found: {inc}"
        # "engineer" must appear exactly once (deduped).
        assert inc.count("engineer") == 1
        # "developer" must be present.
        assert "developer" in inc

    # ------------------------------------------------------------------
    # Happy-path / merge / preservation tests (migrated from JSON to form)
    # ------------------------------------------------------------------

    def test_fresh_apply_writes_new_prefilter(self, client_with_csrf, tmp_path, monkeypatch):
        """Suggestions are written to an empty config's prefilter block."""
        client, token = client_with_csrf
        path = str(tmp_path / "config.json")
        monkeypatch.setattr(app_module, "_CONFIG_PATH", path)
        self._write_config(path, {})
        resp = self._post_suggestions(client, ["engineer"], ["director"], csrf_token=token)
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True
        with open(path) as fh:
            cfg = json.load(fh)
        assert "engineer" in cfg["prefilter"]["title_include"]
        assert "director" in cfg["prefilter"]["title_exclude"]

    def test_merge_apply_unions_with_existing(self, client_with_csrf, tmp_path, monkeypatch):
        """Suggestions are merged with existing prefilter without removing old terms."""
        client, token = client_with_csrf
        path = str(tmp_path / "config.json")
        monkeypatch.setattr(app_module, "_CONFIG_PATH", path)
        self._write_config(
            path,
            {"prefilter": {"title_include": ["staff"], "title_exclude": ["intern"]}},
        )
        resp = self._post_suggestions(client, ["engineer"], ["manager"], csrf_token=token)
        assert resp.status_code == 200
        with open(path) as fh:
            cfg = json.load(fh)
        pf = cfg["prefilter"]
        assert "staff" in pf["title_include"]
        assert "engineer" in pf["title_include"]
        assert "intern" in pf["title_exclude"]
        assert "manager" in pf["title_exclude"]

    def test_apply_preserves_other_prefilter_keys(self, client_with_csrf, tmp_path, monkeypatch):
        """require_contract_time / type are not touched by apply."""
        client, token = client_with_csrf
        path = str(tmp_path / "config.json")
        monkeypatch.setattr(app_module, "_CONFIG_PATH", path)
        self._write_config(
            path,
            {
                "prefilter": {
                    "title_include": [],
                    "title_exclude": [],
                    "require_contract_time": "full_time",
                    "require_contract_type": "permanent",
                }
            },
        )
        resp = self._post_suggestions(client, ["engineer"], [], csrf_token=token)
        assert resp.status_code == 200
        with open(path) as fh:
            cfg = json.load(fh)
        pf = cfg["prefilter"]
        assert pf["require_contract_time"] == "full_time"
        assert pf["require_contract_type"] == "permanent"

    def test_returns_500_when_config_unreadable(self, client_with_csrf, tmp_path, monkeypatch):
        """Missing config.json returns 500."""
        client, token = client_with_csrf
        path = str(tmp_path / "config_missing.json")
        monkeypatch.setattr(app_module, "_CONFIG_PATH", path)
        # path never created — doesn't exist
        resp = self._post_suggestions(client, ["engineer"], [], csrf_token=token)
        assert resp.status_code == 500
        assert resp.get_json()["success"] is False

    def test_dedup_on_apply(self, client_with_csrf, tmp_path, monkeypatch):
        """Applying suggestions that duplicate existing terms does not create dupes."""
        client, token = client_with_csrf
        path = str(tmp_path / "config.json")
        monkeypatch.setattr(app_module, "_CONFIG_PATH", path)
        self._write_config(
            path,
            {"prefilter": {"title_include": ["engineer"], "title_exclude": []}},
        )
        resp = self._post_suggestions(
            client, ["engineer", "developer"], [], csrf_token=token
        )
        assert resp.status_code == 200
        with open(path) as fh:
            cfg = json.load(fh)
        assert cfg["prefilter"]["title_include"].count("engineer") == 1

    # ------------------------------------------------------------------
    # Regression: issue #259 — browser CSRF flow (GET /profile → POST)
    # ------------------------------------------------------------------

    def test_csrf_token_round_trip_via_profile_get(
        self, tmp_path, monkeypatch
    ):
        """GET /profile establishes the CSRF token; POST with that token succeeds.

        Regression test for issue #259.  The existing ``client_with_csrf``
        fixture bypasses the real browser flow by injecting the token directly
        into the session.  This test uses the same client instance to simulate
        the actual sequence a browser follows:

        1. GET /profile — session cookie is written with ``csrf_token``.
        2. The token is embedded in the rendered HTML as ``var _csrfToken``.
        3. POST /api/apply-prefilter-suggestions — same session cookie is sent,
           token is read from the form body and compared against the session.

        The POST must return 200, not 403, proving the CSRF guard is satisfied
        when the correct browser-flow token is used.
        """
        config_path = str(tmp_path / "config.json")
        monkeypatch.setattr(app_module, "_CONFIG_PATH", config_path)
        self._write_config(config_path, {})

        with flask_app.test_client() as c:
            # Step 1: GET /profile to establish the session and receive the
            # CSRF token rendered into the page.
            get_resp = c.get("/profile")
            assert get_resp.status_code == 200, (
                f"GET /profile returned {get_resp.status_code}"
            )

            # Step 2: Extract the CSRF token from the rendered HTML.
            html = get_resp.data.decode("utf-8", errors="replace")
            m = re.search(r'var _csrfToken = "([^"]+)";', html)
            assert m is not None, (
                "var _csrfToken not found in GET /profile response — "
                "session CSRF token was not rendered into the template"
            )
            csrf_token = m.group(1)

            # Step 3: POST with the token extracted from the page, using the
            # same client instance so the session cookie is sent automatically.
            post_resp = c.post(
                "/api/apply-prefilter-suggestions",
                data={
                    "csrf_token": csrf_token,
                    "title_include": json.dumps(["engineer"]),
                    "title_exclude": json.dumps([]),
                },
            )
            assert post_resp.status_code == 200, (
                f"Expected 200, got {post_resp.status_code}: "
                f"{post_resp.data.decode()}"
            )
            assert post_resp.get_json()["success"] is True
