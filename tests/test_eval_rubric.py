"""
tests/test_eval_rubric.py — Unit tests for scripts/eval_rubric.py.

Covers:
  - _validate_rubric_response() — schema validation
  - _truncate() — text truncation
  - _prepare_scoring_profile() — profile transformation
  - _fetch_stratified_sample() — stratified sampling from DB
  - _score_old() — old prompt scoring with error handling
  - _score_rubric() — new rubric scoring with validation and weighting
  - Constants: weights, valid recommendations
"""

import json
import os
import sys
from unittest.mock import MagicMock, patch
from typing import Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scripts.eval_rubric import (
    _validate_rubric_response,
    _truncate,
    _prepare_scoring_profile,
    _fetch_stratified_sample,
    _score_old,
    _score_rubric,
    _SKILLS_WEIGHT,
    _ROLE_FIT_WEIGHT,
    _VALID_RECOMMENDATIONS,
)


# ---------------------------------------------------------------------------
# _truncate() tests
# ---------------------------------------------------------------------------


def test_truncate_short_text_unchanged():
    """Text shorter than max_len is returned unchanged."""
    assert _truncate("short", max_len=50) == "short"


def test_truncate_exact_boundary():
    """Text exactly at max_len is returned unchanged."""
    text = "a" * 50
    assert _truncate(text, max_len=50) == text


def test_truncate_over_limit():
    """Text longer than max_len is truncated with '...'."""
    text = "a" * 60
    result = _truncate(text, max_len=50)
    assert len(result) == 50
    assert result.endswith("...")
    assert result == "a" * 47 + "..."


def test_truncate_custom_max_len():
    """Custom max_len parameter is respected."""
    text = "verylongtext"
    result = _truncate(text, max_len=10)
    assert len(result) == 10
    assert result == "verylon..."


def test_truncate_empty_string():
    """Empty string returns empty string."""
    assert _truncate("", max_len=50) == ""


def test_truncate_default_max_len():
    """Default max_len is 50 characters."""
    text = "a" * 60
    result = _truncate(text)
    assert len(result) == 50
    assert result.endswith("...")


# ---------------------------------------------------------------------------
# _validate_rubric_response() tests
# ---------------------------------------------------------------------------


def _make_valid_rubric() -> dict:
    """Return a minimally valid rubric response."""
    return {
        "dimensions": {
            "skills_match": 8,
            "role_fit": 7,
            "red_flags": 9,
        },
        "hiring_assessment": "Ideal candidate",
        "role_fit_assessment": "Exact match",
        "deal_breakers": [],
        "matched_skills": ["Python"],
        "missing_skills": [],
        "concerns": [],
        "archetype": "Backend Engineer",
        "apply_recommendation": "Strong Yes",
        "verdict": "Good match.",
    }


def test_validate_rubric_response_valid():
    """Valid response with all required keys passes."""
    result = _validate_rubric_response(_make_valid_rubric())
    assert result is None


def test_validate_rubric_response_missing_top_level_key():
    """Missing a required top-level key returns error."""
    rubric = _make_valid_rubric()
    del rubric["verdict"]
    result = _validate_rubric_response(rubric)
    assert result is not None
    assert "Missing keys" in result


def test_validate_rubric_response_missing_multiple_keys():
    """Missing multiple keys reports all in error."""
    rubric = _make_valid_rubric()
    del rubric["verdict"]
    del rubric["archetype"]
    result = _validate_rubric_response(rubric)
    assert result is not None
    assert "Missing keys" in result


def test_validate_rubric_response_dimensions_not_dict():
    """dimensions must be a dict."""
    rubric = _make_valid_rubric()
    rubric["dimensions"] = [8, 7, 9]
    result = _validate_rubric_response(rubric)
    assert result is not None
    assert "'dimensions' must be a dict" in result


def test_validate_rubric_response_missing_dimension_key():
    """dimensions missing a required key returns error."""
    rubric = _make_valid_rubric()
    del rubric["dimensions"]["role_fit"]
    result = _validate_rubric_response(rubric)
    assert result is not None
    assert "'dimensions' missing keys" in result


def test_validate_rubric_response_dimension_out_of_range_high():
    """dimensions value > 10 is invalid."""
    rubric = _make_valid_rubric()
    rubric["dimensions"]["skills_match"] = 11
    result = _validate_rubric_response(rubric)
    assert result is not None
    assert "must be a number 0–10" in result


