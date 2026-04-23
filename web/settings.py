"""Settings blueprint — LLM provider and job source credential management.

Owns the 5 routes for viewing and updating settings:
  GET/POST  /settings                     credential form for LLM providers
                                          and job sources
  GET       /settings/config              301 redirect to /profile
  POST      /api/validate-keys            validate all configured LLM providers
  POST      /api/providers/reorder        persist new LLM provider fallback order
  POST      /api/job-sources/<k>/toggle   persist enabled/disabled for a source
"""

from __future__ import annotations

from flask import (
    Blueprint,
    redirect,
    render_template,
    request,
    url_for,
)

from credentials import save_providers
from job_sources import get_sources
from providers import _PROVIDER_CLASS_MAP
from services.profile_store import (
    _CONFIG_PATH,
    _KEYS_PATH,
    _PROVIDERS_PATH,
    load_config,
    _write_json_atomic,
)
from services.provider_schemas import (
    _build_llm_schemas,
    _get_search_validation_issues,
    _load_providers_safe,
    _validate_with_timeout,
)

settings_bp = Blueprint("settings_bp", __name__)


@settings_bp.route("/settings", methods=["GET", "POST"], endpoint="settings")
def settings() -> str:
    """Settings page — manage LLM provider credentials and job source credentials.

    GET:  Builds ``llm_schemas`` and ``source_schemas`` from the provider/source
          registries and passes only boolean ``has_values`` flags — never raw
          credential values — to the template.  Tab is selected via ``?tab=``
          query param (default: ``llm``).

    POST: Parses namespaced form fields (``<provider_key>__<field_name>``),
          deep-merges non-blank values into ``providers.json`` via
          :func:`credentials.save_providers`, then redirects to
          ``GET /settings?tab=<active_tab>``.

    Returns:
        Rendered ``settings.html`` template on GET or POST-with-error.
        A redirect to ``GET /settings?tab=<active_tab>`` on successful POST.
    """
    error = None

    if request.method == "POST":
        active_tab = request.form.get("tab", "llm").strip()

        # --- Build updates dict from namespaced form fields ---
        # Only populate the section that corresponds to the active tab.
        # Processing the other section would send blank values for every
        # field not present in the submitted form, causing _deep_merge to
        # overwrite previously-saved credentials with empty strings
        # (cross-tab wipe bug, issue #71).
        updates: dict = {}

        if active_tab == "llm":
            updates["llm"] = {}
            # Load the current stored state once so we can fill in missing
            # non-password field defaults when the JS dirty-tracker omits
            # unchanged fields from the POST body (fixes issue #231).
            _current_providers = _load_providers_safe(
                providers_path=_PROVIDERS_PATH,
                keys_path=_KEYS_PATH,
                config_path=_CONFIG_PATH,
            )
            _current_llm = _current_providers.get("llm") or {}
            for provider_key, cls in _PROVIDER_CLASS_MAP.items():
                schema = cls.settings_schema()
                provider_updates: dict = {}
                for field in schema["fields"]:
                    field_name = field["name"]
                    form_key = f"{provider_key}__{field_name}"
                    raw = request.form.get(form_key)
                    if raw is None:
                        # Field not present in form at all — skip to preserve
                        # any existing stored value.
                        continue
                    stripped = raw.strip()
                    # No-JS guard: skip empty password fields unless the
                    # explicit __clear__ flag is present.  This prevents a
                    # native (no-JS) form submit from wiping an existing key
                    # just because the password placeholder was left blank.
                    if field.get("type") == "password" and stripped == "":
                        clear_key = (
                            f"__clear__{provider_key}__{field_name}"
                        )
                        if request.form.get(clear_key) != "1":
                            continue
                    provider_updates[field_name] = stripped
                # After processing normal fields, check for explicit __clear__
                # flags on password fields.
                for field in schema["fields"]:
                    if field.get("type") != "password":
                        continue
                    clear_key = (
                        f"__clear__{provider_key}__{field['name']}"
                    )
                    if request.form.get(clear_key) == "1":
                        provider_updates[field["name"]] = ""
                # When the provider is being updated, ensure every
                # non-password field that was NOT in the POST body (because
                # JS dirty-tracking only sends changed fields) is written
                # with its current stored value or its schema default.
                if provider_updates:
                    stored_cfg = _current_llm.get(provider_key) or {}
                    for field in schema["fields"]:
                        if field.get("type") == "password":
                            continue
                        field_name = field["name"]
                        if field_name in provider_updates:
                            continue
                        stored_val = stored_cfg.get(field_name, "")
                        if not stored_val:
                            default_val = field.get("default", "")
                            if default_val:
                                provider_updates[field_name] = default_val
                    updates["llm"][provider_key] = provider_updates

        elif active_tab == "sources":
            updates["job_sources"] = {}
            # Job sources: JS dirty-tracking sends only the fields the user
            # actually changed, so we must skip sources that have no form
            # data at all.
            for source_key, cls in get_sources().items():
                schema_fields = cls.settings_schema()["fields"]
                cred_keys = [
                    f"{source_key}__{f['name']}" for f in schema_fields
                ]
                clear_keys = [
                    f"__clear__{source_key}__{f['name']}"
                    for f in schema_fields
                ]
                enabled_key = f"{source_key}__enabled"
                source_in_form = any(
                    request.form.get(k) is not None
                    for k in cred_keys + [enabled_key] + clear_keys
                )
                if not source_in_form:
                    continue

                source_updates: dict = {}

                # Checkbox: only update enabled when the field was explicitly
                # submitted.
                if enabled_key in request.form:
                    source_updates["enabled"] = (
                        request.form.get(enabled_key) == "on"
                    )

                for field in schema_fields:
                    field_name = field["name"]
                    form_key = f"{source_key}__{field_name}"
                    raw = request.form.get(form_key)
                    if raw is None:
                        continue
                    stripped = raw.strip()
                    if field.get("type") == "password" and stripped == "":
                        clear_key = (
                            f"__clear__{source_key}__{field_name}"
                        )
                        if request.form.get(clear_key) != "1":
                            continue
                    source_updates[field_name] = stripped
                # Explicit __clear__ flags for password fields.
                for field in schema_fields:
                    if field.get("type") != "password":
                        continue
                    clear_key = (
                        f"__clear__{source_key}__{field['name']}"
                    )
                    if request.form.get(clear_key) == "1":
                        source_updates[field["name"]] = ""

                updates["job_sources"][source_key] = source_updates

        try:
            save_providers(updates, providers_path=_PROVIDERS_PATH)
        except OSError:
            error = "Could not save settings — check file permissions."

        # Save search fields to config.json on the search tab.
        if error is None and active_tab == "search":
            existing_cfg = load_config(_CONFIG_PATH)
            existing_search = existing_cfg.get("search") or {}
            updated_search = dict(existing_search)

            for field_name in (
                "search_country", "search_what", "search_where"
            ):
                raw = request.form.get(field_name, "").strip()
                # Strip the "search_" prefix to get the config key.
                config_key = field_name[len("search_"):]
                if raw:
                    updated_search[config_key] = raw
                elif field_name in request.form:
                    updated_search.pop(config_key, None)

            rpp_str = request.form.get(
                "search_results_per_page", ""
            ).strip()
            mp_str = request.form.get("search_max_pages", "").strip()
            if rpp_str:
                try:
                    updated_search["results_per_page"] = int(rpp_str)
                except ValueError:
                    pass
            if mp_str:
                try:
                    updated_search["max_pages"] = int(mp_str)
                except ValueError:
                    pass

            updated_cfg = dict(existing_cfg)
            updated_cfg["search"] = updated_search
            try:
                _write_json_atomic(_CONFIG_PATH, updated_cfg)
            except OSError:
                error = (
                    "Could not save config — check file permissions."
                )

        if error is None:
            return redirect(url_for("settings_bp.settings", tab=active_tab))

    # --- GET (or POST with error) ---
    active_tab = request.args.get("tab", "llm").strip()
    providers_data = _load_providers_safe(
        providers_path=_PROVIDERS_PATH,
        keys_path=_KEYS_PATH,
        config_path=_CONFIG_PATH,
    )
    llm_section: dict = providers_data.get("llm") or {}
    sources_section: dict = providers_data.get("job_sources") or {}

    # provider_order from providers.json determines display sequence.
    provider_order: list[str] = providers_data.get("provider_order") or []
    llm_schemas = _build_llm_schemas(llm_section, provider_order)

    source_schemas: list[tuple] = []
    for key, cls in get_sources().items():
        schema = cls.settings_schema()
        cfg = sources_section.get(key) or {}
        required_fields = [
            f["name"] for f in schema["fields"] if f.get("required")
        ]
        if required_fields:
            has_values = all(
                bool(cfg.get(fn, "").strip()) for fn in required_fields
            )
        else:
            # No-credential sources are never "configured".
            has_values = False
        is_enabled = bool(cfg.get("enabled", False))
        credentials_required = bool(required_fields)
        populated_fields = {
            f["name"] for f in schema["fields"]
            if bool(cfg.get(f["name"], "").strip())
        }
        source_schemas.append((
            key,
            schema,
            has_values,
            is_enabled,
            credentials_required,
            populated_fields,
        ))

    saved = False
    if request.method == "POST" and error:
        pass  # fall through to render with error

    search_cfg = load_config(_CONFIG_PATH).get("search") or {}
    search_issues = _get_search_validation_issues(
        providers_path=_PROVIDERS_PATH, config_path=_CONFIG_PATH
    )

    return render_template(
        "settings.html",
        view="settings",
        llm_schemas=llm_schemas,
        source_schemas=source_schemas,
        active_tab=active_tab,
        saved=saved,
        error=error,
        search_cfg=search_cfg,
        search_issues=search_issues,
    )


