"""Config and profile I/O helpers for the Job Matcher web server.

This module owns all JSON-on-disk reads and writes for the two primary
configuration files (``config/config.json`` and ``config/profile.json``),
the legacy keys file (``config/keys.json``), and the unified provider
credential store (``config/providers.json``).

It also provides the form-parsing helpers that convert raw HTML form data
into the structured dicts written back to disk.

Public API
----------
Path constants (module-level)
    ``_CONFIG_DIR``, ``_KEYS_PATH``, ``_CONFIG_PATH``,
    ``_PROFILE_PATH``, ``_PROVIDERS_PATH``

Legacy credential defaults
    ``_KEYS_DEFAULTS``

Read helpers
    ``load_config(path) -> dict``
    ``load_profile(path) -> dict``

Write helper
    ``_write_json_atomic(path, data) -> None``

Validation
    ``_validate_profile_form(threshold_str) -> list[str]``

Form parsing
    ``_parse_education_rows(form) -> list[dict]``
    ``_parse_repeating_rows(form, field_name) -> list[str]``

Design notes
------------
Zero Flask imports — this module must remain importable without a running
Flask application so that ``services/pdf_import.py`` (Phase 3) and other
non-request contexts can call ``load_profile`` safely.
"""

from __future__ import annotations

import json
import os
from typing import Any

from config_io import atomic_config_write

# ---------------------------------------------------------------------------
# Path constants
# ---------------------------------------------------------------------------

_CONFIG_DIR: str = os.path.join(os.path.dirname(__file__), "..", "config")
_KEYS_PATH: str = os.path.join(_CONFIG_DIR, "keys.json")
_CONFIG_PATH: str = os.path.join(_CONFIG_DIR, "config.json")
_PROFILE_PATH: str = os.path.join(_CONFIG_DIR, "profile.json")
_PROVIDERS_PATH: str = os.path.join(_CONFIG_DIR, "providers.json")

# Default structure mirrors keys.example.json — used when keys.json is absent.
_KEYS_DEFAULTS: dict[str, Any] = {
    "providers": {
        "anthropic": {"api_key": "", "model": "claude-haiku-4-5-20251001"},
        "openai":    {"api_key": "", "model": "gpt-4o-mini"},
        "gemini":    {"api_key": "", "model": "gemini-1.5-flash"},
    },
    "preferred_provider": "anthropic",
}


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def load_config(path: str = _CONFIG_PATH) -> dict[str, Any]:
    """Load ``config/config.json`` if it exists; return safe defaults otherwise.

    Allows the server to start and display the UI even before the user has
    created their config file.  The ``scoring.threshold`` key is always
    present in the returned dict so callers can rely on it without a further
    ``get`` guard.

    Args:
        path: Absolute or relative path to the JSON config file.  Defaults
            to the project ``config/config.json``.

    Returns:
        Parsed config dict.  Falls back to ``{"scoring": {"threshold": 7.0}}``
        on any read or parse error.
    """
    defaults: dict[str, Any] = {
        "scoring": {
            "threshold": 7.0,
        }
    }
    if not os.path.exists(path):
        return defaults
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        # Ensure scoring.threshold has a fallback even if the key is missing.
        data.setdefault("scoring", {})
        data["scoring"].setdefault("threshold", 7.0)
        return data
    except (json.JSONDecodeError, OSError):
        return defaults


# ---------------------------------------------------------------------------
# Atomic JSON write
# ---------------------------------------------------------------------------


def _write_json_atomic(path: str, data: dict[str, Any]) -> None:
    """Write *data* as JSON to *path* under an advisory lock, atomically.

    Acquires a cross-platform file lock on ``<path>.lock``, then writes
    *data* atomically via a ``<path>.tmp`` → :func:`os.replace` rename.
    The lock serialises concurrent writers so no update is lost.

    This is a thin wrapper around :func:`config_io.atomic_config_write`
    for callers that have already built the full dict to write.  New code
    should prefer the context-manager form directly so the read also
    happens under the lock.

    Args:
        path: Destination file path.
        data: Dict to serialise as indented JSON.  Replaces whatever the
            file currently contains.

    Raises:
        filelock.Timeout: If the advisory lock cannot be acquired within
            the default timeout.
        OSError: If writing or renaming fails.
    """
    with atomic_config_write(path) as on_disk:
        on_disk.clear()
        on_disk.update(data)


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------


