"""
providers/ — Pluggable LLM provider package for Job Matcher.

Public API
----------
* ``LLMProvider``          — abstract base class; import from here or ``providers.base``
* ``AnthropicProvider``    — Anthropic Claude backend (default)
* ``OpenAIProvider``       — OpenAI Chat Completions backend
* ``GeminiProvider``       — Google Gemini GenerativeAI backend
* ``make_provider()``      — factory that reads ``config`` and returns the right provider
* ``build_provider_chain`` — build an ordered fallback list from a parsed keys.json dict

Usage
-----
    from providers import make_provider, build_provider_chain

    provider = make_provider(config)              # reads scoring.provider from config
    result   = provider.complete(prompt)          # returns scored dict
    cost_usd = tokens / 1e6 * provider.input_cost_per_mtok

    chain = build_provider_chain(keys)            # ordered list from keys.json
"""

from __future__ import annotations

import os

from .base import LLMProvider
from .anthropic_provider import AnthropicProvider
from .openai_provider import OpenAIProvider
from .gemini_provider import GeminiProvider

__all__ = [
    "LLMProvider",
    "AnthropicProvider",
    "OpenAIProvider",
    "GeminiProvider",
    "make_provider",
    "build_provider_chain",
]


def make_provider(config: dict) -> LLMProvider:
    """Instantiate and return the correct ``LLMProvider`` for *config*.

    Reads ``config["scoring"]["provider"]`` (default: ``"anthropic"``) and
    ``config["scoring"]["model"]``, then resolves the API key from the config
    dict first and the corresponding environment variable second.

    Supported provider names and their key sources:

    +-------------+-----------------------------+----------------------------+
    | provider    | config key                  | env var                    |
    +=============+=============================+============================+
    | anthropic   | ``anthropic_api_key``       | ``ANTHROPIC_API_KEY``      |
    | openai      | ``openai_api_key``          | ``OPENAI_API_KEY``         |
    | gemini      | ``google_api_key``          | ``GOOGLE_API_KEY``         |
    +-------------+-----------------------------+----------------------------+

    Args:
        config: Full config dict as returned by ``ingest.load_config()``.

    Returns:
        An initialised ``LLMProvider`` instance.

    Raises:
        ValueError: If ``provider`` names an unsupported backend.
    """
    scoring = config.get("scoring", {})
    provider_name: str = scoring.get("provider", "anthropic")
    model: str = scoring["model"]

    if provider_name == "anthropic":
        api_key = config.get("anthropic_api_key") or os.environ.get("ANTHROPIC_API_KEY", "")
        return AnthropicProvider(api_key=api_key, model=model)

    if provider_name == "openai":
        api_key = config.get("openai_api_key") or os.environ.get("OPENAI_API_KEY", "")
        return OpenAIProvider(api_key=api_key, model=model)

    if provider_name == "gemini":
        api_key = config.get("google_api_key") or os.environ.get("GOOGLE_API_KEY", "")
        return GeminiProvider(api_key=api_key, model=model)

    raise ValueError(
        f"Unknown provider: {provider_name!r}. "
        "Supported values: 'anthropic', 'openai', 'gemini'."
    )


# ---------------------------------------------------------------------------
# Provider name → class mapping used by build_provider_chain
# ---------------------------------------------------------------------------

_PROVIDER_CLASS_MAP: dict[str, type[LLMProvider]] = {
    "anthropic": AnthropicProvider,
    "openai":    OpenAIProvider,
    "gemini":    GeminiProvider,
}


def build_provider_chain(keys: dict) -> list[LLMProvider]:
    """Return an ordered list of initialised ``LLMProvider`` instances.

    Reads ``keys["providers"]`` and ``keys.get("preferred_provider")`` to
    determine ordering:

    1. ``preferred_provider`` goes first if it has a non-empty ``api_key``.
    2. The remaining providers follow in dict insertion order, skipping any
       with an empty ``api_key``.
    3. If ``preferred_provider`` is absent or empty the full list is used in
       dict insertion order.

    Args:
        keys: Parsed contents of keys.json.

    Returns:
        List of ``LLMProvider`` instances in fallback order.  Providers with
        empty ``api_key`` values are silently skipped.

    Raises:
        ValueError: If ``keys["providers"]`` is missing or all providers have
            empty ``api_key`` values.
    """
    raw_providers: dict = keys.get("providers")
    if not raw_providers:
        raise ValueError("keys['providers'] is missing or empty.")

    preferred: str = keys.get("preferred_provider", "") or ""

    # Build a filtered list of (name, cfg) pairs that have a non-empty key.
    valid: list[tuple[str, dict]] = [
        (name, cfg)
        for name, cfg in raw_providers.items()
        if cfg.get("api_key", "")
    ]

    if not valid:
        raise ValueError(
            "No providers with a non-empty api_key found in keys['providers']."
        )

    # Reorder so that preferred_provider comes first, if it is among the valid ones.
    if preferred and any(name == preferred for name, _ in valid):
        ordered = [entry for entry in valid if entry[0] == preferred]
        ordered += [entry for entry in valid if entry[0] != preferred]
    else:
        ordered = valid

    chain: list[LLMProvider] = []
    for name, cfg in ordered:
        cls = _PROVIDER_CLASS_MAP.get(name)
        if cls is None:
            # Unknown provider names are silently skipped to allow forward
            # compatibility as new providers are added to keys.json before
            # they are implemented here.
            continue
        chain.append(cls(api_key=cfg["api_key"], model=cfg.get("model", "")))

    if not chain:
        raise ValueError(
            "No supported providers found in keys['providers']. "
            f"Supported names: {list(_PROVIDER_CLASS_MAP)}."
        )

    return chain
