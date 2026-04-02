"""
providers/base.py — Abstract base class for LLM scoring providers.

All concrete providers must implement ``complete()``, ``input_cost_per_mtok``,
and ``output_cost_per_mtok``.  The ``complete()`` method receives a fully
rendered prompt string and must return a dict whose keys match the JSON
contract expected by ``score_listing()``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class LLMProvider(ABC):
    """Interface that every LLM backend must satisfy.

    Concrete sub-classes encapsulate provider-specific SDK calls, retry
    logic, token-count extraction, and pricing constants so that
    ``score_listing()`` in ``ingest.py`` remains provider-agnostic.
    """

    @abstractmethod
    def complete(self, prompt: str) -> dict:
        """Send *prompt* to the LLM and return a parsed scoring result.

        Implementors must:
        * Attempt the API call up to 2 times (2-second delay between attempts).
        * Strip markdown code fences from the raw response before JSON parsing.
        * Validate that the returned dict contains all five required keys.
        * Raise ``RuntimeError`` if both attempts fail so that callers can
          treat ``None`` as "definitive failure" rather than catching
          exceptions themselves.

        Args:
            prompt: Fully rendered prompt string (profile + job description).

        Returns:
            Dict with exactly these keys:

            * ``score``          — int, 0–10
            * ``matched_skills`` — list[str]
            * ``missing_skills`` — list[str]
            * ``concerns``       — list[str]
            * ``verdict``        — str (one sentence)
            * ``tokens_input``   — int | None
            * ``tokens_output``  — int | None

        Raises:
            RuntimeError: If all retry attempts fail.
        """
        ...

    @property
    @abstractmethod
    def input_cost_per_mtok(self) -> float:
        """USD cost per million input tokens for the configured model."""
        ...

    @property
    @abstractmethod
    def output_cost_per_mtok(self) -> float:
        """USD cost per million output tokens for the configured model."""
        ...

    @classmethod
    @abstractmethod
    def settings_schema(cls) -> dict:
        """Return the settings schema for this provider.

        The returned dict describes the credentials and configuration fields
        that the Settings UI should render for this provider.

        Returns:
            Dict with exactly two keys:

            * ``display_name`` — str, human-readable name shown in the UI.
            * ``fields``       — list of field dicts.  Each field dict must
              have: ``name`` (str), ``label`` (str), ``type`` (``"text"``
              or ``"password"``), ``required`` (bool).  Text fields may
              also include a ``default`` (str) for pre-populated values.
        """
        ...

    @classmethod
    @abstractmethod
    def validate_credentials(cls, api_key: str, model: str) -> tuple[str, str | None]:
        """Validate *api_key* and *model* by making a minimal live API call.

        Implementations should make the cheapest possible test call (e.g.
        ``max_tokens=1``) to confirm that the key is accepted and the model
        name is recognised.  The key must never appear in any return value
        or log output.

        Args:
            api_key: Provider API key string.
            model:   Provider model name string.

        Returns:
            A ``(state, detail)`` tuple where *state* is one of:

            * ``"valid"``          — key and model accepted.
            * ``"invalid_key"``    — authentication or permission error.
            * ``"unknown_model"``  — key valid but model not found.
            * ``"unreachable"``    — network error or unexpected exception.

            *detail* is a short human-readable string describing the failure
            (trimmed to 200 chars, newlines removed), or ``None`` on success.
            The raw api_key value must never appear in *detail*.

        Raises:
            NotImplementedError: If not overridden by the concrete subclass.
        """
        raise NotImplementedError