def load_profile(path: str = _PROFILE_PATH) -> dict[str, Any]:
    """Load ``config/profile.json`` if it exists; return an empty dict otherwise.

    Returns an empty dict (not hard-coded defaults) so the profile form shows
    blank fields rather than confusing placeholder values when the file is
    absent.

    Legacy migration: education entries that are plain strings (old format
    ``"education": ["B.S. in Computer Science"]``) are converted to structured
    dicts on load so the template never receives a string where it expects a
    dict.

    Args:
        path: Absolute or relative path to the profile JSON file.  Defaults
            to the project ``config/profile.json``.

    Returns:
        Parsed profile dict, or ``{}`` on any read or parse error.
    """
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data: dict[str, Any] = json.load(fh)
    except (json.JSONDecodeError, OSError):
        return {}

    # Normalise legacy free-text education strings to structured dicts.
    raw_edu = data.get("education", [])
    if raw_edu and any(not isinstance(e, dict) for e in raw_edu):
        data["education"] = [
            {
                "degree_type": "",
                "degree_field": str(e),
                "school": "",
                "graduation_year": "",
            }
            if not isinstance(e, dict)
            else e
            for e in raw_edu
        ]

    return data


# ---------------------------------------------------------------------------
# Profile form validation
# ---------------------------------------------------------------------------


def _validate_profile_form(threshold_str: str) -> list[str]:
    """Validate the structured profile form fields.

    Validates only the fields that can be invalid in a structured form.
    Raw JSON parsing errors are no longer possible because the form owns the
    field types.

    Args:
        threshold_str: The raw string value submitted for
            ``scoring.threshold``.

    Returns:
        List of human-readable error strings.  An empty list means all
        validated fields are acceptable.
    """
    errors: list[str] = []

    # scoring.threshold must parse as a float in [0, 10].
    if not threshold_str.strip():
        errors.append("scoring.threshold is required")
    else:
        try:
            val = float(threshold_str.strip())
            if not (0 <= val <= 10):
                errors.append("scoring.threshold must be between 0 and 10")
        except ValueError:
            errors.append("scoring.threshold must be a number")

    return errors


# ---------------------------------------------------------------------------
# Form parsing helpers
# ---------------------------------------------------------------------------


def _parse_education_rows(form: Any) -> list[dict[str, str]]:
    """Extract structured education objects from education table form fields.

    Reads the four parallel ``edu_type[]``, ``edu_field[]``, ``edu_school[]``,
    and ``edu_year[]`` arrays from the submitted form and zips them into
    structured dicts.  Rows where all four fields are empty are silently
    discarded.

    The *form* parameter is typed as ``Any`` to avoid importing
    ``werkzeug.datastructures.ImmutableMultiDict`` — callers pass
    ``request.form`` directly.

    Args:
        form: The Flask ``request.form`` ImmutableMultiDict (or any object
            that implements ``.getlist(key)``).

    Returns:
        List of dicts, each with keys ``degree_type``, ``degree_field``,
        ``school``, and ``graduation_year``.
    """
    types = form.getlist("edu_type[]")
    fields = form.getlist("edu_field[]")
    schools = form.getlist("edu_school[]")
    years = form.getlist("edu_year[]")

    # Zip to the shortest list to guard against mismatched row counts.
    rows: list[dict[str, str]] = []
    for deg_type, deg_field, school, year in zip(
        types, fields, schools, years
    ):
        deg_type = deg_type.strip()
        deg_field = deg_field.strip()
        school = school.strip()
        year = year.strip()
        # Discard non-numeric year values to prevent nonsense input.
        if year and not year.isdigit():
            year = ""
        # Skip rows where every field is empty.
        if not any([deg_type, deg_field, school, year]):
            continue
        rows.append(
            {
                "degree_type": deg_type,
                "degree_field": deg_field,
                "school": school,
                "graduation_year": year,
            }
        )
    return rows


def _parse_repeating_rows(form: Any, field_name: str) -> list[str]:
    """Extract non-empty strings from repeating form row inputs.

    The repeating-row pattern names inputs as ``<field_name>[]``, submitting
    one value per row.  Empty rows (whitespace-only) are discarded so the
    stored array does not contain blank entries.

    The *form* parameter is typed as ``Any`` to avoid importing
    ``werkzeug.datastructures.ImmutableMultiDict`` — callers pass
    ``request.form`` directly.

    Args:
        form: The Flask ``request.form`` ImmutableMultiDict (or any object
            that implements ``.getlist(key)``).
        field_name: Base name used in the HTML (e.g. ``"primary_skills"``).

    Returns:
        List of stripped non-empty strings.
    """
    values = form.getlist(f"{field_name}[]")
    return [v.strip() for v in values if v.strip()]
