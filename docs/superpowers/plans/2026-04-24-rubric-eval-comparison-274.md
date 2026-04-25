# Rubric Eval Comparison Run — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend `scripts/eval_rubric.py` with seeded sampling and structured output, then execute a 100-listing live-DB eval run that produces a decision-ready artifact for issue #341.

**Architecture:** Small, bounded additions to one existing script — new CLI flags (`--seed`, `--output`), seed-passed to the existing sampling function, and two pure rendering functions (`_render_markdown_report`, `_render_json_sidecar`) that serialize the evaluation results into a decision-ready markdown file plus a JSON sidecar. No new modules, no database writes, no changes to ingest or schema.

**Tech Stack:** Python 3, `psycopg2`, `pytest`, existing LLM provider chain. Runs on branch `feat/274-rubric-eval-comparison` in the pre-existing worktree `.worktrees/feat-274-rubric-eval-comparison`.

**Referenced spec:** `docs/superpowers/specs/2026-04-24-rubric-eval-comparison-274-design.md` (commit `c2b96dc`).

**Pre-conditions verified:**
- Worktree `.worktrees/feat-274-rubric-eval-comparison` exists, branched from `main` at `fe9c885` (post-#339/#340 merge).
- `scripts/eval_rubric.py` currently defines `_fetch_stratified_sample(conn, high_n, mid_n, low_n)` on line 284 and `_print_summary(evaluated, provider_label)` on line 751 — both referenced below.
- Tests live at `tests/test_eval_rubric.py` and `tests/test_eval_rubric_3way.py`. New tests append to the first file.
- `scripts/eval_rubric.py` uses `from __future__ import annotations` so forward refs are fine.

**Shell note:** All Bash commands below are POSIX. User's primary shell is PowerShell but `Bash` is what the implementing agent has. Commands work identically in both for `git`, `pytest`, `ruff`; only the live-run invocation in Task 8 uses PowerShell syntax because the user runs that step interactively.

**Worktree path:** All file paths below are **relative to the worktree root** `I:/Web Development/job-matcher-pr/.worktrees/feat-274-rubric-eval-comparison`. Never edit files in the main checkout at `I:/Web Development/job-matcher-pr`.

---

## File Structure

**Modify:**
- `scripts/eval_rubric.py` — add `_normalize_seed()`, threading `seed` through `_fetch_stratified_sample()` and `main()`, adding `_build_run_meta()`, `_compute_decision()`, `_render_markdown_report()`, `_render_json_sidecar()`, and `--seed` / `--output` CLI flags.

**Create:**
- `tests/fixtures/eval_rubric/synthetic_evaluated.json` — 10-listing fixture used by markdown/JSON render snapshot tests.
- `tests/fixtures/eval_rubric/expected_markdown.md` — golden markdown output for the fixture.

**Create at run time (Task 8):**
- `docs/eval/2026-04-24-rubric-comparison-274.md` — markdown report produced by the live run.
- `docs/eval/2026-04-24-rubric-comparison-274.json` — JSON sidecar produced by the live run.

**Test file:**
- `tests/test_eval_rubric.py` — append new tests to the existing file. No new test file.

---

### Task 1: Seed normalization helper (pure function)

**Files:**
- Modify: `scripts/eval_rubric.py` (add helper near existing `_connect` around line 260)
- Test: `tests/test_eval_rubric.py` (append to end of file)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_eval_rubric.py`:

```python
# ---------------------------------------------------------------------------
# Issue #274: seed normalization + seeded sampling
# ---------------------------------------------------------------------------

from scripts.eval_rubric import _normalize_seed


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
```

- [ ] **Step 2: Run test to verify it fails**

Run:
```bash
cd "I:/Web Development/job-matcher-pr/.worktrees/feat-274-rubric-eval-comparison"
pytest tests/test_eval_rubric.py::TestNormalizeSeed -v
```

Expected: ImportError or `AttributeError: module 'scripts.eval_rubric' has no attribute '_normalize_seed'`.

- [ ] **Step 3: Implement `_normalize_seed`**

In `scripts/eval_rubric.py`, add immediately after the `_connect()` function (before `_fetch_stratified_sample`):

```python
def _normalize_seed(seed: int) -> float:
    """Map an integer seed into the [-1.0, 1.0] range setseed() requires.

    PostgreSQL's ``setseed(value)`` accepts a float in [-1.0, 1.0]. We map
    the Python int seed into that range via modulo and linear scaling so
    any integer seed produces a deterministic, in-range value.

    Args:
        seed: Arbitrary integer seed.

    Returns:
        Normalized seed in [-1.0, 1.0].
    """
    return (seed % 10_000_000) / 10_000_000 * 2 - 1
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_eval_rubric.py::TestNormalizeSeed -v`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add scripts/eval_rubric.py tests/test_eval_rubric.py
git commit -m "feat(eval): add _normalize_seed helper for deterministic sampling (refs #274)"
```

---

### Task 2: Thread `seed` through `_fetch_stratified_sample`

**Files:**
- Modify: `scripts/eval_rubric.py:284-337` (existing `_fetch_stratified_sample`)
- Test: `tests/test_eval_rubric.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_eval_rubric.py`:

```python
from unittest.mock import MagicMock, call
from scripts.eval_rubric import _fetch_stratified_sample


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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_eval_rubric.py::TestFetchStratifiedSampleSeeded -v`
Expected: FAIL — either `TypeError: _fetch_stratified_sample() got an unexpected keyword argument 'seed'` or the setseed call is missing from `execute.call_args_list[0]`.

- [ ] **Step 3: Modify `_fetch_stratified_sample`**

Replace the function signature and body in `scripts/eval_rubric.py` (currently lines 284-337). The new version adds the required `seed` parameter and calls `setseed()` once on the cursor before the tier queries.

```python
def _fetch_stratified_sample(
    conn: psycopg2.extensions.connection,
    high_n: int,
    mid_n: int,
    low_n: int,
    seed: int,
) -> list[dict]:
    """Fetch a stratified sample of scored listings with full descriptions.

    Stratification targets:
    - High tier:  score >= 8
    - Mid tier:   5 <= score < 8
    - Low tier:   score < 5

    If a tier has fewer listings than requested, all available are returned.
    Listings are ordered randomly within each tier via PostgreSQL's
    ``random()`` function, seeded by ``setseed(_normalize_seed(seed))`` before
    the first query so the sample is reproducible across runs for a given
    ``seed`` and DB state.

    Args:
        conn:   Open psycopg2 connection.
        high_n: Target count for high-tier listings.
        mid_n:  Target count for mid-tier listings.
        low_n:  Target count for low-tier listings.
        seed:   Integer seed for sample reproducibility.

    Returns:
        List of listing dicts (plain Python dicts), combined across tiers.
    """
    query = """
        SELECT id, title, company, description, score
        FROM listings
        WHERE description IS NOT NULL
          AND description != ''
          AND seen = 1
          AND score IS NOT NULL
          AND {where_clause}
        ORDER BY random()
        LIMIT %s
    """

    tiers = [
        ("score >= 8", high_n),
        ("score >= 5 AND score < 8", mid_n),
        ("score < 5", low_n),
    ]

    results: list[dict] = []
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT setseed(%s)", (_normalize_seed(seed),))
        for where_clause, limit in tiers:
            cur.execute(
                query.format(where_clause=where_clause),
                (limit,),
            )
            rows = cur.fetchall()
            results.extend(dict(row) for row in rows)

    return results
```

- [ ] **Step 4: Run tests to verify the new tests pass AND nothing else broke**

Run: `pytest tests/test_eval_rubric.py -v`
Expected: all tests pass (new TestFetchStratifiedSampleSeeded tests + all existing eval tests). If existing tests call `_fetch_stratified_sample` without `seed`, they must be updated — add `seed=0` to any such call. Grep first:

```bash
grep -n "_fetch_stratified_sample" tests/test_eval_rubric.py tests/test_eval_rubric_3way.py scripts/eval_rubric.py
```

If any call site is missing `seed=`, update it to `seed=0` for test contexts or to the real seed in production code.

- [ ] **Step 5: Commit**

```bash
git add scripts/eval_rubric.py tests/test_eval_rubric.py
git commit -m "feat(eval): seeded stratified sampling via setseed (refs #274)"
```

---

### Task 3: Decision computation function (pure, covers the 80% threshold)

**Files:**
- Modify: `scripts/eval_rubric.py` (add `_compute_decision` near `_print_summary`)
- Test: `tests/test_eval_rubric.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_eval_rubric.py`:

```python
from scripts.eval_rubric import _compute_decision


def _make_eval(old_missing, new_req, new_nth, score=7.0):
    """Helper: build a minimal evaluated entry with the fields _compute_decision
    reads."""
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
    and renders the tune/no-change recommendation against the #341 threshold."""

    def test_ratio_above_threshold_recommends_tune(self):
        # 85 required, 15 nice-to-have -> 85% required -> tune
        evaluated = [_make_eval(0, 85, 15)]
        decision = _compute_decision(evaluated)
        assert decision["required_ratio"] == 0.85
        assert decision["recommendation"] == "tune"

    def test_ratio_at_threshold_recommends_no_change(self):
        # Exactly 80% -> threshold is strict >, so no change
        evaluated = [_make_eval(0, 80, 20)]
        decision = _compute_decision(evaluated)
        assert decision["required_ratio"] == 0.80
        assert decision["recommendation"] == "no change needed"

    def test_ratio_just_above_threshold_recommends_tune(self):
        evaluated = [_make_eval(0, 81, 19)]
        decision = _compute_decision(evaluated)
        assert decision["recommendation"] == "tune"

    def test_ratio_just_below_threshold_recommends_no_change(self):
        evaluated = [_make_eval(0, 79, 21)]
        decision = _compute_decision(evaluated)
        assert decision["recommendation"] == "no change needed"

    def test_empty_evaluated_returns_null_recommendation(self):
        decision = _compute_decision([])
        assert decision["recommendation"] == "insufficient data"
        assert decision["required_ratio"] is None

    def test_all_failed_new_results_returns_null_recommendation(self):
        evaluated = [{"listing": {"id": 1, "score": 7.0}, "old": None, "new": None}]
        decision = _compute_decision(evaluated)
        assert decision["recommendation"] == "insufficient data"

    def test_per_tier_breakdown_present(self):
        evaluated = [
            _make_eval(0, 30, 10, score=9.0),  # high tier
            _make_eval(0, 50, 20, score=6.0),  # mid tier
            _make_eval(0, 80, 10, score=3.0),  # low tier
        ]
        decision = _compute_decision(evaluated)
        assert "tier_breakdown" in decision
        assert decision["tier_breakdown"]["high"]["required_ratio"] == 0.75
        # mid: 50/(50+20) = 0.7142857...
        assert round(decision["tier_breakdown"]["mid"]["required_ratio"], 4) == 0.7143
        # low: 80/(80+10) = 0.8888...
        assert round(decision["tier_breakdown"]["low"]["required_ratio"], 4) == 0.8889

    def test_threshold_value_in_output(self):
        decision = _compute_decision([_make_eval(0, 1, 1)])
        assert decision["threshold"] == 0.80
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_eval_rubric.py::TestComputeDecision -v`
Expected: ImportError on `_compute_decision`.

- [ ] **Step 3: Implement `_compute_decision`**

Add to `scripts/eval_rubric.py`, above `_print_summary` (around line 750):

```python
_DECISION_THRESHOLD = 0.80  # Issue #341: > 80% required -> tune


def _tier_of(score: object) -> str:
    """Classify a listing's DB score into 'high', 'mid', or 'low'."""
    if not isinstance(score, (int, float)):
        return "unknown"
    if score >= 8:
        return "high"
    if score >= 5:
        return "mid"
    return "low"


def _compute_decision(evaluated: list[dict]) -> dict:
    """Compute the tune/no-change recommendation for Issue #341.

    The metric is the fraction of missing skills the rubric classified as
    ``required`` across all successful evaluations:

        required_ratio = sum(required) / (sum(required) + sum(nice_to_have))

    Threshold (Issue #341): > 80% -> "tune"; <= 80% -> "no change needed".
    If no evaluations produced both old and new results with valid counts,
    returns "insufficient data".

    Args:
        evaluated: List of result dicts with ``listing``, ``old``, ``new`` keys.

    Returns:
        Dict with keys: ``required_ratio`` (float or None), ``threshold``
        (float), ``recommendation`` (str), ``tier_breakdown`` (dict keyed by
        'high'/'mid'/'low' with per-tier required_ratio + counts), and
        ``counts`` (total required + nice_to_have).
    """
    by_tier: dict[str, dict[str, int]] = {
        "high": {"req": 0, "nth": 0, "n": 0},
        "mid": {"req": 0, "nth": 0, "n": 0},
        "low": {"req": 0, "nth": 0, "n": 0},
        "unknown": {"req": 0, "nth": 0, "n": 0},
    }
    total_req = 0
    total_nth = 0

    for e in evaluated:
        new = e.get("new")
        if new is None:
            continue
        req = len(new.get("missing_required_skills") or [])
        nth = len(new.get("missing_nice_to_have_skills") or [])
        tier = _tier_of((e.get("listing") or {}).get("score"))
        by_tier[tier]["req"] += req
        by_tier[tier]["nth"] += nth
        by_tier[tier]["n"] += 1
        total_req += req
        total_nth += nth

    def _ratio(req: int, nth: int) -> Optional[float]:
        combined = req + nth
        return (req / combined) if combined > 0 else None

    aggregate_ratio = _ratio(total_req, total_nth)

    if aggregate_ratio is None:
        recommendation = "insufficient data"
    elif aggregate_ratio > _DECISION_THRESHOLD:
        recommendation = "tune"
    else:
        recommendation = "no change needed"

    tier_breakdown = {}
    for tier, counts in by_tier.items():
        if tier == "unknown" and counts["n"] == 0:
            continue  # hide empty unknown bucket
        tier_breakdown[tier] = {
            "n": counts["n"],
            "required": counts["req"],
            "nice_to_have": counts["nth"],
            "required_ratio": _ratio(counts["req"], counts["nth"]),
        }

    return {
        "required_ratio": aggregate_ratio,
        "threshold": _DECISION_THRESHOLD,
        "recommendation": recommendation,
        "counts": {"required": total_req, "nice_to_have": total_nth},
        "tier_breakdown": tier_breakdown,
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_eval_rubric.py::TestComputeDecision -v`
Expected: 8 passed.

- [ ] **Step 5: Commit**

```bash
git add scripts/eval_rubric.py tests/test_eval_rubric.py
git commit -m "feat(eval): compute tune/no-change decision with per-tier breakdown (refs #274)"
```

---

### Task 4: Run metadata builder (pure, no DB/git calls in tests)

**Files:**
- Modify: `scripts/eval_rubric.py` (add `_build_run_meta` near `_compute_decision`)
- Test: `tests/test_eval_rubric.py`

Design note: `_build_run_meta` takes commit SHA, timestamp, and provider label as **parameters** so tests can pass deterministic values. `main()` is responsible for calling `git rev-parse HEAD` and `datetime.now()` to supply the live values.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_eval_rubric.py`:

```python
from scripts.eval_rubric import _build_run_meta


class TestBuildRunMeta:
    """Tests for _build_run_meta: constructs the metadata dict embedded in
    both the markdown and JSON artifacts."""

    def test_carries_all_required_fields(self):
        listings = [
            {"id": "src-1", "score": 9.0, "title": "x"},
            {"id": "src-2", "score": 6.0, "title": "y"},
            {"id": "src-3", "score": 3.0, "title": "z"},
        ]
        meta = _build_run_meta(
            listings=listings,
            requested_counts={"high": 32, "mid": 36, "low": 32},
            seed=20260424,
            provider_label="anthropic/claude-haiku-4-5",
            commit_sha="abc1234",
            run_iso="2026-04-24T15:00:00",
        )

        assert meta["commit_sha"] == "abc1234"
        assert meta["provider"] == "anthropic/claude-haiku-4-5"
        assert meta["seed"] == 20260424
        assert meta["run_iso"] == "2026-04-24T15:00:00"
        assert meta["requested_counts"] == {"high": 32, "mid": 36, "low": 32}
        assert meta["actual_counts"] == {"high": 1, "mid": 1, "low": 1}
        assert meta["sampled_ids"] == ["src-1", "src-2", "src-3"]

    def test_empty_listings_gives_zero_counts(self):
        meta = _build_run_meta(
            listings=[],
            requested_counts={"high": 1, "mid": 1, "low": 1},
            seed=0,
            provider_label="x/y",
            commit_sha="0",
            run_iso="2026-04-24T00:00:00",
        )
        assert meta["actual_counts"] == {"high": 0, "mid": 0, "low": 0}
        assert meta["sampled_ids"] == []

    def test_missing_score_falls_to_unknown_bucket(self):
        listings = [{"id": "a", "score": None, "title": "t"}]
        meta = _build_run_meta(
            listings=listings,
            requested_counts={"high": 0, "mid": 0, "low": 0},
            seed=0,
            provider_label="x/y",
            commit_sha="0",
            run_iso="2026-04-24T00:00:00",
        )
        # Listings with unclassifiable scores don't contribute to tier counts.
        assert meta["actual_counts"] == {"high": 0, "mid": 0, "low": 0}
        assert meta["sampled_ids"] == ["a"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_eval_rubric.py::TestBuildRunMeta -v`
Expected: ImportError.

- [ ] **Step 3: Implement `_build_run_meta`**

Add to `scripts/eval_rubric.py`, immediately after `_compute_decision`:

```python
def _build_run_meta(
    listings: list[dict],
    requested_counts: dict,
    seed: int,
    provider_label: str,
    commit_sha: str,
    run_iso: str,
) -> dict:
    """Build the run-metadata dict embedded in both artifacts.

    Args:
        listings:          Listings actually returned by the sample query.
        requested_counts:  Dict with keys 'high'/'mid'/'low' showing what was
                           asked for (pre-downgrade).
        seed:              Seed used for the run.
        provider_label:    String like 'anthropic/claude-haiku-4-5'.
        commit_sha:        Git commit SHA (short or long). Caller supplies.
        run_iso:           ISO-8601 timestamp string. Caller supplies.

    Returns:
        Dict with deterministic field set (see tests for exact shape).
    """
    actual_counts = {"high": 0, "mid": 0, "low": 0}
    for listing in listings:
        tier = _tier_of(listing.get("score"))
        if tier in actual_counts:
            actual_counts[tier] += 1

    return {
        "commit_sha": commit_sha,
        "provider": provider_label,
        "seed": seed,
        "run_iso": run_iso,
        "requested_counts": dict(requested_counts),
        "actual_counts": actual_counts,
        "sampled_ids": [listing.get("id") for listing in listings],
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_eval_rubric.py::TestBuildRunMeta -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add scripts/eval_rubric.py tests/test_eval_rubric.py
git commit -m "feat(eval): build run-metadata dict for artifact serialization (refs #274)"
```

---

### Task 5: JSON sidecar renderer

**Files:**
- Modify: `scripts/eval_rubric.py` (add `_render_json_sidecar`)
- Test: `tests/test_eval_rubric.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_eval_rubric.py`:

```python
import json as _json
from scripts.eval_rubric import _render_json_sidecar


class TestRenderJsonSidecar:
    """Tests for _render_json_sidecar: produces the JSON artifact payload."""

    def _fixture(self):
        meta = {
            "commit_sha": "abc1234",
            "provider": "anthropic/claude-haiku-4-5",
            "seed": 20260424,
            "run_iso": "2026-04-24T15:00:00",
            "requested_counts": {"high": 1, "mid": 1, "low": 1},
            "actual_counts": {"high": 1, "mid": 1, "low": 1},
            "sampled_ids": ["a", "b", "c"],
        }
        decision = {
            "required_ratio": 0.75,
            "threshold": 0.80,
            "recommendation": "no change needed",
            "counts": {"required": 3, "nice_to_have": 1},
            "tier_breakdown": {
                "high": {"n": 1, "required": 1, "nice_to_have": 0, "required_ratio": 1.0},
                "mid": {"n": 1, "required": 1, "nice_to_have": 0, "required_ratio": 1.0},
                "low": {"n": 1, "required": 1, "nice_to_have": 1, "required_ratio": 0.5},
            },
        }
        evaluated = [
            {
                "listing": {"id": "a", "title": "High role", "score": 9.0},
                "old": {"missing_skills": ["x"], "score": 9.0},
                "new": {
                    "match_score": 9.0,
                    "missing_required_skills": ["r"],
                    "missing_nice_to_have_skills": [],
                },
            },
        ]
        return meta, decision, evaluated

    def test_returns_serializable_dict(self):
        meta, decision, evaluated = self._fixture()
        payload = _render_json_sidecar(evaluated, meta, decision)
        # Must be JSON-serializable.
        _json.dumps(payload)
        assert payload["meta"] == meta
        assert payload["decision"] == decision

    def test_contains_per_listing_rows(self):
        meta, decision, evaluated = self._fixture()
        payload = _render_json_sidecar(evaluated, meta, decision)
        assert len(payload["per_listing"]) == 1
        row = payload["per_listing"][0]
        assert row["source_id"] == "a"
        assert row["title"] == "High role"
        assert row["tier"] == "high"
        assert row["old_missing"] == 1
        assert row["required"] == 1
        assert row["nice_to_have"] == 0

    def test_per_listing_handles_failed_old_or_new(self):
        meta, decision, _ = self._fixture()
        evaluated = [
            {"listing": {"id": "x", "title": "t", "score": 6.0}, "old": None, "new": None},
        ]
        payload = _render_json_sidecar(evaluated, meta, decision)
        row = payload["per_listing"][0]
        assert row["old_missing"] is None
        assert row["required"] is None
        assert row["nice_to_have"] is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_eval_rubric.py::TestRenderJsonSidecar -v`
Expected: ImportError.

- [ ] **Step 3: Implement `_render_json_sidecar`**

Add to `scripts/eval_rubric.py`, immediately after `_build_run_meta`:

```python
def _render_json_sidecar(
    evaluated: list[dict],
    meta: dict,
    decision: dict,
) -> dict:
    """Build the JSON sidecar payload for a rubric eval run.

    The returned dict is JSON-serializable and carries everything the
    markdown renderer also shows, so downstream tools can re-derive or
    re-format without re-running the eval.

    Args:
        evaluated: Per-listing results with ``listing``, ``old``, ``new`` keys.
        meta:      Dict from ``_build_run_meta``.
        decision:  Dict from ``_compute_decision``.

    Returns:
        JSON-serializable dict with keys ``meta``, ``decision``, ``per_listing``.
    """
    per_listing = []
    for e in evaluated:
        listing = e.get("listing") or {}
        old = e.get("old")
        new = e.get("new")
        old_missing = len(old.get("missing_skills") or []) if old else None
        if new:
            required = len(new.get("missing_required_skills") or [])
            nice_to_have = len(new.get("missing_nice_to_have_skills") or [])
        else:
            required = None
            nice_to_have = None
        per_listing.append({
            "source_id": listing.get("id"),
            "title": listing.get("title"),
            "tier": _tier_of(listing.get("score")),
            "old_missing": old_missing,
            "required": required,
            "nice_to_have": nice_to_have,
        })

    return {
        "meta": meta,
        "decision": decision,
        "per_listing": per_listing,
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_eval_rubric.py::TestRenderJsonSidecar -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add scripts/eval_rubric.py tests/test_eval_rubric.py
git commit -m "feat(eval): render JSON sidecar for rubric comparison runs (refs #274)"
```

---

### Task 6: Markdown report renderer

**Files:**
- Modify: `scripts/eval_rubric.py` (add `_render_markdown_report`)
- Test: `tests/test_eval_rubric.py`

Design note: Uses exact string formatting — tests verify presence of key substrings and the Decision section's exact line, not a full snapshot. This keeps tests resilient to future cosmetic changes while still enforcing the decision-critical content.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_eval_rubric.py`:

```python
from scripts.eval_rubric import _render_markdown_report


class TestRenderMarkdownReport:
    """Tests for _render_markdown_report: produces the Markdown artifact."""

    def _fixture_no_change(self):
        """Fixture whose aggregate ratio lands below 0.80."""
        meta = {
            "commit_sha": "abc1234",
            "provider": "anthropic/claude-haiku-4-5",
            "seed": 20260424,
            "run_iso": "2026-04-24T15:00:00",
            "requested_counts": {"high": 32, "mid": 36, "low": 32},
            "actual_counts": {"high": 1, "mid": 1, "low": 1},
            "sampled_ids": ["src-a", "src-b", "src-c"],
        }
        decision = {
            "required_ratio": 0.75,
            "threshold": 0.80,
            "recommendation": "no change needed",
            "counts": {"required": 3, "nice_to_have": 1},
            "tier_breakdown": {
                "high": {"n": 1, "required": 1, "nice_to_have": 0, "required_ratio": 1.0},
                "mid":  {"n": 1, "required": 1, "nice_to_have": 0, "required_ratio": 1.0},
                "low":  {"n": 1, "required": 1, "nice_to_have": 1, "required_ratio": 0.5},
            },
        }
        evaluated = [
            {
                "listing": {"id": "src-a", "title": "High role", "score": 9.0},
                "old": {"missing_skills": ["x"], "score": 9.0},
                "new": {
                    "match_score": 9.0,
                    "missing_required_skills": ["r"],
                    "missing_nice_to_have_skills": [],
                },
            },
        ]
        return evaluated, meta, decision

    def test_has_issue_274_header(self):
        evaluated, meta, decision = self._fixture_no_change()
        md = _render_markdown_report(evaluated, meta, decision)
        assert "# Rubric Eval Comparison" in md
        assert "Issue #274" in md

    def test_decision_section_shows_recommendation(self):
        evaluated, meta, decision = self._fixture_no_change()
        md = _render_markdown_report(evaluated, meta, decision)
        assert "## Decision" in md
        # Exact wording of the recommendation line.
        assert "**RECOMMENDATION: no change needed**" in md
        assert "75.0%" in md  # 0.75 formatted as percent

    def test_decision_tune_recommendation(self):
        evaluated, meta, decision = self._fixture_no_change()
        decision = dict(decision)
        decision["required_ratio"] = 0.85
        decision["recommendation"] = "tune"
        md = _render_markdown_report(evaluated, meta, decision)
        assert "**RECOMMENDATION: tune**" in md
        assert "85.0%" in md

    def test_run_metadata_fields_present(self):
        evaluated, meta, decision = self._fixture_no_change()
        md = _render_markdown_report(evaluated, meta, decision)
        assert "abc1234" in md  # commit
        assert "anthropic/claude-haiku-4-5" in md
        assert "20260424" in md  # seed
        assert "src-a" in md and "src-b" in md and "src-c" in md

    def test_per_tier_table_present(self):
        evaluated, meta, decision = self._fixture_no_change()
        md = _render_markdown_report(evaluated, meta, decision)
        assert "## Per-Tier Breakdown" in md
        assert "| Tier |" in md
        assert "High" in md and "Mid" in md and "Low" in md

    def test_per_listing_table_present(self):
        evaluated, meta, decision = self._fixture_no_change()
        md = _render_markdown_report(evaluated, meta, decision)
        assert "## Per-Listing Results" in md
        assert "High role" in md  # the one synthetic listing
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_eval_rubric.py::TestRenderMarkdownReport -v`
Expected: ImportError.

- [ ] **Step 3: Implement `_render_markdown_report`**

Add to `scripts/eval_rubric.py`, immediately after `_render_json_sidecar`:

```python
def _render_markdown_report(
    evaluated: list[dict],
    meta: dict,
    decision: dict,
) -> str:
    """Render the Markdown comparison report for a rubric eval run.

    Produces a decision-ready artifact with the top-level Decision section
    answering Issue #341's tune/no-change call, plus supporting per-tier
    and per-listing tables.

    Args:
        evaluated: Per-listing results with ``listing``, ``old``, ``new`` keys.
        meta:      Dict from ``_build_run_meta``.
        decision:  Dict from ``_compute_decision``.

    Returns:
        Complete markdown document as a single string, no trailing call needed.
    """
    lines: list[str] = []

    # --- Header ---
    run_date = meta.get("run_iso", "").split("T")[0]
    lines.append(f"# Rubric Eval Comparison — {run_date} (Issue #274)")
    lines.append("")

    # --- Metadata ---
    lines.append("## Run Metadata")
    lines.append(f"- Commit: `{meta['commit_sha']}`")
    lines.append(f"- Provider: `{meta['provider']}`")
    lines.append(f"- Seed: `{meta['seed']}`")
    lines.append(f"- Run at: `{meta['run_iso']}`")
    req = meta["requested_counts"]
    act = meta["actual_counts"]
    lines.append(
        f"- Sample (requested): {sum(req.values())} listings "
        f"({req['high']} high / {req['mid']} mid / {req['low']} low)"
    )
    lines.append(
        f"- Sample (actual): {sum(act.values())} listings "
        f"({act['high']} high / {act['mid']} mid / {act['low']} low)"
    )
    ids_joined = ", ".join(str(sid) for sid in meta["sampled_ids"])
    lines.append(f"- Sampled source_ids: `{ids_joined}`")
    lines.append("")

    # --- Decision ---
    lines.append("## Decision")
    lines.append("- Metric: `required / (required + nice_to_have)` across all successful evaluations")
    lines.append(f"- Threshold (Issue #341): `> {decision['threshold'] * 100:.0f}% → tune`; "
                 f"`≤ {decision['threshold'] * 100:.0f}% → close as \"no change\"`")
    ratio = decision["required_ratio"]
    if ratio is None:
        lines.append("- **Aggregate result: insufficient data**")
    else:
        lines.append(f"- **Aggregate result: {ratio * 100:.1f}%**")
    lines.append(f"- **RECOMMENDATION: {decision['recommendation']}**")
    lines.append("")

    # --- Per-tier table ---
    lines.append("## Per-Tier Breakdown")
    lines.append("| Tier | N | % required | % nice-to-have |")
    lines.append("|------|---|------------|----------------|")
    for tier_key, label in [("high", "High"), ("mid", "Mid"), ("low", "Low")]:
        tb = decision["tier_breakdown"].get(tier_key)
        if not tb:
            continue
        req_ratio = tb.get("required_ratio")
        if req_ratio is None:
            req_cell = "—"
            nth_cell = "—"
        else:
            req_cell = f"{req_ratio * 100:.1f}%"
            nth_cell = f"{(1 - req_ratio) * 100:.1f}%"
        lines.append(
            f"| {label} | {tb['n']} | {req_cell} | {nth_cell} |"
        )
    lines.append("")

    # --- Per-listing table ---
    lines.append("## Per-Listing Results")
    lines.append("| source_id | title | tier | old_missing | required | nice_to_have |")
    lines.append("|-----------|-------|------|-------------|----------|--------------|")
    for e in evaluated:
        listing = e.get("listing") or {}
        old = e.get("old")
        new = e.get("new")
        title = _truncate(listing.get("title") or "", 50)
        tier = _tier_of(listing.get("score"))
        old_cell = len(old.get("missing_skills") or []) if old else "FAILED"
        if new:
            req_cell = len(new.get("missing_required_skills") or [])
            nth_cell = len(new.get("missing_nice_to_have_skills") or [])
        else:
            req_cell = "FAILED"
            nth_cell = "FAILED"
        lines.append(
            f"| `{listing.get('id', '?')}` | {title} | {tier} | "
            f"{old_cell} | {req_cell} | {nth_cell} |"
        )
    lines.append("")

    return "\n".join(lines)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_eval_rubric.py::TestRenderMarkdownReport -v`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add scripts/eval_rubric.py tests/test_eval_rubric.py
git commit -m "feat(eval): render Markdown comparison report for rubric eval runs (refs #274)"
```

---

### Task 7: Wire `--seed` and `--output` into `main()`

**Files:**
- Modify: `scripts/eval_rubric.py` (`_parse_args` ~line 877, `main()` ~line 912)
- Test: `tests/test_eval_rubric.py`

Design note: `main()` is the integration point. It handles: seed defaulting to today's date if not given, fetching commit SHA via `subprocess.run(["git", "rev-parse", "HEAD"])` with a `"unknown"` fallback, capturing the ISO timestamp, and writing both artifacts when `--output` is set. We test the arg parsing alone; the full `main()` path requires a live DB and is covered by the Task 8 run.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_eval_rubric.py`:

```python
from scripts.eval_rubric import _parse_args
from unittest.mock import patch


class TestParseArgs:
    """Tests for the extended CLI argument parser."""

    def test_seed_defaults_to_date_integer(self):
        with patch("sys.argv", ["eval_rubric.py"]):
            args = _parse_args()
        # Default seed is today's date as YYYYMMDD int.
        assert isinstance(args.seed, int)
        assert args.seed >= 20260101  # sanity: positive and plausible

    def test_seed_flag_overrides_default(self):
        with patch("sys.argv", ["eval_rubric.py", "--seed", "42"]):
            args = _parse_args()
        assert args.seed == 42

    def test_output_defaults_to_none(self):
        with patch("sys.argv", ["eval_rubric.py"]):
            args = _parse_args()
        assert args.output is None

    def test_output_flag_captures_path(self):
        with patch("sys.argv", ["eval_rubric.py", "--output", "docs/eval/x.md"]):
            args = _parse_args()
        assert args.output == "docs/eval/x.md"

    def test_existing_count_flag_still_works(self):
        with patch("sys.argv", ["eval_rubric.py", "--count", "50"]):
            args = _parse_args()
        assert args.count == 50
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_eval_rubric.py::TestParseArgs -v`
Expected: FAIL — likely `AttributeError: 'Namespace' object has no attribute 'seed'`.

- [ ] **Step 3: Extend `_parse_args`**

Add to the top of `scripts/eval_rubric.py` near the other imports (after `import sys`):

```python
import subprocess
from datetime import datetime, date
```

Modify `_parse_args` in `scripts/eval_rubric.py` (currently lines 877-909). Add the two new arguments after the existing `--verbose`:

```python
    parser.add_argument(
        "--seed",
        type=int,
        default=int(date.today().strftime("%Y%m%d")),
        metavar="N",
        help=(
            "Integer seed for deterministic sampling. Defaults to today's "
            "date as YYYYMMDD so runs on the same day reproduce the sample."
        ),
    )
    parser.add_argument(
        "--output",
        metavar="PATH",
        default=None,
        help=(
            "Write a markdown report to PATH and a JSON sidecar to the "
            "same path with .json extension. Script still prints to stdout."
        ),
    )
    return parser.parse_args()
```

- [ ] **Step 4: Run tests to verify `_parse_args` tests pass**

Run: `pytest tests/test_eval_rubric.py::TestParseArgs -v`
Expected: 5 passed.

- [ ] **Step 5: Extend `main()` to use `--seed` and `--output`**

Modify `main()` in `scripts/eval_rubric.py`. The changes:

1. Pass `args.seed` to `_fetch_stratified_sample`.
2. After `_print_summary(...)` completes, if `args.output` is set, compute decision + meta and write both artifacts.

In `main()`, replace the existing call `listings = _fetch_stratified_sample(conn, high_n, mid_n, low_n)` with:

```python
    listings = _fetch_stratified_sample(conn, high_n, mid_n, low_n, seed=args.seed)
```

Then, at the end of `main()` (after the existing `_print_summary(evaluated, provider_label)` call), add:

```python
    # --- Write artifacts if --output was set ---
    if args.output:
        try:
            commit_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()[:7]
        except (subprocess.CalledProcessError, FileNotFoundError):
            commit_sha = "unknown"

        run_iso = datetime.now().isoformat(timespec="seconds")
        requested_counts = {"high": high_n, "mid": mid_n, "low": low_n}

        meta = _build_run_meta(
            listings=listings,
            requested_counts=requested_counts,
            seed=args.seed,
            provider_label=provider_label,
            commit_sha=commit_sha,
            run_iso=run_iso,
        )
        decision = _compute_decision(evaluated)

        md_path = args.output
        json_path = md_path.rsplit(".", 1)[0] + ".json"

        os.makedirs(os.path.dirname(md_path) or ".", exist_ok=True)

        with open(md_path, "w", encoding="utf-8") as f:
            f.write(_render_markdown_report(evaluated, meta, decision))
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(_render_json_sidecar(evaluated, meta, decision), f, indent=2)

        print(f"\nArtifacts written:\n  {md_path}\n  {json_path}")
```

- [ ] **Step 6: Run the full eval-rubric test suite to ensure nothing regressed**

Run: `pytest tests/test_eval_rubric.py tests/test_eval_rubric_3way.py tests/test_eval_encoding.py -v`
Expected: all pass. If any pre-existing test fails because it called `_fetch_stratified_sample` without `seed`, update it per Task 2 Step 4.

- [ ] **Step 7: Commit**

```bash
git add scripts/eval_rubric.py tests/test_eval_rubric.py
git commit -m "feat(eval): wire --seed and --output flags into main (closes #274 once run committed)"
```

---

### Task 8: Full verification + live run

**Files:**
- Create at runtime: `docs/eval/2026-04-24-rubric-comparison-274.md` (and `.json` sidecar)

- [ ] **Step 1: Full CI mirror**

Run these two commands; both must pass before invoking the live run. This is the same pair CI runs as separate jobs.

```bash
cd "I:/Web Development/job-matcher-pr/.worktrees/feat-274-rubric-eval-comparison"
ruff check .
pytest
```

Expected: `ruff` clean; `pytest` 100% passing (expect ~2070+ tests, same order of magnitude as Task 7 Step 6 plus whatever else is in the repo).

- [ ] **Step 2: Confirm the worktree is on the feature branch**

```bash
git -C "I:/Web Development/job-matcher-pr/.worktrees/feat-274-rubric-eval-comparison" branch --show-current
```

Expected: `feat/274-rubric-eval-comparison`. If anything else, STOP and investigate.

- [ ] **Step 3: Confirm DATABASE_URL points at prod (read-only script, but still)**

`scripts/eval_rubric.py` is read-only — it never writes to the DB. But it needs a `DATABASE_URL` that points at a database with scored listings. Run from the user's PowerShell:

```powershell
cd "I:/Web Development/job-matcher-pr/.worktrees/feat-274-rubric-eval-comparison"
echo $env:DATABASE_URL
```

If empty or pointing at a test DB without real scored listings, export the prod connection string before the run. The user should handle this step interactively.

- [ ] **Step 4: Execute the live run**

From the user's PowerShell, in the worktree:

```powershell
python scripts/eval_rubric.py `
  --count 100 `
  --seed 20260424 `
  --output docs/eval/2026-04-24-rubric-comparison-274.md
```

Expected runtime: 15-25 minutes (100 listings × 2 LLM calls × Haiku 4.5 at ~3-8s per call). Watch for auth failures on the Anthropic provider — if it drops mid-run the script will short-circuit on that provider.

Expected terminal output ends with:
```
Artifacts written:
  docs/eval/2026-04-24-rubric-comparison-274.md
  docs/eval/2026-04-24-rubric-comparison-274.json
```

- [ ] **Step 5: Sanity-check the artifact**

Open the markdown file and verify:
1. The Decision section shows a RECOMMENDATION line with either "tune" or "no change needed".
2. The sample (actual) counts are ≥ 80 total. If any tier is significantly under its requested count, note it for the PR body.
3. The per-tier table has values for all three rows, not `—`.
4. The per-listing table has ≥ 80 rows.

If all four hold, proceed to commit. If the aggregate Decision row reads "insufficient data" or any check fails, STOP and investigate — a broken run should not be committed.

- [ ] **Step 6: Commit the artifacts**

```bash
cd "I:/Web Development/job-matcher-pr/.worktrees/feat-274-rubric-eval-comparison"
git add docs/eval/2026-04-24-rubric-comparison-274.md docs/eval/2026-04-24-rubric-comparison-274.json
git commit -m "eval(rubric): 100-listing comparison run results (closes #274)"
```

- [ ] **Step 7: Push + open PR**

```bash
git push -u origin feat/274-rubric-eval-comparison
```

Then open a PR via `mcp__plugin_github_github__create_pull_request` with body (user will fill in the actual recommendation):

```markdown
## Summary
- Extends `scripts/eval_rubric.py` with `--seed` (deterministic sampling) and `--output` (markdown + JSON artifacts) flags
- Adds pure functions: `_normalize_seed`, `_compute_decision`, `_build_run_meta`, `_render_markdown_report`, `_render_json_sidecar`
- Runs the 100-listing live eval and commits the resulting artifact under `docs/eval/`

## Decision
See `docs/eval/2026-04-24-rubric-comparison-274.md` for the full Decision block. Summary:
- RECOMMENDATION: `<copy from artifact>`
- Aggregate required-ratio: `<copy from artifact>`

Closes #274

Refs #248 (parent), #341 (follow-up tune/no-tune call; this PR produces its input data)

🤖 *Generated by Claude Code on behalf of @cbeaulieu-gt*
```

The closing keyword `Closes #274` must be plain text in the PR body (no backticks) per the repo's closing-keyword convention.

---

### Task 9: Post-merge decision handoff

**Precondition:** PR from Task 8 is merged.

- [ ] **Step 1: Comment on #274**

Use `mcp__plugin_github_github__add_issue_comment` on repo `cbeaulieu-gt/job-matcher-pr` issue `274`. Body:

```markdown
Eval comparison run complete. Artifact committed at `docs/eval/2026-04-24-rubric-comparison-274.md` (commit `<merge SHA>`).

**Decision:** `<paste Decision section from artifact>`

Closing this issue — see #341 for the tune/no-tune follow-up.

🤖 *Generated by Claude Code on behalf of @cbeaulieu-gt*
```

- [ ] **Step 2: Close #274**

If the `Closes #274` in the PR body auto-closed it on merge, skip. Otherwise close via `mcp__plugin_github_github__issue_write` with state update.

- [ ] **Step 3: Comment on #341 with decision input**

Use `add_issue_comment` on issue `341`. Body:

```markdown
Eval comparison run (#274) is complete.

**Aggregate required-ratio: <N.N>%** — threshold is > 80%.

**Recommendation: <tune | no change needed>**

Per-tier breakdown:
<paste table from artifact>

See `docs/eval/2026-04-24-rubric-comparison-274.md` for the full artifact.

Ticking the first two AC boxes here since the run has executed and produced metrics. The tune/no-tune decision lives with @cbeaulieu-gt.

🤖 *Generated by Claude Code on behalf of @cbeaulieu-gt*
```

Update the #341 issue body to check the first two AC boxes:
- [x] Run rubric eval on the existing corpus with the current prompt (post-#339 merge)
- [x] Produce aggregate metrics on required/nice-to-have split balance and disjoint-set warnings

- [ ] **Step 4: If `recommendation == "no change needed"`, close #341**

Use `issue_write` to close #341 with final state reason `completed` and a short comment:
```markdown
Closing: measured at <N.N>%, below the 80% threshold. No prompt tune required.

🤖 *Generated by Claude Code on behalf of @cbeaulieu-gt*
```

If `recommendation == "tune"`, leave #341 open — a separate session will handle the prompt revision.

- [ ] **Step 5: Clean up the worktree**

```bash
git -C "I:/Web Development/job-matcher-pr" worktree remove .worktrees/feat-274-rubric-eval-comparison
```

If that errors because origin auto-deleted the branch on merge, the worktree may still be present; run `git worktree prune` to sweep it.

---

## What I'm deliberately *not* planning

- No changes to `ingest.py`, production scoring path, or `db.py`.
- No DB schema migration.
- No UI work.
- No prompt revision. That's #341 work, gated on the Task 9 Step 4 decision.
- No multi-provider runs (spec explicitly chose prod-default Haiku).
- No variance-across-multiple-runs averaging (YAGNI — single run is the measurement).

---

🤖 *Generated by Claude Code on behalf of @cbeaulieu-gt*
