"""Profile blueprint — candidate preference form and PDF resume import.

Owns the 4 routes for viewing and updating the candidate profile:
  GET/POST  /profile                         structured candidate preference form
  POST      /profile/import-pdf              PDF resume LLM extraction
  GET       /profile/import-pdf/status/<id>  async PDF import job status poll
  POST      /api/apply-prefilter-suggestions merge LLM filter suggestions
"""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timezone

from flask import (
    Blueprint,
    jsonify,
    render_template,
    request,
    session,
)

from providers import build_provider_chain, generate_with_fallback
from services.pdf_import import (
    _MAX_CONCURRENT_PDF_JOBS,
    _PDF_ASYNC_THRESHOLD,
    _build_import_prompt,
    _extract_pdf_text,
    _merge_import_result,
    _merge_prefilter_suggestions,
    _normalise_education,
    _parse_import_response,
    _pdf_executor,
    _pdf_jobs,
    _pdf_jobs_lock,
    _prune_pdf_jobs,
    _run_pdf_import_job,
)
from services.profile_store import (
    _CONFIG_PATH,
    _KEYS_PATH,
    _PROFILE_PATH,
    _PROVIDERS_PATH,
    _parse_education_rows,
    _parse_repeating_rows,
    _validate_profile_form,
    _write_json_atomic,
    load_config,
    load_profile,
)
from services.provider_schemas import _load_providers_safe

profile_bp = Blueprint("profile_bp", __name__)