def test_validate_rubric_response_dimension_out_of_range_low():
    """dimensions value < 0 is invalid."""
    rubric = _make_valid_rubric()
    rubric["dimensions"]["role_fit"] = -1
    result = _validate_rubric_response(rubric)
    assert result is not None
    assert "must be a number 0–10" in result


def test_validate_rubric_response_dimension_boundary_zero():
    """dimensions value of 0 is valid."""
    rubric = _make_valid_rubric()
    rubric["dimensions"]["red_flags"] = 0
    result = _validate_rubric_response(rubric)
    assert result is None


def test_validate_rubric_response_dimension_boundary_ten():
    """dimensions value of 10 is valid."""
    rubric = _make_valid_rubric()
    rubric["dimensions"]["skills_match"] = 10
    result = _validate_rubric_response(rubric)
    assert result is None


def test_validate_rubric_response_dimension_float():
    """dimensions can be floats."""
    rubric = _make_valid_rubric()
    rubric["dimensions"]["skills_match"] = 8.5
    result = _validate_rubric_response(rubric)
    assert result is None


def test_validate_rubric_response_dimension_string_invalid():
    """dimensions value as string is invalid."""
    rubric = _make_valid_rubric()
    rubric["dimensions"]["skills_match"] = "8"
    result = _validate_rubric_response(rubric)
    assert result is not None
    assert "must be a number 0–10" in result


def test_validate_rubric_response_invalid_recommendation():
    """apply_recommendation must be one of the valid set."""
    rubric = _make_valid_rubric()
    rubric["apply_recommendation"] = "Invalid"
    result = _validate_rubric_response(rubric)
    assert result is not None
    assert "apply_recommendation" in result


def test_validate_rubric_response_all_valid_recommendations():
    """All valid recommendations pass validation."""
    for rec in _VALID_RECOMMENDATIONS:
        rubric = _make_valid_rubric()
        rubric["apply_recommendation"] = rec
        result = _validate_rubric_response(rubric)
        assert result is None, f"Recommendation {rec} should be valid"


# ---------------------------------------------------------------------------
# _prepare_scoring_profile() tests
# ---------------------------------------------------------------------------


@patch("scripts.eval_rubric.format_skills_for_prompt")
@patch("scripts.eval_rubric.format_education_for_prompt")
@patch("scripts.eval_rubric._generate_location_notes")
def test_prepare_scoring_profile_location_block_present(
    mock_gen_loc, mock_fmt_ed, mock_fmt_sk
):
    """Profile with nested location block is converted to location_notes."""
    mock_fmt_sk.side_effect = lambda p: p
    mock_fmt_ed.side_effect = lambda p: p
    mock_gen_loc.return_value = "Miami, FL (25 km radius)"

    profile = {
        "primary_skills": [{"description": "Python", "years_active": 5}],
        "location": {
            "center": "Miami, FL",
            "radius_km": 25,
        },
        "seniority": "senior",
    }

    result = _prepare_scoring_profile(profile)

    # location block should be removed
    assert "location" not in result
    # location_notes should be added
    assert "location_notes" in result
    assert result["location_notes"] == "Miami, FL (25 km radius)"
    # other fields preserved
    assert result["seniority"] == "senior"


@patch("scripts.eval_rubric.format_skills_for_prompt")
@patch("scripts.eval_rubric.format_education_for_prompt")
@patch("scripts.eval_rubric._generate_location_notes")
def test_prepare_scoring_profile_location_notes_in_block(
    mock_gen_loc, mock_fmt_ed, mock_fmt_sk
):
    """Profile location block with explicit notes uses those notes."""
    mock_fmt_sk.side_effect = lambda p: p
    mock_fmt_ed.side_effect = lambda p: p

    profile = {
        "location": {
            "notes": "Prefer West Coast",
            "center": "ignored",
            "radius_km": 0,
        },
    }

    result = _prepare_scoring_profile(profile)

    # Should use explicit notes, not generate
    assert result["location_notes"] == "Prefer West Coast"
    mock_gen_loc.assert_not_called()


