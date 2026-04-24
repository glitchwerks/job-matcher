"""
tests/test_eval_rubric.py — Unit tests for scripts/eval_rubric.py.

Covers:
  - _validate_rubric_response() — schema validation
  - _truncate() — text truncation
  - _prepare_scoring_profile() — profile transformation
  - _fetch_stratified_sample() — stratified sampling from DB
  - _score_old() — old prompt scoring with error handling
  - _score_rubric() — new rubric scoring with validation and weighting
  - _print_summary() — summary stats including Issue #248 split metrics
  - Constants: weights, valid recommendations
"""

import json
import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scripts.eval_rubric import (
    _validate_rubric_response,
    _truncate,
    _prepare_scoring_profile,
    _fetch_stratified_sample,
    _score_old,
    _score_rubric,
    _print_summary,
    _SKILLS_WEIGHT,
    _ROLE_FIT_WEIGHT,
    _VALID_RECOMMENDATIONS,
    _normalize_seed,
    _compute_decision,
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
        "missing_required_skills": [],
        "missing_nice_to_have_skills": [],
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
        results = _fetch_stratified_sample(
            mock_conn, high_n=1, mid_n=1, low_n=1, seed=0
        )

    assert len(results) >= 3  # At least one from each tier


def test_fetch_stratified_sample_respects_limits():
    """Each tier query has correct limit parameter."""
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    mock_cursor.fetchall.return_value = []

    with patch("scripts.eval_rubric.psycopg2.extras.RealDictCursor"):
        _fetch_stratified_sample(mock_conn, high_n=5, mid_n=10, low_n=15, seed=0)

    # Should call execute 4 times: one setseed + three tier queries
    calls = mock_cursor.execute.call_args_list
    assert len(calls) == 4
    # Verify limits are in the params of the three tier calls (skip index 0)
    limits = [call[0][1][0] for call in calls[1:]]
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
        results = _fetch_stratified_sample(mock_conn, 2, 2, 2, seed=0)

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
        "missing_required_skills": ["Kubernetes"],
        "missing_nice_to_have_skills": ["Terraform"],
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
    # New split fields should be present
    assert result["missing_required_skills"] == ["Kubernetes"]
    assert result["missing_nice_to_have_skills"] == ["Terraform"]
    # Old flat field must not be present
    assert "missing_skills" not in result


def test_score_rubric_match_score_computation():
    """match_score is correctly computed from weights."""
    mock_provider = MagicMock()
    response = json.dumps({
        "dimensions": {"skills_match": 10, "role_fit": 5, "red_flags": 7},
        "hiring_assessment": "Ideal",
        "role_fit_assessment": "Strong fit",
        "deal_breakers": [],
        "matched_skills": [],
        "missing_required_skills": [],
        "missing_nice_to_have_skills": [],
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
        "missing_required_skills": [],
        "missing_nice_to_have_skills": [],
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
        "missing_required_skills": [],
        "missing_nice_to_have_skills": [],
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
        "missing_required_skills": [],
        "missing_nice_to_have_skills": [],
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
        "missing_required_skills": [],
        "missing_nice_to_have_skills": [],
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
# _score_rubric() — missing skill split field tests
# ---------------------------------------------------------------------------


def _make_rubric_response(**overrides) -> str:
    """Build a valid rubric JSON response string, optionally overriding fields."""
    base = {
        "dimensions": {"skills_match": 7, "role_fit": 6, "red_flags": 8},
        "hiring_assessment": "Strong candidate, minor gaps",
        "role_fit_assessment": "Strong fit, minor mismatch",
        "deal_breakers": [],
        "matched_skills": ["Python", "PostgreSQL"],
        "missing_required_skills": [],
        "missing_nice_to_have_skills": [],
        "concerns": [],
        "archetype": "Backend Engineer",
        "apply_recommendation": "Yes",
        "verdict": "Good match overall.",
    }
    base.update(overrides)
    return json.dumps(base)


def test_score_rubric_returns_missing_required_skills():
    """Result contains missing_required_skills as a list."""
    mock_provider = MagicMock()
    mock_provider.generate.return_value = _make_rubric_response(
        missing_required_skills=["Go", "Docker"],
        missing_nice_to_have_skills=["Terraform"],
    )

    result = _score_rubric("desc", "{}", mock_provider)

    assert result is not None
    assert isinstance(result["missing_required_skills"], list)
    assert result["missing_required_skills"] == ["Go", "Docker"]


def test_score_rubric_returns_missing_nice_to_have_skills():
    """Result contains missing_nice_to_have_skills as a list."""
    mock_provider = MagicMock()
    mock_provider.generate.return_value = _make_rubric_response(
        missing_required_skills=["Rust"],
        missing_nice_to_have_skills=["Kubernetes", "Helm"],
    )

    result = _score_rubric("desc", "{}", mock_provider)

    assert result is not None
    assert isinstance(result["missing_nice_to_have_skills"], list)
    assert result["missing_nice_to_have_skills"] == ["Kubernetes", "Helm"]


def test_score_rubric_rejects_missing_required_skills():
    """Validation fails (returns None) when missing_required_skills is absent."""
    mock_provider = MagicMock()
    # Build a response that omits missing_required_skills entirely
    raw = {
        "dimensions": {"skills_match": 7, "role_fit": 6, "red_flags": 8},
        "hiring_assessment": "Strong candidate, minor gaps",
        "role_fit_assessment": "Strong fit, minor mismatch",
        "deal_breakers": [],
        "matched_skills": ["Python"],
        # missing_required_skills intentionally absent
        "missing_nice_to_have_skills": ["Terraform"],
        "concerns": [],
        "archetype": "Backend Engineer",
        "apply_recommendation": "Yes",
        "verdict": "Good match.",
    }
    mock_provider.generate.return_value = json.dumps(raw)

    # Validation will fail because missing_required_skills is a required key —
    # this is the expected behaviour: schema validation catches the absence and
    # returns None so the caller can retry or log a warning.
    result = _score_rubric("desc", "{}", mock_provider)
    assert result is None


def test_score_rubric_rejects_missing_nice_to_have_skills():
    """Validation fails (returns None) when missing_nice_to_have_skills is absent."""
    mock_provider = MagicMock()
    raw = {
        "dimensions": {"skills_match": 7, "role_fit": 6, "red_flags": 8},
        "hiring_assessment": "Strong candidate, minor gaps",
        "role_fit_assessment": "Strong fit, minor mismatch",
        "deal_breakers": [],
        "matched_skills": ["Python"],
        "missing_required_skills": ["Go"],
        # missing_nice_to_have_skills intentionally absent
        "concerns": [],
        "archetype": "Backend Engineer",
        "apply_recommendation": "Yes",
        "verdict": "Good match.",
    }
    mock_provider.generate.return_value = json.dumps(raw)

    # Schema validation should catch the absent key and return None.
    result = _score_rubric("desc", "{}", mock_provider)
    assert result is None


def test_score_rubric_two_arrays_are_disjoint():
    """A skill that appears in required cannot also appear in nice-to-have."""
    mock_provider = MagicMock()
    mock_provider.generate.return_value = _make_rubric_response(
        missing_required_skills=["Rust", "C++"],
        missing_nice_to_have_skills=["Terraform", "Helm"],
    )

    result = _score_rubric("desc", "{}", mock_provider)

    assert result is not None
    req_set = set(result["missing_required_skills"])
    nth_set = set(result["missing_nice_to_have_skills"])
    assert req_set.isdisjoint(nth_set), (
        f"Skills appear in both arrays: {req_set & nth_set}"
    )


def test_validate_rubric_response_warns_on_non_disjoint_arrays(capsys):
    """A skill appearing in both arrays emits a stderr warning but passes."""
    rubric = _make_valid_rubric()
    # "Python" appears in both — deliberate disjoint violation.
    rubric["missing_required_skills"] = ["Python", "Go"]
    rubric["missing_nice_to_have_skills"] = ["Python", "Terraform"]

    result = _validate_rubric_response(rubric)

    # Validation itself must still return None (not a hard failure).
    assert result is None, (
        "Non-disjoint arrays should warn, not fail validation"
    )
    # The warning must be visible on stderr.
    captured = capsys.readouterr()
    assert "Disjoint-set violation" in captured.err
    assert "Python" in captured.err


def test_score_rubric_non_disjoint_arrays_warns_and_returns_result(capsys):
    """_score_rubric surfaces a stderr warning when arrays overlap but still returns result."""
    mock_provider = MagicMock()
    mock_provider.generate.return_value = _make_rubric_response(
        missing_required_skills=["Docker", "Kubernetes"],
        missing_nice_to_have_skills=["Kubernetes", "Helm"],  # Kubernetes in both
    )

    result = _score_rubric("desc", "{}", mock_provider)

    # Result is still returned — non-disjoint is not a parse failure.
    assert result is not None
    # Warning must appear on stderr.
    captured = capsys.readouterr()
    assert "Disjoint-set violation" in captured.err
    assert "Kubernetes" in captured.err


def test_score_rubric_old_missing_skills_field_not_in_result():
    """The legacy missing_skills field must not be present in the parsed result."""
    mock_provider = MagicMock()
    mock_provider.generate.return_value = _make_rubric_response(
        missing_required_skills=["Java"],
        missing_nice_to_have_skills=[],
    )

    result = _score_rubric("desc", "{}", mock_provider)

    assert result is not None
    assert "missing_skills" not in result


def test_validate_rubric_response_rejects_old_missing_skills_key():
    """Validation fails when response has legacy missing_skills instead of split fields."""
    rubric = _make_valid_rubric()
    # Swap out the new fields for the old flat one
    del rubric["missing_required_skills"]
    del rubric["missing_nice_to_have_skills"]
    rubric["missing_skills"] = []

    result = _validate_rubric_response(rubric)

    assert result is not None
    assert "Missing keys" in result


def test_validate_rubric_response_accepts_both_split_fields():
    """Validation passes when both missing_required_skills and missing_nice_to_have_skills are present."""
    rubric = _make_valid_rubric()
    rubric["missing_required_skills"] = ["Scala"]
    rubric["missing_nice_to_have_skills"] = ["Kafka"]

    result = _validate_rubric_response(rubric)

    assert result is None


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


# ---------------------------------------------------------------------------
# _print_summary() — missing-skills split metrics (Issue #248)
# ---------------------------------------------------------------------------


def _make_eval_entry(
    old_missing: int,
    new_req: int,
    new_nth: int,
    old_score: int = 7,
    match_score: float = 6.0,
) -> dict:
    """Build a synthetic evaluated-entry dict for _print_summary tests.

    Args:
        old_missing: Count of flat missing_skills from old prompt.
        new_req:     Count of missing_required_skills from rubric prompt.
        new_nth:     Count of missing_nice_to_have_skills from rubric prompt.
        old_score:   Scalar score returned by old prompt (default 7).
        match_score: Computed match_score from rubric prompt (default 6.0).

    Returns:
        Dict with ``listing``, ``old``, and ``new`` sub-dicts.
    """
    old_result = {
        "score": old_score,
        "matched_skills": [],
        "missing_skills": [f"SkillA{i}" for i in range(old_missing)],
        "concerns": [],
        "verdict": "ok",
    }
    new_result = {
        "match_score": match_score,
        "listing_quality": 8,
        "apply_recommendation": "Yes",
        "hiring_assessment": "Strong candidate, minor gaps",
        "role_fit_assessment": "Strong fit, minor mismatch",
        "deal_breakers": [],
        "matched_skills": [],
        "missing_required_skills": [
            f"Req{i}" for i in range(new_req)
        ],
        "missing_nice_to_have_skills": [
            f"Nth{i}" for i in range(new_nth)
        ],
        "concerns": [],
        "archetype": "Engineer",
        "verdict": "ok",
    }
    return {
        "listing": {"id": 1, "title": "Test Job", "score": old_score},
        "old": old_result,
        "new": new_result,
    }


def test_print_summary_emits_metric1_total_gap(capsys):
    """Summary prints Metric 1: old flat vs new combined count."""
    entries = [_make_eval_entry(old_missing=4, new_req=2, new_nth=2)]
    _print_summary(evaluated=entries, provider_label="test/model")

    captured = capsys.readouterr()
    assert "Metric 1" in captured.out
    assert "total gap count" in captured.out
    # Output format is "old_flat=N" — value may be int or float repr.
    assert "old_flat=4" in captured.out
    assert "new_combined(req+nth)=4" in captured.out


def test_print_summary_emits_metric2_required_only(capsys):
    """Summary prints Metric 2: old flat vs new required-only count."""
    entries = [_make_eval_entry(old_missing=4, new_req=2, new_nth=2)]
    _print_summary(evaluated=entries, provider_label="test/model")

    captured = capsys.readouterr()
    assert "Metric 2" in captured.out
    assert "required-only" in captured.out
    # Output format is "new_required=N" — value may be int or float repr.
    assert "new_required=2" in captured.out


def test_print_summary_flags_drop_when_old_substantially_higher(capsys):
    """Summary flags listings where old_missing > (new_req+new_nth) * 1.20."""
    # old=10, combined=6 → 6 < 10*0.80=8 → should flag
    entries = [_make_eval_entry(old_missing=10, new_req=3, new_nth=3)]
    _print_summary(evaluated=entries, provider_label="test/model")

    captured = capsys.readouterr()
    assert "WARNING" in captured.out
    assert "LLM may be dropping items" in captured.out


def test_print_summary_no_flag_when_counts_comparable(capsys):
    """Summary does not flag when new combined count is within 20% of old."""
    # old=4, combined=4 → no drop
    entries = [_make_eval_entry(old_missing=4, new_req=2, new_nth=2)]
    _print_summary(evaluated=entries, provider_label="test/model")

    captured = capsys.readouterr()
    assert "LLM may be dropping items" not in captured.out


def test_print_summary_old_path_reads_flat_missing_skills(capsys):
    """Summary reads old prompt's missing_skills (not the new split fields)."""
    # old missing_skills has 3 items; new has 1 req + 1 nth
    entries = [_make_eval_entry(old_missing=3, new_req=1, new_nth=1)]
    _print_summary(evaluated=entries, provider_label="test/model")

    captured = capsys.readouterr()
    # old_flat avg should be 3 (may render as int or float)
    assert "old_flat=3" in captured.out
    # combined avg should be 2 (may render as int or float)
    assert "new_combined(req+nth)=2" in captured.out


# ---------------------------------------------------------------------------
# Issue #274: seed normalization + seeded sampling
# ---------------------------------------------------------------------------


class TestNormalizeSeed:
    """Tests for _normalize_seed: maps ints into the [-1.0, 1.0] range
    that PostgreSQL's setseed() requires."""

    def test_zero_maps_to_negative_one(self):
        # (0 % 10_000_000) / 10_000_000 * 2 - 1 = -1.0
        assert _normalize_seed(0) == -1.0

    def test_five_million_maps_to_zero(self):
        # (5_000_000 / 10_000_000) * 2 - 1 = 0.0
        assert _normalize_seed(5_000_000) == 0.0

    def test_ten_million_wraps_to_negative_one(self):
        # (10_000_000 % 10_000_000) / 10_000_000 * 2 - 1 = -1.0
        assert _normalize_seed(10_000_000) == -1.0

    def test_large_seed_stays_in_range(self):
        result = _normalize_seed(20260424)
        assert -1.0 <= result <= 1.0

    def test_negative_seed_handled(self):
        # Python's modulo keeps sign of divisor, so (-1) % 10_000_000 = 9_999_999
        result = _normalize_seed(-1)
        assert -1.0 <= result <= 1.0


# ---------------------------------------------------------------------------
# _fetch_stratified_sample() — seeded RNG tests
# ---------------------------------------------------------------------------


class TestFetchStratifiedSampleSeeded:
    """Tests that the sample query seeds PostgreSQL's RNG before querying."""

    def test_setseed_called_before_sample_queries(self):
        # Arrange: mock cursor that returns empty rows for all three tier queries
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = []
        mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
        mock_cursor.__exit__ = MagicMock(return_value=False)

        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor

        # Act
        _fetch_stratified_sample(mock_conn, 10, 10, 10, seed=20260424)

        # Assert: first execute call must be SELECT setseed(...) with
        # the normalized seed.
        first_call = mock_cursor.execute.call_args_list[0]
        assert "setseed" in first_call.args[0].lower()
        # _normalize_seed(20260424) = (20260424 % 10_000_000) / 10_000_000 * 2 - 1
        expected_normalized = (20260424 % 10_000_000) / 10_000_000 * 2 - 1
        assert first_call.args[1] == (expected_normalized,)

    def test_four_execute_calls_total(self):
        """One setseed + three per-tier sample queries."""
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = []
        mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
        mock_cursor.__exit__ = MagicMock(return_value=False)

        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor

        _fetch_stratified_sample(mock_conn, 5, 5, 5, seed=42)

        assert mock_cursor.execute.call_count == 4


# ---------------------------------------------------------------------------
# _compute_decision() tests
# ---------------------------------------------------------------------------


def _make_eval(
    old_missing: int,
    new_req: int,
    new_nth: int,
    score: float = 7.0,
) -> dict:
    """Build a minimal evaluated entry with the fields _compute_decision reads.

    Args:
        old_missing: Number of missing skills in the old result.
        new_req: Number of missing required skills in the new result.
        new_nth: Number of missing nice-to-have skills in the new result.
        score: DB score for tier classification. Defaults to 7.0 (mid tier).

    Returns:
        A dict with ``listing``, ``old``, and ``new`` keys as produced by the
        eval pipeline.
    """
    return {
        "listing": {"id": 1, "title": "x", "score": score},
        "old": {"missing_skills": ["s"] * old_missing, "score": score},
        "new": {
            "missing_required_skills": ["r"] * new_req,
            "missing_nice_to_have_skills": ["n"] * new_nth,
            "match_score": score,
        },
    }


class TestComputeDecision:
    """Tests for _compute_decision: aggregates required/nice-to-have ratio
    and renders the tune/no-change recommendation against the #341 threshold.
    """

    def test_ratio_above_threshold_recommends_tune(self) -> None:
        """85/(85+15) = 0.85 > 0.80 threshold -> 'tune'."""
        evaluated = [_make_eval(0, 85, 15)]
        decision = _compute_decision(evaluated)
        assert decision["required_ratio"] == 0.85
        assert decision["recommendation"] == "tune"

    def test_ratio_at_threshold_recommends_no_change(self) -> None:
        """80/(80+20) = 0.80 exactly at threshold -> 'no change needed'."""
        evaluated = [_make_eval(0, 80, 20)]
        decision = _compute_decision(evaluated)
        assert decision["required_ratio"] == 0.80
        assert decision["recommendation"] == "no change needed"

    def test_ratio_just_above_threshold_recommends_tune(self) -> None:
        """81/(81+19) = 0.81 just above threshold -> 'tune'."""
        evaluated = [_make_eval(0, 81, 19)]
        decision = _compute_decision(evaluated)
        assert decision["recommendation"] == "tune"

    def test_ratio_just_below_threshold_recommends_no_change(self) -> None:
        """79/(79+21) = 0.79 just below threshold -> 'no change needed'."""
        evaluated = [_make_eval(0, 79, 21)]
        decision = _compute_decision(evaluated)
        assert decision["recommendation"] == "no change needed"

    def test_empty_evaluated_returns_null_recommendation(self) -> None:
        """Empty input list produces 'insufficient data' with None ratio."""
        decision = _compute_decision([])
        assert decision["recommendation"] == "insufficient data"
        assert decision["required_ratio"] is None

    def test_all_failed_new_results_returns_null_recommendation(self) -> None:
        """Entries with new=None (score failures) produce 'insufficient data'."""
        evaluated = [
            {"listing": {"id": 1, "score": 7.0}, "old": None, "new": None}
        ]
        decision = _compute_decision(evaluated)
        assert decision["recommendation"] == "insufficient data"

    def test_per_tier_breakdown_present(self) -> None:
        """Tier breakdown keys and per-tier ratios are correct."""
        evaluated = [
            _make_eval(0, 30, 10, score=9.0),  # high: 30/(30+10) = 0.75
            _make_eval(0, 50, 20, score=6.0),  # mid:  50/(50+20) ≈ 0.7143
            _make_eval(0, 80, 10, score=3.0),  # low:  80/(80+10) ≈ 0.8889
        ]
        decision = _compute_decision(evaluated)
        assert "tier_breakdown" in decision
        assert decision["tier_breakdown"]["high"]["required_ratio"] == 0.75
        # mid: 50/(50+20) = 0.7142857...
        assert (
            round(decision["tier_breakdown"]["mid"]["required_ratio"], 4)
            == 0.7143
        )
        # low: 80/(80+10) = 0.8888...
        assert (
            round(decision["tier_breakdown"]["low"]["required_ratio"], 4)
            == 0.8889
        )

    def test_threshold_value_in_output(self) -> None:
        """Result dict carries the threshold constant (0.80) for serialization."""
        decision = _compute_decision([_make_eval(0, 1, 1)])
        assert decision["threshold"] == 0.80