@profile_bp.route("/profile", methods=["GET", "POST"], endpoint="profile")
def profile():
    """Profile page — structured form for candidate preferences.

    GET:  Loads both ``profile.json`` and the candidate-facing subset of
          ``config.json``, and passes structured dicts to the template.
          No raw JSON is exposed; no sensitive fields are present.

    POST: Parses individual form fields, writes ``profile.json`` from the
          profile fields, and deep-merges only the candidate-facing config
          fields (``search.*`` candidate keys, ``scoring.threshold``,
          ``prefilter.*``) back into ``config.json`` — leaving technical
          keys (``results_per_page``, ``max_pages``, ``model``, etc.)
          untouched.  Returns 422 on validation errors without touching
          either file.

    Returns:
        Rendered ``profile.html`` template.  Status code is 200 on GET
        and successful POST, 422 on validation errors, 500 on write
        failures.
    """
    saved = False
    error = None
    status_code = 200

    if request.method == "POST":
        # --- Validate before touching disk ---
        threshold_str = request.form.get("scoring_threshold", "")
        validation_errors = _validate_profile_form(threshold_str)
        if validation_errors:
            error = "; ".join(validation_errors)
            status_code = 422
        else:
            field_errors: list[str] = []

            # Build location block.
            location_block: dict = {}
            loc_center = request.form.get("location_center", "").strip()
            loc_radius = request.form.get("location_radius_km", "").strip()
            loc_fallback = request.form.get(
                "location_geocode_fallback", "pass"
            ).strip()
            loc_notes = request.form.get("location_notes", "").strip()
            if loc_center:
                location_block["center"] = loc_center
            if loc_radius:
                try:
                    radius = float(loc_radius)
                    if radius > 0:
                        location_block["radius_km"] = radius
                    else:
                        field_errors.append(
                            "location.radius_km must be greater than 0"
                        )
                except ValueError:
                    field_errors.append(
                        "location.radius_km must be a number"
                    )
            location_block["geocode_fallback"] = loc_fallback or "pass"
            if loc_notes:
                location_block["notes"] = loc_notes

            # Parse structured primary_skills fields.
            # Each skill is submitted as parallel arrays:
            #   skill_description[]   — the skill name
            #   skill_years_active[]  — years of experience (integer)
            #   skill_active_idx[]    — indices (0-based) of active rows
            descriptions = request.form.getlist("skill_description[]")
            years_raw = request.form.getlist("skill_years_active[]")
            active_indices_raw = request.form.getlist(
                "skill_active_idx[]"
            )
            try:
                active_indices = {
                    int(x) for x in active_indices_raw if x.strip()
                }
            except ValueError:
                active_indices = set()

            primary_skills: list[dict] = []
            for i, desc in enumerate(descriptions):
                desc = desc.strip()
                if not desc:
                    continue
                years_str = years_raw[i] if i < len(years_raw) else "0"
                try:
                    years = int(years_str)
                except (ValueError, TypeError):
                    field_errors.append(
                        f"Primary skill '{desc}': years must be a whole"
                        f" number, got '{years_str}'"
                    )
                    continue
                if years < 0:
                    field_errors.append(
                        f"Primary skill '{desc}': years_active cannot"
                        " be negative"
                    )
                primary_skills.append({
                    "description": desc,
                    "years_active": years,
                    "active": i in active_indices,
                })

            new_profile: dict = {
                "primary_skills": primary_skills,
                "anti_preferences": _parse_repeating_rows(
                    request.form, "anti_preferences"
                ),
                "education": _parse_education_rows(request.form),
                "seniority": request.form.get("seniority", "").strip(),
                "preferred_industries": _parse_repeating_rows(
                    request.form, "preferred_industries"
                ),
                "location": location_block,
                "scoring_notes": _parse_repeating_rows(
                    request.form, "scoring_notes"
                ),
            }

            # Build the candidate-facing config.json subset.
            existing_cfg = load_config(_CONFIG_PATH)
            existing_search = existing_cfg.get("search") or {}
            existing_scoring = existing_cfg.get("scoring") or {}
            existing_prefilter = existing_cfg.get("prefilter") or {}

            salary_min_str = request.form.get(
                "search_salary_min", ""
            ).strip()
            distance_str = request.form.get(
                "search_distance", ""
            ).strip()
            max_days_str = request.form.get(
                "search_max_days_old", ""
            ).strip()

            updated_search = dict(existing_search)
            updated_search["country"] = request.form.get(
                "search_country", ""
            ).strip()
            updated_search["what"] = request.form.get(
                "search_what", ""
            ).strip()
            updated_search["where"] = request.form.get(
                "search_where", ""
            ).strip()
            if distance_str:
                try:
                    dist = int(distance_str)
                    if dist >= 0:
                        updated_search["distance"] = dist
                    else:
                        field_errors.append(
                            "search.distance must be 0 or greater"
                        )
                except ValueError:
                    field_errors.append(
                        "search.distance must be a whole number"
                    )
            else:
                updated_search.pop("distance", None)
            if salary_min_str:
                try:
                    sal = int(salary_min_str)
                    if sal >= 0:
                        updated_search["salary_min"] = sal
                    else:
                        field_errors.append(
                            "search.salary_min must be 0 or greater"
                        )
                except ValueError:
                    field_errors.append(
                        "search.salary_min must be a whole number"
                    )
            else:
                updated_search.pop("salary_min", None)
            if max_days_str:
                try:
                    days = int(max_days_str)
                    if days > 0:
                        updated_search["max_days_old"] = days
                    else:
                        field_errors.append(
                            "search.max_days_old must be greater than 0"
                        )
                except ValueError:
                    field_errors.append(
                        "search.max_days_old must be a whole number"
                    )
            else:
                updated_search.pop("max_days_old", None)

            if field_errors:
                error = "; ".join(field_errors)
                status_code = 422

            if not field_errors:
                # scoring.threshold — already validated above.
                updated_scoring = dict(existing_scoring)
                updated_scoring["threshold"] = float(
                    threshold_str.strip()
                )

                require_contract_time_raw = request.form.get(
                    "prefilter_require_contract_time", ""
                ).strip()
                require_contract_type_raw = request.form.get(
                    "prefilter_require_contract_type", ""
                ).strip()
                updated_prefilter = dict(existing_prefilter)
                updated_prefilter["title_include"] = (
                    _parse_repeating_rows(
                        request.form, "prefilter_title_include"
                    )
                )
                updated_prefilter["title_exclude"] = (
                    _parse_repeating_rows(
                        request.form, "prefilter_title_exclude"
                    )
                )
                updated_prefilter["require_contract_time"] = (
                    require_contract_time_raw or None
                )
                updated_prefilter["require_contract_type"] = (
                    require_contract_type_raw or None
                )

                new_cfg = dict(existing_cfg)
                new_cfg["search"] = updated_search
                new_cfg["scoring"] = updated_scoring
                new_cfg["prefilter"] = updated_prefilter

                try:
                    _write_json_atomic(_PROFILE_PATH, new_profile)
                    _write_json_atomic(_CONFIG_PATH, new_cfg)
                    saved = True
                except OSError:
                    error = (
                        "Could not save — check file permissions."
                    )
                    status_code = 500

    # Load current values for the form (GET, or POST after error).
    cfg = load_config(_CONFIG_PATH)
    prof = load_profile(_PROFILE_PATH)

    # Establish the session CSRF token so the import drawer can include
    # it on the POST /api/apply-prefilter-suggestions request.
    session.setdefault("csrf_token", secrets.token_urlsafe(32))

    return render_template(
        "profile.html",
        view="profile",
        prof=prof,
        cfg=cfg,
        saved=saved,
        error=error,
        csrf_token=session["csrf_token"],
    ), status_code


