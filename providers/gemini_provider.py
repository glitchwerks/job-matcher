"""
providers/gemini_provider.py — Google Gemini backend for LLM scoring.

Wraps the ``google-genai`` SDK.  Uses the same JSON contract and
retry pattern as the other providers so that ``score_listing()`` in
``ingest.py`` is provider-agnostic.
"""

from __future__ import annotations

import logging
import time

from google import genai

from .base import LLMProvider
from .anthropic_provider import _parse_json_response

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Pricing table (USD per million tokens, as of 2025-03)
# ---------------------------------------------------------------------------

_GEMINI_PRICING: dict[str, tuple[float, float]] = {
    # model_id: (input_cost_per_mtok, output_cost_per_mtok)
    "gemini-1.5-flash": (0.075,  0.30),
    "gemini-1.5-pro":   (3.50,  10.50),
}

_FALLBACK_INPUT  = 0.075   # flash pricing as safe default
_FALLBACK_OUTPUT = 0.30


def _pricing_for_model(model: str) -> tuple[float, float]:
    """Return ``(input_cost_per_mtok, output_cost_per_mtok)`` for *model*.

    Performs an exact-match lookup and falls back to flash pricing for
    unrecognised model names.

    Args:
        model: Gemini model ID string.

    Returns:
        Tuple of (input cost, output cost) per million tokens.
    """
    if model in _GEMINI_PRICING:
        return _GEMINI_PRICING[model]
    logger.warning(
        "Unknown Gemini model %r — falling back to gemini-1.5-flash pricing", model
    )
    return _FALLBACK_INPUT, _FALLBACK_OUTPUT


class GeminiProvider(LLMProvider):
    """LLM provider backed by the Google Gemini GenerativeAI API.

    Args:
        api_key: Google API key.
        model:   Model ID (e.g. ``"gemini-1.5-flash"`` or ``"gemini-1.5-pro"``).
    """

    def __init__(self, api_key: str, model: str) -> None:
        self._client = genai.Client(api_key=api_key)
        self._model_name = model
        self._input_cost, self._output_cost = _pricing_for_model(model)

    # ------------------------------------------------------------------
    # LLMProvider interface
    # ------------------------------------------------------------------

    @property
    def input_cost_per_mtok(self) -> float:
        """USD cost per million input tokens for the configured model."""
        return self._input_cost

    @property
    def output_cost_per_mtok(self) -> float:
        """USD cost per million output tokens for the configured model."""
        return self._output_cost

    def complete(self, prompt: str) -> dict:
        """Call the Gemini GenerativeAI API and return a parsed scoring dict.

        Retries once on any exception (2-second delay).  Gemini raises a
        variety of error types depending on the failure mode, so we catch
        ``Exception`` broadly.  Raises ``RuntimeError`` if both attempts fail.

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
                response = self._client.models.generate_content(
                    model=self._model_name,
                    contents=prompt,
                )
            except Exception as exc:  # noqa: BLE001 — Gemini raises varied errors
                logger.warning(
                    "Gemini API error (attempt %d/2): %s", attempt + 1, exc
                )
                continue

            # Extract text from the first candidate.
            try:
                raw_content = response.text
            except (AttributeError, ValueError) as exc:
                logger.warning(
                    "Unexpected Gemini response structure (attempt %d/2): %s",
                    attempt + 1,
                    exc,
                )
                continue

            parsed = _parse_json_response(raw_content, attempt)
            if parsed is None:
                continue

            # Attach token usage from usage_metadata.
            try:
                parsed["tokens_input"]  = response.usage_metadata.prompt_token_count
                parsed["tokens_output"] = response.usage_metadata.candidates_token_count
            except AttributeError:
                parsed["tokens_input"]  = None
                parsed["tokens_output"] = None

            return parsed

        raise RuntimeError("Gemini scoring failed after 2 attempts")