@patch("scripts.eval_rubric.format_skills_for_prompt")
@patch("scripts.eval_rubric.format_education_for_prompt")
@patch("scripts.eval_rubric._generate_location_notes")
def test_prepare_scoring_profile_no_location(
    mock_gen_loc, mock_fmt_ed, mock_fmt_sk
):
    """Profile without location block is left unchanged."""
    mock_fmt_sk.side_effect = lambda p: p
    mock_fmt_ed.side_effect = lambda p: p
    mock_gen_loc.return_value = None  # Return None when location is absent

    profile = {
        "primary_skills": [{"description": "Python"}],
        "seniority": "mid",
    }

    result = _prepare_scoring_profile(profile)

    # location_notes should not be added if location is absent and no notes generated
    assert "location_notes" not in result
    assert "location" not in result


@patch("scripts.eval_rubric.format_skills_for_prompt")
@patch("scripts.eval_rubric.format_education_for_prompt")
@patch("scripts.eval_rubric._generate_location_notes")
def test_prepare_scoring_profile_calls_format_functions(
    mock_gen_loc, mock_fmt_ed, mock_fmt_sk
):
    """Profile is passed through format_skills and format_education."""
    # These functions should be called and return modified profiles
    def fmt_sk(p):
        p["_sk_formatted"] = True
        return p

    def fmt_ed(p):
        p["_ed_formatted"] = True
        return p

    mock_fmt_sk.side_effect = fmt_sk
    mock_fmt_ed.side_effect = fmt_ed
    mock_gen_loc.return_value = None

    profile = {"primary_skills": [], "education": []}
    result = _prepare_scoring_profile(profile)

    assert result.get("_sk_formatted") is True
    assert result.get("_ed_formatted") is True


@patch("scripts.eval_rubric.format_skills_for_prompt")
@patch("scripts.eval_rubric.format_education_for_prompt")
@patch("scripts.eval_rubric._generate_location_notes")
def test_prepare_scoring_profile_original_unchanged(
    mock_gen_loc, mock_fmt_ed, mock_fmt_sk
):
    """Original profile dict is not mutated."""
    mock_fmt_sk.side_effect = lambda p: p
    mock_fmt_ed.side_effect = lambda p: p
    mock_gen_loc.return_value = "notes"

    profile = {
        "location": {"center": "SF"},
        "seniority": "junior",
    }
    profile_copy = dict(profile)

    _prepare_scoring_profile(profile)

    # Original should be unchanged (though top-level keys might exist in both)
    assert profile == profile_copy


# ---------------------------------------------------------------------------
# _fetch_stratified_sample() tests
# ---------------------------------------------------------------------------


def test_fetch_stratified_sample_all_tiers():
    """Fetches from all three tiers and returns combined results."""
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

    # Mock psycopg2.extras.RealDictCursor to return dicts
    def mock_execute(query, params):
        if "score >= 8" in query:
            return [{"id": 1, "title": "High", "score": 9}]
        elif "score >= 5 AND score < 8" in query:
            return [{"id": 2, "title": "Mid", "score": 6}]
        elif "score < 5" in query:
            return [{"id": 3, "title": "Low", "score": 3}]

    mock_cursor.execute.side_effect = mock_execute
    mock_cursor.fetchall.side_effect = lambda: mock_execute(
        mock_cursor.execute.call_args[0][0],
        mock_cursor.execute.call_args[0][1],
    )

    with patch("scripts.eval_rubric.psycopg2.extras.RealDictCursor"):
        results = _fetch_stratified_sample(mock_conn, high_n=1, mid_n=1, low_n=1)

    assert len(results) >= 3  # At least one from each tier


def test_fetch_stratified_sample_respects_limits():
    """Each tier query has correct limit parameter."""
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    mock_cursor.fetchall.return_value = []

    with patch("scripts.eval_rubric.psycopg2.extras.RealDictCursor"):
        _fetch_stratified_sample(mock_conn, high_n=5, mid_n=10, low_n=15)

    # Should call execute 3 times (one per tier) with the limits
    calls = mock_cursor.execute.call_args_list
    assert len(calls) == 3
    # Verify limits are in the params (second arg to each execute call)
    limits = [call[0][1][0] for call in calls]
    assert 5 in limits
    assert 10 in limits
    assert 15 in limits


def test_fetch_stratified_sample_empty_tier():
    """Returns what's available even if a tier is empty."""
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

    call_count = [0]

    def mock_fetchall():
        call_count[0] += 1
        if call_count[0] == 1:  # High tier
            return [{"id": 1, "title": "H", "score": 9}]
        elif call_count[0] == 2:  # Mid tier
            return []  # Empty
        else:  # Low tier
            return [{"id": 3, "title": "L", "score": 2}]

    mock_cursor.fetchall.side_effect = mock_fetchall

    with patch("scripts.eval_rubric.psycopg2.extras.RealDictCursor"):
        results = _fetch_stratified_sample(mock_conn, 2, 2, 2)

    # Should still return results from high and low
    assert len(results) >= 2


