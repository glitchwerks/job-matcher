"""PDF resume import helpers and async job machinery for Job Matcher.

This module owns all PDF-to-profile extraction logic:

* Text extraction from raw PDF bytes (``_extract_pdf_text``).
* LLM prompt construction (``_build_import_prompt``).
* LLM response parsing + validation (``_parse_import_response``).
* Education entry normalisation (``_normalise_education``).
* Profile merge helpers (``_merge_import_result``,
  ``_merge_prefilter_suggestions``).
* Async job state — ``_pdf_jobs`` dict, ``_pdf_jobs_lock``, and the
  ``_pdf_executor`` :class:`~concurrent.futures.ThreadPoolExecutor`
  singleton.
* The background worker ``_run_pdf_import_job``.

Import-time side-effects
------------------------
``_pdf_executor`` is a module-level :class:`~concurrent.futures.ThreadPoolExecutor`
created exactly once when this module is first imported.  It spawns up to
3 daemon threads (``pdf-import-0`` … ``pdf-import-2``).  No Flask context
is required; this module intentionally has zero Flask imports.

Allowed imports: stdlib, third-party packages, ``services.profile_store``
(for ``load_profile`` and ``_PROFILE_PATH``), and ``credentials`` / the
``providers`` package (for ``build_provider_chain`` /
``generate_with_fallback``).  Never import from ``app`` or ``web``.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time as _time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from io import BytesIO
from typing import Optional

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from providers import build_provider_chain, generate_with_fallback
from providers.anthropic_provider import strip_fences
from services.profile_store import load_profile

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# PDF text extraction
# ---------------------------------------------------------------------------


def _extract_pdf_text(pdf_bytes: bytes) -> str:
    """Extract all text from a PDF given its raw bytes.

    Args:
        pdf_bytes: Raw bytes of the uploaded PDF file.

    Returns:
        Concatenated text from all pages (empty string if no text found).

    Raises:
        ValueError: If pypdf cannot parse the bytes as a valid PDF.
    """
    try:
        reader = PdfReader(BytesIO(pdf_bytes))
        return "".join(page.extract_text() or "" for page in reader.pages)
    except (PdfReadError, ValueError, IOError) as exc:
        raise ValueError(f"Could not read PDF: {exc}") from exc


# ---------------------------------------------------------------------------
# LLM prompts
# ---------------------------------------------------------------------------

_IMPORT_PROMPT_FRESH = """You are extracting structured profile data from a resume/CV.

RESUME TEXT:
{resume_text}

Extract the following fields and respond with ONLY a JSON object. No explanation, no markdown, no code fences.

The JSON must have exactly these keys:
- "primary_skills": array of objects, each with "skill" (string), "years" (integer estimate), "status" ("active" or "dormant")
- "education": array of objects, each with "degree_type" (e.g. "B.S.", "M.S."), "degree_field" (e.g. "Computer Science"), "school" (institution name), "graduation_year" (four-digit year string)
- "seniority": string inferred from job titles (e.g. "Junior", "Mid-level", "Senior", "Staff", "Lead", "Principal")
- "preferred_industries": array of strings inferred from work history (e.g. "fintech", "healthtech", "developer tooling")
- "location_center": string from contact info if present (e.g. "Miami, FL"), or null if not found

If a field cannot be confidently extracted, use an empty array, empty string, or null as appropriate. Do not guess or hallucinate values.

JSON only:"""

# Appended to _IMPORT_PROMPT_FRESH (before the final "JSON only:" sentinel)
# when the caller opts in to prefilter title suggestions.  Kept separate so
# the base prompt is byte-for-byte identical when the toggle is off.
_IMPORT_PROMPT_PREFILTER_EXTENSION = """
Additionally, suggest job-title keyword filters based on the roles this
candidate has held and the jobs they would plausibly target:
- "prefilter_suggestions": object with exactly two keys:
  - "title_include": array of lowercase substring strings that SHOULD appear
    in a job title for it to be relevant (e.g. ["engineer", "developer"])
  - "title_exclude": array of lowercase substring strings that should NEVER
    appear in a job title (e.g. ["manager", "director", "intern"])

Rules for prefilter_suggestions:
- Use simple substrings, not regular expressions.
- All strings must be lowercase.
- "title_include" and "title_exclude" must be completely disjoint — no string
  may appear in both lists (case-insensitively).
