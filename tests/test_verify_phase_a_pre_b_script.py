"""Structural smoke tests for scripts/verify_phase_a_pre_b.ps1.

These tests do not execute the PowerShell script. They verify that:
1. The script file exists (catches accidental deletion/rename).
2. The param block declares the four expected parameters.
3. The script body references all four step labels used in the summary table.

No DB connection, no subprocess execution. Pure file-system assertions.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_WORKTREE_ROOT = Path(__file__).parent.parent
_SCRIPT_PATH = _WORKTREE_ROOT / "scripts" / "verify_phase_a_pre_b.ps1"

# The four step labels that must appear in the script (used in the summary table).
_REQUIRED_STEP_LABELS = [
    "Source-string fixture refresh",
    "Pre-aggregator baseline",
    "Live ingest smoke run",
    "Post-aggregator baseline",
]

# The four parameters the script must declare.
_REQUIRED_PARAMS = [
    "-DatabaseUrl",
    "-WorktreeRoot",
    "-SkipSmoke",
]


class TestVerifyPhaseAPreBScriptExists:
    """The script file must exist at the expected path."""

    def test_script_file_exists(self) -> None:
        assert _SCRIPT_PATH.exists(), (
            f"Script not found: {_SCRIPT_PATH}. "
            "Has it been deleted or renamed?"
        )

    def test_script_is_nonempty(self) -> None:
        assert _SCRIPT_PATH.stat().st_size > 0, (
            f"Script is empty: {_SCRIPT_PATH}"
        )


class TestVerifyPhaseAPreBScriptParamBlock:
    """The param block must declare all required parameters."""

    @pytest.fixture(scope="class")
    def script_content(self) -> str:
        return _SCRIPT_PATH.read_text(encoding="utf-8")

    @pytest.mark.parametrize("param", _REQUIRED_PARAMS)
    def test_param_declared(self, script_content: str, param: str) -> None:
        assert param in script_content, (
            f"Required param '{param}' not found in {_SCRIPT_PATH.name}"
        )

    def test_cmdletbinding_present(self, script_content: str) -> None:
        assert "[CmdletBinding()]" in script_content, (
            f"[CmdletBinding()] not found in {_SCRIPT_PATH.name}; "
            "required for -Verbose support"
        )

    def test_error_action_preference_stop(self, script_content: str) -> None:
        assert "ErrorActionPreference" in script_content, (
            f"$ErrorActionPreference not set in {_SCRIPT_PATH.name}; "
            "script must set it to 'Stop'"
        )


class TestVerifyPhaseAPreBScriptStepLabels:
    """The script must reference all four step labels in its summary table."""

    @pytest.fixture(scope="class")
    def script_content(self) -> str:
        return _SCRIPT_PATH.read_text(encoding="utf-8")

    @pytest.mark.parametrize("label", _REQUIRED_STEP_LABELS)
    def test_step_label_present(self, script_content: str, label: str) -> None:
        assert label in script_content, (
            f"Step label '{label}' not found in {_SCRIPT_PATH.name}; "
            "the summary table may be incomplete or misspelled"
        )


class TestVerifyPhaseAPreBScriptMarkers:
    """The script must reference the key log markers used in step 3."""

    @pytest.fixture(scope="class")
    def script_content(self) -> str:
        return _SCRIPT_PATH.read_text(encoding="utf-8")

    def test_aggregator_marker_referenced(self, script_content: str) -> None:
        """Script must grep for the JobAggregatorProvider routing marker."""
        assert "JobAggregatorProvider" in script_content, (
            f"JobAggregatorProvider marker not referenced in {_SCRIPT_PATH.name}"
        )

    def test_legacy_provider_marker_referenced(self, script_content: str) -> None:
        """Script must check that non-arbeitnow sources appear in the log."""
        # The legacy path is confirmed by 'Fetching from source:' lines for
        # sources other than arbeitnow (there is no explicit LegacyInTreeProvider
        # log line at runtime). The script must reference this pattern.
        assert "Fetching from source" in script_content, (
            f"'Fetching from source' log pattern not referenced in {_SCRIPT_PATH.name}; "
            "step 3 must verify legacy sources were fetched"
        )

    def test_job_aggregator_sources_env_var_referenced(
        self, script_content: str
    ) -> None:
        """Script must set JOB_AGGREGATOR_SOURCES env var for the smoke run."""
        assert "JOB_AGGREGATOR_SOURCES" in script_content, (
            f"JOB_AGGREGATOR_SOURCES env var not set in {_SCRIPT_PATH.name}"
        )


class TestVerifyPhaseAPreBScriptRedaction:
    """The password redaction regex must be robust to @ characters in the password."""

    @pytest.fixture(scope="class")
    def script_content(self) -> str:
        return _SCRIPT_PATH.read_text(encoding="utf-8")

    def test_redaction_regex_uses_host_anchor(self, script_content: str) -> None:
        """Regex suffix must use [^/?@]+ to anchor at the LAST @ before the host.

        The naive pattern [^@]+ for the password portion stops at the FIRST @,
        so a password containing @ would cause the match to fail and the real
        password would appear in logs. The correct pattern anchors the suffix
        host segment with [^/?@]+ so the replacement always consumes everything
        between the credentials colon and the final @ regardless of password content.
        """
        assert "[^/?@]+" in script_content, (
            f"Redaction regex in {_SCRIPT_PATH.name} does not use '[^/?@]+' for "
            "the host-segment anchor. This guard exists because the naive "
            "'[^@]+' pattern leaks passwords that contain '@'. "
            "Use: -replace '(?<prefix>postgresql://[^:/@]+:).+(?<suffix>@[^/?@]+)'"
        )
