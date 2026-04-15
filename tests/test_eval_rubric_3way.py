"""
tests/test_eval_rubric_3way.py — Unit tests for scripts/eval_rubric_3way.py.

Covers:
  - _normalize_ops_score() — 1-5 scale to 0-10 scale conversion
  - _parse_report() — markdown report parsing and extraction
  - _stratified_sample() — stratified sampling across tiers
  - _pearson() — Pearson correlation coefficient computation
  - _score_tier() — score classification into High/Mid/Low tiers
"""

import os
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scripts.eval_rubric_3way import (
    _normalize_ops_score,
    _parse_report,
    _stratified_sample,
    _pearson,
    _score_tier,
    _TIER_HIGH,
    _TIER_MID,
    _TIER_LOW,
)


# ---------------------------------------------------------------------------
# _normalize_ops_score() tests
# ---------------------------------------------------------------------------


def test_normalize_ops_score_minimum():
    """Score of 1.0 (1-5 scale) maps to 0.0 (0-10 scale)."""
    assert _normalize_ops_score(1.0) == 0.0


def test_normalize_ops_score_midpoint():
    """Score of 3.0 (1-5 scale) maps to 5.0 (0-10 scale)."""
    assert _normalize_ops_score(3.0) == 5.0


def test_normalize_ops_score_maximum():
    """Score of 5.0 (1-5 scale) maps to 10.0 (0-10 scale)."""
    assert _normalize_ops_score(5.0) == 10.0


def test_normalize_ops_score_between_values():
    """Intermediate scores are correctly mapped."""
    assert _normalize_ops_score(2.0) == 2.5
    assert _normalize_ops_score(4.0) == 7.5


def test_normalize_ops_score_decimal_precision():
    """Result is rounded to one decimal place."""
    result = _normalize_ops_score(2.3)
    assert isinstance(result, float)
    assert result == 3.2  # (2.3 - 1) * 2.5 = 3.25 rounds to 3.2


def test_normalize_ops_score_formula():
    """Formula is (raw - 1) * 2.5."""
    for raw in [1.0, 1.5, 2.0, 3.0, 4.0, 5.0]:
        expected = (raw - 1.0) * 2.5
        result = _normalize_ops_score(raw)
        assert result == round(expected, 1)


# ---------------------------------------------------------------------------
# _parse_report() tests
# ---------------------------------------------------------------------------


def test_parse_report_basic_english_header(tmp_path):
    """Parse basic English header with company and title."""
    md_content = """# Evaluation: Acme Corp -- Senior Engineer

**URL:** https://example.com/job
**Score:** 4.0 / 5
"""
    report_file = tmp_path / "test.md"
    report_file.write_text(md_content, encoding="utf-8")

    result = _parse_report(report_file)

    assert result is not None
    assert result["company"] == "Acme Corp"
    assert result["title"] == "Senior Engineer"
    assert result["url"] == "https://example.com/job"
    assert result["ops_score_raw"] == 4.0


def test_parse_report_spanish_evaluacion_header(tmp_path):
    """Parse Spanish 'Evaluacion' header variant."""
    md_content = """# Evaluacion: Company ABC -- Ingeniero Backend

**URL:** https://example.com/job
**Score:** 3.5 / 5
"""
    report_file = tmp_path / "test.md"
    report_file.write_text(md_content, encoding="utf-8")

    result = _parse_report(report_file)

    assert result is not None
    assert result["company"] == "Company ABC"
    assert result["title"] == "Ingeniero Backend"


def test_parse_report_single_dash_separator(tmp_path):
    """Parse header with single dash separator."""
    md_content = """# Evaluation: XYZ Inc - Product Manager

**URL:** https://example.com/job
**Score:** 3.0 / 5
"""
    report_file = tmp_path / "test.md"
    report_file.write_text(md_content, encoding="utf-8")

    result = _parse_report(report_file)

    assert result is not None
    assert result["company"] == "XYZ Inc"
    assert result["title"] == "Product Manager"


def test_parse_report_em_dash_separator(tmp_path):
    """Parse header with em-dash separator."""
    md_content = """# Evaluation: TechCo — Data Scientist

**URL:** https://example.com/job
**Score:** 4.5 / 5
"""
    report_file = tmp_path / "test.md"
    report_file.write_text(md_content, encoding="utf-8")

    result = _parse_report(report_file)

    assert result is not None
    assert result["company"] == "TechCo"
    assert result["title"] == "Data Scientist"


