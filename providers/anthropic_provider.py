"""
providers/anthropic_provider.py — Anthropic Claude backend for LLM scoring.

Wraps the ``anthropic`` SDK.  Retry logic, JSON parsing, and code-fence
stripping live here so that ``score_listing()`` in ``ingest.py`` is
provider-agnostic.
"""

from __future__ import annotations

import json
import logging
import time

import anthropic

from .base import LLMProvider

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Pricing table (USD per million tokens, as of 2025-03)
# Keys are prefix-matched against the model name so that dated variants
# (e.g. ``claude-haiku-4-5-20251001``) resolve correctly.
# ---------------------------------------------------------------------------

_ANTHROPIC_PRICING: list[tuple[str, float, float]] = [
    # (model_prefix, input_cost_per_mtok, output_cost_per_mtok)
    ("claude-opus-",   15.00, 75.00),
    ("claude-sonnet-",  3.00, 15.00),
    ("claude-haiku-",   0.80,  4.00),
]

_FALLBACK_INPUT  = 0.80   # Haiku pricing as safe default
_FALLBACK_OUTPUT = 4.00

_SCORE_KEYS = {"score", "matched_skills", "missing_skills", "concerns", "verdict"}


def _pricing_for_model(model: str) -> tuple[float, float]:
    """Return ``(input_cost_per_mtok, output_cost_per_mtok)`` for *model*.

    Walks ``_ANTHROPIC_PRICING`` in order and returns the first prefix match.
    Falls back to Haiku pricing for unrecognised model names.

    Args:
        model: Anthropic model ID string.

    Returns:
        Tuple of (input cost, output cost) per million tokens.
    """
    for prefix, inp, out in _ANTHROPIC_PRICING:
        if model.startswith(prefix):
            return inp, out
    logger.warning(
        "Unknown Anthropic model %r — falling back to Haiku pricing", model
    )
    return _FALLBACK_INPUT, _FALLBACK_OUTPUT


class AnthropicProvider(LLMProvider):
    """LLM provider backed by the Anthropic Messages API.

    Args:
        api_key: Anthropic API key.
        model:   Model ID (e.g. ``"claude-haiku-4-5-20251001"``).
    """

    def __init__(self, api_key: str, model: str) -> None:
        self._client = anthropic.Anthropic(api_key=api_key)
        self._model = model
        self._input_cost, self._output_cost = _pricing_for_model(model)

    # ------------------------------------------------------------------
    # LLMProvider interface
    # ------------------------------------------------------------------

    @classmethod
    def settings_schema(cls) -> dict:
        """Return the settings schema for Anthropic.

        Returns:
            Schema dict with ``display_name`` and ``fields`` for the
            Anthropic API key and model ID.
        """
        return {
            "display_name": "Anthropic",
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
                    "default": "claude-haiku-4-5-20251001",
                },
            ],
        }

    @classmethod
    def validate_credentials(cls, api_key: str, model: str) -> str:
        """Send a 1-token test call to Anthropic and return a state string.

        Returns one of: ``'valid'``, ``'invalid_key'``, ``'unknown_model'``,
        ``'unreachable'``.  The api_key is never logged or included in any
        return value.

        Args:
            api_key: Anthropic API key.
            model:   Anthropic model ID.

        Returns:
            State string describing the validation outcome.
        """
        try:
            client = anthropic.Anthropic(api_key=api_key)
            client.messages.create(
                model=model,
                max_tokens=1,
                messages=[{"role": "user", "content": "hi"}],
            )
            return "valid"
        except anthropic.AuthenticationError:
            return "invalid_key"
        except anthropic.PermissionDeniedError:
            return "invalid_key"
        except anthropic.NotFoundError:
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
        """Call the Anthropic Messages API and return a parsed scoring dict.

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
                message = self._client.messages.create(
                    model=self._model,
                    max_tokens=1024,
                    messages=[{"role": "user", "content": prompt}],
                )
            except anthropic.APIError as exc:
                logger.warning(
                    "Anthropic API error (attempt %d/2): %s", attempt + 1, exc
                )
                continue

            # Extract text content from the first content block.
            try:
                raw_content = message.content[0].text
            except (IndexError, AttributeError) as exc:
                logger.warning(
                    "Unexpected Anthropic response structure (attempt %d/2): %s",
                    attempt + 1,
                    exc,
                )
                continue

            parsed = _parse_json_response(raw_content, attempt)
            if parsed is None:
                continue

            # Attach token usage.
            try:
                parsed["tokens_input"]  = message.usage.input_tokens
                parsed["tokens_output"] = message.usage.output_tokens
            except AttributeError:
                parsed["tokens_input"]  = None
                parsed["tokens_output"] = None

            return parsed

        raise RuntimeError("Anthropic scoring failed after 2 attempts")


# ---------------------------------------------------------------------------
# Shared JSON parsing helper (used by all providers via import)
# ---------------------------------------------------------------------------

def strip_fences(raw: str) -> str:
    """Remove markdown code fences from *raw* if present.

    Strips a leading `` ```[lang] `` line and a trailing `` ``` `` line.
    Operates on the stripped version of *raw* so leading/trailing whitespace
    is handled automatically.

    Args:
        raw: Raw LLM response text.

    Returns:
        String with fence lines removed and surrounding whitespace stripped.
    """
    stripped = raw.strip()
    lines = stripped.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _parse_json_response(raw_content: str, attempt: int) -> dict | None:
    """Strip fences, parse JSON, and validate required keys.

    Logs a warning and returns ``None`` on any parse or validation failure
    so the caller can move on to the next retry.

    Args:
        raw_content: Raw text from the LLM response.
        attempt:     0-based attempt index (used for log messages).

    Returns:
        Validated dict on success, ``None`` on failure.
    """
    stripped = strip_fences(raw_content)

    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError as exc:
        logger.warning(
            "LLM returned non-JSON (attempt %d/2): %s — raw: %.200s",
            attempt + 1,
            exc,
            raw_content,
        )
        return None

    if not isinstance(parsed, dict) or not _SCORE_KEYS.issubset(parsed.keys()):
        missing_keys = _SCORE_KEYS - set(parsed.keys())
        logger.warning(
            "Score response missing keys %s (attempt %d/2)",
            missing_keys,
            attempt + 1,
        )
        return None

    return parsed
