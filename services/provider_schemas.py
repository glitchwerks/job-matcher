"""Provider schema building, config validation, runtime version introspection,
and API-key validation helpers for the Job Matcher web server.

All functions in this module are Flask-free and rely only on stdlib plus
project-local modules (``credentials``, ``providers``, ``ingest``,
``services.profile_store``).  They may be imported by Flask route handlers
as well as by CLI tools or tests without pulling in the Flask application.

Public API
----------
Runtime versions
    :func:`get_runtime_versions` — build the runtime component-version list.
    :data:`RUNTIME_VERSIONS` — module-level cache populated at import time.

Config warnings / validation
    :func:`_config_warnings` — human-readable Adzuna credential warnings.
    :func:`_get_search_validation_issues` — structured search-config issues.

Credential masking
    :func:`_mask_config_keys` — deep-copy a config dict with secrets redacted.

Settings UI helpers
    :func:`_build_llm_schemas` — ordered LLM provider schema list for the
    settings template.
    :func:`_load_providers_safe` — load ``providers.json`` with safe defaults.

Key validation
    :data:`_VALIDATE_TIMEOUT_SECONDS` — per-provider validation timeout.
    :func:`_validate_with_timeout` — run a provider validator with a deadline.
"""

from __future__ import annotations

import copy
import logging
import os
import subprocess
import sys
import threading
from functools import lru_cache
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as pkg_version
from typing import Any, Callable, Optional

from credentials import CredentialError, load_providers
from ingest import ValidationIssue, validate_search_config
from providers import _PROVIDER_CLASS_MAP
from providers.base import _sanitise_detail
from services.profile_store import (
    _CONFIG_PATH,
    _KEYS_PATH,
    _PROVIDERS_PATH,
    load_config,
)

_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Runtime version introspection
# ---------------------------------------------------------------------------


def get_runtime_versions() -> list[dict[str, str]]:
    """Return a list of ``{component, version}`` dicts for key runtime deps.

    Called once at startup and cached in :data:`RUNTIME_VERSIONS`.  Each
    package lookup is wrapped in a try/except so a missing optional package
    (e.g. ``gunicorn``) never crashes the server — it surfaces as ``'n/a'``.

    App version resolution order:

    1. ``VERSION`` file in the same directory as this module.
    2. Latest git tag via ``git describe --tags --abbrev=0``.
    3. Fallback string ``"dev"``.

    Returns:
        List of dicts, each with ``"component"`` and ``"version"`` keys.
    """
    def _pkg(name: str) -> str:
        try:
            return pkg_version(name)
        except PackageNotFoundError:
            return "n/a"

    # Python version — compact x.y.z form.
    python_ver = (
        f"{sys.version_info.major}"
        f".{sys.version_info.minor}"
        f".{sys.version_info.micro}"
    )

    # App version: VERSION file → git tag → "dev".
    version_file = os.path.join(os.path.dirname(__file__), "..", "VERSION")
    if os.path.exists(version_file):
        with open(version_file, "r", encoding="utf-8") as fh:
            app_ver = fh.read().strip() or "dev"
    else:
        try:
            result = subprocess.run(
                ["git", "describe", "--tags", "--abbrev=0"],
                capture_output=True,
                text=True,
                cwd=os.path.dirname(__file__) or ".",
            )
            app_ver = (
                result.stdout.strip() if result.returncode == 0 else "dev"
            )
        except OSError:
            app_ver = "dev"

    return [
        {"component": "App",            "version": app_ver},
        {"component": "Python",         "version": python_ver},
        {"component": "Flask",          "version": _pkg("flask")},
        {"component": "anthropic",      "version": _pkg("anthropic")},
        {"component": "beautifulsoup4", "version": _pkg("beautifulsoup4")},
        {"component": "waitress",       "version": _pkg("waitress")},
    ]


RUNTIME_VERSIONS: list[dict[str, str]] = get_runtime_versions()
"""Module-level cache of runtime component versions, populated at import time.

Re-read on every import to reflect the environment at startup.  Not updated
at runtime — a server restart is required to pick up package upgrades.
"""


# ---------------------------------------------------------------------------
# Config warnings
# ---------------------------------------------------------------------------


