# PDF Resume Import Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add PDF resume upload to `/profile` that extracts candidate data via LLM and pre-fills the profile form for review before saving.

**Architecture:** Refactor `LLMProvider` to expose a generic `generate()` method (decoupled from scoring), add a `POST /profile/import-pdf` endpoint that extracts PDF text via `pypdf` and sends it through the provider chain, and add a collapsible UI section at the top of the profile form with vanilla JS to distribute the response across form fields.

**Tech Stack:** Python 3.11+, Flask, pypdf, existing LLM provider chain (Anthropic/OpenAI/Gemini), vanilla JS

**Issue:** #41
**Design spec:** `docs/superpowers/specs/2026-04-07-pdf-profile-import-design.md`

---

## File Structure

| File | Responsibility |
|---|---|
| `providers/base.py` | Add `generate()` abstract method to `LLMProvider` |
| `providers/anthropic_provider.py` | Extract HTTP call into `generate()`, `complete()` wraps it |
| `providers/openai_provider.py` | Same refactor |
| `providers/gemini_provider.py` | Same refactor |
| `providers/__init__.py` | Add `generate_with_fallback()` helper |
| `app.py` | Add `POST /profile/import-pdf` endpoint + import prompt logic |
| `templates/profile.html` | Add collapsible import section + client-side JS |
| `requirements.txt` | Add `pypdf>=4.0.0` |
| `tests/test_providers.py` | Add `generate()` regression tests |
| `tests/test_profile_import.py` | New — all import-specific tests |

---

### Task 1: Add `generate()` to LLMProvider base class

**Files:**
- Modify: `providers/base.py:43-72`
- Test: `tests/test_providers.py`

- [ ] **Step 1: Write failing test for `generate()` abstract method**

In `tests/test_providers.py`, add a test that verifies `generate` is an abstract method on `LLMProvider`:

```python
class TestGenerateAbstract:
    def test_generate_is_abstract_method(self):
        """LLMProvider.generate is declared as an abstract method."""
        import inspect
        from providers.base import LLMProvider
        assert hasattr(LLMProvider, "generate")
        assert getattr(LLMProvider.generate, "__isabstractmethod__", False)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_providers.py::TestGenerateAbstract -v`
Expected: FAIL — `LLMProvider` has no `generate` attribute yet.

- [ ] **Step 3: Add `generate()` abstract method to `LLMProvider`**

In `providers/base.py`, add this method between the existing `complete()` method (line 72) and the `input_cost_per_mtok` property (line 74):

```python
    @abstractmethod
    def generate(self, prompt: str) -> str:
        """Send *prompt* to the LLM and return the raw response text.

        This is the low-level interface for arbitrary LLM calls.  Unlike
        ``complete()``, it performs no JSON parsing or key validation —
        callers are responsible for interpreting the returned string.

        Implementors must:
        * Attempt the API call up to 2 times (2-second delay between attempts).
        * Raise ``RuntimeError`` if both attempts fail.

        Args:
            prompt: Arbitrary prompt string.

        Returns:
            Raw response text from the LLM.

        Raises:
            RuntimeError: If all retry attempts fail.
        """
        ...
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_providers.py::TestGenerateAbstract -v`
Expected: PASS

- [ ] **Step 5: Commit**

```
git add providers/base.py tests/test_providers.py
git commit -m "refactor(providers): add generate() abstract method to LLMProvider

ref #41"
```

---

### Task 2: Implement `generate()` in AnthropicProvider

**Files:**
- Modify: `providers/anthropic_provider.py:155-212`
- Test: `tests/test_providers.py`

- [ ] **Step 1: Write failing test for `AnthropicProvider.generate()`**

Add to `tests/test_providers.py`:

```python
class TestAnthropicGenerate:
    def test_generate_returns_raw_text(self):
        """generate() returns the raw text from the Anthropic API."""
        from types import SimpleNamespace
        from unittest.mock import MagicMock, patch

        mock_client = MagicMock()
        mock_message = SimpleNamespace(
            content=[SimpleNamespace(text="Hello, world!")],
            usage=SimpleNamespace(input_tokens=10, output_tokens=5),
        )
        mock_client.messages.create.return_value = mock_message

        with patch("providers.anthropic_provider.anthropic") as mock_anthropic:
            mock_anthropic.Anthropic.return_value = mock_client
            mock_anthropic.APIError = Exception
            from providers.anthropic_provider import AnthropicProvider
            provider = AnthropicProvider.__new__(AnthropicProvider)
            provider._client = mock_client
            provider._model = "claude-haiku-4-5-20251001"

        result = provider.generate("test prompt")
        assert result == "Hello, world!"

    def test_generate_raises_after_two_failures(self):
        """generate() raises RuntimeError after 2 failed attempts."""
        from unittest.mock import MagicMock, patch

        mock_client = MagicMock()
        mock_client.messages.create.side_effect = Exception("API error")

        with patch("providers.anthropic_provider.anthropic") as mock_anthropic:
            mock_anthropic.APIError = Exception
            from providers.anthropic_provider import AnthropicProvider
            provider = AnthropicProvider.__new__(AnthropicProvider)
            provider._client = mock_client
            provider._model = "claude-haiku-4-5-20251001"

        with patch("providers.anthropic_provider.time.sleep"):
            with pytest.raises(RuntimeError):
                provider.generate("test prompt")

    def test_complete_still_works_after_refactor(self):
        """complete() still returns a parsed scoring dict (regression)."""
        from types import SimpleNamespace
        from unittest.mock import MagicMock, patch
        import json

        score_dict = {
            "score": 8,
            "matched_skills": ["Python"],
            "missing_skills": [],
            "concerns": [],
            "verdict": "Good match",
        }
        mock_client = MagicMock()
        mock_message = SimpleNamespace(
            content=[SimpleNamespace(text=json.dumps(score_dict))],
            usage=SimpleNamespace(input_tokens=10, output_tokens=5),
        )
        mock_client.messages.create.return_value = mock_message

        with patch("providers.anthropic_provider.anthropic") as mock_anthropic:
            mock_anthropic.Anthropic.return_value = mock_client
            mock_anthropic.APIError = Exception
            from providers.anthropic_provider import AnthropicProvider
            provider = AnthropicProvider.__new__(AnthropicProvider)
            provider._client = mock_client
            provider._model = "claude-haiku-4-5-20251001"

        result = provider.complete("test prompt")
        assert result["score"] == 8
        assert result["matched_skills"] == ["Python"]
        assert "tokens_input" in result
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_providers.py::TestAnthropicGenerate -v`
Expected: FAIL — `generate()` not implemented yet.

- [ ] **Step 3: Implement `generate()` and refactor `complete()` in AnthropicProvider**

In `providers/anthropic_provider.py`, replace the existing `complete()` method (lines 155–212) with:

```python
    def generate(self, prompt: str) -> str:
        """Send *prompt* to the Anthropic Messages API and return raw text.

        Retries once on API error (2-second delay).
        Raises ``RuntimeError`` if both attempts fail.

        Args:
            prompt: Arbitrary prompt string.

        Returns:
            Raw response text from the LLM.

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

            try:
                return message.content[0].text
            except (IndexError, AttributeError) as exc:
                logger.warning(
                    "Unexpected Anthropic response structure (attempt %d/2): %s",
                    attempt + 1,
                    exc,
                )
                continue

        raise RuntimeError("Anthropic generate failed after 2 attempts")

    def complete(self, prompt: str) -> dict:
        """Call the Anthropic Messages API and return a parsed scoring dict.

        Delegates to ``generate()`` for the raw API call, then parses and
        validates the JSON response.  Retries once on malformed JSON
        (2-second delay).  Raises ``RuntimeError`` if both attempts fail.

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
```

Note: `complete()` keeps its own retry loop and direct API call (not delegating to `generate()`) because it needs access to the full `message` object for token usage extraction. `generate()` only returns text.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_providers.py::TestAnthropicGenerate -v`
Expected: PASS (all 3 tests)

Also run full provider tests for regression:
Run: `pytest tests/test_providers.py -v`
Expected: All existing tests still PASS.

- [ ] **Step 5: Commit**

```
git add providers/anthropic_provider.py tests/test_providers.py
git commit -m "refactor(providers): implement generate() in AnthropicProvider

ref #41"
```

---

### Task 3: Implement `generate()` in OpenAIProvider

**Files:**
- Modify: `providers/openai_provider.py:147-204`
- Test: `tests/test_providers.py`

- [ ] **Step 1: Write failing test for `OpenAIProvider.generate()`**

Add to `tests/test_providers.py`:

```python
class TestOpenAIGenerate:
    def test_generate_returns_raw_text(self):
        """generate() returns the raw text from the OpenAI API."""
        from types import SimpleNamespace
        from unittest.mock import MagicMock, patch

        mock_client = MagicMock()
        mock_response = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="Hello from OpenAI"))],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
        )
        mock_client.chat.completions.create.return_value = mock_response

        with patch("providers.openai_provider.openai") as mock_openai:
            mock_openai.OpenAI.return_value = mock_client
            mock_openai.APIError = Exception
            from providers.openai_provider import OpenAIProvider
            provider = OpenAIProvider.__new__(OpenAIProvider)
            provider._client = mock_client
            provider._model = "gpt-4o-mini"

        result = provider.generate("test prompt")
        assert result == "Hello from OpenAI"

    def test_generate_raises_after_two_failures(self):
        """generate() raises RuntimeError after 2 failed attempts."""
        from unittest.mock import MagicMock, patch

        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = Exception("API error")

        with patch("providers.openai_provider.openai") as mock_openai:
            mock_openai.APIError = Exception
            from providers.openai_provider import OpenAIProvider
            provider = OpenAIProvider.__new__(OpenAIProvider)
            provider._client = mock_client
            provider._model = "gpt-4o-mini"

        with patch("providers.openai_provider.time.sleep"):
            with pytest.raises(RuntimeError):
                provider.generate("test prompt")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_providers.py::TestOpenAIGenerate -v`
Expected: FAIL

- [ ] **Step 3: Implement `generate()` in OpenAIProvider**

In `providers/openai_provider.py`, add this method before the existing `complete()` method (before line 147):

```python
    def generate(self, prompt: str) -> str:
        """Send *prompt* to the OpenAI Chat Completions API and return raw text.

        Retries once on API error (2-second delay).
        Raises ``RuntimeError`` if both attempts fail.

        Args:
            prompt: Arbitrary prompt string.

        Returns:
            Raw response text from the LLM.

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

            try:
                return response.choices[0].message.content or ""
            except (IndexError, AttributeError) as exc:
                logger.warning(
                    "Unexpected OpenAI response structure (attempt %d/2): %s",
                    attempt + 1,
                    exc,
                )
                continue

        raise RuntimeError("OpenAI generate failed after 2 attempts")
```

`complete()` remains unchanged — it keeps its own loop for token extraction.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_providers.py::TestOpenAIGenerate -v`
Expected: PASS

Run: `pytest tests/test_providers.py -v`
Expected: All tests PASS (regression check).

- [ ] **Step 5: Commit**

```
git add providers/openai_provider.py tests/test_providers.py
git commit -m "refactor(providers): implement generate() in OpenAIProvider

ref #41"
```

---

### Task 4: Implement `generate()` in GeminiProvider

**Files:**
- Modify: `providers/gemini_provider.py:152-209`
- Test: `tests/test_providers.py`

- [ ] **Step 1: Write failing test for `GeminiProvider.generate()`**

Add to `tests/test_providers.py`:

```python
class TestGeminiGenerate:
    def test_generate_returns_raw_text(self):
        """generate() returns the raw text from the Gemini API."""
        from types import SimpleNamespace
        from unittest.mock import MagicMock, patch

        mock_client = MagicMock()
        mock_response = SimpleNamespace(
            text="Hello from Gemini",
            usage_metadata=SimpleNamespace(
                prompt_token_count=10, candidates_token_count=5
            ),
        )
        mock_client.models.generate_content.return_value = mock_response

        from providers.gemini_provider import GeminiProvider
        provider = GeminiProvider.__new__(GeminiProvider)
        provider._client = mock_client
        provider._model_name = "gemini-2.0-flash"

        result = provider.generate("test prompt")
        assert result == "Hello from Gemini"

    def test_generate_raises_after_two_failures(self):
        """generate() raises RuntimeError after 2 failed attempts."""
        from unittest.mock import MagicMock, patch

        mock_client = MagicMock()
        mock_client.models.generate_content.side_effect = Exception("API error")

        from providers.gemini_provider import GeminiProvider
        provider = GeminiProvider.__new__(GeminiProvider)
        provider._client = mock_client
        provider._model_name = "gemini-2.0-flash"

        with patch("providers.gemini_provider.time.sleep"):
            with pytest.raises(RuntimeError):
                provider.generate("test prompt")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_providers.py::TestGeminiGenerate -v`
Expected: FAIL

- [ ] **Step 3: Implement `generate()` in GeminiProvider**

In `providers/gemini_provider.py`, add this method before the existing `complete()` method (before line 152):

```python
    def generate(self, prompt: str) -> str:
        """Send *prompt* to the Gemini API and return raw text.

        Retries once on any exception (2-second delay).  Gemini raises a
        variety of error types, so we catch ``Exception`` broadly.
        Raises ``RuntimeError`` if both attempts fail.

        Args:
            prompt: Arbitrary prompt string.

        Returns:
            Raw response text from the LLM.

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

            try:
                return response.text
            except (AttributeError, ValueError) as exc:
                logger.warning(
                    "Unexpected Gemini response structure (attempt %d/2): %s",
                    attempt + 1,
                    exc,
                )
                continue

        raise RuntimeError("Gemini generate failed after 2 attempts")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_providers.py::TestGeminiGenerate -v`
Expected: PASS

Run: `pytest tests/test_providers.py -v`
Expected: All tests PASS.

- [ ] **Step 5: Commit**

```
git add providers/gemini_provider.py tests/test_providers.py
git commit -m "refactor(providers): implement generate() in GeminiProvider

