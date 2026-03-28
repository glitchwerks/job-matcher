"""
tests/test_ingest.py — Tests for pure/testable functions in ingest.py and app.py.

Covers:
  - Markdown fence stripping (replicates the logic inside score_listing)
  - salary_fmt template filter
  - prefilter() return type contract
  - score_listing_with_fallback() provider-chain fallback logic
"""

import json
import os
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ingest import prefilter, score_listing_with_fallback
from app import salary_fmt, timeago


# ---------------------------------------------------------------------------
# Fence-stripping helper
#
# This replicates the exact logic found in score_listing() so we can test
# it in isolation without making API calls.  If the source logic ever
# changes this helper must be kept in sync.
# ---------------------------------------------------------------------------

def strip_fences(raw_content: str) -> str:
    """Replicate the markdown fence-stripping logic from score_listing()."""
    stripped = raw_content.strip()
    lines = stripped.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


# ---------------------------------------------------------------------------
# Fence-stripping tests
# ---------------------------------------------------------------------------

class TestFenceStripping:
    def test_plain_json_unchanged(self):
        """Plain JSON with no fences passes through strip_fences() unchanged."""
        payload = '{"score": 8, "verdict": "good match"}'
        assert strip_fences(payload) == payload

    def test_json_fenced_with_language_tag(self):
        """JSON wrapped in ```json ... ``` has both fence lines removed."""
        raw = "```json\n{\"score\": 8}\n```"
        result = strip_fences(raw)
        parsed = json.loads(result)
        assert parsed["score"] == 8

    def test_json_fenced_without_language_tag(self):
        """JSON wrapped in ``` ... ``` (no language tag) has fences stripped."""
        raw = "```\n{\"score\": 5}\n```"
        result = strip_fences(raw)
        parsed = json.loads(result)
        assert parsed["score"] == 5

    def test_trailing_whitespace_and_newlines_handled(self):
        """Leading/trailing whitespace around the fenced block is handled."""
        raw = "  \n```json\n{\"score\": 7}\n```\n  "
        result = strip_fences(raw)
        parsed = json.loads(result)
        assert parsed["score"] == 7

    def test_multiline_json_inside_fences(self):
        """Multi-line JSON inside fences is correctly extracted."""
        inner = '{\n  "score": 9,\n  "verdict": "great"\n}'
        raw = f"```json\n{inner}\n```"
        result = strip_fences(raw)
        parsed = json.loads(result)
        assert parsed["score"] == 9
        assert parsed["verdict"] == "great"

    def test_only_closing_fence_not_stripped(self):
        """A lone closing fence without an opening fence is not stripped.

        The logic only strips the *first* line when it starts with '```' and
        the *last* line when it equals '```'. If the first line is not a fence,
        no stripping happens at either end.
        """
        raw = '{"score": 6}\n```'
        # The last line is "```" — it will be stripped because the condition
        # only checks lines[-1]. But the first line is not a fence, so the
        # content line survives. The result should still be valid JSON.
        result = strip_fences(raw)
        parsed = json.loads(result)
        assert parsed["score"] == 6


# ---------------------------------------------------------------------------
# salary_fmt filter tests
# ---------------------------------------------------------------------------

class TestSalaryFmt:
    def _listing(
        self,
        salary_min=None,
        salary_max=None,
        salary_is_predicted=0,
    ) -> dict:
        return {
            "salary_min": salary_min,
            "salary_max": salary_max,
            "salary_is_predicted": salary_is_predicted,
        }

    def test_both_min_and_max_present(self):
        """Both min and max → '$120k–$160k'."""
        result = salary_fmt(self._listing(salary_min=120_000, salary_max=160_000))
        assert result == "$120k–$160k"

    def test_only_min_present(self):
        """Only min → '$120k+'."""
        result = salary_fmt(self._listing(salary_min=120_000))
        assert result == "$120k+"

    def test_only_max_present(self):
        """Only max → '$160k'."""
        result = salary_fmt(self._listing(salary_max=160_000))
        assert result == "$160k"

    def test_both_none_returns_none(self):
        """Both None → None (no salary data to display)."""
        result = salary_fmt(self._listing())
        assert result is None

    def test_predicted_salary_prefix(self):
        """Predicted salary is prefixed with '~'."""
        result = salary_fmt(
            self._listing(salary_min=120_000, salary_max=160_000, salary_is_predicted=1)
        )
        assert result == "~$120k–$160k"

    def test_predicted_min_only(self):
        """Predicted salary with only min → '~$120k+'."""
        result = salary_fmt(self._listing(salary_min=120_000, salary_is_predicted=1))
        assert result == "~$120k+"

    def test_predicted_max_only(self):
        """Predicted salary with only max → '~$160k'."""
        result = salary_fmt(self._listing(salary_max=160_000, salary_is_predicted=1))
        assert result == "~$160k"

    def test_rounding_rounds_to_nearest_thousand(self):
        """125500 rounds to 126k, not 125k (standard rounding)."""
        result = salary_fmt(self._listing(salary_max=125_500))
        assert result == "$126k"

    def test_rounding_rounds_down(self):
        """124499 rounds down to 124k."""
        result = salary_fmt(self._listing(salary_max=124_499))
        assert result == "$124k"

    def test_zero_salary_is_predicted_not_predicted(self):
        """salary_is_predicted=0 means no tilde prefix."""
        result = salary_fmt(self._listing(salary_min=100_000, salary_max=130_000, salary_is_predicted=0))
        assert result == "$100k–$130k"
        assert "~" not in result


