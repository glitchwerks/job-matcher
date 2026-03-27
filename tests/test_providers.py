"""
tests/test_providers.py — Unit tests for the providers/ package.

All external SDK calls are mocked so no real API keys or network access are
needed.  Covers:

  - AnthropicProvider.complete(): happy path, retry on API error, retry on
    bad JSON, failure after two attempts
  - OpenAIProvider.complete(): happy path, retry on API error
  - GeminiProvider.complete(): happy path, retry on exception
  - make_provider() factory: correct class selected per provider name,
    unknown provider raises ValueError
  - Code fence stripping (strip_fences) shared helper
  - Pricing helpers return correct values for known and unknown models
"""

from __future__ import annotations

import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from providers import make_provider, AnthropicProvider, OpenAIProvider, GeminiProvider
from providers.anthropic_provider import (
    strip_fences,
    _pricing_for_model as anthropic_pricing,
)
from providers.openai_provider import _pricing_for_model as openai_pricing
from providers.gemini_provider import _pricing_for_model as gemini_pricing


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_VALID_SCORE_DICT = {
    "score": 8,
    "matched_skills": ["Python", "AWS"],
    "missing_skills": ["Kubernetes"],
    "concerns": [],
    "verdict": "Strong match on backend skills.",
}


def _make_anthropic_message(content_text: str, input_tokens: int = 100, output_tokens: int = 50):
    """Build a minimal fake Anthropic message object."""
    content_block = SimpleNamespace(text=content_text)
    usage = SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens)
    return SimpleNamespace(content=[content_block], usage=usage)


def _make_openai_response(content_text: str, prompt_tokens: int = 100, completion_tokens: int = 50):
    """Build a minimal fake OpenAI chat completion object."""
    message = SimpleNamespace(content=content_text)
    choice = SimpleNamespace(message=message)
    usage = SimpleNamespace(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens)
    return SimpleNamespace(choices=[choice], usage=usage)


def _make_gemini_response(content_text: str, prompt_token_count: int = 100, candidates_token_count: int = 50):
    """Build a minimal fake Gemini response object."""
    usage_metadata = SimpleNamespace(
        prompt_token_count=prompt_token_count,
        candidates_token_count=candidates_token_count,
    )
    return SimpleNamespace(text=content_text, usage_metadata=usage_metadata)


# ---------------------------------------------------------------------------
# strip_fences
# ---------------------------------------------------------------------------

class TestStripFences:
    def test_plain_json_unchanged(self):
        """Plain JSON with no fences passes through unchanged."""
        payload = '{"score": 8}'
        assert strip_fences(payload) == payload

    def test_json_with_json_language_tag(self):
        """```json ... ``` fences are stripped."""
        raw = '```json\n{"score": 8}\n```'
        result = strip_fences(raw)
        assert json.loads(result)["score"] == 8

    def test_json_with_bare_fence(self):
        """``` ... ``` fences (no language tag) are stripped."""
        raw = '```\n{"score": 5}\n```'
        result = strip_fences(raw)
        assert json.loads(result)["score"] == 5

    def test_surrounding_whitespace_handled(self):
        """Leading/trailing whitespace around fenced block is stripped."""
        raw = '  \n```json\n{"score": 7}\n```\n  '
        result = strip_fences(raw)
        assert json.loads(result)["score"] == 7

    def test_no_opening_fence_only_closing(self):
        """A closing fence without an opening fence does not corrupt content."""
        raw = '{"score": 6}\n```'
        result = strip_fences(raw)
        assert json.loads(result)["score"] == 6

    def test_multiline_json_inside_fences(self):
        """Multi-line JSON inside fences is extracted correctly."""
        inner = '{\n  "score": 9,\n  "verdict": "great"\n}'
        raw = f"```json\n{inner}\n```"
        result = strip_fences(raw)
        parsed = json.loads(result)
        assert parsed["score"] == 9
        assert parsed["verdict"] == "great"


# ---------------------------------------------------------------------------
# Pricing helpers
# ---------------------------------------------------------------------------

class TestAnthropicPricing:
    def test_haiku_model(self):
        inp, out = anthropic_pricing("claude-haiku-4-5-20251001")
        assert inp == 0.80
        assert out == 4.00

    def test_sonnet_model(self):
        inp, out = anthropic_pricing("claude-sonnet-3-5")
        assert inp == 3.00
        assert out == 15.00

    def test_opus_model(self):
        inp, out = anthropic_pricing("claude-opus-3")
        assert inp == 15.00
        assert out == 75.00

    def test_unknown_model_falls_back_to_haiku(self):
        inp, out = anthropic_pricing("claude-future-model-99")
        assert inp == 0.80
        assert out == 4.00