ref #41"
```

---

### Task 5: Add `generate_with_fallback()` to providers package

**Files:**
- Modify: `providers/__init__.py`
- Test: `tests/test_provider_chain.py`

- [ ] **Step 1: Write failing tests for `generate_with_fallback()`**

Add to `tests/test_provider_chain.py`:

```python
class TestGenerateWithFallback:
    def test_returns_text_and_model_on_success(self):
        """Returns (raw_text, 'provider/model') on first provider success."""
        from unittest.mock import MagicMock
        from providers import generate_with_fallback

        provider = MagicMock()
        provider.generate.return_value = "LLM response text"
        provider.__class__.__name__ = "AnthropicProvider"
        provider._model = "claude-haiku-4-5-20251001"

        result = generate_with_fallback("test prompt", [provider], set())
        assert result == ("LLM response text", "anthropic/claude-haiku-4-5-20251001")

    def test_skips_dead_providers(self):
        """Providers in dead_providers set are skipped."""
        from unittest.mock import MagicMock
        from providers import generate_with_fallback

        dead = MagicMock()
        dead.__class__.__name__ = "AnthropicProvider"
        alive = MagicMock()
        alive.generate.return_value = "response"
        alive.__class__.__name__ = "OpenAIProvider"
        alive._model = "gpt-4o-mini"

        dead_set = set()
        dead_set.add(id(dead))

        result = generate_with_fallback("prompt", [dead, alive], dead_set)
        assert result is not None
        dead.generate.assert_not_called()

    def test_auth_error_kills_provider(self):
        """Auth errors (401/403) add provider to dead_providers permanently."""
        from unittest.mock import MagicMock
        from providers import generate_with_fallback

        provider = MagicMock()
        provider.generate.side_effect = RuntimeError("401 Unauthorized")
        provider.__class__.__name__ = "AnthropicProvider"

        dead_set = set()
        result = generate_with_fallback("prompt", [provider], dead_set)
        assert result is None
        assert id(provider) in dead_set

    def test_transient_error_tries_next_provider(self):
        """Transient errors skip to the next provider."""
        from unittest.mock import MagicMock
        from providers import generate_with_fallback

        bad = MagicMock()
        bad.generate.side_effect = RuntimeError("Connection timeout")
        bad.__class__.__name__ = "AnthropicProvider"

        good = MagicMock()
        good.generate.return_value = "fallback response"
        good.__class__.__name__ = "OpenAIProvider"
        good._model = "gpt-4o-mini"

        result = generate_with_fallback("prompt", [bad, good], set())
        assert result == ("fallback response", "openai/gpt-4o-mini")

    def test_all_fail_returns_none(self):
        """Returns None when all providers fail."""
        from unittest.mock import MagicMock
        from providers import generate_with_fallback

        provider = MagicMock()
        provider.generate.side_effect = RuntimeError("timeout")
        provider.__class__.__name__ = "AnthropicProvider"

        result = generate_with_fallback("prompt", [provider], set())
        assert result is None

    def test_empty_chain_returns_none(self):
        """Returns None when chain is empty."""
        from providers import generate_with_fallback
        result = generate_with_fallback("prompt", [], set())
        assert result is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_provider_chain.py::TestGenerateWithFallback -v`
Expected: FAIL — `generate_with_fallback` not importable yet.

- [ ] **Step 3: Implement `generate_with_fallback()`**

Add to `providers/__init__.py` after the existing `build_provider_chain()` function:

```python
def generate_with_fallback(
    prompt: str,
    chain: list,
    dead_providers: set,
) -> tuple[str, str] | None:
    """Try providers in order for a raw text generation call.

    Same retry/fallback semantics as ``score_listing_with_fallback()`` in
    ``ingest.py``:

    * Auth errors (401/403 in the RuntimeError message) permanently add the
      provider to *dead_providers* for the remainder of the run.
    * Transient failures log a warning and skip to the next provider.

    Args:
        prompt:         Arbitrary prompt string.
        chain:          Ordered list of ``LLMProvider`` instances.
        dead_providers: Set of ``id(provider)`` values to skip.  Modified
                        in-place when an auth error is detected.

    Returns:
        ``(raw_text, "provider/model")`` on success, or ``None`` if all
        providers fail.
    """
    for provider in chain:
        if id(provider) in dead_providers:
            continue

        provider_name = provider.__class__.__name__.replace("Provider", "").lower()
        model_name = getattr(provider, "_model", None) or getattr(provider, "_model_name", "unknown")

        try:
            raw_text = provider.generate(prompt)
            return (raw_text, f"{provider_name}/{model_name}")
        except RuntimeError as exc:
            exc_lower = str(exc).lower()
            if any(marker in exc_lower for marker in ("401", "403", "unauthorized", "authentication")):
                logger.warning(
                    "Auth error from %s — permanently disabled for this run: %s",
                    provider_name,
                    exc,
                )
                dead_providers.add(id(provider))
            else:
                logger.warning(
                    "Transient error from %s, trying next provider: %s",
                    provider_name,
                    exc,
                )

    return None
```

Also add `generate_with_fallback` to any `__all__` list if one exists, or ensure it's importable.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_provider_chain.py::TestGenerateWithFallback -v`
Expected: PASS (all 6 tests)

Run: `pytest tests/test_provider_chain.py -v`
Expected: All tests PASS.

- [ ] **Step 5: Commit**

```
git add providers/__init__.py tests/test_provider_chain.py
git commit -m "feat(providers): add generate_with_fallback() helper

Reuses the same auth-error/transient-error pattern as scoring fallback.

ref #41"
```

---

### Task 6: Add `pypdf` dependency

**Files:**
- Modify: `requirements.txt`

- [ ] **Step 1: Add `pypdf>=4.0.0` to `requirements.txt`**

Add after the last line:

```
pypdf>=4.0.0
```

- [ ] **Step 2: Install the dependency**

Run: `uv pip install pypdf>=4.0.0`
Expected: Successfully installed pypdf.

- [ ] **Step 3: Commit**

```
git add requirements.txt
git commit -m "deps: add pypdf for PDF resume text extraction

ref #41"
```

---

### Task 7: Add PDF extraction and import prompt logic to `app.py`

**Files:**
- Modify: `app.py`
- Test: `tests/test_profile_import.py` (new)

This is the core logic task. It adds the import endpoint and all supporting functions.

- [ ] **Step 1: Write failing tests for PDF extraction helper**

Create `tests/test_profile_import.py`:

```python
"""
tests/test_profile_import.py — Tests for PDF resume import (issue #41).

Covers:
  - PDF text extraction: valid PDF, empty PDF, corrupt file, text too short
  - Import prompt construction: fresh vs merge mode, JSON schema in prompt
  - Import response parsing: well-formed JSON, markdown fences, missing fields, malformed
  - Merge logic: skills added/preserved, education appended, seniority preserved, industries deduped
  - Import endpoint: success, no file, non-PDF, no provider, LLM failure
"""

from __future__ import annotations

import io
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app as app_module
from app import app as flask_app


# ===========================================================================
# Fixtures
# ===========================================================================

@pytest.fixture()
def tmp_config_path(tmp_path, monkeypatch):
    path = str(tmp_path / "config.json")
    monkeypatch.setattr(app_module, "_CONFIG_PATH", path)
    return path


@pytest.fixture()
def tmp_profile_path(tmp_path, monkeypatch):
    path = str(tmp_path / "profile.json")
    monkeypatch.setattr(app_module, "_PROFILE_PATH", path)
    return path


@pytest.fixture()
def tmp_providers_path(tmp_path, monkeypatch):
    path = str(tmp_path / "providers.json")
    monkeypatch.setattr(app_module, "_PROVIDERS_PATH", path)
    return path


@pytest.fixture()
def tmp_keys_path(tmp_path, monkeypatch):
    path = str(tmp_path / "keys.json")
    monkeypatch.setattr(app_module, "_KEYS_PATH", path)
    return path


@pytest.fixture()
def client():
    flask_app.config["TESTING"] = True
    with flask_app.test_client() as c:
        yield c


def _write_profile(path, data=None):
    if data is None:
        data = {
            "primary_skills": ["Python, 5yr, active"],
            "seniority": "Senior",
            "education": ["BS Computer Science, MIT, 2015"],
            "preferred_industries": ["fintech"],
            "location": {"center": "Miami, FL"},
        }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def _write_config(path, data=None):
    if data is None:
        data = {"search": {"what": "engineer", "where": "Miami"}, "scoring": {"threshold": 7.0}}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def _write_providers(path, data=None):
    if data is None:
        data = {
            "provider_order": ["anthropic"],
            "llm": {"anthropic": {"api_key": "test-key", "model": "claude-haiku-4-5-20251001"}},
        }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def _make_pdf_bytes(text: str) -> bytes:
    """Create a minimal valid PDF containing *text*."""
    from pypdf import PdfWriter
    from pypdf.generic import AnnotationBuilder
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    # Use reportlab-free approach: add text via page content stream
    page = writer.pages[0]
    # Minimal PDF content stream with text
    from io import BytesIO
    buf = BytesIO()
    writer.write(buf)
    # For test simplicity, use a real pypdf approach to add text
    # Since pypdf doesn't easily add text without reportlab,
    # we'll mock the extraction instead
    return buf.getvalue()


# ===========================================================================
# PDF text extraction
# ===========================================================================

class TestPdfExtraction:
    def test_extract_text_from_valid_pdf(self):
        """_extract_pdf_text() returns concatenated page text."""
        from unittest.mock import MagicMock, patch
        mock_reader = MagicMock()
        mock_reader.pages = [MagicMock(), MagicMock()]
        mock_reader.pages[0].extract_text.return_value = "Page 1 content. "
        mock_reader.pages[1].extract_text.return_value = "Page 2 content."

        with patch("app.PdfReader", return_value=mock_reader):
            from app import _extract_pdf_text
            result = _extract_pdf_text(b"fake pdf bytes")

        assert result == "Page 1 content. Page 2 content."

    def test_extract_text_empty_pdf_returns_empty_string(self):
        """_extract_pdf_text() returns empty string for a PDF with no text."""
        from unittest.mock import MagicMock, patch
        mock_reader = MagicMock()
        mock_reader.pages = [MagicMock()]
        mock_reader.pages[0].extract_text.return_value = ""

        with patch("app.PdfReader", return_value=mock_reader):
            from app import _extract_pdf_text
            result = _extract_pdf_text(b"fake pdf bytes")

        assert result == ""

    def test_extract_text_corrupt_pdf_raises(self):
        """_extract_pdf_text() raises ValueError on corrupt PDF."""
        from unittest.mock import patch
        with patch("app.PdfReader", side_effect=Exception("Invalid PDF")):
            from app import _extract_pdf_text
            with pytest.raises(ValueError, match="Could not read PDF"):
                _extract_pdf_text(b"not a pdf")


# ===========================================================================
# Import prompt construction
# ===========================================================================

class TestImportPromptConstruction:
    def test_fresh_mode_excludes_current_profile(self):
        """In fresh mode, the prompt does not contain current profile data."""
        from app import _build_import_prompt
        prompt = _build_import_prompt("Resume text here", mode="fresh", current_profile=None)
        assert "Resume text here" in prompt
        assert "existing profile" not in prompt.lower()

    def test_merge_mode_includes_current_profile(self):
        """In merge mode, the prompt includes the current profile JSON."""
        from app import _build_import_prompt
        profile = {"primary_skills": ["Python, 5yr, active"], "seniority": "Senior"}
        prompt = _build_import_prompt("Resume text here", mode="merge", current_profile=profile)
        assert "Resume text here" in prompt
        assert "Python, 5yr, active" in prompt

    def test_prompt_requests_json_schema(self):
        """The prompt requests specific JSON keys."""
        from app import _build_import_prompt
        prompt = _build_import_prompt("Resume text", mode="fresh", current_profile=None)
        assert "primary_skills" in prompt
        assert "education" in prompt
        assert "seniority" in prompt
        assert "preferred_industries" in prompt
        assert "location_center" in prompt


# ===========================================================================
# Import response parsing
# ===========================================================================

class TestImportResponseParsing:
    def test_parse_valid_json(self):
        """_parse_import_response() parses well-formed JSON."""
        from app import _parse_import_response
        raw = json.dumps({
            "primary_skills": [{"skill": "Python", "years": 5, "status": "active"}],
            "education": ["BS CS, MIT, 2015"],
            "seniority": "Senior",
            "preferred_industries": ["fintech"],
            "location_center": "Miami, FL",
        })
        result = _parse_import_response(raw)
        assert result["primary_skills"][0]["skill"] == "Python"
        assert result["seniority"] == "Senior"

    def test_parse_strips_markdown_fences(self):
        """_parse_import_response() strips ```json ... ``` fences."""
        from app import _parse_import_response
        raw = '```json\n{"primary_skills": [], "education": [], "seniority": "", "preferred_industries": [], "location_center": null}\n```'
        result = _parse_import_response(raw)
        assert result is not None
        assert result["primary_skills"] == []

    def test_parse_missing_fields_default_to_empty(self):
        """_parse_import_response() fills missing fields with defaults."""
        from app import _parse_import_response
        raw = json.dumps({"primary_skills": [{"skill": "Go", "years": 3, "status": "active"}]})
        result = _parse_import_response(raw)
        assert result["education"] == []
        assert result["seniority"] == ""
        assert result["preferred_industries"] == []
        assert result["location_center"] is None

    def test_parse_malformed_json_returns_none(self):
        """_parse_import_response() returns None on malformed JSON."""
        from app import _parse_import_response
        result = _parse_import_response("this is not json {{{")
        assert result is None


# ===========================================================================
# Merge logic
# ===========================================================================

class TestImportMergeLogic:
    def test_merge_adds_new_skills(self):
        """_merge_import_result() adds skills not in current profile."""
        from app import _merge_import_result
        current = {"primary_skills": ["Python, 5yr, active"]}
        imported = {
            "primary_skills": [
                {"skill": "Python", "years": 5, "status": "active"},
                {"skill": "Go", "years": 2, "status": "active"},
            ],
            "education": [],
            "seniority": "",
            "preferred_industries": [],
            "location_center": None,
        }
        result = _merge_import_result(current, imported)
        skills = result["primary_skills"]
        assert len(skills) == 2
        assert "Python, 5yr, active" in skills
        assert any("Go" in s for s in skills)

    def test_merge_appends_education(self):
        """_merge_import_result() appends new education entries."""
        from app import _merge_import_result
        current = {"education": ["BS CS, MIT, 2015"]}
        imported = {
            "primary_skills": [],
            "education": ["BS CS, MIT, 2015", "MS Data Science, Stanford, 2018"],
            "seniority": "",
            "preferred_industries": [],
            "location_center": None,
        }
        result = _merge_import_result(current, imported)
        assert len(result["education"]) == 2
        assert "MS Data Science, Stanford, 2018" in result["education"]

    def test_merge_preserves_existing_seniority(self):
        """_merge_import_result() keeps existing seniority if set."""
        from app import _merge_import_result
        current = {"seniority": "Staff"}
        imported = {
            "primary_skills": [],
            "education": [],
            "seniority": "Senior",
            "preferred_industries": [],
            "location_center": None,
        }
        result = _merge_import_result(current, imported)
        assert result["seniority"] == "Staff"

    def test_merge_fills_empty_seniority(self):
        """_merge_import_result() fills seniority from PDF if currently empty."""
        from app import _merge_import_result
        current = {"seniority": ""}
        imported = {
            "primary_skills": [],
            "education": [],
            "seniority": "Senior",
            "preferred_industries": [],
            "location_center": None,
        }
        result = _merge_import_result(current, imported)
        assert result["seniority"] == "Senior"

    def test_merge_deduplicates_industries(self):
        """_merge_import_result() adds new industries without duplicating."""
        from app import _merge_import_result
        current = {"preferred_industries": ["fintech"]}
        imported = {
            "primary_skills": [],
            "education": [],
            "seniority": "",
            "preferred_industries": ["fintech", "healthtech"],
            "location_center": None,
        }
        result = _merge_import_result(current, imported)
        assert result["preferred_industries"] == ["fintech", "healthtech"]


# ===========================================================================
# Import endpoint
# ===========================================================================

class TestImportEndpoint:
    def test_success_returns_profile_json(
        self, client, tmp_profile_path, tmp_config_path, tmp_providers_path, tmp_keys_path
    ):
        """POST /profile/import-pdf with valid PDF returns extracted profile."""
        _write_profile(tmp_profile_path)
        _write_config(tmp_config_path)
        _write_providers(tmp_providers_path)

        llm_response = json.dumps({
            "primary_skills": [{"skill": "Python", "years": 5, "status": "active"}],
            "education": ["BS CS, MIT"],
            "seniority": "Senior",
            "preferred_industries": ["fintech"],
            "location_center": "Miami, FL",
        })

        from unittest.mock import patch, MagicMock
        with patch("app._extract_pdf_text", return_value="A long resume text " * 10), \
             patch("app.build_provider_chain") as mock_chain, \
             patch("app.generate_with_fallback", return_value=(llm_response, "anthropic/claude-haiku-4-5-20251001")):
            mock_chain.return_value = [MagicMock()]
            resp = client.post(
                "/profile/import-pdf",
                data={"file": (io.BytesIO(b"fake pdf"), "resume.pdf"), "mode": "fresh"},
                content_type="multipart/form-data",
            )

        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["profile"]["seniority"] == "Senior"
        assert "model_used" in data

    def test_no_file_returns_400(
        self, client, tmp_profile_path, tmp_config_path, tmp_providers_path, tmp_keys_path
    ):
        """POST /profile/import-pdf without a file returns 400."""
        _write_config(tmp_config_path)
        resp = client.post("/profile/import-pdf", data={"mode": "fresh"})
        assert resp.status_code == 400

    def test_non_pdf_returns_400(
        self, client, tmp_profile_path, tmp_config_path, tmp_providers_path, tmp_keys_path
    ):
        """POST /profile/import-pdf with a non-PDF file returns 400."""
        _write_config(tmp_config_path)
        resp = client.post(
            "/profile/import-pdf",
            data={"file": (io.BytesIO(b"hello"), "resume.txt"), "mode": "fresh"},
            content_type="multipart/form-data",
        )
        assert resp.status_code == 400

    def test_no_provider_returns_503(
        self, client, tmp_profile_path, tmp_config_path, tmp_providers_path, tmp_keys_path
    ):
        """POST /profile/import-pdf with no configured LLM provider returns 503."""
        _write_profile(tmp_profile_path)
        _write_config(tmp_config_path)
        _write_providers(tmp_providers_path, data={"provider_order": [], "llm": {}})

        from unittest.mock import patch
        with patch("app._extract_pdf_text", return_value="A long resume text " * 10), \
             patch("app.build_provider_chain", return_value=[]):
            resp = client.post(
                "/profile/import-pdf",
                data={"file": (io.BytesIO(b"fake pdf"), "resume.pdf"), "mode": "fresh"},
                content_type="multipart/form-data",
            )

        assert resp.status_code == 503

    def test_llm_failure_returns_502(
        self, client, tmp_profile_path, tmp_config_path, tmp_providers_path, tmp_keys_path
    ):
        """POST /profile/import-pdf returns 502 when LLM fails."""
        _write_profile(tmp_profile_path)
        _write_config(tmp_config_path)
        _write_providers(tmp_providers_path)

        from unittest.mock import patch, MagicMock
        with patch("app._extract_pdf_text", return_value="A long resume text " * 10), \
             patch("app.build_provider_chain") as mock_chain, \
             patch("app.generate_with_fallback", return_value=None):
            mock_chain.return_value = [MagicMock()]
            resp = client.post(
                "/profile/import-pdf",
                data={"file": (io.BytesIO(b"fake pdf"), "resume.pdf"), "mode": "fresh"},
                content_type="multipart/form-data",
            )

        assert resp.status_code == 502

    def test_text_too_short_returns_422(
        self, client, tmp_profile_path, tmp_config_path, tmp_providers_path, tmp_keys_path
    ):
        """POST /profile/import-pdf returns 422 when extracted text is too short."""
        _write_config(tmp_config_path)

        from unittest.mock import patch
        with patch("app._extract_pdf_text", return_value="Too short"):
            resp = client.post(
                "/profile/import-pdf",
                data={"file": (io.BytesIO(b"fake pdf"), "resume.pdf"), "mode": "fresh"},
                content_type="multipart/form-data",
            )

        assert resp.status_code == 422

    def test_corrupt_pdf_returns_400(
        self, client, tmp_profile_path, tmp_config_path, tmp_providers_path, tmp_keys_path
    ):
        """POST /profile/import-pdf returns 400 when PDF cannot be read."""
        _write_config(tmp_config_path)

        from unittest.mock import patch
        with patch("app._extract_pdf_text", side_effect=ValueError("Could not read PDF")):
            resp = client.post(
                "/profile/import-pdf",
                data={"file": (io.BytesIO(b"fake pdf"), "resume.pdf"), "mode": "fresh"},
                content_type="multipart/form-data",
            )

        assert resp.status_code == 400

    def test_profile_not_saved_by_import(
        self, client, tmp_profile_path, tmp_config_path, tmp_providers_path, tmp_keys_path
    ):
        """Import endpoint does NOT write to profile.json."""
        _write_profile(tmp_profile_path, {"primary_skills": ["Original"]})
        _write_config(tmp_config_path)
        _write_providers(tmp_providers_path)

        llm_response = json.dumps({
            "primary_skills": [{"skill": "Go", "years": 2, "status": "active"}],
            "education": [],
            "seniority": "Junior",
            "preferred_industries": [],
            "location_center": None,
        })

        from unittest.mock import patch, MagicMock
        with patch("app._extract_pdf_text", return_value="A long resume text " * 10), \
             patch("app.build_provider_chain") as mock_chain, \
             patch("app.generate_with_fallback", return_value=(llm_response, "anthropic/claude-haiku-4-5-20251001")):
            mock_chain.return_value = [MagicMock()]
            client.post(
                "/profile/import-pdf",
                data={"file": (io.BytesIO(b"fake pdf"), "resume.pdf"), "mode": "fresh"},
                content_type="multipart/form-data",
            )

        with open(tmp_profile_path, encoding="utf-8") as f:
            saved = json.load(f)
        assert saved["primary_skills"] == ["Original"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_profile_import.py -v`