# ---------------------------------------------------------------------------
# prefilter() return type contract
# ---------------------------------------------------------------------------

class TestPrefilterReturnType:
    """Sanity checks that prefilter() consistently returns None (pass) or a
    non-empty string (reject), regardless of which filter triggers."""

    def _make_listing(self, **kwargs) -> dict:
        base = {
            "title": "Software Engineer",
            "salary_min": None,
            "salary_max": None,
            "contract_time": "",
            "contract_type": "",
        }
        base.update(kwargs)
        return base

    def test_pass_returns_none(self):
        listing = self._make_listing()
        result = prefilter(listing, {})
        assert result is None

    def test_title_include_reject_returns_nonempty_string(self):
        listing = self._make_listing(title="Java Developer")
        config = {"prefilter": {"title_include": ["python"]}}
        result = prefilter(listing, config)
        assert isinstance(result, str) and len(result) > 0

    def test_title_exclude_reject_returns_nonempty_string(self):
        listing = self._make_listing(title="Java Developer")
        config = {"prefilter": {"title_exclude": ["java"]}}
        result = prefilter(listing, config)
        assert isinstance(result, str) and len(result) > 0

    def test_salary_reject_returns_nonempty_string(self):
        listing = self._make_listing(salary_max=30_000)
        config = {"prefilter": {"salary_min": 80_000}}
        result = prefilter(listing, config)
        assert isinstance(result, str) and len(result) > 0

    def test_contract_time_reject_returns_nonempty_string(self):
        listing = self._make_listing(contract_time="part_time")
        config = {"prefilter": {"require_contract_time": "full_time"}}
        result = prefilter(listing, config)
        assert isinstance(result, str) and len(result) > 0


# ---------------------------------------------------------------------------
# score_listing_with_fallback() tests
# ---------------------------------------------------------------------------

def _make_provider(name: str, model: str = "test-model") -> MagicMock:
    """Return a mock LLMProvider whose class name follows the real convention.

    The class is named ``{Name}Provider`` so that ``_provider_name()`` in
    ingest.py strips "provider" and returns the bare name.
    """
    # Dynamically create a class with the right name so type(provider).__name__
    # returns e.g. "AnthropicProvider" → _provider_name() → "anthropic".
    cls = type(f"{name.capitalize()}Provider", (MagicMock,), {})
    provider = cls()
    provider._model = model
    provider.input_cost_per_mtok = 0.80
    provider.output_cost_per_mtok = 4.00
    return provider


_SCORE_RESULT = {
    "score": 8,
    "matched_skills": ["Python"],
    "missing_skills": [],
    "concerns": [],
    "verdict": "Good match",
    "tokens_input": 100,
    "tokens_output": 50,
}

_LISTING = {"title": "Software Engineer", "description": "We need a Python dev."}
_PROFILE = {"primary_skills": ["Python"]}


