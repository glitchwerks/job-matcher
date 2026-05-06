"""Structural smoke tests for scripts/verify_phase_a_pre_b.ps1.

These tests do not execute the PowerShell script end-to-end. They verify:
1. The script file exists (catches accidental deletion/rename).
2. The param block declares the four expected parameters.
3. The script body references all four step labels used in the summary table.
4. The script parses cleanly under pwsh (regression for #357 array-literal bug).

Tests 1-3 are pure file-system assertions (no DB, no subprocess).
Test 4 invokes pwsh only if it is on PATH; skipped otherwise so Linux CI
without pwsh does not false-fail.
"""

from __future__ import annotations

import shutil
import subprocess
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


class TestVerifyPhaseAPreBScriptParses:
    """The script must parse without errors under pwsh (regression for #357).

    This class uses pwsh to parse the script as a ScriptBlock without
    executing it, catching array-literal syntax errors like adjacent string
    tokens on the same line inside @(...).

    The test is skipped when pwsh is not on PATH so Linux CI without
    PowerShell does not false-fail.
    """

    def test_script_parses_without_errors(self) -> None:
        """Script must parse cleanly as a PowerShell ScriptBlock.

        Uses [scriptblock]::Create(...) which performs a full parse without
        executing any code. A parser error causes $Error to be non-empty and
        the command exits with code 1.

        Raises:
            pytest.skip.Exception: If pwsh is not found on PATH.
        """
        pwsh = shutil.which("pwsh")
        if pwsh is None:
            pytest.skip("pwsh not on PATH — skipping parse validation")

        script_path = str(_SCRIPT_PATH.resolve())
        parse_command = (
            "[scriptblock]::Create("
            "(Get-Content -Raw -Path '"
            + script_path.replace("'", "''")
            + "')) | Out-Null; "
            "if ($Error) { exit 1 }"
        )
        result = subprocess.run(
            [pwsh, "-NoProfile", "-NoLogo", "-Command", parse_command],
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == 0, (
            f"pwsh parser rejected {_SCRIPT_PATH.name}.\n"
            f"stderr: {result.stderr.strip()}\n"
            f"stdout: {result.stdout.strip()}"
        )
        assert result.stderr.strip() == "", (
            f"pwsh emitted unexpected stderr while parsing "
            f"{_SCRIPT_PATH.name}:\n{result.stderr.strip()}"
        )