@profile_bp.route(
    "/profile/import-pdf", methods=["POST"],
    endpoint="profile_import_pdf"
)
def profile_import_pdf():
    """Import profile data from an uploaded PDF resume via LLM extraction.

    Accepts a multipart/form-data POST with:
    - ``file``: PDF file upload (required, max 10 MB).
    - ``mode``: ``"fresh"`` (default) or ``"merge"``.

    **Small PDFs** (extracted text <= ``_PDF_ASYNC_THRESHOLD`` chars) are
    processed synchronously and return the result directly.

    **Large PDFs** (extracted text > ``_PDF_ASYNC_THRESHOLD`` chars) are
    dispatched to a daemon thread; the response is HTTP 202 with a
    ``job_id`` that the client must poll via
    ``GET /profile/import-pdf/status/<job_id>``.

    Returns JSON — does NOT write profile.json.  The response payload is
    intended for client-side form pre-fill so the user can review before
    saving.

    Returns:
        200 ``{"success": True, "profile": {...}, "model_used": "..."}``
        202 ``{"async": True, "job_id": "<uuid>"}`` (large PDF)
        400 invalid input (no file, non-PDF, unreadable PDF)
        413 file or extracted text exceeds size limits
        422 extracted text too short to be useful
        502 LLM failure (all providers failed or unparseable response)
        503 no LLM provider configured
    """
    import uuid as _uuid  # noqa: PLC0415

    uploaded = request.files.get("file")
    if not uploaded or not uploaded.filename:
        return jsonify({"success": False, "error": "No file uploaded."}), 400
    if not uploaded.filename.lower().endswith(".pdf"):
        return jsonify(
            {"success": False, "error": "Only PDF files are accepted."}
        ), 400

    mode = request.form.get("mode", "fresh")
    if mode not in ("fresh", "merge"):
        mode = "fresh"

    suggest_filters = request.form.get("suggest_filters") == "1"

    pdf_bytes = uploaded.read()
    if len(pdf_bytes) > 10 * 1024 * 1024:
        return jsonify(
            {"success": False, "error": "PDF exceeds the 10 MB size limit."}
        ), 413
    try:
        resume_text = _extract_pdf_text(pdf_bytes)
    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400

    if len(resume_text.strip()) < 50:
        return jsonify({
            "success": False,
            "error": (
                "Could not extract meaningful text from this PDF."
            ),
        }), 422

    if len(resume_text) > 50_000:
        return jsonify({
            "success": False,
            "error": (
                "Extracted PDF text exceeds the 50,000 character limit."
            ),
        }), 413
    resume_text = "".join(
        ch for ch in resume_text if ch.isprintable() or ch in "\n\r\t"
    )

    if len(resume_text) > _PDF_ASYNC_THRESHOLD:
        job_id = str(_uuid.uuid4())
        with _pdf_jobs_lock:
            active = sum(
                1 for j in _pdf_jobs.values()
                if j["status"] in ("pending", "running")
            )
            if active >= _MAX_CONCURRENT_PDF_JOBS:
                return jsonify({
                    "success": False,
                    "error": (
                        "Too many concurrent imports. Please wait and"
                        " try again."
                    ),
                }), 429
            _pdf_jobs[job_id] = {
                "id": job_id,
                "status": "pending",
                "result": None,
                "error": None,
                "created_at": datetime.now(timezone.utc).timestamp(),
            }
        providers_dict = _load_providers_safe(
            providers_path=_PROVIDERS_PATH,
            keys_path=_KEYS_PATH,
            config_path=_CONFIG_PATH,
        )
        _pdf_executor.submit(
            _run_pdf_import_job,
            job_id,
            resume_text,
            mode,
            providers_dict,
            _PROFILE_PATH,
            suggest_filters,
        )
        return jsonify({"async": True, "job_id": job_id}), 202

    # Small PDF — synchronous path
    providers_dict = _load_providers_safe(
        providers_path=_PROVIDERS_PATH,
        keys_path=_KEYS_PATH,
        config_path=_CONFIG_PATH,
    )
    chain = build_provider_chain(providers_dict)
    if not chain:
        return jsonify({
            "success": False,
            "error": (
                "No LLM provider is configured. Add one in Settings first."
            ),
        }), 503

    current_profile = (
        load_profile(_PROFILE_PATH) if mode == "merge" else None
    )
    prompt = _build_import_prompt(
        resume_text, suggest_filters=suggest_filters
    )
    result = generate_with_fallback(prompt, chain, set())
    if result is None:
        return jsonify({
            "success": False,
            "error": (
                "All LLM providers failed. Check your API keys in"
                " Settings."
            ),
        }), 502

    raw_text, model_used = result

    parsed = _parse_import_response(raw_text)
    if parsed is None:
        return jsonify({
            "success": False,
            "error": (
                "LLM returned an unparseable response. Try again."
            ),
        }), 502

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
            "education": _normalise_education(
                parsed.get("education", [])
            ),
            "seniority": parsed.get("seniority", ""),
            "preferred_industries": parsed.get(
                "preferred_industries", []
            ),
            "location_center": parsed.get("location_center"),
        }

    response_payload: dict = {
        "success": True,
        "profile": profile_result,
        "model_used": model_used,
    }
    if suggest_filters and "prefilter_suggestions" in parsed:
        response_payload["prefilter_suggestions"] = (
            parsed["prefilter_suggestions"]
        )

    return jsonify(response_payload), 200