def test_parse_report_missing_score_returns_none(tmp_path):
    """Report without score returns None."""
    md_content = """# Evaluation: Company -- Title

**URL:** https://example.com/job
"""
    report_file = tmp_path / "test.md"
    report_file.write_text(md_content, encoding="utf-8")

    result = _parse_report(report_file)

    assert result is None


def test_parse_report_missing_url_returns_none(tmp_path):
    """Report without URL returns None."""
    md_content = """# Evaluation: Company -- Title

**Score:** 3.0 / 5
"""
    report_file = tmp_path / "test.md"
    report_file.write_text(md_content, encoding="utf-8")

    result = _parse_report(report_file)

    assert result is None


def test_parse_report_file_not_found():
    """Non-existent file returns None."""
    result = _parse_report(Path("/nonexistent/path/file.md"))
    assert result is None


def test_parse_report_score_decimal():
    """Decimal scores are parsed correctly."""
    md_content = """# Evaluation: Company -- Title

**URL:** https://example.com/job
**Score:** 3.7 / 5
"""
    from pathlib import Path
    import tempfile

    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
        f.write(md_content)
        temp_path = Path(f.name)

    try:
        result = _parse_report(temp_path)
        assert result["ops_score_raw"] == 3.7
    finally:
        temp_path.unlink()


def test_parse_report_normalized_score():
    """ops_score_norm is correctly computed from raw score."""
    md_content = """# Evaluation: Company -- Title

**URL:** https://example.com/job
**Score:** 3.0 / 5
"""
    from pathlib import Path
    import tempfile

    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
        f.write(md_content)
        temp_path = Path(f.name)

    try:
        result = _parse_report(temp_path)
        assert result["ops_score_norm"] == 5.0  # (3.0 - 1) * 2.5
    finally:
        temp_path.unlink()


def test_parse_report_deal_breaker_location_score_1(tmp_path):
    """Deal-breaker detected from location score of 1.0."""
    md_content = """# Evaluation: Company -- Title

**URL:** https://example.com/job
**Score:** 3.0 / 5

| Dimension | Score |
|-----------|-------|
| Location  | 1.0   |
| Skills    | 4.0   |
"""
    report_file = tmp_path / "test.md"
    report_file.write_text(md_content, encoding="utf-8")

    result = _parse_report(report_file)

    assert result["deal_breaker"] is True
    assert result["deal_breaker_reason"] == "location"


def test_parse_report_deal_breaker_explicit_section(tmp_path):
    """Deal-breaker detected from explicit DEAL-BREAKER section."""
    md_content = """# Evaluation: Company -- Title

**URL:** https://example.com/job
**Score:** 3.0 / 5

## DEAL-BREAKER
Location mismatch
"""
    report_file = tmp_path / "test.md"
    report_file.write_text(md_content, encoding="utf-8")

    result = _parse_report(report_file)

    assert result["deal_breaker"] is True
    assert "explicit deal-breaker" in result["deal_breaker_reason"]


def test_parse_report_deal_breaker_negative_flags(tmp_path):
    """Deal-breaker detected from negative Red Flags score."""
    md_content = """# Evaluation: Company -- Title

**URL:** https://example.com/job
**Score:** 3.0 / 5

| Dimension | Score |
|-----------|-------|
| Red Flags | -2.0  |
"""
    report_file = tmp_path / "test.md"
    report_file.write_text(md_content, encoding="utf-8")

    result = _parse_report(report_file)

    assert result["deal_breaker"] is True
    assert "negative red flags" in result["deal_breaker_reason"]


def test_parse_report_no_deal_breaker(tmp_path):
    """No deal-breaker detected when none of the signals present."""
    md_content = """# Evaluation: Company -- Title

**URL:** https://example.com/job
**Score:** 3.0 / 5

| Dimension | Score |
|-----------|-------|
| Location  | 3.0   |
| Skills    | 4.0   |
"""
    report_file = tmp_path / "test.md"
    report_file.write_text(md_content, encoding="utf-8")

    result = _parse_report(report_file)

    assert result["deal_breaker"] is False
    assert result["deal_breaker_reason"] is None


def test_parse_report_returns_expected_keys(tmp_path):
    """Parsed report has all expected keys."""
    md_content = """# Evaluation: Company -- Title

**URL:** https://example.com/job
**Score:** 3.0 / 5
"""
    report_file = tmp_path / "test.md"
    report_file.write_text(md_content, encoding="utf-8")

    result = _parse_report(report_file)

    expected_keys = {
        "path", "filename", "title", "company", "url",
        "ops_score_raw", "ops_score_norm",
        "deal_breaker", "deal_breaker_reason",
    }
    assert set(result.keys()) == expected_keys


# ---------------------------------------------------------------------------
# _stratified_sample() tests
# ---------------------------------------------------------------------------