class TestOpenAIPricing:
    def test_gpt4o_mini(self):
        inp, out = openai_pricing("gpt-4o-mini")
        assert inp == 0.15
        assert out == 0.60

    def test_gpt4o(self):
        inp, out = openai_pricing("gpt-4o")
        assert inp == 2.50
        assert out == 10.00

    def test_unknown_model_falls_back_to_mini(self):
        inp, out = openai_pricing("gpt-5-turbo")
        assert inp == 0.15
        assert out == 0.60


class TestGeminiPricing:
    def test_flash_model(self):
        inp, out = gemini_pricing("gemini-1.5-flash")
        assert inp == 0.075
        assert out == 0.30

    def test_pro_model(self):
        inp, out = gemini_pricing("gemini-1.5-pro")
        assert inp == 3.50
        assert out == 10.50

    def test_unknown_model_falls_back_to_flash(self):
        inp, out = gemini_pricing("gemini-2.0-ultra")
        assert inp == 0.075
        assert out == 0.30


# ---------------------------------------------------------------------------
# AnthropicProvider
# ---------------------------------------------------------------------------

class TestAnthropicProvider:
    """Tests for AnthropicProvider.complete() — Anthropic SDK is mocked."""

    def _make_provider(self) -> AnthropicProvider:
        with patch("providers.anthropic_provider.anthropic.Anthropic"):
            return AnthropicProvider(api_key="test-key", model="claude-haiku-4-5-20251001")

    def test_happy_path_returns_parsed_dict(self):
        """complete() returns a valid scoring dict on a successful API call."""
        provider = self._make_provider()
        fake_msg = _make_anthropic_message(json.dumps(_VALID_SCORE_DICT))

        provider._client.messages.create.return_value = fake_msg

        result = provider.complete("test prompt")

        assert result["score"] == 8
        assert result["matched_skills"] == ["Python", "AWS"]
        assert result["tokens_input"] == 100
        assert result["tokens_output"] == 50

    def test_fenced_json_is_parsed_correctly(self):
        """complete() strips markdown fences before JSON parsing."""
        provider = self._make_provider()
        fenced = f"```json\n{json.dumps(_VALID_SCORE_DICT)}\n```"
        provider._client.messages.create.return_value = _make_anthropic_message(fenced)

        result = provider.complete("test prompt")
        assert result["score"] == 8

    def test_retry_on_api_error_then_success(self):
        """complete() retries once after an APIError and returns the second attempt's result."""
        import anthropic as anthropic_sdk

        provider = self._make_provider()
        fake_msg = _make_anthropic_message(json.dumps(_VALID_SCORE_DICT))

        provider._client.messages.create.side_effect = [
            anthropic_sdk.APIError("rate limit", request=MagicMock(), body=None),
            fake_msg,
        ]

        with patch("providers.anthropic_provider.time.sleep"):
            result = provider.complete("test prompt")

        assert result["score"] == 8
        assert provider._client.messages.create.call_count == 2

    def test_both_attempts_fail_raises_runtime_error(self):
        """complete() raises RuntimeError after two consecutive failures."""
        import anthropic as anthropic_sdk

        provider = self._make_provider()
        provider._client.messages.create.side_effect = anthropic_sdk.APIError(
            "server error", request=MagicMock(), body=None
        )

        with patch("providers.anthropic_provider.time.sleep"):
            with pytest.raises(RuntimeError, match="2 attempts"):
                provider.complete("test prompt")

        assert provider._client.messages.create.call_count == 2

    def test_retry_on_invalid_json_then_success(self):
        """complete() retries after malformed JSON on the first attempt."""
        provider = self._make_provider()

        bad_msg  = _make_anthropic_message("not valid json at all")
        good_msg = _make_anthropic_message(json.dumps(_VALID_SCORE_DICT))

        provider._client.messages.create.side_effect = [bad_msg, good_msg]

        with patch("providers.anthropic_provider.time.sleep"):
            result = provider.complete("test prompt")

        assert result["score"] == 8

    def test_missing_required_keys_triggers_retry(self):
        """complete() retries when the JSON response is missing required keys."""
        provider = self._make_provider()

        incomplete = {"score": 7}   # missing matched_skills, missing_skills, concerns, verdict
        bad_msg  = _make_anthropic_message(json.dumps(incomplete))
        good_msg = _make_anthropic_message(json.dumps(_VALID_SCORE_DICT))

        provider._client.messages.create.side_effect = [bad_msg, good_msg]

        with patch("providers.anthropic_provider.time.sleep"):
            result = provider.complete("test prompt")

        assert result["score"] == 8

    def test_pricing_properties(self):
        """Haiku model resolves to correct pricing constants."""
        provider = self._make_provider()
        assert provider.input_cost_per_mtok  == 0.80
        assert provider.output_cost_per_mtok == 4.00