def _config_warnings(
    providers_path: Optional[str] = None,
) -> list[str]:
    """Return human-readable warnings for missing/empty configuration.

    Adzuna credentials are read from ``providers.json`` (via
    :func:`credentials.load_providers`), consistent with how
    ``make_enabled_sources`` resolves them.  A warning is shown only when
    Adzuna is explicitly enabled (``enabled: true``) but its credentials are
    missing.  Env-var overrides (``ADZUNA_APP_ID`` / ``ADZUNA_APP_KEY``) are
    also honoured.

    Args:
        providers_path: Override path to ``providers.json``.  Defaults to
            :data:`services.profile_store._PROVIDERS_PATH`.

    Returns:
        List of human-readable warning strings (may contain HTML).
        Empty list when there are no warnings.
    """
    if providers_path is None:
        providers_path = _PROVIDERS_PATH
    warnings: list[str] = []
    try:
        providers = load_providers(providers_path=providers_path)
    except CredentialError:
        providers = {}

    adzuna_src: dict = (
        (providers.get("job_sources") or {}).get("adzuna") or {}
    )

    # Only warn when Adzuna is explicitly enabled but credentials are absent.
    adzuna_explicitly_enabled: bool = adzuna_src.get("enabled", False)
    if not adzuna_explicitly_enabled:
        return warnings

    adzuna_id = str(adzuna_src.get("app_id", "") or "").strip()
    adzuna_key = str(adzuna_src.get("app_key", "") or "").strip()
    # Honour containerised / CI env-var overrides.
    if not adzuna_id:
        adzuna_id = os.environ.get("ADZUNA_APP_ID", "").strip()
    if not adzuna_key:
        adzuna_key = os.environ.get("ADZUNA_APP_KEY", "").strip()
    if not adzuna_id or not adzuna_key:
        warnings.append(
            "Adzuna is enabled but credentials are not configured — it will "
            "be skipped. Add your App ID and App Key on the "
            '<a href="/settings">Settings page</a>.'
        )
    return warnings


# ---------------------------------------------------------------------------
# Search validation
# ---------------------------------------------------------------------------


def _mtime(path: str) -> float:
    """Return the modification time of *path*, or ``0.0`` if it is absent.

    Used as part of the cache key for :func:`_cached_validation`.  Returning
    ``0.0`` for a missing file mirrors the existing graceful-fallback behaviour
    in :func:`_get_search_validation_issues` — no extra error is raised.

    Args:
        path: Filesystem path to stat.

    Returns:
        ``os.path.getmtime(path)`` when the file exists, ``0.0`` otherwise.
    """
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


@lru_cache(maxsize=1)
def _cached_validation(
    mtime_tuple: tuple[float, float],
    providers_path: str,
    config_path: str,
) -> list[ValidationIssue]:
    """Load config files from disk and return validation issues.

    This function is wrapped with :func:`functools.lru_cache` with
    ``maxsize=1``.  There is effectively a single config/providers path-pair
    in normal operation, so only the *current* mtime entry is needed.  A
    bound of 1 ensures that each config edit evicts the previous entry rather
    than accumulating an unbounded number of stale mtime entries over the
    lifetime of a long-running Gunicorn worker.

    The cache key is *(mtime_tuple, providers_path, config_path)* so any
    out-of-process modification to either config file (including a
    ``/settings`` POST rewrite) automatically busts the cache via the changed
    mtime.

    Callers should **not** call this function directly; use
    :func:`_get_search_validation_issues` instead.

    Args:
        mtime_tuple: 2-tuple of ``(providers_mtime, config_mtime)`` floats
            used purely as a cache-busting key.  ``0.0`` encodes a missing
            file (see :func:`_mtime`).
        providers_path: Absolute path to ``providers.json``.
        config_path: Absolute path to ``config.json``.

    Returns:
        List of :class:`ingest.ValidationIssue` objects.  Empty when all
        enabled sources have complete search configuration or when the config
        files cannot be loaded.
    """
    try:
        providers = load_providers(providers_path=providers_path)
    except CredentialError:
        providers = {}

    try:
        config = load_config(config_path)
    except SystemExit as exc:
        # config.json missing or malformed — treat as "no issues" so the
        # /settings page can still render and the user can fix the file.
        _logger.warning(
            "Could not load config for search validation: %s", exc
        )
        return []

    return validate_search_config(config, providers)