def test_stratified_sample_even_distribution():
    """Sample roughly 1/3 from each tier when all tiers well-populated."""
    reports = [
        {"ops_score_raw": 4.5},  # High
        {"ops_score_raw": 4.2},  # High
        {"ops_score_raw": 4.0},  # High
        {"ops_score_raw": 3.5},  # Mid
        {"ops_score_raw": 3.0},  # Mid
        {"ops_score_raw": 2.8},  # Mid
        {"ops_score_raw": 2.0},  # Low
        {"ops_score_raw": 1.5},  # Low
        {"ops_score_raw": 1.0},  # Low
    ]

    sample = _stratified_sample(reports, count=9)

    assert len(sample) == 9
    # Should have roughly 3 from each tier
    high = [r for r in sample if r["ops_score_raw"] >= 4.0]
    mid = [r for r in sample if 2.5 <= r["ops_score_raw"] < 4.0]
    low = [r for r in sample if r["ops_score_raw"] < 2.5]
    assert len(high) > 0
    assert len(mid) > 0
    assert len(low) > 0


def test_stratified_sample_respects_count_limit():
    """Sample size never exceeds requested count."""
    reports = [{"ops_score_raw": i * 0.5 + 1.0} for i in range(100)]
    sample = _stratified_sample(reports, count=30)
    assert len(sample) == 30


def test_stratified_sample_fewer_available():
    """Sample size is less if fewer reports available than requested."""
    reports = [
        {"ops_score_raw": 4.5},
        {"ops_score_raw": 3.0},
        {"ops_score_raw": 1.5},
    ]

    sample = _stratified_sample(reports, count=10)

    assert len(sample) == 3


def test_stratified_sample_tier_with_no_items():
    """Backfill from other tiers when one tier is empty."""
    reports = [
        {"ops_score_raw": 4.5},
        {"ops_score_raw": 4.2},
        {"ops_score_raw": 4.0},
        # No mid tier reports
        {"ops_score_raw": 2.0},
        {"ops_score_raw": 1.5},
    ]

    sample = _stratified_sample(reports, count=5)

    # Should backfill from high/low since mid is empty
    assert len(sample) == 5


def test_stratified_sample_deterministic_within_tier():
    """Tiers are sorted for deterministic selection."""
    reports = [
        {"ops_score_raw": 4.5, "id": "H1"},
        {"ops_score_raw": 4.0, "id": "H2"},
        {"ops_score_raw": 3.9, "id": "M1"},
        {"ops_score_raw": 3.0, "id": "M2"},
        {"ops_score_raw": 2.0, "id": "L1"},
        {"ops_score_raw": 1.5, "id": "L2"},
    ]

    sample1 = _stratified_sample(reports, count=6)
    sample2 = _stratified_sample(reports, count=6)

    # Should be identical (order and content)
    assert [r["id"] for r in sample1] == [r["id"] for r in sample2]


def test_stratified_sample_high_tier_descending():
    """High tier is sorted descending (best scores first)."""
    reports = [
        {"ops_score_raw": 4.0, "order": "last"},
        {"ops_score_raw": 4.5, "order": "first"},
        {"ops_score_raw": 4.2, "order": "middle"},
    ]

    sample = _stratified_sample(reports, count=3)

    # High tier should be sorted descending, so 4.5 before 4.2 before 4.0
    high = [r for r in sample if r["ops_score_raw"] >= 4.0]
    assert high[0]["ops_score_raw"] == 4.5


def test_stratified_sample_low_tier_ascending():
    """Low tier is sorted ascending (best scores first among low tier)."""
    reports = [
        {"ops_score_raw": 1.0, "order": "worst"},
        {"ops_score_raw": 2.0, "order": "best_of_low"},
        {"ops_score_raw": 1.5, "order": "middle"},
    ]

    sample = _stratified_sample(reports, count=3)

    # Low tier should be sorted ascending, so 1.0 before 1.5 before 2.0
    low = [r for r in sample if r["ops_score_raw"] < 2.5]
    assert low[0]["ops_score_raw"] == 1.0


# ---------------------------------------------------------------------------
# _pearson() tests
# ---------------------------------------------------------------------------


def test_pearson_perfect_positive_correlation():
    """Perfect positive correlation returns r=1.0."""
    xs = [1.0, 2.0, 3.0, 4.0, 5.0]
    ys = [1.0, 2.0, 3.0, 4.0, 5.0]

    r = _pearson(xs, ys)

    assert r == 1.0