@settings_bp.route("/settings/config", endpoint="settings_config_redirect")
def settings_config_redirect():
    """Redirect legacy /settings/config URL to /profile.

    Returns:
        A 301 permanent redirect to the profile page.
    """
    return redirect(url_for("profile_bp.profile"), code=301)


@settings_bp.route("/api/validate-keys", methods=["POST"],
                   endpoint="validate_keys")
def validate_keys():
    """Validate each configured LLM provider by making a minimal test call.

    Loops ``_PROVIDER_CLASS_MAP`` so new providers are included automatically
    without any template or route changes.

    Returns an HTML partial (not JSON) intended for HTMX to swap into the
    page.  Each provider gets one of five states: valid, invalid_key,
    unknown_model, unreachable, not_configured.  Each provider call is
    bounded to ``_VALIDATE_TIMEOUT_SECONDS`` seconds; a timeout maps to
    ``unreachable``.

    No API key values are logged or returned in the response.

    Returns:
        Rendered ``_validation_results.html`` template fragment.
    """
    providers_data = _load_providers_safe(
        providers_path=_PROVIDERS_PATH,
        keys_path=_KEYS_PATH,
        config_path=_CONFIG_PATH,
    )
    llm_section: dict = providers_data.get("llm") or {}

    providers_list = []
    for provider_key, cls in _PROVIDER_CLASS_MAP.items():
        schema = cls.settings_schema()
        display_name: str = schema.get("display_name", provider_key.title())

        cfg = llm_section.get(provider_key, {})
        api_key = cfg.get("api_key", "").strip()
        model = cfg.get("model", "").strip()

        if not api_key:
            state = "not_configured"
            detail = None
        else:
            state, detail = _validate_with_timeout(
                cls.validate_credentials, api_key, model
            )

        providers_list.append({
            "key": provider_key,
            "display_name": display_name,
            "state": state,
            "detail": detail,
        })

    return render_template(
        "_validation_results.html", providers=providers_list
    )