# ---------------------------------------------------------------------------
# _score_old() tests
# ---------------------------------------------------------------------------


def test_score_old_success():
    """_score_old returns result from provider.complete()."""
    mock_provider = MagicMock()
    expected_result = {
        "score": 8,
        "matched_skills": ["Python"],
        "missing_skills": [],
        "concerns": [],
        "verdict": "Good match",
    }
    mock_provider.complete.return_value = expected_result

    result = _score_old(
        description="Job description",
        profile_json='{"skills": []}',
        provider=mock_provider,
        verbose=False,
    )

    assert result == expected_result
    # Verify provider.complete was called
    assert mock_provider.complete.called
    call_args = mock_provider.complete.call_args[0][0]
    assert "Job description" in call_args
    assert '{"skills": []}' in call_args


def test_score_old_runtime_error_returns_none():
    """_score_old returns None when provider raises RuntimeError."""
    mock_provider = MagicMock()
    mock_provider.complete.side_effect = RuntimeError("API timeout")

    result = _score_old(
        description="description",
        profile_json="{}",
        provider=mock_provider,
    )

    assert result is None


def test_score_old_verbose_prints_response(capsys):
    """_score_old with verbose=True prints the response."""
    mock_provider = MagicMock()
    expected_result = {"score": 7, "verdict": "match"}
    mock_provider.complete.return_value = expected_result

    _score_old(
        description="desc",
        profile_json="{}",
        provider=mock_provider,
        verbose=True,
    )

    captured = capsys.readouterr()
    # Should print the response
    assert "[OLD RAW RESPONSE]" in captured.out


def test_score_old_verbose_false_no_print(capsys):
    """_score_old with verbose=False does not print the response."""
    mock_provider = MagicMock()
    mock_provider.complete.return_value = {"score": 7}

    _score_old(
        description="desc",
        profile_json="{}",
        provider=mock_provider,
        verbose=False,
    )

    captured = capsys.readouterr()
    # Should not print detailed response
    assert "[OLD RAW RESPONSE]" not in captured.out


# ---------------------------------------------------------------------------
# _score_rubric() tests
# ---------------------------------------------------------------------------


def test_score_rubric_success():
    """_score_rubric parses response and computes match_score."""
    mock_provider = MagicMock()
    response = json.dumps({
        "dimensions": {"skills_match": 8, "role_fit": 6, "red_flags": 9},
        "hiring_assessment": "Strong candidate",
        "role_fit_assessment": "Strong fit",
        "deal_breakers": [],
        "matched_skills": ["Python"],
        "missing_skills": [],
        "concerns": [],
        "archetype": "Backend Engineer",
        "apply_recommendation": "Yes",
        "verdict": "Good match",
    })
    mock_provider.generate.return_value = response

    result = _score_rubric(
        description="desc",
        profile_json="{}",
        provider=mock_provider,
    )

    assert result is not None
    # match_score = 0.60 * 8 + 0.40 * 6 = 4.8 + 2.4 = 7.2
    assert result["match_score"] == 7.2
    assert result["listing_quality"] == 9
    assert result["apply_recommendation"] == "Yes"


def test_score_rubric_match_score_computation():
    """match_score is correctly computed from weights."""
    mock_provider = MagicMock()
    response = json.dumps({
        "dimensions": {"skills_match": 10, "role_fit": 5, "red_flags": 7},
        "hiring_assessment": "Ideal",
        "role_fit_assessment": "Strong fit",
        "deal_breakers": [],
        "matched_skills": [],
        "missing_skills": [],
        "concerns": [],
        "archetype": "Role",
        "apply_recommendation": "Strong Yes",
        "verdict": "Perfect",
    })
    mock_provider.generate.return_value = response

    result = _score_rubric("desc", "{}", mock_provider)

    # 0.60 * 10 + 0.40 * 5 = 6.0 + 2.0 = 8.0
    assert result["match_score"] == 8.0