def _get_search_validation_issues(
    providers_path: Optional[str] = None,
    config_path: Optional[str] = None,
) -> list[ValidationIssue]:
    """Return search-config validation issues for all enabled sources.

    Results are mtime-keyed: repeated calls with unchanged config files are
    served from an :func:`functools.lru_cache` in-memory cache.  Any
    out-of-process modification to either file (including a ``/settings``
    POST rewrite) busts the cache automatically via the changed mtime.

    Delegates to :func:`_cached_validation` for the actual disk reads and
    validation logic.  Used by both the ``/settings`` GET render and the
    ``/api/ingest/preflight`` endpoint so the same validation logic is never
    duplicated.

    Any unexpected exception from the underlying validation (e.g. a
    :class:`filelock.Timeout` when :func:`credentials.migrate_from_legacy`
    tries to acquire a write lock on the providers path) is caught here and
    logged as a warning.  This prevents a transient I/O error from turning
    into a ``GET /settings`` HTTP 500.

    Args:
        providers_path: Override path to ``providers.json``.  Defaults to
            :data:`services.profile_store._PROVIDERS_PATH`.
        config_path: Override path to ``config.json``.  Defaults to
            :data:`services.profile_store._CONFIG_PATH`.

    Returns:
        List of :class:`ingest.ValidationIssue` objects.  Empty when all
        enabled sources have complete search configuration.
    """
    if providers_path is None:
        providers_path = _PROVIDERS_PATH
    if config_path is None:
        config_path = _CONFIG_PATH
    mts = (_mtime(providers_path), _mtime(config_path))
    try:
        return _cached_validation(mts, providers_path, config_path)
    except Exception as exc:
        _logger.warning(
            "Search-config validation failed unexpectedly; "
            "returning empty issue list so /settings can still render. "
            "Error: %s",
            exc,
        )
        return []


# ---------------------------------------------------------------------------
# Credential masking
# ---------------------------------------------------------------------------


def _mask_config_keys(data: dict) -> dict:
    """Return a deep copy of *data* with sensitive key values replaced by ``'***'``.

    Any key whose name (lower-cased) ends in ``_api_key``, ``_app_key``, or
    ``_app_id`` is considered sensitive.  The walk is recursive so nested
    dicts (e.g. ``search`` or ``prefilter`` sub-objects) are handled too.

    The original dict is never mutated — callers always receive a fresh copy.
    This is display-only: the masked value is never written back to disk.

    Args:
        data: Arbitrarily nested dict to redact.

    Returns:
        Deep copy of *data* with sensitive values replaced by ``'***'``.
    """
    _SENSITIVE_SUFFIXES = ("_api_key", "_app_key", "_app_id")

    def _walk(obj: Any) -> Any:
        if isinstance(obj, dict):
            result: dict = {}
            for k, v in obj.items():
                if (
                    isinstance(k, str)
                    and k.lower().endswith(_SENSITIVE_SUFFIXES)
                ):
                    result[k] = "***"
                else:
                    result[k] = _walk(v)
            return result
        if isinstance(obj, list):
            return [_walk(item) for item in obj]
        return copy.deepcopy(obj)

    return _walk(data)


# ---------------------------------------------------------------------------
# Settings UI helpers
# ---------------------------------------------------------------------------


def _build_llm_schemas(
    llm_section: dict,
    provider_order: list[str],
) -> list[tuple[str, dict, bool, dict, set]]:
    """Build the ordered LLM schema list for the settings template.

    Returns a list of
    ``(provider_key, schema_dict, has_values, current_values,
    populated_fields)`` tuples.  Providers in *provider_order* come first
    (unknown/duplicate keys are skipped), followed by any registry providers
    not listed, in registry insertion order.

    ``has_values`` is ``True`` only when every required field in the schema
    has a non-blank stored value.  Checking all required fields (not just
    ``api_key``) prevents a provider with a key but an empty model string
    from falsely showing "● configured".

    ``current_values`` maps non-password field names to their stored value
    (or the field's ``default`` if not yet stored).  This dict is passed to
    the template so that non-password inputs can be pre-populated.

    ``populated_fields`` is a set of field names that have a non-empty stored
    value.  The template uses this to conditionally render the Clear button
    next to password fields.

    Args:
        llm_section:    The ``"llm"`` sub-dict from ``providers.json``.
        provider_order: The ``provider_order`` list from ``providers.json``.

    Returns:
        Ordered list of 5-tuples as described above.
    """
    seen: set[str] = set()
    schemas: list[tuple[str, dict, bool, dict, set]] = []

    def _make_entry(
        key: str,
    ) -> tuple[str, dict, bool, dict, set]:
        cls = _PROVIDER_CLASS_MAP[key]
        schema = cls.settings_schema()
        cfg = llm_section.get(key) or {}
        has_values = all(
            bool(cfg.get(f["name"], "").strip())
            for f in schema["fields"]
            if f.get("required")
        )
        current_values = {
            f["name"]: cfg.get(f["name"]) or f.get("default") or ""
            for f in schema["fields"]
            if f.get("type") != "password"
        }
        populated_fields = {
            f["name"]
            for f in schema["fields"]
            if bool(cfg.get(f["name"], "").strip())
        }
        return (key, schema, has_values, current_values, populated_fields)

    for key in provider_order:
        if key in _PROVIDER_CLASS_MAP and key not in seen:
            schemas.append(_make_entry(key))
            seen.add(key)
    for key in _PROVIDER_CLASS_MAP:
        if key not in seen:
            schemas.append(_make_entry(key))
            seen.add(key)
    return schemas


