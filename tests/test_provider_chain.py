"""
tests/test_provider_chain.py — Unit tests for build_provider_chain().

All provider SDK clients are patched at import time so no real API keys or
network access are needed.  Covers:

  - preferred_provider placed first in the chain
  - providers with empty api_key silently skipped
  - preferred_provider absent → dict insertion order used
  - all empty api_keys → ValueError raised
  - preferred_provider present but its api_key is empty → skipped
  - single valid provider → chain length 1
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from providers import build_provider_chain, AnthropicProvider, OpenAIProvider, GeminiProvider


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_keys(
    anthropic_key: str = "test-key-anthropic",
    openai_key: str    = "test-key-openai",
    gemini_key: str    = "test-key-gemini",
    preferred: str     = "anthropic",
) -> dict:
    """Return a keys.json-shaped dict with the given keys and preferred provider."""
    d: dict = {
        "providers": {
            "anthropic": {"api_key": anthropic_key, "model": "claude-haiku-4-5-20251001"},
            "openai":    {"api_key": openai_key,    "model": "gpt-4o-mini"},
            "gemini":    {"api_key": gemini_key,    "model": "gemini-1.5-flash"},
        },
    }
    if preferred is not None:
        d["preferred_provider"] = preferred
    return d


# Patch all three SDK constructors so no real clients are created.
_PATCHES = [
    patch("providers.anthropic_provider.anthropic.Anthropic"),
    patch("providers.openai_provider.openai.OpenAI"),
    patch("providers.gemini_provider.genai.Client"),
]


def _start_patches() -> list:
    return [p.start() for p in _PATCHES]


def _stop_patches(mocks) -> None:
    for p in _PATCHES:
        p.stop()


# ---------------------------------------------------------------------------
# TestBuildProviderChain
# ---------------------------------------------------------------------------

class TestBuildProviderChain(unittest.TestCase):
    """Unit tests for build_provider_chain()."""

    def setUp(self) -> None:
        self._mocks = _start_patches()

    def tearDown(self) -> None:
        _stop_patches(self._mocks)

    # ------------------------------------------------------------------
    # 1. preferred_provider goes first
    # ------------------------------------------------------------------

    def test_preferred_first(self):
        """Chain starts with the preferred_provider when all three have keys."""
        keys = _make_keys(preferred="openai")
        chain = build_provider_chain(keys)

        self.assertIsInstance(chain[0], OpenAIProvider,
            "OpenAI should be first when preferred_provider='openai'")
        self.assertEqual(len(chain), 3,
            "All three providers should appear in the chain")

    # ------------------------------------------------------------------
    # 2. Providers with empty api_key are skipped
    # ------------------------------------------------------------------

    def test_empty_key_skipped(self):
        """Providers with an empty api_key are silently omitted from the chain."""
        keys = _make_keys(anthropic_key="", preferred="openai")
        chain = build_provider_chain(keys)

        types = [type(p) for p in chain]
        self.assertNotIn(AnthropicProvider, types,
            "AnthropicProvider should be absent when its api_key is empty")
        self.assertIn(OpenAIProvider, types,
            "OpenAIProvider should be present with a valid key")

    # ------------------------------------------------------------------
    # 3. No preferred_provider → dict insertion order
    # ------------------------------------------------------------------

    def test_preferred_missing_falls_back_to_order(self):
        """When preferred_provider is absent the order follows dict insertion order."""
        keys = _make_keys()
        # Remove preferred_provider entirely so the function uses insertion order.
        del keys["preferred_provider"]

        chain = build_provider_chain(keys)

        self.assertIsInstance(chain[0], AnthropicProvider,
            "First provider should be 'anthropic' (first in dict) when preferred is absent")
        self.assertIsInstance(chain[1], OpenAIProvider)
        self.assertIsInstance(chain[2], GeminiProvider)

    # ------------------------------------------------------------------
    # 4. All empty keys → ValueError
    # ------------------------------------------------------------------

    def test_no_valid_providers_raises(self):
        """ValueError is raised when every provider has an empty api_key."""
        keys = _make_keys(anthropic_key="", openai_key="", gemini_key="", preferred="anthropic")

        with self.assertRaises(ValueError):
            build_provider_chain(keys)

    # ------------------------------------------------------------------
    # 5. preferred_provider has empty key → skip it, use next valid one
    # ------------------------------------------------------------------

    def test_preferred_empty_key_skipped(self):
        """preferred_provider is skipped when its api_key is empty; next valid provider leads."""
        keys = _make_keys(anthropic_key="", preferred="anthropic")
        chain = build_provider_chain(keys)

        self.assertIsInstance(chain[0], OpenAIProvider,
            "OpenAI (first remaining valid provider) should lead when preferred key is empty")
        types = [type(p) for p in chain]
        self.assertNotIn(AnthropicProvider, types,
            "AnthropicProvider must not appear when its api_key is empty")

    # ------------------------------------------------------------------
    # 6. Single provider → chain length 1
    # ------------------------------------------------------------------

    def test_single_provider(self):
        """A keys dict with only one provider produces a chain of length 1."""
        keys = {
            "providers": {
                "gemini": {"api_key": "test-key-123", "model": "gemini-1.5-flash"},
            },
            "preferred_provider": "gemini",
        }
        chain = build_provider_chain(keys)

        self.assertEqual(len(chain), 1,
            "Chain should contain exactly one provider")
        self.assertIsInstance(chain[0], GeminiProvider)


if __name__ == "__main__":
    unittest.main()