def test_score_rubric_deal_breakers_force_hard_no():
    """deal_breakers non-empty forces apply_recommendation to 'Hard No'."""
    mock_provider = MagicMock()
    response = json.dumps({
        "dimensions": {"skills_match": 9, "role_fit": 8, "red_flags": 8},
        "hiring_assessment": "Ideal",
        "role_fit_assessment": "Exact",
        "deal_breakers": ["Requires relocation"],
        "matched_skills": [],
        "missing_skills": [],
        "concerns": [],
        "archetype": "Role",
        "apply_recommendation": "Strong Yes",  # Will be overridden
        "verdict": "match",
    })
    mock_provider.generate.return_value = response

    result = _score_rubric("desc", "{}", mock_provider)

    # Even though the recommendation was Strong Yes, it should be Hard No
    assert result["apply_recommendation"] == "Hard No"


def test_score_rubric_invalid_json_returns_none():
    """Invalid JSON response returns None."""
    mock_provider = MagicMock()
    mock_provider.generate.return_value = "not valid json"

    result = _score_rubric("desc", "{}", mock_provider)

    assert result is None


def test_score_rubric_validation_failure_returns_none():
    """Response failing validation returns None."""
    mock_provider = MagicMock()
    response = json.dumps({
        "dimensions": {"skills_match": 11, "role_fit": 5, "red_flags": 7},
        # skills_match is 11, which is > 10, invalid
        "hiring_assessment": "Strong",
        "role_fit_assessment": "Strong",
        "deal_breakers": [],
        "matched_skills": [],
        "missing_skills": [],
        "concerns": [],
        "archetype": "Role",
        "apply_recommendation": "Yes",
        "verdict": "match",
    })
    mock_provider.generate.return_value = response

    result = _score_rubric("desc", "{}", mock_provider)

    assert result is None


def test_score_rubric_runtime_error_returns_none():
    """RuntimeError from provider returns None."""
    mock_provider = MagicMock()
    mock_provider.generate.side_effect = RuntimeError("API error")

    result = _score_rubric("desc", "{}", mock_provider)

    assert result is None


def test_score_rubric_verbose_prints_response(capsys):
    """_score_rubric with verbose=True prints the response."""
    mock_provider = MagicMock()
    response = json.dumps({
        "dimensions": {"skills_match": 7, "role_fit": 6, "red_flags": 8},
        "hiring_assessment": "Strong",
        "role_fit_assessment": "Strong",
        "deal_breakers": [],
        "matched_skills": [],
        "missing_skills": [],
        "concerns": [],
        "archetype": "Engineer",
        "apply_recommendation": "Yes",
        "verdict": "Good",
    })
    mock_provider.generate.return_value = response

    _score_rubric("desc", "{}", mock_provider, verbose=True)

    captured = capsys.readouterr()
    assert "[RUBRIC RAW RESPONSE]" in captured.out


def test_score_rubric_fenced_json_is_stripped():
    """JSON wrapped in code fences is correctly parsed."""
    mock_provider = MagicMock()
    response = "```json\n" + json.dumps({
        "dimensions": {"skills_match": 7, "role_fit": 6, "red_flags": 8},
        "hiring_assessment": "Strong",
        "role_fit_assessment": "Strong",
        "deal_breakers": [],
        "matched_skills": [],
        "missing_skills": [],
        "concerns": [],
        "archetype": "Engineer",
        "apply_recommendation": "Yes",
        "verdict": "Good",
    }) + "\n```"
    mock_provider.generate.return_value = response

    result = _score_rubric("desc", "{}", mock_provider)

    assert result is not None
    assert result["match_score"] == 6.6  # 0.60*7 + 0.40*6


# ---------------------------------------------------------------------------
# Constants tests
# ---------------------------------------------------------------------------


def test_skills_weight_is_060():
    """_SKILLS_WEIGHT should be 0.60."""
    assert _SKILLS_WEIGHT == 0.60


def test_role_fit_weight_is_040():
    """_ROLE_FIT_WEIGHT should be 0.40."""
    assert _ROLE_FIT_WEIGHT == 0.40


def test_weights_sum_to_one():
    """Weights should sum to 1.0."""
    assert _SKILLS_WEIGHT + _ROLE_FIT_WEIGHT == 1.0


def test_valid_recommendations_contains_expected():
    """_VALID_RECOMMENDATIONS has all expected values."""
    expected = {"Strong Yes", "Yes", "Maybe", "No", "Hard No"}
    assert _VALID_RECOMMENDATIONS == expected