Expected: FAIL — import functions don't exist yet.

- [ ] **Step 3: Implement the import functions and endpoint in `app.py`**

Add these imports near the top of `app.py` (after existing imports):

```python
from io import BytesIO
from pypdf import PdfReader
from providers import build_provider_chain, generate_with_fallback
from providers.anthropic_provider import strip_fences
```

Add these helper functions before the `/profile` route:

```python
def _extract_pdf_text(pdf_bytes: bytes) -> str:
    """Extract concatenated plaintext from all pages of a PDF.

    Args:
        pdf_bytes: Raw PDF file bytes.

    Returns:
        Concatenated text from all pages.

    Raises:
        ValueError: If the PDF cannot be read.
    """
    try:
        reader = PdfReader(BytesIO(pdf_bytes))
    except Exception as exc:
        raise ValueError(f"Could not read PDF: {exc}") from exc
    return "".join(page.extract_text() or "" for page in reader.pages)


_IMPORT_PROMPT_FRESH = """You are extracting structured profile data from a resume/CV.

RESUME TEXT:
{resume_text}

Extract the following fields and respond with ONLY a JSON object. No explanation, no markdown, no code fences.

The JSON must have exactly these keys:
- "primary_skills": array of objects, each with "skill" (string), "years" (integer estimate), "status" ("active" or "dormant")
- "education": array of strings, each formatted as "Degree, Institution, Year" (e.g. "BS Computer Science, MIT, 2015")
- "seniority": string inferred from job titles (e.g. "Junior", "Mid-level", "Senior", "Staff", "Lead", "Principal")
- "preferred_industries": array of strings inferred from work history (e.g. "fintech", "healthtech", "developer tooling")
- "location_center": string from contact info if present (e.g. "Miami, FL"), or null if not found

If a field cannot be confidently extracted, use an empty array, empty string, or null as appropriate. Do not guess or hallucinate values.

JSON only:"""

_IMPORT_PROMPT_MERGE = """You are extracting structured profile data from a resume/CV to merge with an existing candidate profile.

EXISTING PROFILE:
{current_profile}

RESUME TEXT:
{resume_text}

Extract the following fields and respond with ONLY a JSON object. No explanation, no markdown, no code fences.

The JSON must have exactly these keys:
- "primary_skills": array of objects, each with "skill" (string), "years" (integer estimate), "status" ("active" or "dormant"). Include ALL skills from both the resume and existing profile. Do not remove existing skills.
- "education": array of strings, each formatted as "Degree, Institution, Year". Include entries from both resume and existing profile. Do not duplicate identical entries.
- "seniority": string inferred from job titles. If the existing profile already has a seniority value, keep it unchanged. Only fill this if the existing value is empty.
- "preferred_industries": array of strings inferred from work history. Include industries from both resume and existing profile without duplicates.
- "location_center": string from contact info if present (e.g. "Miami, FL"), or null if not found. If the existing profile has a location, keep it.

If a field cannot be confidently extracted, preserve the existing value. Do not guess or hallucinate values.

JSON only:"""


def _build_import_prompt(
    resume_text: str,
    mode: str,
    current_profile: dict | None,
) -> str:
    """Build the LLM prompt for PDF resume import.

    Args:
        resume_text:     Extracted plaintext from the PDF.
        mode:            ``"fresh"`` or ``"merge"``.
        current_profile: Current profile dict (used in merge mode).

    Returns:
        Fully rendered prompt string.
    """
    if mode == "merge" and current_profile:
        return _IMPORT_PROMPT_MERGE.format(
            current_profile=json.dumps(current_profile, indent=2),
            resume_text=resume_text,
        )
    return _IMPORT_PROMPT_FRESH.format(resume_text=resume_text)


def _parse_import_response(raw: str) -> dict | None:
    """Parse the LLM response for a resume import.

    Strips markdown code fences, parses JSON, and fills missing fields
    with sensible defaults.

    Args:
        raw: Raw LLM response text.

    Returns:
        Parsed dict with all expected import fields, or ``None`` on
        parse failure.
    """
    try:
        cleaned = strip_fences(raw)
        data = json.loads(cleaned)
    except (json.JSONDecodeError, ValueError):
        return None

    # Fill missing fields with defaults.
    data.setdefault("primary_skills", [])
    data.setdefault("education", [])
    data.setdefault("seniority", "")
    data.setdefault("preferred_industries", [])
    data.setdefault("location_center", None)

    return data


def _merge_import_result(current: dict, imported: dict) -> dict:
    """Merge imported profile data with the current profile.

    - Skills: existing skills preserved, new ones appended (formatted as
      ``"skill, Nyr, status"`` strings matching the profile format).
    - Education: new entries appended, duplicates skipped.
    - Seniority: kept if already set; filled from import if empty.
    - Industries: union of existing and imported, deduplicated.
    - Location: kept if already set; filled from import if empty.

    Args:
        current:  Current profile dict from ``profile.json``.
        imported: Parsed import response from ``_parse_import_response()``.

    Returns:
        Merged profile dict ready for form pre-fill.
    """
    result = {}

    # --- Skills ---
    existing_skills = list(current.get("primary_skills", []))
    existing_skill_names = {s.split(",")[0].strip().lower() for s in existing_skills}
    for skill_obj in imported.get("primary_skills", []):
        name = skill_obj.get("skill", "")
        if name.lower() not in existing_skill_names:
            years = skill_obj.get("years", 0)
            status = skill_obj.get("status", "active")
            existing_skills.append(f"{name}, {years}yr, {status}")
            existing_skill_names.add(name.lower())
    result["primary_skills"] = existing_skills

    # --- Education ---
    existing_edu = list(current.get("education", []))
    existing_edu_lower = {e.lower() for e in existing_edu}
    for entry in imported.get("education", []):
        if entry.lower() not in existing_edu_lower:
            existing_edu.append(entry)
            existing_edu_lower.add(entry.lower())
    result["education"] = existing_edu

    # --- Seniority ---
    current_seniority = current.get("seniority", "")
    result["seniority"] = current_seniority if current_seniority else imported.get("seniority", "")

    # --- Industries ---
    existing_industries = list(current.get("preferred_industries", []))
    existing_lower = {i.lower() for i in existing_industries}
    for industry in imported.get("preferred_industries", []):
        if industry.lower() not in existing_lower:
            existing_industries.append(industry)
            existing_lower.add(industry.lower())
    result["preferred_industries"] = existing_industries

    # --- Location ---
    current_location = current.get("location", {})
    current_center = current_location.get("center", "") if isinstance(current_location, dict) else ""
    result["location_center"] = current_center if current_center else imported.get("location_center")

    return result
```

