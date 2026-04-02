"""
credentials.py — Unified credential loading for Job Matcher.

Single shared module imported by both ingest.py and app.py.

Public API
----------
* ``CredentialError``       — raised when usable credentials cannot be found
* ``load_providers()``      — read providers.json (or migrate / fall back to env vars)
* ``migrate_from_legacy()`` — atomic migration from keys.json + config.json

Credential precedence
---------------------
1. ``providers.json`` (present + parseable) → use it; env vars are NOT consulted.
2. ``providers.json`` absent → attempt :func:`migrate_from_legacy`.
3. Migration returns data → return it (also wrote providers.json for next run).
4. Migration returns None → build from env vars.
5. No env vars → raise ``CredentialError``.

Note: a present ``providers.json`` with empty credential values is "configured but
empty".  Env vars do NOT override — this is intentional and documented in
``providers.example.json``.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Default models used when constructing a providers dict from env vars.
# ---------------------------------------------------------------------------

_ENV_LLM_DEFAULTS: tuple[tuple[str, str, str], ...] = (
    ("ANTHROPIC_API_KEY", "anthropic", "claude-haiku-4-5-20251001"),
    ("OPENAI_API_KEY",    "openai",    "gpt-4o-mini"),
    ("GOOGLE_API_KEY",    "gemini",    "gemini-1.5-flash"),
)

_CONFIG_DIR = os.path.join(os.path.dirname(__file__), "config")
_DEFAULT_CONFIG_PATH = os.path.join(_CONFIG_DIR, "config.json")
_DEFAULT_KEYS_PATH = os.path.join(_CONFIG_DIR, "keys.json")
_DEFAULT_PROVIDERS_PATH = os.path.join(_CONFIG_DIR, "providers.json")


# ===========================================================================
# Public exception
# ===========================================================================

class CredentialError(Exception):
    """Raised when usable credentials cannot be obtained.

    Callers decide how to handle this:
    - ``ingest.py`` → log a message and call ``sys.exit(1)``
    - ``app.py``    → return safe empty-defaults so the settings UI still renders
    """


# ===========================================================================
# migrate_from_legacy()
# ===========================================================================

def migrate_from_legacy(
    providers_path: str = _DEFAULT_PROVIDERS_PATH,
    keys_path: str = _DEFAULT_KEYS_PATH,
    config_path: str = _DEFAULT_CONFIG_PATH,
) -> Optional[dict]:
    """Attempt to build a ``providers.json``-shaped dict from legacy credential files.

    Handles all four migration cases:

    +----------------------------+----------------------------+-----------------------------------+
    | keys.json present          | config.json present        | Behaviour                         |
    +============================+============================+===================================+
    | Yes                        | Yes (with Adzuna keys)     | Migrate both; write providers.json|
    | Yes                        | No / no Adzuna keys        | Migrate LLM only; Adzuna = ""     |
    | No                         | Yes (with Adzuna keys)     | Adzuna migrates; LLM = empty      |
    | No                         | No                         | Return None (no file written)     |
    +----------------------------+----------------------------+-----------------------------------+

    Migration is **atomic**: the data is written to ``providers.json.tmp`` first,
    then renamed with ``os.replace()``.  If the write fails, the temp file is
    cleaned up and no partial output is left on disk.  The original ``keys.json``
    and ``config.json`` are never modified.

    Args:
        providers_path: Destination path for the migrated ``providers.json``.
        keys_path:      Path to the legacy ``keys.json`` (may be absent).
        config_path:    Path to the legacy ``config.json`` (may be absent).

    Returns:
        The migrated providers dict on success, or ``None`` when neither source
        file is present (triggering env-var fallback in the caller).
    """
    keys_data: Optional[dict] = None
    config_data: Optional[dict] = None

    # --- Read keys.json if present ---
    if os.path.exists(keys_path):
        try:
            with open(keys_path, encoding="utf-8") as fh:
                keys_data = json.load(fh)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Could not read %s during migration: %s", keys_path, exc)
            keys_data = None

    # --- Read config.json if present ---
    if os.path.exists(config_path):
        try:
            with open(config_path, encoding="utf-8") as fh:
                config_data = json.load(fh)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Could not read %s during migration: %s", config_path, exc)
            config_data = None

    # Case 4: neither file — no migration possible
    if keys_data is None and config_data is None:
        return None

    # ---------------------------------------------------------------------------
    # Build LLM section
    # ---------------------------------------------------------------------------
    llm_section: dict = {}
    provider_order: list[str] = []

    if keys_data is not None:
        raw_providers: dict = keys_data.get("providers") or {}
        preferred: str = keys_data.get("preferred_provider", "") or ""

        # Build provider_order: preferred first, then remaining in insertion order.
        if preferred and preferred in raw_providers:
            provider_order.append(preferred)
        for name in raw_providers:
            if name not in provider_order:
                provider_order.append(name)

        for name, cfg in raw_providers.items():
            llm_section[name] = {
                "api_key": cfg.get("api_key", ""),
                "model":   cfg.get("model", ""),
            }

    # ---------------------------------------------------------------------------
    # Build job_sources section
    # ---------------------------------------------------------------------------
    adzuna_app_id  = ""
    adzuna_app_key = ""

    if config_data is not None:
        adzuna_app_id  = config_data.get("adzuna_app_id", "")  or ""
        adzuna_app_key = config_data.get("adzuna_app_key", "") or ""

    providers_dict: dict = {
        "provider_order": provider_order,
        "llm": llm_section,
        "job_sources": {
            "adzuna": {
                "app_id":  adzuna_app_id,
                "app_key": adzuna_app_key,
            }
        },
    }

    # ---------------------------------------------------------------------------
    # Atomic write
    # ---------------------------------------------------------------------------
    tmp_path = providers_path + ".tmp"
    _write_ok = False
    try:
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(providers_dict, fh, indent=2)
        os.replace(tmp_path, providers_path)
        _write_ok = True
        logger.info(
            "Migrated legacy credentials to %s. "
            "keys.json and config.json have not been modified.",
            os.path.abspath(providers_path),
        )
    finally:
        # If the write or rename failed, clean up the temp file so the next
        # run does not find a partial artifact. If _write_ok is True the
        # rename already consumed the temp file, so remove() would no-op anyway.
        if not _write_ok:
            try:
                os.remove(tmp_path)
            except OSError:
                pass

    return providers_dict


# ===========================================================================
# load_providers()
# ===========================================================================

def load_providers(
    providers_path: str = _DEFAULT_PROVIDERS_PATH,
    keys_path: str = _DEFAULT_KEYS_PATH,
    config_path: str = _DEFAULT_CONFIG_PATH,
) -> dict:
    """Load the unified credential store and return it as a dict.

    Precedence
    ----------
    1. ``providers.json`` present and parseable → return it directly.
       **Env vars are NOT consulted** even if the file has empty values.
    2. ``providers.json`` absent → call :func:`migrate_from_legacy`.
       - If migration produces data, return it.
       - Otherwise, fall back to env vars.
    3. No usable credentials anywhere → raise :exc:`CredentialError`.

    Args:
        providers_path: Path to ``providers.json`` (overrideable for tests).
        keys_path:      Path to legacy ``keys.json`` (passed to migration).
        config_path:    Path to legacy ``config.json`` (passed to migration).

    Returns:
        Dict shaped as::

            {
                "provider_order": ["anthropic", ...],
                "llm": {
                    "anthropic": {"api_key": "...", "model": "..."},
                    ...
                },
                "job_sources": {
                    "adzuna": {"app_id": "...", "app_key": "..."}
                }
            }

    Raises:
        CredentialError: When no usable credentials can be found.  The caller
            decides whether to call ``sys.exit(1)`` or return empty defaults.
    """
    # --- Path 1: providers.json present ---
    if os.path.exists(providers_path):
        try:
            with open(providers_path, encoding="utf-8") as fh:
                data = json.load(fh)
        except json.JSONDecodeError as exc:
            raise CredentialError(
                f"providers.json contains invalid JSON and cannot be parsed: {exc}"
            ) from exc
        except OSError as exc:
            raise CredentialError(
                f"Could not read providers.json: {exc}"
            ) from exc
        # Env vars are NOT consulted when the file is present.
        return data

    # --- Path 2: providers.json absent — try migration ---
    try:
        migrated = migrate_from_legacy(
            providers_path=providers_path,
            keys_path=keys_path,
            config_path=config_path,
        )
    except OSError as exc:
        # Migration write failed; log and fall through to env vars.
        logger.warning("Migration failed (%s); falling back to env vars.", exc)
        migrated = None

    if migrated is not None:
        return migrated

    # --- Path 3: env var fallback ---
    llm_section: dict = {}
    provider_order: list[str] = []

    for env_var, provider_name, default_model in _ENV_LLM_DEFAULTS:
        api_key = os.environ.get(env_var, "") or ""
        if api_key:
            llm_section[provider_name] = {"api_key": api_key, "model": default_model}
            provider_order.append(provider_name)

    adzuna_app_id  = os.environ.get("ADZUNA_APP_ID",  "") or ""
    adzuna_app_key = os.environ.get("ADZUNA_APP_KEY", "") or ""

    # Require at least one LLM key OR one Adzuna credential to proceed.
    has_llm    = bool(llm_section)
    has_adzuna = bool(adzuna_app_id or adzuna_app_key)

    if not has_llm and not has_adzuna:
        raise CredentialError(
            "No credentials found. Either:\n"
            "  1. Copy providers.example.json to providers.json and fill in your keys, or\n"
            "  2. Set at least one of ANTHROPIC_API_KEY, OPENAI_API_KEY, GOOGLE_API_KEY, "
            "ADZUNA_APP_ID, ADZUNA_APP_KEY as an environment variable."
        )

    return {
        "provider_order": provider_order,
        "llm": llm_section,
        "job_sources": {
            "adzuna": {
                "app_id":  adzuna_app_id,
                "app_key": adzuna_app_key,
            }
        },
    }


# ===========================================================================
# save_providers()
# ===========================================================================

def save_providers(
    updates: dict,
    providers_path: str = _DEFAULT_PROVIDERS_PATH,
) -> None:
    """Deep-merge *updates* into ``providers.json`` and write atomically.

    All values present in *updates* are applied, including blank strings
    (``""``).  Submitting a blank string for a credential field clears the
    stored value, allowing users to remove credentials via the UI.  Only keys
    that are absent from *updates* entirely are left unchanged.

    The write is atomic: data is written to ``providers.json.tmp`` first,
    then renamed with ``os.replace()``.  If the write fails the temp file
    is cleaned up and the original ``providers.json`` is left unchanged.

    Args:
        updates:        Nested dict shaped like ``providers.json``::

                            {
                                "llm": {
                                    "anthropic": {"api_key": "new-key"},
                                },
                                "job_sources": {
                                    "adzuna": {"app_id": "new-id"},
                                },
                            }

                        Only the keys present in *updates* are touched;
                        everything else in the existing file is preserved.
                        Pass ``""`` for a field to clear it.

        providers_path: Path to ``providers.json``; created from scratch
                        when absent.

    Raises:
        OSError: If the file cannot be written (permissions, disk full, …).
    """
    # --- Load existing data (start from empty skeleton when absent) ---
    if os.path.exists(providers_path):
        try:
            with open(providers_path, encoding="utf-8") as fh:
                existing: dict = json.load(fh)
        except (json.JSONDecodeError, OSError):
            existing = {}
    else:
        existing = {}

    # Ensure top-level sections exist.
    existing.setdefault("provider_order", [])
    existing.setdefault("llm", {})
    existing.setdefault("job_sources", {})

    def _deep_merge(base: dict, patch: dict) -> None:
        """Recursively apply *patch* values into *base* in-place.

        All values are applied as-is, including blank strings so that
        credential fields can be cleared via the UI.  Nested dicts are
        merged recursively; only keys absent from *patch* are left unchanged.
        """
        for key, value in patch.items():
            if isinstance(value, dict):
                base.setdefault(key, {})
                _deep_merge(base[key], value)
            else:
                base[key] = value

    _deep_merge(existing, updates)

    # --- Atomic write ---
    tmp_path = providers_path + ".tmp"
    _write_ok = False
    try:
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(existing, fh, indent=2)
        os.replace(tmp_path, providers_path)
        _write_ok = True
    finally:
        if not _write_ok:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
