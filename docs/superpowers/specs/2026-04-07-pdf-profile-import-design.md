# Design: PDF Resume Import for Profile

**Issue:** #41
**Date:** 2026-04-07

## Overview

Add a PDF resume upload flow to the `/profile` page that extracts candidate data via the configured LLM provider and pre-populates the profile form for review before saving. Supports two modes: Start Fresh (replace) and Merge (additive).

## Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Import modes | Both: Start Fresh + Merge | Users need Start Fresh for initial setup, Merge for incremental updates after gaining new skills/roles |
| LLM integration | New `generate()` method on `LLMProvider` | Decouples "talk to LLM" from "score a listing"; reusable for future LLM tasks |
| UI placement | Collapsible section at top of profile form | Most discoverable; collapsed by default keeps page clean for return visits |
| PDF parsing | `pypdf>=4.0.0` | Pure Python, no system deps, handles standard resumes well |
| Auto-save | No | Import pre-fills the form; user reviews and clicks existing Save button |

## 1. Provider Refactor

### `LLMProvider.generate()` (new abstract method)

Add to `providers/base.py`:

```python
@abstractmethod
def generate(self, prompt: str) -> str:
    """Send an arbitrary prompt and return the raw response text."""
```

Each provider implementation (Anthropic, OpenAI, Google) extracts their existing HTTP call logic from `complete()` into `generate()`. Then `complete()` becomes: call `generate()` -> parse the scoring-specific JSON response.

### `generate_with_fallback()` (new helper)

Add to `providers/__init__.py`:

```python
def generate_with_fallback(prompt: str, chain: list, dead_providers: set) -> tuple[str, str] | None:
    """Try providers in order. Returns (raw_text, "provider/model") or None.
    
    Same retry/fallback semantics as score_listing_with_fallback():
    - Auth errors (401/403) permanently remove provider for the run
    - Transient failures skip to the next provider
    """
```

## 2. PDF Extraction & Import Endpoint

### New dependency

`pypdf>=4.0.0` added to `requirements.txt`.

### `POST /profile/import-pdf`

**Request:** Multipart form data with:
- `file`: PDF file (max ~5MB)
- `mode`: `"fresh"` or `"merge"`

**Validation:**
- File present and has `.pdf` extension
- Readable by `pypdf.PdfReader`
- Extracted text >= 50 characters (otherwise: "Could not extract meaningful text from this PDF")

**Flow:**
1. Extract plaintext via `pypdf.PdfReader` — concatenate all pages
2. Build import prompt with extracted text + mode context
3. Call `generate_with_fallback()` with the prompt
4. Parse JSON response, stripping markdown fences (same pattern as scoring)
5. Return JSON response

**Success response:**
```json
{"success": true, "profile": {...}, "model_used": "provider/model"}
```

**Error responses:**
- No file / non-PDF: 400
- Text too short: 422
- No configured provider: 503
- LLM failure: 502
- File too large: 413

**The endpoint does NOT save anything.** It returns extracted data for the client to pre-fill the form.

### Import prompt design

The prompt instructs the LLM to return a JSON object with:
- `primary_skills` — array of `{skill: str, years: int, status: "active"|"dormant"}`
- `education` — array of free-text strings (matching existing profile format)
- `seniority` — string inferred from job titles (e.g. "Senior", "Lead", "Staff")
- `preferred_industries` — array of strings
- `location_center` — geocodable string from contact info, or null

**Start Fresh mode:** Prompt receives only the PDF text. Output replaces all populated fields.

**Merge mode:** Prompt also receives the current profile and is instructed to:
- Add new skills not already present in `primary_skills`
- Append new education entries not already listed
- Leave `seniority` alone if already set; fill from PDF if currently empty
- Add new industries not already in `preferred_industries`

**Fields NOT extracted** (preserved as-is regardless of mode):
- `anti_preferences`
- `scoring_notes`
- `location.radius_km`
- `location.geocode_fallback`
- `location.notes`

## 3. UI (Profile Page)

### Collapsible import section

Placed at the **top** of the profile form, using existing `.provider-row` card pattern.

**Collapsed by default.** Header: "Import from Resume" with chevron toggle.

**Contents when expanded:**
- **Mode selector:** Two radio buttons
  - "Start Fresh" (default) — `.field-hint`: "Replaces all populated fields based solely on the PDF"
  - "Merge with existing profile" — `.field-hint`: "Adds new entries alongside existing profile data"
- **File input:** `<input type="file" accept=".pdf">` styled with `.settings-input`
- **Import button:** `.btn` class, disabled until a file is selected
- **Status area:** Spinner during processing, success/error messages after

### Client-side JS flow

Vanilla JS (no framework — consistent with existing codebase). No HTMX for this interaction because the response needs JS processing to distribute values across many form fields.

1. User selects mode + file, clicks Import
2. JS sends `FormData` to `POST /profile/import-pdf` via `fetch()`
3. On success, JS iterates returned profile fields and fills existing form inputs:
   - Text fields via `.value`
   - Repeating rows (skills, education, industries) by clearing + rebuilding `.row-list` containers
4. Success banner (`.save-notice` style): "Profile pre-filled from resume. Review the fields below and click Save."
5. On error, `.save-error` style message with error text
6. User reviews, edits, clicks existing Save button (normal POST `/profile`)

## 4. Testing

### New test file: `tests/test_profile_import.py`

| Test Class | Coverage |
|---|---|
| `TestPdfExtraction` | Valid PDF -> text; empty PDF -> error; corrupt file -> error; text too short -> error |
| `TestImportPromptConstruction` | Start Fresh prompt excludes current profile; Merge prompt includes it; correct JSON schema requested |
| `TestImportResponseParsing` | Well-formed JSON parsed; markdown fences stripped; missing fields default to empty; malformed JSON -> error |
| `TestImportMergeLogic` | New skills added, existing preserved; education appended not duplicated; seniority preserved if set; industries deduplicated |
| `TestImportEndpoint` | 200 success (mocked LLM); no file -> 400; non-PDF -> 400; no provider -> 503; LLM failure -> 502; file too large -> 413 |

### Provider refactor tests

Added to each existing provider test file — verify `generate()` returns raw text and `complete()` still works identically (regression).

All tests use `unittest.mock.patch` — no real LLM or PDF calls.

## Files Changed

| File | Change |
|---|---|
| `providers/base.py` | Add `generate()` abstract method |
| `providers/anthropic_provider.py` | Extract HTTP logic into `generate()`, `complete()` calls it |
| `providers/openai_provider.py` | Same refactor |
| `providers/google_provider.py` | Same refactor |
| `providers/__init__.py` | Add `generate_with_fallback()` |
| `app.py` | Add `POST /profile/import-pdf` endpoint |
| `templates/profile.html` | Add collapsible import section + JS |
| `requirements.txt` | Add `pypdf>=4.0.0` |
| `tests/test_profile_import.py` | New — all import tests |
| `tests/test_providers_*.py` | Add `generate()` regression tests |
| `docs/STYLE_GUIDE.md` | Document collapsible section pattern if new |

## Out of Scope

- Importing `anti_preferences` or `scoring_notes` (too subjective)
- Importing `location.radius_km`, `geocode_fallback` (not in resumes)
- Bulk import / multiple profiles
- Non-PDF formats (Word, LinkedIn export)
