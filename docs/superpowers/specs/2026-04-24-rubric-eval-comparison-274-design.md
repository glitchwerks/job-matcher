# Rubric Eval Comparison Run — Design

**Date:** 2026-04-24
**Issue:** [#274](https://github.com/cbeaulieu-gt/job-matcher-pr/issues/274) (parent: #248; follow-up: #341)
**Author:** Claude Code on behalf of @cbeaulieu-gt
**Status:** Approved — ready for implementation plan

---

## 1. Purpose

Run the existing A/B rubric evaluation (`scripts/eval_rubric.py`, merged via PR #339) against a statistically meaningful live sample to produce a decision input for issue #341 (tune-or-close prompt-categorization follow-up).

The required output is a reproducible, decision-ready artifact that mechanically answers: **does the current prompt over-classify missing skills as `required` beyond the 80% threshold #341 uses as its tune trigger?**

## 2. Scope

**In scope:**

- Minimal extension of `scripts/eval_rubric.py` to make runs reproducible and to emit structured output artifacts.
- One live-DB eval run at `--count 100` using the production default provider (Anthropic Haiku 4.5).
- Generation and commit of a markdown report + JSON sidecar under `docs/eval/`.
- Comment on #274 announcing run completion; comment on #341 carrying the Decision + per-tier breakdown; close #274.

**Explicitly out of scope:**

- No changes to `ingest.py` / production scoring path.
- No DB schema changes.
- No UI changes.
- No prompt tuning (#341 work is gated on this run's output).
- No multi-pass variance estimation — one run is the measurement.

## 3. Sample Design

- **Size:** 100 listings, a 4× expansion from the 25-listing Apr 18 exploratory run already recorded in #274's body. Chosen for adequate statistical power on a single binary categorization threshold without entering real-money cost territory.
- **Stratification:** Preserve the existing 8 / 9 / 8 high / mid / low ratio used by `_fetch_stratified_sample` — scales to **32 high / 36 mid / 32 low** at the 100-listing count. The `max(1, round(...))` fallback in the current script handles any rounding edge cases.
- **Provider:** Whatever is first in `providers.json::provider_order` — currently `anthropic` / `claude-haiku-4-5-20251001`. This matches production scoring behavior, so the tune/no-tune decision for #341 directly reflects what production Haiku does with the prompt rather than the cleaner output of a stronger model the system never runs.
- **Reproducibility:** Deterministic seed (see section 4).

## 4. Code Changes — `scripts/eval_rubric.py`

Three additions, approximately 60 lines of production code plus tests. The existing A/B comparison logic stays untouched.

### 4.1 New CLI flags

| Flag | Type | Default | Purpose |
|------|------|---------|---------|
| `--seed` | `int` | `int(date.today().strftime("%Y%m%d"))` | Seed for stratified sampling. Default makes unseeded runs reproducible within the same calendar day. |
| `--output` | `str` | `None` | When set, also writes a markdown report to this path plus a JSON sidecar at the same stem with `.json`. Script still prints to stdout for interactive use. |

### 4.2 `_fetch_stratified_sample` becomes seedable

Add a `seed` parameter. Before the sample query, call `cur.execute("SELECT setseed(%s)", (_normalize_seed(seed),))` where `_normalize_seed` maps the integer seed into the `[-1.0, 1.0]` range PostgreSQL's `setseed()` requires (e.g. via `(seed % 10_000_000) / 10_000_000 * 2 - 1`).

If any tier returns fewer rows than requested (live DB might not have 32 high-tier listings on a given day), log a clear warning and downgrade the per-tier counts rather than erroring. The artifact records the actual counts used, not the requested counts.

### 4.3 Two new render functions

- `_write_markdown_report(evaluated: list[dict], meta: dict, path: str) -> None` — renders the full markdown artifact described in section 5.
- `_write_json_sidecar(evaluated: list[dict], meta: dict, path: str) -> None` — writes the same data as structured JSON to `path.replace(".md", ".json")`. Enables regenerating the #274 comment or any downstream analysis without re-running the eval.

`meta` carries: commit SHA (from `git rev-parse HEAD`), provider label, seed, requested counts, actual counts, sampled `source_id` list, run timestamp.

## 5. Artifact Layout

### 5.1 Filesystem

- Markdown: `docs/eval/2026-04-24-rubric-comparison-274.md`
- JSON sidecar: `docs/eval/2026-04-24-rubric-comparison-274.json`
- Creates `docs/eval/` on first run.

### 5.2 Markdown structure

```markdown
# Rubric Eval Comparison — 2026-04-24 (Issue #274)

## Run Metadata
- Commit: <sha>
- Provider: anthropic/claude-haiku-4-5-20251001
- Seed: 20260424
- Sample (requested): 100 listings (32 high / 36 mid / 32 low)
- Sample (actual):    <resolved counts>
- Sampled source_ids: [comma-separated list]
- Run at: <ISO-8601 timestamp>

## Decision
- Metric: % of missing skills classified as `required`
  = sum(required) / (sum(required) + sum(nice_to_have))
- Threshold (Issue #341): > 80% → tune prompt; ≤ 80% → close "no change"
- **Aggregate result: <N.N>%**
- **RECOMMENDATION: <tune|no change needed>**

## Per-Tier Breakdown
| Tier | N | % required | % nice-to-have | disjoint warnings |
|------|---|-----------|----------------|-------------------|
| High | ... | ... | ... | ... |
| Mid  | ... | ... | ... | ... |
| Low  | ... | ... | ... | ... |

## Aggregate Metrics (A/B — old flat vs new split)
<existing comparison table currently printed to stdout, rendered as markdown>

## Per-Listing Results
| source_id | title | tier | old_count | required | nice_to_have | disjoint |
|-----------|-------|------|-----------|----------|--------------|----------|
| ... | ... | ... | ... | ... | ... | ... |
```

The Decision section intentionally sits at the top so a reader can make the #341 call without scrolling through the data.

## 6. Testing

Unit tests (no live DB, no LLM calls). All run as part of the standard `pytest` suite in CI.

- **Seed determinism.** Mock cursor. Assert `setseed()` is called with the correctly normalized seed before the sample query executes.
- **Markdown rendering.** Feed `_write_markdown_report` a synthetic 10-listing result set with known counts; snapshot-compare the generated markdown against a checked-in fixture, excluding volatile fields (timestamp, commit SHA).
- **JSON sidecar shape.** Same synthetic set; assert the JSON has all required keys, that counts round-trip, and that `source_ids` list matches.
- **Decision threshold.** Call the decision-rendering logic with synthetic aggregate ratios at 0.799, 0.800, 0.801; assert the recommendation text matches the `> 80%` rule exactly.
- **Graceful tier downgrade.** Mock DB returning fewer rows than requested in one tier; assert a warning is logged, the artifact's `Sample (actual)` line reflects the reduced counts, and the run completes successfully.

Pre-push verification mirrors CI: `ruff check .` clean, full `pytest` green.

## 7. Run Procedure

The run executes inside the `feat/274-rubric-eval-comparison` worktree *after* the code changes are written, tested, and committed on that branch, but *before* the PR is opened. This keeps the code changes and the authoritative run output on the same PR.

Invocation:

```powershell
python scripts/eval_rubric.py `
  --count 100 `
  --seed 20260424 `
  --output docs/eval/2026-04-24-rubric-comparison-274.md
```

After the run completes successfully, commit both output files (`*.md` + `*.json`) onto the `feat/274-rubric-eval-comparison` branch, then open the PR carrying all three things together: the tool extensions, the tests, and the run output.

**Why one PR, not two:** a single PR means merging it makes the code changes available on `main` *and* the authoritative run output land at the same time, closing #274 in one step. Splitting into two PRs would require running the eval against a code version that isn't yet on `main`, then waiting, then committing output later — same end state, more moving parts.

## 8. Decision Handoff (after merge)

1. **On #274:** post a comment with the Decision block verbatim and a link to the committed artifact. Close #274.
2. **On #341:** post a comment with the Decision + per-tier breakdown, tick the first two acceptance-criteria boxes (eval run executed, metrics produced). Then:
   - If `RECOMMENDATION: no change needed` → close #341 immediately as "measured, no change needed."
   - If `RECOMMENDATION: tune` → keep #341 open. Next session tackles the prompt revision with these measured numbers as the baseline for comparison.

## 9. Risks and Mitigations

| Risk | Mitigation |
|------|-----------|
| Live DB has < 32 listings in a tier | Graceful downgrade + warning (section 4.2). Artifact records actual counts. |
| Haiku categorization output is noisy, driving the metric near the 80% threshold | Accepted — Haiku is what ships, so Haiku is what we measure. Per-tier breakdown (section 5.2) exposes whether noise is concentrated in low-tier listings, which is itself decision-relevant. |
| LLM-call cost drifts unexpectedly | 100 listings × 2 prompts on Haiku is ≲ $0.15; tracked in logs already. Below any real budget concern. |
| Nondeterministic LLM output makes re-runs non-comparable | Seeded sample guarantees the same inputs; LLM variance within the same prompt is accepted as measurement noise and is not averaged across runs (YAGNI). |
| Rerunning later shows different results | The sampled `source_id` list is recorded so a future run can target the same listings; the seed lets a future run draw the same sample from the same DB state. Neither guarantees identical LLM outputs, which is correct — that variance is part of the signal. |

---

🤖 *Generated by Claude Code on behalf of @cbeaulieu-gt*