# ---------------------------------------------------------------------------
# OpenAIProvider
# ---------------------------------------------------------------------------

class TestOpenAIProvider:
    """Tests for OpenAIProvider.complete() — openai SDK is mocked."""

    def _make_provider(self) -> OpenAIProvider:
        with patch("providers.openai_provider.openai.OpenAI"):
            return OpenAIProvider(api_key="test-key", model="gpt-4o-mini")

    def test_happy_path_returns_parsed_dict(self):
        """complete() returns a valid scoring dict on a successful API call."""
        provider = self._make_provider()
        fake_resp = _make_openai_response(json.dumps(_VALID_SCORE_DICT))

        provider._client.chat.completions.create.return_value = fake_resp

        result = provider.complete("test prompt")

        assert result["score"] == 8
        assert result["tokens_input"] == 100
        assert result["tokens_output"] == 50

    def test_fenced_json_is_parsed_correctly(self):
        """complete() strips markdown fences before JSON parsing."""
        provider = self._make_provider()
        fenced = f"```json\n{json.dumps(_VALID_SCORE_DICT)}\n```"
        provider._client.chat.completions.create.return_value = _make_openai_response(fenced)

        result = provider.complete("test prompt")
        assert result["score"] == 8

    def test_retry_on_api_error_then_success(self):
        """complete() retries once after an APIError."""
        import openai as openai_sdk

        provider = self._make_provider()
        fake_resp = _make_openai_response(json.dumps(_VALID_SCORE_DICT))

        provider._client.chat.completions.create.side_effect = [
            openai_sdk.APIError("rate limit", request=MagicMock(), body=None),
            fake_resp,
        ]

        with patch("providers.openai_provider.time.sleep"):
            result = provider.complete("test prompt")

        assert result["score"] == 8
        assert provider._client.chat.completions.create.call_count == 2

    def test_both_attempts_fail_raises_runtime_error(self):
        """complete() raises RuntimeError after two consecutive failures."""
        import openai as openai_sdk

        provider = self._make_provider()
        provider._client.chat.completions.create.side_effect = openai_sdk.APIError(
            "server error", request=MagicMock(), body=None
        )

        with patch("providers.openai_provider.time.sleep"):
            with pytest.raises(RuntimeError, match="2 attempts"):
                provider.complete("test prompt")

    def test_pricing_properties(self):
        """gpt-4o-mini model resolves to correct pricing constants."""
        provider = self._make_provider()
        assert provider.input_cost_per_mtok  == 0.15
        assert provider.output_cost_per_mtok == 0.60


# ---------------------------------------------------------------------------
# GeminiProvider
# ---------------------------------------------------------------------------

class TestGeminiProvider:
    """Tests for GeminiProvider.complete() — google.generativeai SDK is mocked."""

    def _make_provider(self) -> GeminiProvider:
        with patch("providers.gemini_provider.genai.configure"), \
             patch("providers.gemini_provider.genai.GenerativeModel"):
            return GeminiProvider(api_key="test-key", model="gemini-1.5-flash")

    def test_happy_path_returns_parsed_dict(self):
        """complete() returns a valid scoring dict on a successful API call."""
        provider = self._make_provider()
        fake_resp = _make_gemini_response(json.dumps(_VALID_SCORE_DICT))

        with patch("providers.gemini_provider.genai.GenerativeModel") as MockModel:
            MockModel.return_value.generate_content.return_value = fake_resp
            result = provider.complete("test prompt")

        assert result["score"] == 8
        assert result["tokens_input"] == 100
        assert result["tokens_output"] == 50

    def test_fenced_json_is_parsed_correctly(self):
        """complete() strips markdown fences before JSON parsing."""
        provider = self._make_provider()
        fenced = f"```json\n{json.dumps(_VALID_SCORE_DICT)}\n```"
        fake_resp = _make_gemini_response(fenced)

        with patch("providers.gemini_provider.genai.GenerativeModel") as MockModel:
            MockModel.return_value.generate_content.return_value = fake_resp
            result = provider.complete("test prompt")

        assert result["score"] == 8

    def test_retry_on_exception_then_success(self):
        """complete() retries once after a generic exception (Gemini raises varied types)."""
        provider = self._make_provider()
        fake_resp = _make_gemini_response(json.dumps(_VALID_SCORE_DICT))

        with patch("providers.gemini_provider.genai.GenerativeModel") as MockModel, \
             patch("providers.gemini_provider.time.sleep"):
            MockModel.return_value.generate_content.side_effect = [
                RuntimeError("connection reset"),
                fake_resp,
            ]
            result = provider.complete("test prompt")

        assert result["score"] == 8

    def test_both_attempts_fail_raises_runtime_error(self):
        """complete() raises RuntimeError after two consecutive failures."""
        provider = self._make_provider()

        with patch("providers.gemini_provider.genai.GenerativeModel") as MockModel, \
             patch("providers.gemini_provider.time.sleep"):
            MockModel.return_value.generate_content.side_effect = RuntimeError("quota exceeded")
            with pytest.raises(RuntimeError, match="2 attempts"):
                provider.complete("test prompt")

    def test_pricing_properties(self):
        """gemini-1.5-flash model resolves to correct pricing constants."""
        provider = self._make_provider()
        assert provider.input_cost_per_mtok  == 0.075
        assert provider.output_cost_per_mtok == 0.30