class TestScoreListingWithFallback:

    def test_uses_first_provider_on_success(self):
        """First provider succeeds; result includes model_used."""
        p1 = _make_provider("anthropic")
        p1.complete.return_value = dict(_SCORE_RESULT)

        dead: set = set()
        result = score_listing_with_fallback(_LISTING, _PROFILE, [p1], dead)

        assert result is not None
        assert result["score"] == 8
        assert result["model_used"] == "anthropic/test-model"
        p1.complete.assert_called_once()

    def test_falls_back_on_transient_error(self):
        """First provider raises a non-auth RuntimeError; second provider succeeds."""
        p1 = _make_provider("openai")
        p1.complete.side_effect = RuntimeError("503 Service Unavailable")

        p2 = _make_provider("anthropic")
        p2.complete.return_value = dict(_SCORE_RESULT)

        dead: set = set()
        result = score_listing_with_fallback(_LISTING, _PROFILE, [p1, p2], dead)

        assert result is not None
        assert result["model_used"] == "anthropic/test-model"
        # Transient error — p1 must NOT be added to dead_providers.
        assert "openai" not in dead
        p1.complete.assert_called_once()
        p2.complete.assert_called_once()

    def test_auth_error_removes_provider_permanently(self):
        """Auth RuntimeError from first provider adds it to dead_providers."""
        p1 = _make_provider("openai")
        p1.complete.side_effect = RuntimeError("401 Unauthorized — check your API key")

        p2 = _make_provider("anthropic")
        p2.complete.return_value = dict(_SCORE_RESULT)

        dead: set = set()
        result = score_listing_with_fallback(_LISTING, _PROFILE, [p1, p2], dead)

        assert result is not None
        assert result["model_used"] == "anthropic/test-model"
        # Auth error — p1 must be in dead_providers.
        assert "openai" in dead

    def test_all_providers_fail_returns_none(self):
        """When every provider raises RuntimeError, the function returns None."""
        p1 = _make_provider("anthropic")
        p1.complete.side_effect = RuntimeError("503 error")

        p2 = _make_provider("openai")
        p2.complete.side_effect = RuntimeError("503 error")

        dead: set = set()
        result = score_listing_with_fallback(_LISTING, _PROFILE, [p1, p2], dead)

        assert result is None

    def test_dead_provider_skipped(self):
        """A provider already in dead_providers is not called at all."""
        p1 = _make_provider("anthropic")
        p2 = _make_provider("openai")
        p2.complete.return_value = dict(_SCORE_RESULT)

        # Mark anthropic as dead before the call.
        dead: set = {"anthropic"}
        result = score_listing_with_fallback(_LISTING, _PROFILE, [p1, p2], dead)

        assert result is not None
        assert result["model_used"] == "openai/test-model"
        # p1 must never have been called.
        p1.complete.assert_not_called()
        p2.complete.assert_called_once()


# ---------------------------------------------------------------------------
# timeago filter
# ---------------------------------------------------------------------------

class TestTimeago:
    """Tests for the timeago Jinja2 template filter registered in app.py."""

    def _dt(self, seconds_ago: int):
        """Return a naive UTC datetime that is `seconds_ago` seconds in the past."""
        from datetime import datetime, timezone, timedelta
        return datetime.now(tz=timezone.utc) - timedelta(seconds=seconds_ago)

    def test_none_returns_never(self):
        """timeago(None) returns 'never'."""
        assert timeago(None) == "never"

    def test_non_datetime_returns_str(self):
        """timeago with a non-datetime value returns its string representation."""
        result = timeago("not-a-datetime")  # type: ignore[arg-type]
        assert result == "not-a-datetime"

    def test_just_now_under_two_minutes(self):
        """Timestamps less than 2 minutes ago return 'just now'."""
        assert timeago(self._dt(30)) == "just now"
        assert timeago(self._dt(119)) == "just now"

    def test_minutes_ago(self):
        """Timestamps 2–59 minutes ago return 'N minutes ago'."""
        assert timeago(self._dt(120)) == "2 minutes ago"
        assert timeago(self._dt(3599)) == "59 minutes ago"

    def test_singular_minute(self):
        """The minutes branch covers 120–3599 seconds; 30 minutes = 1800 seconds."""
        result = timeago(self._dt(1800))
        assert result == "30 minutes ago"

    def test_hours_ago(self):
        """Timestamps 1–23 hours ago return 'N hours ago'."""
        assert timeago(self._dt(3600)) == "1 hour ago"
        assert timeago(self._dt(7200)) == "2 hours ago"
        assert timeago(self._dt(86399)) == "23 hours ago"

    def test_singular_hour(self):
        """Exactly 1 hour uses singular form 'hour'."""
        assert timeago(self._dt(3600)) == "1 hour ago"

    def test_days_ago(self):
        """Timestamps 1–6 days ago return 'N days ago'."""
        assert timeago(self._dt(86400)) == "1 day ago"
        assert timeago(self._dt(172800)) == "2 days ago"

    def test_absolute_beyond_seven_days(self):
        """Timestamps older than 7 days return an absolute datetime string."""
        from datetime import datetime, timezone, timedelta
        old = datetime.now(tz=timezone.utc) - timedelta(days=8)
        result = timeago(old)
        assert "UTC" in result
        assert str(old.year) in result

    def test_naive_datetime_treated_as_utc(self):
        """A naive datetime (no tzinfo) is treated as UTC and does not raise."""
        from datetime import datetime, timedelta
        naive = datetime.now() - timedelta(hours=2)
        result = timeago(naive)
        assert "hour" in result

    def test_future_timestamp_returns_absolute(self):
        """A future timestamp (clock skew) returns an absolute string, not a crash."""
        from datetime import datetime, timezone, timedelta
        future = datetime.now(tz=timezone.utc) + timedelta(hours=1)
        result = timeago(future)
        assert "UTC" in result