@settings_bp.route(
    "/api/providers/reorder", methods=["POST"],
    endpoint="api_providers_reorder"
)
def api_providers_reorder():
    """Persist a new LLM provider fallback order.

    Expects JSON body: ``{"order": ["anthropic", "gemini", "openai"]}``

    All entries must be known keys in ``_PROVIDER_CLASS_MAP``; unknown
    keys → 400.  ``order`` may be a subset of the registry (omitted
    providers are appended at runtime by ``build_provider_chain()``).
    Writes only ``provider_order`` at the top level of ``providers.json``.
    Returns the rendered ``_provider_order.html`` fragment on success (200).
    Returns a plain-text error message on failure (400/500).

    Returns:
        200 rendered ``_provider_order.html`` on success.
        400 plain text on missing/invalid fields or unknown provider keys.
        500 plain text on file-write failure.
    """
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return "Invalid request body — expected JSON object.", 400

    order = body.get("order")
    if not isinstance(order, list):
        return (
            "Missing or invalid 'order' field — expected a JSON array.",
            400,
        )

    if not all(isinstance(k, str) for k in order):
        return "All entries in 'order' must be strings.", 400

    unknown = [k for k in order if k not in _PROVIDER_CLASS_MAP]
    if unknown:
        return f"Unknown provider key(s): {', '.join(unknown)}", 400

    if len(order) != len(set(order)):
        return "Duplicate provider key(s) in order list.", 400

    try:
        save_providers(
            {"provider_order": order}, providers_path=_PROVIDERS_PATH
        )
    except OSError:
        return "Could not save order — check file permissions.", 500

    # Re-build llm_schemas in the new order for the response fragment.
    providers_data = _load_providers_safe(
        providers_path=_PROVIDERS_PATH,
        keys_path=_KEYS_PATH,
        config_path=_CONFIG_PATH,
    )
    llm_section: dict = providers_data.get("llm") or {}
    llm_schemas = _build_llm_schemas(llm_section, order)

    return render_template("_provider_order.html", llm_schemas=llm_schemas)