# ---------------------------------------------------------------------------
# make_provider() factory
# ---------------------------------------------------------------------------

class TestMakeProvider:
    """Tests for the make_provider() factory function."""

    def _base_config(self, provider: str, model: str) -> dict:
        """Return a minimal config dict for the given provider."""
        return {
            "anthropic_api_key": "anthro-key" if provider == "anthropic" else "",
            "openai_api_key":    "oai-key"    if provider == "openai"    else "",
            "google_api_key":    "google-key" if provider == "gemini"    else "",
            "scoring": {
                "provider":  provider,
                "model":     model,
                "threshold": 7.0,
            },
        }

    def test_anthropic_provider_selected(self):
        """make_provider() returns AnthropicProvider when provider='anthropic'."""
        config = self._base_config("anthropic", "claude-haiku-4-5-20251001")
        with patch("providers.anthropic_provider.anthropic.Anthropic"):
            provider = make_provider(config)
        assert isinstance(provider, AnthropicProvider)

    def test_openai_provider_selected(self):
        """make_provider() returns OpenAIProvider when provider='openai'."""
        config = self._base_config("openai", "gpt-4o-mini")
        with patch("providers.openai_provider.openai.OpenAI"):
            provider = make_provider(config)
        assert isinstance(provider, OpenAIProvider)

    def test_gemini_provider_selected(self):
        """make_provider() returns GeminiProvider when provider='gemini'."""
        config = self._base_config("gemini", "gemini-1.5-flash")
        with patch("providers.gemini_provider.genai.configure"), \
             patch("providers.gemini_provider.genai.GenerativeModel"):
            provider = make_provider(config)
        assert isinstance(provider, GeminiProvider)

    def test_unknown_provider_raises_value_error(self):
        """make_provider() raises ValueError for an unrecognised provider name."""
        config = self._base_config("anthropic", "some-model")
        config["scoring"]["provider"] = "cohere"
        with pytest.raises(ValueError, match="cohere"):
            make_provider(config)

    def test_default_provider_is_anthropic(self):
        """make_provider() defaults to Anthropic when 'provider' key is absent."""
        config = {
            "anthropic_api_key": "anthro-key",
            "scoring": {"model": "claude-haiku-4-5-20251001", "threshold": 7.0},
        }
        with patch("providers.anthropic_provider.anthropic.Anthropic"):
            provider = make_provider(config)
        assert isinstance(provider, AnthropicProvider)

    def test_api_key_falls_back_to_env_var(self):
        """make_provider() uses the ANTHROPIC_API_KEY env var when config key is absent."""
        config = {
            "scoring": {"provider": "anthropic", "model": "claude-haiku-4-5-20251001"},
        }
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "env-key"}), \
             patch("providers.anthropic_provider.anthropic.Anthropic") as MockAnthopic:
            make_provider(config)
        MockAnthopic.assert_called_once_with(api_key="env-key")

    def test_openai_api_key_from_env(self):
        """make_provider() uses OPENAI_API_KEY env var when config key is absent."""
        config = {
            "scoring": {"provider": "openai", "model": "gpt-4o-mini"},
        }
        with patch.dict(os.environ, {"OPENAI_API_KEY": "oai-env-key"}), \
             patch("providers.openai_provider.openai.OpenAI") as MockOpenAI:
            make_provider(config)
        MockOpenAI.assert_called_once_with(api_key="oai-env-key")

    def test_gemini_api_key_from_env(self):
        """make_provider() uses GOOGLE_API_KEY env var when config key is absent."""
        config = {
            "scoring": {"provider": "gemini", "model": "gemini-1.5-flash"},
        }
        with patch.dict(os.environ, {"GOOGLE_API_KEY": "google-env-key"}), \
             patch("providers.gemini_provider.genai.configure") as mock_configure, \
             patch("providers.gemini_provider.genai.GenerativeModel"):
            make_provider(config)
        mock_configure.assert_called_once_with(api_key="google-env-key")