def test_pearson_perfect_negative_correlation():
    """Perfect negative correlation returns r=-1.0."""
    xs = [1.0, 2.0, 3.0, 4.0, 5.0]
    ys = [5.0, 4.0, 3.0, 2.0, 1.0]

    r = _pearson(xs, ys)

    assert r == -1.0


def test_pearson_no_correlation():
    """Uncorrelated data returns r near 0."""
    xs = [1.0, 2.0, 3.0, 4.0, 5.0]
    ys = [5.0, 1.0, 4.0, 2.0, 3.0]

    r = _pearson(xs, ys)

    assert -0.5 < r < 0.5  # Weak correlation


def test_pearson_less_than_two_items_returns_none():
    """Fewer than 2 paired items returns None."""
    assert _pearson([1.0], [1.0]) is None
    assert _pearson([], []) is None


def test_pearson_mismatched_lengths_returns_none():
    """Unequal list lengths returns None."""
    assert _pearson([1.0, 2.0], [1.0, 2.0, 3.0]) is None


def test_pearson_zero_variance_returns_none():
    """Zero variance in either list returns None."""
    xs = [5.0, 5.0, 5.0, 5.0]  # No variance
    ys = [1.0, 2.0, 3.0, 4.0]

    r = _pearson(xs, ys)

    assert r is None


def test_pearson_both_zero_variance_returns_none():
    """Zero variance in both lists returns None."""
    xs = [5.0, 5.0, 5.0]
    ys = [3.0, 3.0, 3.0]

    r = _pearson(xs, ys)

    assert r is None


def test_pearson_rounded_to_two_decimals():
    """Result is rounded to 2 decimal places."""
    xs = [1.0, 2.0, 3.0]
    ys = [1.1, 2.1, 3.1]

    r = _pearson(xs, ys)

    # Should be very close to 1.0 but not exactly
    assert isinstance(r, float)
    # Check that it's properly rounded to 2 decimals
    assert round(r, 2) == r


def test_pearson_partial_correlation():
    """Partial correlations are computed correctly."""
    # y = 2x + noise (positive but not perfect)
    xs = [1.0, 2.0, 3.0, 4.0, 5.0]
    ys = [2.2, 4.1, 6.0, 7.9, 10.0]

    r = _pearson(xs, ys)

    assert r is not None
    assert 0.9 < r <= 1.0  # Strong positive


# ---------------------------------------------------------------------------
# _score_tier() tests
# ---------------------------------------------------------------------------


def test_score_tier_high_boundary_inclusive():
    """score >= 6.5 is classified as High."""
    assert _score_tier(6.5) == _TIER_HIGH
    assert _score_tier(6.6) == _TIER_HIGH
    assert _score_tier(10.0) == _TIER_HIGH


def test_score_tier_high_boundary_exclusive():
    """score < 6.5 is not High."""
    assert _score_tier(6.49) != _TIER_HIGH
    assert _score_tier(6.4) != _TIER_HIGH


def test_score_tier_mid_range():
    """3.5 <= score < 6.5 is classified as Mid."""
    assert _score_tier(3.5) == _TIER_MID
    assert _score_tier(5.0) == _TIER_MID
    assert _score_tier(6.49) == _TIER_MID


def test_score_tier_mid_boundary_exclusive_high():
    """score >= 6.5 is not Mid."""
    assert _score_tier(6.5) != _TIER_MID


def test_score_tier_mid_boundary_exclusive_low():
    """score < 3.5 is not Mid."""
    assert _score_tier(3.49) != _TIER_MID


def test_score_tier_low_boundary_inclusive():
    """score < 3.5 is classified as Low."""
    assert _score_tier(3.49) == _TIER_LOW
    assert _score_tier(1.0) == _TIER_LOW
    assert _score_tier(0.0) == _TIER_LOW


def test_score_tier_low_boundary_exclusive():
    """score >= 3.5 is not Low."""
    assert _score_tier(3.5) != _TIER_LOW
    assert _score_tier(4.0) != _TIER_LOW


def test_score_tier_exact_boundaries():
    """Exact boundary values are classified correctly."""
    # 3.5 is Mid
    assert _score_tier(3.5) == _TIER_MID
    # 6.5 is High
    assert _score_tier(6.5) == _TIER_HIGH


def test_score_tier_floats():
    """Float scores are handled correctly."""
    assert _score_tier(3.6) == _TIER_MID
    assert _score_tier(6.51) == _TIER_HIGH
    assert _score_tier(3.49) == _TIER_LOW


def test_score_tier_integers():
    """Integer scores are handled correctly."""
    assert _score_tier(2) == _TIER_LOW
    assert _score_tier(5) == _TIER_MID
    assert _score_tier(8) == _TIER_HIGH