Add the endpoint in `app.py` (before the existing `/profile` route):

```python
@app.route("/profile/import-pdf", methods=["POST"])
def profile_import_pdf():
    """Import profile data from an uploaded PDF resume via LLM extraction.

    Accepts a PDF file and a mode ('fresh' or 'merge'), extracts text,
    sends it through the LLM provider chain, and returns the parsed
    profile data as JSON for client-side form pre-fill.  Does NOT
    write to ``profile.json``.

    Returns:
        JSON response with ``success``, ``profile``, and ``model_used``
        on success, or ``success=False`` with ``error`` on failure.
    """
    # --- Validate file ---
    uploaded = request.files.get("file")
    if not uploaded or not uploaded.filename:
        return jsonify({"success": False, "error": "No file uploaded."}), 400

    if not uploaded.filename.lower().endswith(".pdf"):
        return jsonify({"success": False, "error": "Only PDF files are accepted."}), 400

    mode = request.form.get("mode", "fresh")
    if mode not in ("fresh", "merge"):
        mode = "fresh"

    # --- Extract text ---
    pdf_bytes = uploaded.read()
    try:
        resume_text = _extract_pdf_text(pdf_bytes)
    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400

    if len(resume_text.strip()) < 50:
        return jsonify({
            "success": False,
            "error": "Could not extract meaningful text from this PDF.",
        }), 422

    # --- Build provider chain ---
    providers_dict = _load_providers()
    chain = build_provider_chain(providers_dict)
    if not chain:
        return jsonify({
            "success": False,
            "error": "No LLM provider is configured. Add one in Settings first.",
        }), 503

    # --- Build prompt and call LLM ---
    current_profile = load_profile() if mode == "merge" else None
    prompt = _build_import_prompt(resume_text, mode, current_profile)

    result = generate_with_fallback(prompt, chain, set())
    if result is None:
        return jsonify({
            "success": False,
            "error": "All LLM providers failed. Check your API keys in Settings.",
        }), 502

    raw_text, model_used = result

    # --- Parse response ---
    parsed = _parse_import_response(raw_text)
    if parsed is None:
        return jsonify({
            "success": False,
            "error": "LLM returned an unparseable response. Try again.",
        }), 502

    # --- Apply merge logic if needed ---
    if mode == "merge":
        current = load_profile()
        profile_result = _merge_import_result(current, parsed)
    else:
        # Fresh mode: format skills as strings for form pre-fill
        formatted_skills = []
        for s in parsed.get("primary_skills", []):
            name = s.get("skill", "")
            years = s.get("years", 0)
            status = s.get("status", "active")
            formatted_skills.append(f"{name}, {years}yr, {status}")
        profile_result = {
            "primary_skills": formatted_skills,
            "education": parsed.get("education", []),
            "seniority": parsed.get("seniority", ""),
            "preferred_industries": parsed.get("preferred_industries", []),
            "location_center": parsed.get("location_center"),
        }

    return jsonify({
        "success": True,
        "profile": profile_result,
        "model_used": model_used,
    }), 200
```