@settings_bp.route(
    "/api/job-sources/<source_key>/toggle", methods=["POST"],
    endpoint="api_job_source_toggle"
)
def api_job_source_toggle(source_key: str):
    """Persist the enabled/disabled state for a single job source.

    Designed for HTMX ``hx-trigger="change"`` on the source toggle
    checkbox so the change is saved immediately without a full form submit.

    Request body (JSON)::

        {"enabled": true}   # or false

    Validation rules:

    * ``source_key`` must exist in the ``SOURCES`` registry → 404.
    * When ``enabled=true``, all ``required`` credential fields must have
      non-empty values already stored in ``providers.json`` → 422.
    * When ``enabled=false``, no credential check is performed.

    Args:
        source_key: The source registry key from the URL path.

    Returns:
        200 JSON ``{"ok": true}`` on success.
        404 JSON ``{"error": "..."}`` for unknown source keys.
        422 JSON ``{"error": "..."}`` when required credentials are missing.
        400 plain text for a malformed request body.
        500 plain text if the file cannot be written.
    """
    from flask import jsonify  # noqa: PLC0415

    if source_key not in get_sources():
        return jsonify(
            {"error": f"Unknown job source: {source_key!r}"}
        ), 404

    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return "Invalid request body — expected JSON object.", 400

    if "enabled" not in body:
        return "Missing 'enabled' field in request body.", 400

    enabled = body["enabled"]
    if not isinstance(enabled, bool):
        return (
            "The 'enabled' field must be a boolean (true or false).",
            400,
        )

    # When enabling, verify required credentials are already stored.
    if enabled:
        cls = get_sources()[source_key]
        schema = cls.settings_schema()
        required_fields = [
            f for f in schema.get("fields", []) if f.get("required")
        ]

        if required_fields:
            providers_data = _load_providers_safe(
                providers_path=_PROVIDERS_PATH,
                keys_path=_KEYS_PATH,
                config_path=_CONFIG_PATH,
            )
            src_cfg: dict = (
                (providers_data.get("job_sources") or {}).get(source_key)
                or {}
            )
            missing = [
                f["label"]
                for f in required_fields
                if not str(src_cfg.get(f["name"], "")).strip()
            ]
            if missing:
                display_name = schema.get("display_name", source_key)
                fields_str = " and ".join(missing)
                return jsonify({
                    "error": (
                        f"{display_name} requires {fields_str} before it"
                        " can be enabled. Add credentials in the Settings"
                        " form and save, then try again."
                    )
                }), 422

    try:
        save_providers(
            {"job_sources": {source_key: {"enabled": enabled}}},
            providers_path=_PROVIDERS_PATH,
        )
    except OSError:
        return "Could not save — check file permissions.", 500

    return jsonify({"ok": True}), 200