@profile_bp.route(
    "/profile/import-pdf/status/<job_id>", methods=["GET"],
    endpoint="profile_import_pdf_status"
)
def profile_import_pdf_status(job_id: str):
    """Poll the status of an async PDF import job.

    Args:
        job_id: UUID returned by ``POST /profile/import-pdf`` when a
                large PDF was submitted (response contained
                ``"async": True``).

    Returns:
        200 ``{"status": "pending"}`` or ``{"status": "running"}``
        200 ``{"status": "complete", "result": {...}}``
        200 ``{"status": "failed", "error": "..."}``
        404 if ``job_id`` is unknown or has already been pruned
    """
    _prune_pdf_jobs()

    with _pdf_jobs_lock:
        job = _pdf_jobs.get(job_id)

    if job is None:
        return jsonify({"error": "Job not found."}), 404

    status = job["status"]
    if status in ("pending", "running"):
        return jsonify({"status": status}), 200
    if status == "complete":
        return jsonify({"status": "complete", "result": job["result"]}), 200
    # status == "failed"
    return jsonify({"status": "failed", "error": job["error"]}), 200


@profile_bp.route(
    "/api/apply-prefilter-suggestions", methods=["POST"],
    endpoint="apply_prefilter_suggestions"
)
def apply_prefilter_suggestions():
    """Merge LLM-suggested title filters into config.json prefilter block.

    Accepts a form-encoded POST with fields:

    * ``csrf_token`` — session-scoped CSRF token (required; 403 on
      mismatch)
    * ``title_include`` — JSON-encoded array of include patterns
    * ``title_exclude`` — JSON-encoded array of exclude patterns

    The suggestions are merged (union-then-dedup, case-insensitive) into
    the existing ``config.json`` ``prefilter`` block via
    ``_merge_prefilter_suggestions()``.  All other prefilter keys
    (``require_contract_time``, ``require_contract_type``) are preserved.

    Returns:
        200 ``{"success": True}`` on success.
        400 on missing/invalid input or overlapping include/exclude terms.
        403 on CSRF token mismatch.
        500 on config read/write failure.
    """
    from flask import current_app  # noqa: PLC0415

    csrf_token = request.form.get("csrf_token", "")
    if not csrf_token or csrf_token != session.get("csrf_token"):
        return jsonify({
            "success": False,
            "error": (
                "Invalid or missing CSRF token — request rejected."
            ),
        }), 403

    inc_json = request.form.get("title_include", "")
    exc_json = request.form.get("title_exclude", "")

    try:
        inc_raw = json.loads(inc_json) if inc_json else None
        exc_raw = json.loads(exc_json) if exc_json else None
    except (json.JSONDecodeError, ValueError):
        inc_raw = None
        exc_raw = None

    if not isinstance(inc_raw, list) or not isinstance(exc_raw, list):
        return jsonify({
            "success": False,
            "error": (
                "title_include and title_exclude must be JSON-encoded"
                " arrays."
            ),
        }), 400

    inc = [str(s).lower() for s in inc_raw]
    exc = [str(s).lower() for s in exc_raw]

    overlap = set(inc) & set(exc)
    if overlap:
        return jsonify({
            "success": False,
            "error": (
                "title_include and title_exclude must be disjoint. "
                f"Overlapping terms: {sorted(overlap)}"
            ),
        }), 400

    try:
        with open(_CONFIG_PATH, "r", encoding="utf-8") as fh:
            cfg = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc_io:
        current_app.logger.error(
            "[apply-prefilter-suggestions] failed to read config: %s",
            exc_io,
        )
        return jsonify({
            "success": False,
            "error": "Could not read config.json.",
        }), 500

    existing_prefilter = cfg.get("prefilter") or {}
    cfg["prefilter"] = _merge_prefilter_suggestions(
        existing_prefilter,
        {"title_include": inc, "title_exclude": exc},
    )

    try:
        with open(_CONFIG_PATH, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2)
            fh.write("\n")
    except OSError as exc_io:
        current_app.logger.error(
            "[apply-prefilter-suggestions] failed to write config: %s",
            exc_io,
        )
        return jsonify({
            "success": False,
            "error": "Could not write config.json.",
        }), 500

    return jsonify({"success": True}), 200