- If you cannot confidently suggest filters, use empty arrays for both keys.
- Do NOT include "require_contract_time" or "require_contract_type" — those
  are separate user preferences, not resume-derived."""

# ---------------------------------------------------------------------------
# Prompt constants — length limits
# ---------------------------------------------------------------------------

# Maximum length (characters) for a single prefilter pattern string.  Bounding
# LLM output prevents pathologically long patterns from bloating config.json.
_MAX_PATTERN_LEN = 64

# Maximum number of patterns allowed in a single title_include or title_exclude
# list within prefilter_suggestions.
_MAX_PATTERNS_PER_LIST = 32


def _build_import_prompt(
    resume_text: str,
    suggest_filters: bool = False,
) -> str:
    """Build the LLM prompt for PDF resume import.

    Both fresh and merge modes use the same extraction-only prompt.  Merging
    is handled deterministically by ``_merge_import_result()`` after the LLM
    responds, so the LLM never needs to see the existing profile.

    When ``suggest_filters`` is ``True`` the prefilter extension is appended
    to the prompt so the LLM also returns ``prefilter_suggestions``.  When it
    is ``False`` the prompt is byte-for-byte identical to the legacy prompt —
    no extra tokens are charged.

    Args:
        resume_text: Extracted plain text from the uploaded PDF.
        suggest_filters: When ``True``, ask the LLM to additionally return
            ``prefilter_suggestions`` (title_include / title_exclude arrays).

    Returns:
        Formatted prompt string ready to send to the LLM.
    """
    if suggest_filters:
        # Insert the prefilter extension before the closing "JSON only:" line.
        base = _IMPORT_PROMPT_FRESH.rstrip()
        # Remove the trailing sentinel, add extension, restore sentinel.
        sentinel = "JSON only:"
        if base.endswith(sentinel):
            base = base[: -len(sentinel)].rstrip()
        return (
            base
            + "\n"
            + _IMPORT_PROMPT_PREFILTER_EXTENSION.strip()
            + "\n\nJSON only:"
        ).format(resume_text=resume_text)
    return _IMPORT_PROMPT_FRESH.format(resume_text=resume_text)


# ---------------------------------------------------------------------------
# LLM response parsing
# ---------------------------------------------------------------------------

def _parse_import_response(raw: str) -> Optional[dict]:
    """Parse the LLM's JSON response for a PDF import request.

    Strips markdown code fences, parses JSON, and fills missing keys with
    safe defaults so callers can always rely on the expected keys existing.

    If ``prefilter_suggestions`` is present its ``title_include`` and
    ``title_exclude`` arrays are validated to be disjoint (case-insensitive).
    Validation failures in the suggestions section drop only that key — the
    core profile data is still returned so the caller does not 502 the whole
    request because of an optional field.  Only a failure to parse the
    top-level JSON at all causes a ``None`` return.

    Each pattern string must be ≤ ``_MAX_PATTERN_LEN`` characters and each list
    must contain ≤ ``_MAX_PATTERNS_PER_LIST`` items.  Over-limit or invalid
    suggestions are dropped with a warning rather than rejecting the whole
    response.

    Args:
        raw: Raw text response from the LLM.

    Returns:
        Parsed dict with all expected keys, or ``None`` if the top-level JSON
        itself cannot be parsed.
    """
    try:
        cleaned = strip_fences(raw)
        data = json.loads(cleaned)
    except (json.JSONDecodeError, ValueError):
        _logger.error(
            "[import] _parse_import_response: failed to parse LLM "
            "response as JSON — raw body (first 500 chars): %r",
            raw[:500],
        )
        return None
    data.setdefault("primary_skills", [])
    data.setdefault("education", [])
    data.setdefault("seniority", "")
    data.setdefault("preferred_industries", [])
    data.setdefault("location_center", None)

    # Validate prefilter_suggestions when present.  Any validation failure
    # drops only this optional key so the core profile is still returned.
    if "prefilter_suggestions" in data:
        pf = data["prefilter_suggestions"]
        if isinstance(pf, dict):
            inc = [str(s).lower() for s in pf.get("title_include", [])]
            exc = [str(s).lower() for s in pf.get("title_exclude", [])]

            # Enforce per-list length cap.  Drop rather than truncate so the
            # LLM cannot silently bloat the config.
            if len(inc) > _MAX_PATTERNS_PER_LIST or len(exc) > _MAX_PATTERNS_PER_LIST:
                _logger.warning(
                    "[import] _parse_import_response: prefilter_suggestions "
                    "list too long (include=%d, exclude=%d, max=%d) — "
                    "dropping suggestions; profile data preserved.",
                    len(inc),
                    len(exc),
                    _MAX_PATTERNS_PER_LIST,
                )
                del data["prefilter_suggestions"]
            else:
                # Enforce per-pattern length cap.
                over_len = [s for s in inc + exc if len(s) > _MAX_PATTERN_LEN]
                if over_len:
                    _logger.warning(
                        "[import] _parse_import_response: prefilter_suggestions "
                        "contains patterns exceeding max length (%d chars): %r — "
                        "dropping suggestions; profile data preserved.",
                        _MAX_PATTERN_LEN,
                        over_len[:5],
                    )
                    del data["prefilter_suggestions"]
                else:
                    overlap = set(inc) & set(exc)
                    if overlap:
                        _logger.warning(
                            "[import] _parse_import_response: prefilter_suggestions "
                            "title_include/title_exclude overlap — dropping suggestions; "
                            "profile data preserved. Overlapping terms: %r",
                            overlap,
                        )
                        del data["prefilter_suggestions"]
                    else:
                        # Normalise to lowercase lists in-place.
                        data["prefilter_suggestions"] = {
                            "title_include": inc,
                            "title_exclude": exc,
                        }
        else:
            # Unexpected type — drop the key rather than passing bad data.
            del data["prefilter_suggestions"]

    return data


# ---------------------------------------------------------------------------
# Education normalisation
# ---------------------------------------------------------------------------

_DEGREE_PREFIX_RE = re.compile(
    r"^(B\.S\.|BS|B\.A\.|BA|M\.S\.|MS|M\.A\.|MA|Ph\.D\.|PhD|MBA"
    r"|Master of Science|Master of Arts|Master of Business Administration"
    r"|Bachelor of Science|Bachelor of Arts|Bachelor of Engineering"
    r"|Doctor of Philosophy|Doctor of|Associate of|Associate)(?=\s|$)",
    re.IGNORECASE,
)
_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")


def _normalise_education(entries: list) -> list[dict]:
    """Normalise a list of education entries to structured dicts.

    Handles three cases per entry:

    * **Flat string** — attempts regex-based parsing into the four structured
      fields (``degree_type``, ``degree_field``, ``school``,
      ``graduation_year``).  Falls back to stuffing the whole string into
      ``degree_field`` if parsing fails.
    * **Dict with missing keys** — fills absent keys with ``""``.
    * **Well-formed dict** — passed through unchanged.

    Args:
        entries: Raw education list from the LLM response.

    Returns:
        List of dicts each containing exactly the four structured keys.
    """
    _EMPTY = {
        "degree_type": "",
        "degree_field": "",
        "school": "",
        "graduation_year": "",
    }

    def _parse_flat(s: str) -> dict:
        """Parse a flat education string into a structured dict.

        Args:
            s: Raw flat-string education entry.

        Returns:
            Dict with ``degree_type``, ``degree_field``, ``school``, and
            ``graduation_year`` keys.
        """
        result = dict(_EMPTY)
        # Extract 4-digit year first.
        year_m = _YEAR_RE.search(s)
        if year_m:
            result["graduation_year"] = year_m.group(0)
            s = (s[: year_m.start()] + s[year_m.end():]).strip(" ,").lstrip()

        # Attempt to match a known degree prefix at the start.
        prefix_m = _DEGREE_PREFIX_RE.match(s)
        if prefix_m:
            result["degree_type"] = prefix_m.group(0).strip()
            remainder = s[prefix_m.end():].strip()
            # Handle "in <field>" connector (e.g. "Master of Science in Data Science")
            if remainder.lower().startswith("in "):
                remainder = remainder[3:].strip()
            # Remaining text split by ", " gives field then school (or just field).
            parts = [p.strip() for p in remainder.split(",", 1)]
            result["degree_field"] = parts[0] if parts else ""
            result["school"] = parts[1] if len(parts) > 1 else ""
        else:
            # No recognised degree prefix — split by "," and use heuristics.
            parts = [p.strip() for p in s.split(",")]
            if len(parts) >= 3:
                # e.g. "Computer Science, MIT, ..." — unlikely but defensible
                result["degree_type"] = ""
                result["degree_field"] = parts[0]
                result["school"] = parts[1]
            elif len(parts) == 2:
                result["degree_field"] = parts[0]
                result["school"] = parts[1]
            elif parts:
                result["degree_field"] = parts[0]
            else:
                result["degree_field"] = s  # fallback: preserve whole string

        return result

    normalised = []
    for entry in entries:
        if isinstance(entry, str):
            normalised.append(_parse_flat(entry.strip()))
        elif isinstance(entry, dict):
            normalised.append({
                "degree_type": entry.get("degree_type", ""),
                "degree_field": entry.get("degree_field", ""),
                "school": entry.get("school", ""),
                "graduation_year": str(entry.get("graduation_year", "")),
            })
        else:
            # Unexpected type — convert to string and fall back.
            normalised.append(_parse_flat(str(entry)))
    return normalised


# ---------------------------------------------------------------------------
# Merge helpers
# ---------------------------------------------------------------------------


def _merge_import_result(current: dict, imported: dict) -> dict:
    """Merge LLM-extracted import data into the existing profile.

    Merging rules:
    - Skills: preserve all existing; append new ones (case-insensitive dedup).
    - Education: preserve all existing; append new ones (case-insensitive dedup).
    - Seniority: keep existing if non-empty; otherwise use imported value.
    - Industries: union of both lists, case-insensitive dedup.
    - Location: keep existing center if set; otherwise use imported value.

    Args:
        current:  Existing profile dict (may be empty).
        imported: Parsed LLM response dict from ``_parse_import_response()``.

    Returns:
        Merged profile dict containing all combined data.
    """
    result = {}

    # Skills: existing preserved (as structured objects), new appended from import.
    # Existing skills may be structured dicts or legacy flat strings — normalise
    # to structured objects so the merged result is always typed.
    def _normalise_skill(s: object) -> dict:
        """Convert a legacy flat string or a structured dict to a skill object."""
        if isinstance(s, dict):
            return s
        # Legacy format: "Python, 5yr, active" or "Python, 5yr, dormant"
        parts = [p.strip() for p in str(s).split(",")]
        description = parts[0] if parts else str(s)
        years = 0
        active = True
        if len(parts) >= 2:
            yr_part = parts[1].lower().replace("yr", "").strip()
            try:
                years = int(yr_part)
            except ValueError:
                pass
        if len(parts) >= 3:
            active = parts[2].lower().strip() != "dormant"
        return {"description": description, "years_active": years, "active": active}

    existing_skills: list[dict] = [
        _normalise_skill(s) for s in current.get("primary_skills", [])
    ]
    existing_skill_names = {s["description"].lower() for s in existing_skills}
    for skill_obj in imported.get("primary_skills", []):
        name = skill_obj.get("skill", "")
        if name.lower() not in existing_skill_names:
            years = skill_obj.get("years", 0)
            status = skill_obj.get("status", "active")
            existing_skills.append({
                "description": name,
                "years_active": int(years) if years else 0,
                "active": status != "dormant",
            })
            existing_skill_names.add(name.lower())
    result["primary_skills"] = existing_skills

    # Education: append new structured objects, skip duplicates (all four
    # fields, case-insensitive).  Existing entries may be structured dicts or
    # legacy flat strings — normalise to dicts.
    def _normalise_edu(e: object) -> dict:
        """Convert a legacy flat string or a structured dict to an education object."""
        return _normalise_education([e])[0]

    def _edu_key(e: dict) -> tuple:
        """Return a case-folded 4-tuple for dedup comparison."""
        return (
            e.get("degree_type", "").lower(),
            e.get("degree_field", "").lower(),
            e.get("school", "").lower(),
            e.get("graduation_year", "").lower(),
        )

    existing_edu: list[dict] = [
        _normalise_edu(e) for e in current.get("education", [])
    ]
    existing_edu_keys = {_edu_key(e) for e in existing_edu}
    for entry in imported.get("education", []):
        entry_norm = _normalise_edu(entry)
        key = _edu_key(entry_norm)
        if key not in existing_edu_keys:
            existing_edu.append(entry_norm)
            existing_edu_keys.add(key)
    result["education"] = existing_edu

    # Seniority: keep existing if set, fill from import if empty
    current_seniority = current.get("seniority", "")
    result["seniority"] = (
        current_seniority if current_seniority else imported.get("seniority", "")
    )

    # Industries: union, deduplicated
    existing_industries = list(current.get("preferred_industries", []))
    existing_lower = {i.lower() for i in existing_industries}
    for industry in imported.get("preferred_industries", []):
        if industry.lower() not in existing_lower:
            existing_industries.append(industry)
            existing_lower.add(industry.lower())
    result["preferred_industries"] = existing_industries

    # Location: keep existing if set
    current_location = current.get("location", {})
    current_center = (
        current_location.get("center", "")
        if isinstance(current_location, dict)
        else ""
    )
    result["location_center"] = (
        current_center if current_center else imported.get("location_center")
    )

    return result


def _merge_prefilter_suggestions(
    existing_prefilter: dict,
    suggestions: dict,
) -> dict:
    """Merge LLM-suggested prefilter patterns into the existing prefilter block.

    Merge rules:
    - ``title_include``: case-insensitive union of existing and suggested
      patterns; existing user-added patterns are never removed.
    - ``title_exclude``: same union-then-dedup rule.
    - All other prefilter keys (``require_contract_time``,
      ``require_contract_type``, etc.) are preserved unchanged from
      ``existing_prefilter``.

    The caller is responsible for ensuring ``suggestions`` has already passed
    the disjoint-set check in ``_parse_import_response()`` — this function
    does not re-validate.

    Args:
        existing_prefilter: The current ``prefilter`` block from
            ``config.json`` (may be empty dict).
        suggestions: The ``prefilter_suggestions`` dict from the parsed LLM
            response, containing ``title_include`` and ``title_exclude`` lists
            of lowercase strings.

    Returns:
        A new prefilter dict with merged title patterns and all other keys
        preserved from ``existing_prefilter``.
    """
    result = dict(existing_prefilter)

    def _merge_list(key: str) -> list[str]:
        """Return deduped union of existing and suggested values for *key*.

        Both existing and suggested patterns are normalised to lowercase so
        the merged output is consistently cased.  Filter matching is already
        case-insensitive, so this is semantically neutral while avoiding
        mixed-case lists like ``["Engineer", "developer"]`` in config.json.

        Args:
            key: The prefilter key to merge (``"title_include"`` or
                ``"title_exclude"``).

        Returns:
            Deduplicated, lowercase merged list.
        """
        existing: list[str] = [
            v.lower() for v in (existing_prefilter.get(key) or [])
        ]
        existing_set = set(existing)
        merged = list(existing)
        for term in suggestions.get(key, []):
            term_lower = term.lower()
            if term_lower not in existing_set:
                merged.append(term_lower)
                existing_set.add(term_lower)
        return merged

    result["title_include"] = _merge_list("title_include")
    result["title_exclude"] = _merge_list("title_exclude")
    return result


# ---------------------------------------------------------------------------
# Async job state
# ---------------------------------------------------------------------------

# Text length threshold above which the import is dispatched to a background
# thread rather than blocking the Flask request.  Adjust as needed.
_PDF_ASYNC_THRESHOLD = 10_000

# Job store: maps job_id (str UUID) → job dict.
# Each entry: {id, status, result, error, created_at, started_at}
# status values: "pending" | "running" | "complete" | "failed"
_pdf_jobs: dict = {}
_pdf_jobs_lock = threading.Lock()

# Bounded thread pool for async PDF imports — prevents resource exhaustion.
# Created exactly once at module import time; do NOT re-create per request.
_pdf_executor = ThreadPoolExecutor(
    max_workers=3, thread_name_prefix="pdf-import"
)
_MAX_CONCURRENT_PDF_JOBS = 3

# Completed/failed jobs are pruned after this many seconds.
_PDF_JOB_TTL_SECONDS = 300  # 5 minutes
# Running jobs older than this are marked failed (hung LLM call protection).
_PDF_JOB_TIMEOUT_SECONDS = 300  # 5 minutes

# Rate-limit pruning so it doesn't run on every status poll.
_last_prune_time: float = 0.0
_PRUNE_INTERVAL_SECONDS = 30


# ---------------------------------------------------------------------------
# Async job helpers
# ---------------------------------------------------------------------------


def _prune_pdf_jobs() -> None:
    """Timeout stuck jobs and remove old completed/failed jobs.

    Rate-limited to run at most once per ``_PRUNE_INTERVAL_SECONDS`` to avoid
    O(n) iteration on every status poll.  Not exported — internal helper only.
    """
    global _last_prune_time
    now_mono = _time.monotonic()
    if now_mono - _last_prune_time < _PRUNE_INTERVAL_SECONDS:
        return
    _last_prune_time = now_mono

    now = datetime.now(timezone.utc).timestamp()
    cutoff = now - _PDF_JOB_TTL_SECONDS
    with _pdf_jobs_lock:
        # Timeout stuck running jobs
        for job in _pdf_jobs.values():
            if (
                job["status"] == "running"
                and job.get("started_at")
                and now - job["started_at"] > _PDF_JOB_TIMEOUT_SECONDS
            ):
                job["status"] = "failed"
                job["error"] = "Job timed out after 5 minutes."

        # Remove old completed/failed jobs
        to_delete = [
            jid
            for jid, job in _pdf_jobs.items()
            if job["status"] in ("complete", "failed")
            and job["created_at"] < cutoff
        ]
        for jid in to_delete:
            del _pdf_jobs[jid]


def _run_pdf_import_job(
    job_id: str,
    resume_text: str,
    mode: str,
    providers_dict: dict,
    profile_path: str,
    suggest_filters: bool = False,
) -> None:
    """Worker function executed in a daemon thread for large PDF imports.

    Calls the LLM provider chain synchronously (which can take 5–30 s), then
    stores the result or error in ``_pdf_jobs`` under ``job_id``.

    Args:
        job_id:          UUID string identifying the job in ``_pdf_jobs``.
        resume_text:     Pre-validated, sanitised resume text to send to LLM.
        mode:            ``"fresh"`` or ``"merge"``.
        providers_dict:  Loaded providers config dict (captured at request
                         time).
        profile_path:    Filesystem path to the profile JSON (for merge mode).
        suggest_filters: When ``True``, the LLM is additionally asked to
                         return ``prefilter_suggestions``
                         (title_include / title_exclude).
    """
    with _pdf_jobs_lock:
        _pdf_jobs[job_id]["status"] = "running"
        _pdf_jobs[job_id]["started_at"] = datetime.now(timezone.utc).timestamp()

    try:
        chain = build_provider_chain(providers_dict)
        if not chain:
            with _pdf_jobs_lock:
                _pdf_jobs[job_id]["status"] = "failed"
                _pdf_jobs[job_id]["error"] = (
                    "No LLM provider is configured. Add one in Settings first."
                )
            return

        current_profile = load_profile(profile_path) if mode == "merge" else None
        prompt = _build_import_prompt(resume_text, suggest_filters=suggest_filters)
        result = generate_with_fallback(prompt, chain, set())
        if result is None:
            with _pdf_jobs_lock:
                _pdf_jobs[job_id]["status"] = "failed"
                _pdf_jobs[job_id]["error"] = (
                    "All LLM providers failed. Check your API keys in Settings."
                )
            return

        raw_text, model_used = result
        parsed = _parse_import_response(raw_text)
        if parsed is None:
            with _pdf_jobs_lock:
                _pdf_jobs[job_id]["status"] = "failed"
                _pdf_jobs[job_id]["error"] = (
                    "LLM returned an unparseable response. Try again."
                )
            return

        if mode == "merge":
            profile_result = _merge_import_result(current_profile, parsed)
        else:
            structured_skills = []
            for s in parsed.get("primary_skills", []):
                name = s.get("skill", "")
                years = s.get("years", 0)
                status = s.get("status", "active")
                structured_skills.append({
                    "description": name,
                    "years_active": int(years) if years else 0,
                    "active": status != "dormant",
                })
            profile_result = {
                "primary_skills": structured_skills,
                "education": _normalise_education(parsed.get("education", [])),
                "seniority": parsed.get("seniority", ""),
                "preferred_industries": parsed.get("preferred_industries", []),
                "location_center": parsed.get("location_center"),
            }

        job_result: dict = {
            "success": True,
            "profile": profile_result,
            "model_used": model_used,
        }
        if suggest_filters and "prefilter_suggestions" in parsed:
            job_result["prefilter_suggestions"] = parsed["prefilter_suggestions"]

        with _pdf_jobs_lock:
            _pdf_jobs[job_id]["status"] = "complete"
            _pdf_jobs[job_id]["result"] = job_result

    except (ValueError, KeyError, TypeError, RuntimeError, OSError) as exc:
        with _pdf_jobs_lock:
            _pdf_jobs[job_id]["status"] = "failed"
            _pdf_jobs[job_id]["error"] = f"Import error: {exc}"
    except Exception as exc:  # noqa: BLE001 — daemon thread; must capture all failures
        with _pdf_jobs_lock:
            _pdf_jobs[job_id]["status"] = "failed"
            _pdf_jobs[job_id]["error"] = f"Unexpected error: {exc}"