def _load_providers_safe(
    providers_path: Optional[str] = None,
    keys_path: Optional[str] = None,
    config_path: Optional[str] = None,
) -> dict:
    """Load ``providers.json`` and return a parsed dict with safe defaults.

    Uses :data:`services.profile_store._PROVIDERS_PATH` as the primary
    credential store.  Falls back to migration from ``_KEYS_PATH`` /
    ``_CONFIG_PATH`` when ``providers.json`` is absent.  Returns safe empty
    defaults when :exc:`credentials.CredentialError` is raised so the
    settings UI always renders, even before any credentials are configured.

    Args:
        providers_path: Override path to ``providers.json``.  Defaults to
            :data:`services.profile_store._PROVIDERS_PATH`.
        keys_path: Override path to the legacy ``keys.json``.  Defaults to
            :data:`services.profile_store._KEYS_PATH`.
        config_path: Override path to ``config.json``.  Defaults to
            :data:`services.profile_store._CONFIG_PATH`.

    Returns:
        ``providers.json``-shaped dict with ``provider_order``, ``llm``, and
        ``job_sources`` keys guaranteed to be present.
    """
    if providers_path is None:
        providers_path = _PROVIDERS_PATH
    if keys_path is None:
        keys_path = _KEYS_PATH
    if config_path is None:
        config_path = _CONFIG_PATH
    try:
        data: dict = load_providers(
            providers_path=providers_path,
            keys_path=keys_path,
            config_path=config_path,
        )
    except CredentialError:
        data = {}

    data.setdefault("provider_order", [])
    data.setdefault("llm", {})
    data.setdefault("job_sources", {})
    return data


# ---------------------------------------------------------------------------
# Key validation
# ---------------------------------------------------------------------------


_VALIDATE_TIMEOUT_SECONDS: int = 5
"""Per-provider timeout (seconds) for API key validation calls."""


def _validate_with_timeout(
    validator: Callable[[str, str], tuple[str, str | None]],
    api_key: str,
    model: str,
) -> tuple[str, str | None]:
    """Run *validator(api_key, model)* in a daemon thread with a fixed timeout.

    Returns the validator's ``(state, detail)`` tuple, or a synthetic
    ``('unreachable', ...)`` tuple if the call does not complete within
    :data:`_VALIDATE_TIMEOUT_SECONDS`.

    Args:
        validator: Callable ``(api_key, model) -> tuple[str, str | None]``.
        api_key:   Provider API key string.
        model:     Provider model name string.

    Returns:
        ``(state, detail)`` where *state* is one of: ``'valid'``,
        ``'invalid_key'``, ``'unknown_model'``, ``'unreachable'``.
        *detail* is ``None`` on success or a short error string on failure.
    """
    result_holder: list[tuple[str, str | None]] = []

    def _target() -> None:
        try:
            result_holder.append(validator(api_key, model))
        except Exception as exc:
            result_holder.append(
                ("unreachable", _sanitise_detail(str(exc), api_key))
            )

    t = threading.Thread(target=_target, daemon=True)
    t.start()
    t.join(timeout=_VALIDATE_TIMEOUT_SECONDS)
    if t.is_alive():
        return (
            "unreachable",
            f"Timed out after {_VALIDATE_TIMEOUT_SECONDS}s",
        )
    return result_holder[0] if result_holder else ("unreachable", None)
