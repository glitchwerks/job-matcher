"""
providers/openai_provider.py — OpenAI Chat Completions backend for LLM scoring.

Wraps the ``openai`` SDK.  Uses the same JSON contract and retry pattern as
``AnthropicProvider`` so that ``score_listing()`` in ``ingest.py`` is
provider-agnostic.
"""

from __future__ import annotations

import logging
import time

import openai

from .base import LLMProvider
from .anthropic_provider import _parse_json_response

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Pricing table (USD per million tokens, as of 2025-03)
# ---------------------------------------------------------------------------

_OPENAI_PRICING: dict[str, tuple[float, float]] = {
    # model_id: (input_cost_per_mtok, output_cost_per_mtok)
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o":      (2.50, 10.00),
}

_FALLBACK_INPUT  = 0.15   # gpt-4o-mini pricing as safe default
_FALLBACK_OUTPUT = 0.60


def _pricing_for_model(model: str) -> tuple[float, float]:
    """Return ``(input_cost_per_mtok, output_cost_per_mtok)`` for *model*.

    Performs an exact-match lookup first, then falls back to gpt-4o-mini
    pricing for unrecognised model names.

    Args:
        model: OpenAI model ID string.

    Returns:
        Tuple of (input cost, output cost) per million tokens.
    """
    if model in _OPENAI_PRICING:
        return _OPENAI_PRICING[model]
    logger.warning(
        "Unknown OpenAI model %r — falling back to gpt-4o-mini pricing", model
    )
    return _FALLBACK_INPUT, _FALLBACK_OUTPUT


class OpenAIProvider(LLMProvider):
    """LLM provider backed by the OpenAI Chat Completions API.

    Args:
        api_key: OpenAI API key.
        model:   Model ID (e.g. ``"gpt-4o-mini"`` or ``"gpt-4o"``).
    """

    def __init__(self, api_key: str, model: str) -> None:
        self._client = openai.OpenAI(api_key=api_key)
        self._model = model
        self._input_cost, self._output_cost = _pricing_for_model(model)

    # ------------------------------------------------------------------
    # LLMProvider interface
    # ------------------------------------------------------------------

    @classmethod
    def settings_schema(cls) -> dict:
        """Return the settings schema for OpenAI.

        Returns:
            Schema dict with ``display_name`` and ``fields`` for the
            OpenAI API key and model ID.
        """
        return {
            "display_name": "OpenAI",
            "fields": [
                {
                    "name": "api_key",
                    "label": "API Key",
                    "type": "password",
                    "required": True,
                },
                {
                    "name": "model",
                    "label": "Model ID",
                    "type": "text",
                    "required": True,
                    "default": "gpt-4o-mini",
                },
            ],
        }

    @classmethod
    def validate_credentials(cls, api_key: str, model: str) -> str:
        """Send a 1-token test call to OpenAI and return a state string.

        Returns one of: ``'valid'``, ``'invalid_key'``, ``'unknown_model'``,
        ``'unreachable'``.  The api_key is never logged or included in any
        return value.

        Args:
            api_key: OpenAI API key.
            model:   OpenAI model ID.

        Returns:
            State string describing the validation outcome.
        """
        try:
            client = openai.OpenAI(api_key=api_key)
            client.chat.completions.create(
                model=model,
                max_tokens=1,
                messages=[{"role": "user", "content": "hi"}],
            )
            return "valid"
        except openai.AuthenticationError:
            return "invalid_key"
        except openai.PermissionDeniedError:
            return "invalid_key"
        except openai.NotFoundError:
            return "unknown_model"
        except Exception:
            return "unreachable"

    @property
    def input_cost_per_mtok(self) -> float:
        """USD cost per million input tokens for the configured model."""
        return self._input_cost

    @property
    def output_cost_per_mtok(self) -> float:
        """USD cost per million output tokens for the configured model."""
        return self._output_cost

    def complete(self, prompt: str) -> dict:
        """Call the OpenAI Chat Completions API and return a parsed scoring dict.

        Retries once on API error or malformed JSON (2-second delay).
        Raises ``RuntimeError`` if both attempts fail.

        Args:
            prompt: Fully rendered prompt string.

        Returns:
            Parsed scoring dict with standard keys plus ``tokens_input`` /
            ``tokens_output``.

        Raises:
            RuntimeError: After two consecutive failures.
        """
        for attempt in range(2):
            if attempt > 0:
                time.sleep(2)

            try:
                response = self._client.chat.completions.create(
                    model=self._model,
                    max_tokens=1024,
                    messages=[{"role": "user", "content": prompt}],
                )
            except openai.APIError as exc:
                logger.warning(
                    "OpenAI API error (attempt %d/2): %s", attempt + 1, exc
                )
                continue

            # Extract text from the first choice.
            try:
                raw_content = response.choices[0].message.content or ""
            except (IndexError, AttributeError) as exc:
                logger.warning(
                    "Unexpected OpenAI response structure (attempt %d/2): %s",
                    attempt + 1,
                    exc,
                )
                continue

            parsed = _parse_json_response(raw_content, attempt)
            if parsed is None:
                continue

            # Attach token usage.
            try:
                parsed["tokens_input"]  = response.usage.prompt_tokens
                parsed["tokens_output"] = response.usage.completion_tokens
            except AttributeError:
                parsed["tokens_input"]  = None
                parsed["tokens_output"] = None

            return parsed

        raise RuntimeError("OpenAI scoring failed after 2 attempts")