Note: `_load_providers()` already exists in `app.py` — it reads and returns the providers.json dict. Verify the exact function name by searching `app.py` for `def _load_providers` or `def load_providers`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_profile_import.py -v`
Expected: All tests PASS.

- [ ] **Step 5: Commit**

```
git add app.py tests/test_profile_import.py
git commit -m "feat: add PDF resume import endpoint and logic

Adds POST /profile/import-pdf with text extraction, LLM prompt
construction, response parsing, and merge logic. Does not save —
returns JSON for client-side form pre-fill.

ref #41"
```

---

### Task 8: Add collapsible import UI to profile template

**Files:**
- Modify: `templates/profile.html`
- Modify: `static/style.css` (if new CSS needed)

- [ ] **Step 1: Add the collapsible import section to `templates/profile.html`**

Insert this block after the `<form>` opening tag (after line 50) and before the first `<div class="provider-row">` (the "Skills & Preferences" section):

```html
    {# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
       Import from Resume (collapsible, collapsed by default)
    ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ #}
    <div class="provider-row import-section">
      <div class="provider-header import-toggle" style="cursor:pointer"
           onclick="this.parentElement.classList.toggle('expanded')">
        <span class="provider-name">Import from Resume</span>
        <span class="import-chevron">&#x25B6;</span>
      </div>

      <div class="import-body">
        <p class="field-hint">
          Upload a PDF resume to pre-fill the profile form below. Review the
          extracted data before saving.
        </p>

        {# Mode selector #}
        <label class="settings-label">Import Mode</label>
        <div class="import-mode-selector">
          <label>
            <input type="radio" name="import_mode" value="fresh" checked>
            Start Fresh
          </label>
          <p class="field-hint">Replaces all populated fields based solely on the PDF.</p>
          <label>
            <input type="radio" name="import_mode" value="merge">
            Merge with existing profile
          </label>
          <p class="field-hint">Adds new entries alongside existing profile data.</p>
        </div>

        {# File input #}
        <label class="settings-label">PDF File</label>
        <input type="file" accept=".pdf" id="import-pdf-file" class="settings-input">

        {# Import button #}
        <button type="button" id="import-pdf-btn" class="btn" disabled>
          Import
        </button>

        {# Status area #}
        <div id="import-status" style="margin-top:0.75rem"></div>
      </div>
    </div>
```

- [ ] **Step 2: Add the client-side JS**

Add this `<script>` block at the bottom of `templates/profile.html`, before `</body>`:

```html
<script>
(function () {
  var fileInput = document.getElementById('import-pdf-file');
  var importBtn = document.getElementById('import-pdf-btn');
  var statusDiv = document.getElementById('import-status');

  // Enable button when a file is selected.
  fileInput.addEventListener('change', function () {
    importBtn.disabled = !fileInput.files.length;
  });

  importBtn.addEventListener('click', function () {
    var file = fileInput.files[0];
    if (!file) return;

    var mode = document.querySelector('input[name="import_mode"]:checked').value;
    var formData = new FormData();
    formData.append('file', file);
    formData.append('mode', mode);

    importBtn.disabled = true;
    importBtn.textContent = 'Importing…';
    statusDiv.innerHTML = '';

    fetch('/profile/import-pdf', { method: 'POST', body: formData })
      .then(function (resp) { return resp.json().then(function (d) { return { ok: resp.ok, data: d }; }); })
      .then(function (result) {
        importBtn.disabled = false;
        importBtn.textContent = 'Import';

        if (!result.ok || !result.data.success) {
          statusDiv.innerHTML = '<p class="save-error">' + (result.data.error || 'Import failed.') + '</p>';
          return;
        }

        var prof = result.data.profile;

        // Fill primary_skills rows.
        _fillRepeatingRows('primary_skills', prof.primary_skills || []);

        // Fill education rows.
        _fillRepeatingRows('education', prof.education || []);

        // Fill preferred_industries rows.
        _fillRepeatingRows('preferred_industries', prof.preferred_industries || []);

        // Fill seniority.
        var seniorityInput = document.querySelector('input[name="seniority"], select[name="seniority"]');
        if (seniorityInput && prof.seniority) seniorityInput.value = prof.seniority;

        // Fill location center.
        var locationInput = document.querySelector('input[name="location_center"]');
        if (locationInput && prof.location_center) locationInput.value = prof.location_center;

        statusDiv.innerHTML = '<p class="save-notice">Profile pre-filled from resume (' +
          result.data.model_used + '). Review the fields below and click Save.</p>';
      })
      .catch(function (err) {
        importBtn.disabled = false;
        importBtn.textContent = 'Import';
        statusDiv.innerHTML = '<p class="save-error">Network error: ' + err.message + '</p>';
      });
  });

  /**
   * Fill a repeating .row-list with values, clearing existing rows first.
   */
  function _fillRepeatingRows(fieldName, values) {
    var list = document.getElementById('list-' + fieldName);
    if (!list) return;

    // Remove all existing rows.
    var rows = list.querySelectorAll('.row-item');
    rows.forEach(function (r) { r.remove(); });

    // Add one row per value (or one empty row if no values).
    var items = values.length ? values : [''];
    items.forEach(function (val) {
      var div = document.createElement('div');
      div.className = 'row-item';
      div.innerHTML =
        '<input class="settings-input" type="text" name="' + fieldName + '[]" value="" autocomplete="off">' +
        '<button type="button" class="btn-row-remove" aria-label="Remove row">&#x2212;</button>';
      div.querySelector('input').value = val;
      list.appendChild(div);
    });
  }
})();
</script>
```

- [ ] **Step 3: Add CSS for the collapsible section**

Add to `static/style.css`:

```css
/* Import section — collapsed by default */
.import-section .import-body {
  display: none;
}
.import-section.expanded .import-body {
  display: block;
}
.import-section.expanded .import-chevron {
  transform: rotate(90deg);
}
.import-chevron {
  float: right;
  transition: transform 0.2s ease;
  font-size: 0.75rem;
  color: var(--text-secondary);
}
.import-mode-selector {
  margin-bottom: 1rem;
}
.import-mode-selector label {
  display: block;
  margin-bottom: 0.25rem;
  cursor: pointer;
}
```

- [ ] **Step 4: Manually verify the UI**

Run: `python app.py`
Navigate to: `http://localhost:5000/profile`

Verify:
- "Import from Resume" section appears at top, collapsed
- Clicking the header expands/collapses it
- File input accepts only `.pdf`
- Import button is disabled until a file is selected
- Selecting a mode and file, then clicking Import shows a spinner/status

- [ ] **Step 5: Commit**

```
git add templates/profile.html static/style.css
git commit -m "feat: add collapsible PDF import UI to profile page

Collapsible section at top of profile form with mode selector
(Start Fresh / Merge), file input, and vanilla JS to pre-fill
form fields from the LLM response.

ref #41"
```

---

### Task 9: Update docs and final verification

**Files:**
- Modify: `docs/STYLE_GUIDE.md` (if collapsible pattern is new)

- [ ] **Step 1: Check if STYLE_GUIDE.md needs updating**

Read `docs/STYLE_GUIDE.md` and check if the collapsible section pattern is already documented. If not, add a brief entry:

```markdown
### Collapsible Sections

Use `.provider-row` with a nested `.import-body` div and a `.import-toggle` header.
Add/remove the `.expanded` class on the parent to show/hide the body.
The chevron (`.import-chevron`) rotates 90° when expanded.
```

- [ ] **Step 2: Run the full test suite**

Run: `pytest tests/test_profile_import.py tests/test_providers.py tests/test_provider_chain.py tests/test_source_json.py -v`
Expected: All tests PASS.

- [ ] **Step 3: Run linter**

Run: `ruff check .`
Expected: No errors.

- [ ] **Step 4: Fix any lint issues and commit**

```
git add docs/STYLE_GUIDE.md
git commit -m "docs: add collapsible section pattern to style guide

ref #41"
```

- [ ] **Step 5: Final commit and PR readiness check**

Verify all changes are committed:
```
git status
git log --oneline main..HEAD
```

Expected: Clean working tree, ~7-9 commits on the branch.

---

## Verification Checklist

After all tasks are complete:

1. `pytest tests/test_providers.py -v` — `generate()` works on all 3 providers, `complete()` regression passes
2. `pytest tests/test_provider_chain.py -v` — `generate_with_fallback()` passes
3. `pytest tests/test_profile_import.py -v` — all import tests pass
4. `ruff check .` — no lint errors
5. Start app → `/profile` → "Import from Resume" section appears collapsed
6. Expand → select mode → upload PDF → form pre-fills → Save works
