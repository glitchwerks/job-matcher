"""
scripts/eval_rubric.py — A/B evaluation: current scoring prompt vs. new rubric.

Pulls a stratified sample of ~25 already-scored listings from PostgreSQL,
re-scores each listing with BOTH the current prompt and the new rubric-based
prompt (Issue #228 / design spec: docs/superpowers/specs/2026-04-13-scoring-rubric-design.md),
then prints a per-listing comparison table and aggregate summary statistics.

This script is READ-ONLY — it never modifies the database.

Usage (from repo root):
    python scripts/eval_rubric.py
    python scripts/eval_rubric.py --count 30
    python scripts/eval_rubric.py --provider anthropic
    python scripts/eval_rubric.py --verbose

Environment:
    DATABASE_URL — PostgreSQL connection string (required, same as rest of app)
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from typing import Optional

# ---------------------------------------------------------------------------
# Ensure repo root is on sys.path so local modules resolve when running
# the script directly (e.g. ``python scripts/eval_rubric.py``).
# ---------------------------------------------------------------------------
_REPO_ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.abspath(_REPO_ROOT))

import psycopg2  # noqa: E402
import psycopg2.extras  # noqa: E402

from credentials import CredentialError, load_providers  # noqa: E402
from ingest import (  # noqa: E402
    _generate_location_notes,
    format_education_for_prompt,
    format_skills_for_prompt,
    _provider_model,
    _provider_name,
)
from providers import LLMProvider, build_provider_chain  # noqa: E402
from providers.anthropic_provider import strip_fences  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_CONFIG_DIR = os.path.join(_REPO_ROOT, "config")
_DEFAULT_PROFILE_PATH = os.path.join(_CONFIG_DIR, "profile.json")

# Tier target counts (total ~25 by default)
_HIGH_TARGET = 8    # score >= 8
_MID_TARGET = 9     # 5 <= score < 8
_LOW_TARGET = 8     # score < 5

# Composite score weights (must match spec)
_SKILLS_WEIGHT = 0.60
_ROLE_FIT_WEIGHT = 0.40

# Valid apply_recommendation values
_VALID_RECOMMENDATIONS = frozenset(
    {"Strong Yes", "Yes", "Maybe", "No", "Hard No"}
)

# ---------------------------------------------------------------------------
# Current (old) prompt — exact copy of _PROMPT_TEMPLATE from ingest.py
# ---------------------------------------------------------------------------

_OLD_PROMPT_TEMPLATE = """\
You are evaluating a job listing for a candidate. Score how well the role \
matches their profile.

CANDIDATE PROFILE:
{profile_json}

JOB DESCRIPTION:
{description}

Respond with ONLY a JSON object. No explanation, no markdown, no code fences. \
The object must have exactly these keys:
- "score": integer from 0 to 10 (10 = perfect match)
- "matched_skills": array of strings (skills from the profile that this role \
uses)
- "missing_skills": array of strings (skills this role requires that the \
candidate lacks or has little experience in)
- "concerns": array of strings (red flags or mismatches, e.g. seniority \
mismatch, wrong industry, anti-preferences violated)
- "verdict": one sentence summarising the match

JSON only:\
"""

# ---------------------------------------------------------------------------
# New rubric prompt (based on design spec)
# ---------------------------------------------------------------------------

_RUBRIC_PROMPT_TEMPLATE = """\
You are evaluating a job listing for a candidate using a structured rubric. \
Assess three dimensions and produce a structured JSON response. Think from two \
perspectives: a hiring manager evaluating the candidate's technical fit, and \
the candidate evaluating whether this role fits their career goals.

=== CANDIDATE PROFILE ===
{profile_json}

=== JOB DESCRIPTION ===
{description}

=== EVALUATION INSTRUCTIONS ===

STEP 1 — DEAL-BREAKER CHECK
Review the candidate's deal_breakers section (if present). For each active \
flag, check whether the role triggers it:
- requires_relocation: true → flag if the role requires relocation outside \
the candidate's location radius
- excluded_anti_preferences: true → flag if the role matches any of the \
candidate's anti_preferences
- requires_clearance: true → flag if the role requires a security clearance \
the candidate does not hold
- requires_visa_sponsorship: true → flag if the role has work authorization \
requirements the candidate cannot meet
- custom entries → apply each verbatim rule

List any triggered deal-breakers as strings in the "deal_breakers" array. \
Return an empty array [] if none apply.

STEP 2 — DIMENSION 1: SKILLS MATCH (weight: 60% of match_score)
Evaluate from a HIRING MANAGER'S perspective. Score 0–10 using this adjacency \
framework:
- Direct match: JD skill is in the candidate's profile (full credit)
- Close adjacent: different but closely related tool/framework (minor deduction)
- Distant adjacent: related domain but significant experience gap (significant \
deduction)
- No adjacency: entirely different skill with no relevant background \
(deal-breaker if central to the role)

Gap pattern logic (LLM judgment call):
- 1 distant adjacent + rest direct/close → NOT a deal-breaker
- 2+ distant adjacencies on CORE competencies → likely a deal-breaker
- Missing skill that IS in the job title → always a deal-breaker

HIRING MANAGER ASSESSMENT LABELS (choose the one that best fits):
- "Ideal candidate"                  → score 9–10: meets all required + most \
preferred skills
- "Strong candidate, minor gaps"     → score 7–8: meets core, gaps are \
nice-to-haves or close-adjacent
- "Decent candidate, worth considering" → score 5–6: most foundations present, \
1–2 required gaps with adjacent experience
- "Stretch candidate, significant gaps" → score 3–4: missing core competencies \
but has related background
- "Not qualified for this role"      → score 1–2: missing the defining skills \
of the position

STEP 3 — DIMENSION 2: ROLE & LEVEL FIT (weight: 40% of match_score)
Evaluate from the CANDIDATE'S perspective. Assess role identity and career \
trajectory — do NOT re-evaluate technical skill overlap (that is covered by \
Dimension 1). Consider: seniority match, function/archetype alignment, career \
trajectory.

ROLE FIT LABELS (choose the one that best fits):
- "Exact match"            → score 9–10: same archetype, correct seniority, \
function aligns with trajectory
- "Strong fit, minor mismatch" → score 7–8: same broad function, seniority \
within one level, or adjacent archetype
- "Partial fit"            → score 5–6: related function but different \
archetype, or seniority gap of two levels
- "Weak fit"               → score 3–4: different function with transferable \
foundations, or major seniority mismatch
- "Wrong role"             → score 1–2: entirely different discipline, or \
entry-level for senior candidate with no upside

STEP 4 — LISTING QUALITY: RED FLAGS (parallel signal — NOT included in \
match_score)
Score the listing itself 0–10. Start at 10, deduct for:
- Stale posting (posted 30+ days ago) → deduct 2–3
- Vague job description (no specific requirements, generic language) → \
deduct 2–3
- Seniority contradictions (title says "Senior" but requirements are \
entry-level, or vice versa) → deduct 1–2
- Staffing agency repost (same JD reposted multiple times or from a recruiter \
farm) → deduct 1–2
- Ghost job signals (excessive requirements relative to industry norm, \
unrealistically broad scope) → deduct 1–2

STEP 5 — APPLY RECOMMENDATION
Guidelines based on match_score = (0.60 × skills_match) + (0.40 × role_fit):
- "Strong Yes" → match_score >= 8.0, no deal-breakers
- "Yes"        → match_score 6.0–7.9, no deal-breakers
- "Maybe"      → match_score 5.0–5.9, no deal-breakers
- "No"         → match_score < 5.0
- "Hard No"    → any deal-breaker present (regardless of score)

Override rules:
- You MAY override upward by ONE tier if the qualitative case is strong \
(e.g., a 5.8 with near-perfect role fit → "Yes" instead of "Maybe")
- You MUST NOT override downward

STEP 6 — RETURN JSON
Return ONLY a valid JSON object, no markdown, no code fences, no explanation. \
Use exactly these keys:

{{
  "dimensions": {{
    "skills_match": <integer 0-10>,
    "role_fit": <integer 0-10>,
    "red_flags": <integer 0-10>
  }},
  "hiring_assessment": "<one of the five labels from Step 2>",
  "role_fit_assessment": "<one of the five labels from Step 3>",
  "deal_breakers": [<strings>],
  "matched_skills": [<strings>],
  "missing_skills": [<strings>],
  "concerns": [<strings>],
  "archetype": "<job archetype, e.g. Backend Engineer, Data Scientist>",
  "apply_recommendation": "<Strong Yes|Yes|Maybe|No|Hard No>",
  "verdict": "<one sentence summarising the match>"
}}

JSON only:\
"""

# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------


def _connect() -> psycopg2.extensions.connection:
    """Open a psycopg2 connection using the DATABASE_URL env var.

    Returns:
        An open psycopg2 connection with autocommit off.

    Raises:
        SystemExit: If DATABASE_URL is not set or the connection fails.
    """
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print(
            "ERROR: DATABASE_URL environment variable is not set.",
            file=sys.stderr,
        )
        sys.exit(1)
    try:
        return psycopg2.connect(db_url)
    except psycopg2.OperationalError as exc:
        print(f"ERROR: Could not connect to database: {exc}", file=sys.stderr)
        sys.exit(1)


def _fetch_stratified_sample(
    conn: psycopg2.extensions.connection,
    high_n: int,
    mid_n: int,
    low_n: int,
) -> list[dict]:
    """Fetch a stratified sample of scored listings with full descriptions.

    Stratification targets:
    - High tier:  score >= 8
    - Mid tier:   5 <= score < 8
    - Low tier:   score < 5

    If a tier has fewer listings than requested, all available are returned.
    Listings are ordered randomly within each tier to avoid systematic bias.

    Args:
        conn:   Open psycopg2 connection.
        high_n: Target count for high-tier listings.
        mid_n:  Target count for mid-tier listings.
        low_n:  Target count for low-tier listings.

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
        for where_clause, limit in tiers:
            cur.execute(
                query.format(where_clause=where_clause),
                (limit,),
            )
            rows = cur.fetchall()
            results.extend(dict(row) for row in rows)

    return results


# ---------------------------------------------------------------------------
# Profile loading and preparation
# ---------------------------------------------------------------------------


def _load_profile(profile_path: str) -> dict:
    """Load and return the candidate profile from profile.json.

    Args:
        profile_path: Absolute or relative path to profile.json.

    Returns:
        Parsed profile dict.

    Raises:
        SystemExit: If the file is missing or contains invalid JSON.
    """
    try:
        with open(profile_path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        print(
            f"ERROR: Profile not found at {profile_path}",
            file=sys.stderr,
        )
        sys.exit(1)
    except json.JSONDecodeError as exc:
        print(
            f"ERROR: profile.json contains invalid JSON: {exc}",
            file=sys.stderr,
        )
        sys.exit(1)


def _prepare_scoring_profile(profile: dict) -> dict:
    """Apply the same transformations as score_listing_with_fallback().

    1. Replace the nested ``location`` block with a flat ``location_notes``
       string so the model receives a clean, readable hint.
    2. Convert structured skill objects to LLM-readable strings.
    3. Convert structured education objects to LLM-readable strings.

    Args:
        profile: Raw profile dict from profile.json.

    Returns:
        Transformed profile dict ready for JSON serialisation into a prompt.
        The original dict is never mutated.
    """
    loc = profile.get("location", {})
    location_notes = loc.get("notes") or _generate_location_notes(
        loc.get("center"), loc.get("radius_km")
    )
    scoring_profile = {k: v for k, v in profile.items() if k != "location"}
    if location_notes:
        scoring_profile["location_notes"] = location_notes

    scoring_profile = format_skills_for_prompt(scoring_profile)
    scoring_profile = format_education_for_prompt(scoring_profile)
    return scoring_profile


# ---------------------------------------------------------------------------
# Provider helpers
# ---------------------------------------------------------------------------


def _build_chain(
    force_provider: Optional[str] = None,
) -> list[LLMProvider]:
    """Build the LLM provider chain from credentials.

    Args:
        force_provider: If given, filter the chain to only this provider name.

    Returns:
        Ordered list of LLMProvider instances.

    Raises:
        SystemExit: If credentials cannot be loaded or the chain is empty.
    """
    try:
        providers_dict = load_providers()
    except CredentialError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    chain = build_provider_chain(providers_dict)

    if force_provider:
        chain = [
            p for p in chain
            if _provider_name(p) == force_provider.lower()
        ]
        if not chain:
            print(
                f"ERROR: Provider '{force_provider}' not found or has no "
                "configured API key.",
                file=sys.stderr,
            )
            sys.exit(1)

    if not chain:
        print(
            "ERROR: No providers available. Check your credentials in "
            "config/providers.json.",
            file=sys.stderr,
        )
        sys.exit(1)

    return chain


def _first_available_provider(chain: list[LLMProvider]) -> LLMProvider:
    """Return the first provider in the chain.

    Args:
        chain: Non-empty list of LLMProvider instances.

    Returns:
        The first LLMProvider.
    """
    return chain[0]


# ---------------------------------------------------------------------------
# Old prompt scoring
# ---------------------------------------------------------------------------


def _score_old(
    description: str,
    profile_json: str,
    provider: LLMProvider,
    verbose: bool = False,
) -> Optional[dict]:
    """Score a listing with the current (old) prompt.

    Args:
        description:  Full job description text.
        profile_json: JSON-serialised candidate profile string.
        provider:     LLMProvider to use.
        verbose:      If True, print the raw LLM response.

    Returns:
        Dict with keys ``score``, ``matched_skills``, ``missing_skills``,
        ``concerns``, ``verdict``, ``tokens_input``, ``tokens_output``;
        or None on failure.
    """
    prompt = _OLD_PROMPT_TEMPLATE.format(
        profile_json=profile_json,
        description=description,
    )
    try:
        result = provider.complete(prompt)
        if verbose:
            print("\n  [OLD RAW RESPONSE]")
            print(json.dumps(result, indent=2))
        return result
    except RuntimeError as exc:
        print(f"  WARNING: Old prompt failed: {exc}", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# New rubric scoring
# ---------------------------------------------------------------------------

_RUBRIC_REQUIRED_KEYS = {
    "dimensions",
    "hiring_assessment",
    "role_fit_assessment",
    "deal_breakers",
    "matched_skills",
    "missing_skills",
    "concerns",
    "archetype",
    "apply_recommendation",
    "verdict",
}

_RUBRIC_DIMENSION_KEYS = {"skills_match", "role_fit", "red_flags"}


def _validate_rubric_response(result: dict) -> Optional[str]:
    """Validate a parsed rubric response dict against the expected schema.

    Args:
        result: Parsed dict from the LLM response.

    Returns:
        An error message string if validation fails, or None if valid.
    """
    missing = _RUBRIC_REQUIRED_KEYS - result.keys()
    if missing:
        return f"Missing keys: {missing}"

    dims = result.get("dimensions")
    if not isinstance(dims, dict):
        return "'dimensions' must be a dict"

    missing_dims = _RUBRIC_DIMENSION_KEYS - dims.keys()
    if missing_dims:
        return f"'dimensions' missing keys: {missing_dims}"

    for key in _RUBRIC_DIMENSION_KEYS:
        val = dims[key]
        if not isinstance(val, (int, float)) or not (0 <= val <= 10):
            return f"dimensions['{key}'] must be a number 0–10, got {val!r}"

    rec = result.get("apply_recommendation")
    if rec not in _VALID_RECOMMENDATIONS:
        return (
            f"apply_recommendation {rec!r} not in "
            f"{sorted(_VALID_RECOMMENDATIONS)}"
        )

    return None


def _score_rubric(
    description: str,
    profile_json: str,
    provider: LLMProvider,
    verbose: bool = False,
) -> Optional[dict]:
    """Score a listing with the new rubric prompt and compute match_score.

    After getting the LLM response:
    - Validates schema
    - Computes: match_score = 0.60 * skills_match + 0.40 * role_fit
    - Sets apply_recommendation = "Hard No" if deal_breakers non-empty

    Args:
        description:  Full job description text.
        profile_json: JSON-serialised candidate profile string.
        provider:     LLMProvider to use.
        verbose:      If True, print the raw LLM response.

    Returns:
        Enriched result dict with added ``match_score`` and
        ``listing_quality`` fields; or None on failure.
    """
    prompt = _RUBRIC_PROMPT_TEMPLATE.format(
        profile_json=profile_json,
        description=description,
    )
    try:
        raw = provider.generate(prompt)
    except RuntimeError as exc:
        print(f"  WARNING: Rubric prompt failed: {exc}", file=sys.stderr)
        return None

    try:
        result = json.loads(strip_fences(raw))
    except json.JSONDecodeError as exc:
        print(f"  WARNING: Rubric response is not valid JSON: {exc}", file=sys.stderr)
        return None

    result["tokens_input"] = None
    result["tokens_output"] = None

    if verbose:
        print("\n  [RUBRIC RAW RESPONSE]")
        print(json.dumps(result, indent=2))

    validation_error = _validate_rubric_response(result)
    if validation_error:
        print(
            f"  WARNING: Rubric response failed validation: {validation_error}",
            file=sys.stderr,
        )
        return None

    dims = result["dimensions"]
    match_score = round(
        _SKILLS_WEIGHT * dims["skills_match"]
        + _ROLE_FIT_WEIGHT * dims["role_fit"],
        2,
    )
    listing_quality = dims["red_flags"]

    # Force Hard No when deal-breakers are present (score is NOT capped)
    if result.get("deal_breakers"):
        result["apply_recommendation"] = "Hard No"

    result["match_score"] = match_score
    result["listing_quality"] = listing_quality
    return result


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------


def _truncate(text: str, max_len: int = 50) -> str:
    """Truncate text to max_len characters, appending '...' if needed.

    Args:
        text:    Input string.
        max_len: Maximum character length before truncation.

    Returns:
        Truncated string.
    """
    if len(text) > max_len:
        return text[: max_len - 3] + "..."
    return text


def _print_listing_comparison(
    listing: dict,
    old_result: Optional[dict],
    new_result: Optional[dict],
    index: int,
    total: int,
) -> None:
    """Print a compact comparison block for a single listing.

    Args:
        listing:    The listing dict from the database.
        old_result: Old prompt result dict, or None on failure.
        new_result: New rubric result dict, or None on failure.
        index:      1-based listing index in the run.
        total:      Total listings being evaluated.
    """
    title = listing.get("title") or "(no title)"
    company = listing.get("company") or ""
    label = f"{title} at {company}" if company else title
    listing_id = listing.get("id", "?")

    print(f"\n{'-' * 3} Listing #{listing_id}: \"{_truncate(label, 60)}\" "
          f"[{index}/{total}] {'-' * 3}")

    if old_result is not None:
        old_score = old_result.get("score", "?")
        old_matched = len(old_result.get("matched_skills") or [])
        old_missing = len(old_result.get("missing_skills") or [])
        old_concerns = len(old_result.get("concerns") or [])
        print(
            f"  Old:  score={old_score}  "
            f"matched={old_matched}  "
            f"missing={old_missing}  "
            f"concerns={old_concerns}"
        )
    else:
        print("  Old:  [FAILED]")

    if new_result is not None:
        match_score = new_result.get("match_score", "?")
        quality = new_result.get("listing_quality", "?")
        rec = new_result.get("apply_recommendation", "?")
        hiring = new_result.get("hiring_assessment", "?")
        role_fit_label = new_result.get("role_fit_assessment", "?")
        deal_breakers = new_result.get("deal_breakers") or []
        new_matched = len(new_result.get("matched_skills") or [])
        new_missing = len(new_result.get("missing_skills") or [])
        print(
            f"  New:  match={match_score}  "
            f"quality={quality}  "
            f"rec={rec}"
        )
        print(
            f'        hiring="{_truncate(hiring, 40)}"  '
            f'role_fit="{_truncate(role_fit_label, 40)}"'
        )
        print(
            f"        deal_breakers={deal_breakers}  "
            f"matched={new_matched}  "
            f"missing={new_missing}"
        )
    else:
        print("  New:  [FAILED]")

    if old_result is not None and new_result is not None:
        old_score_val = old_result.get("score")
        new_score_val = new_result.get("match_score")
        if (
            isinstance(old_score_val, (int, float))
            and isinstance(new_score_val, (int, float))
        ):
            delta = round(new_score_val - old_score_val, 2)
            sign = "+" if delta >= 0 else ""
            print(f"  Delta score: {sign}{delta}")


def _print_summary(
    evaluated: list[dict],
    provider_label: str,
) -> None:
    """Print aggregate summary statistics across all evaluated listings.

    Args:
        evaluated: List of result dicts. Each contains:
                   - ``listing``: the DB listing dict
                   - ``old``:     old prompt result or None
                   - ``new``:     new rubric result or None
        provider_label: The provider/model string used for scoring.
    """
    total = len(evaluated)
    successful_pairs = [
        e for e in evaluated
        if e["old"] is not None and e["new"] is not None
    ]

    # --- Old prompt stats ---
    old_scores = [
        e["old"]["score"]
        for e in evaluated
        if e["old"] is not None
        and isinstance(e["old"].get("score"), (int, float))
    ]

    # --- New rubric stats ---
    new_scores = [
        e["new"]["match_score"]
        for e in evaluated
        if e["new"] is not None
        and isinstance(e["new"].get("match_score"), (int, float))
    ]

    # --- Tier delta stats ---
    # Classify by original score tier (from DB, not re-scored old prompt)
    def _tier_stats(tier_name: str, low: float, high: float) -> None:
        tier = [
            e for e in successful_pairs
            if low <= (e["listing"].get("score") or 0) < high
        ]
        if not tier:
            return
        t_old = [e["old"]["score"] for e in tier
                 if isinstance(e["old"].get("score"), (int, float))]
        t_new = [e["new"]["match_score"] for e in tier
                 if isinstance(e["new"].get("match_score"), (int, float))]
        if t_old and t_new:
            om = round(statistics.mean(t_old), 1)
            nm = round(statistics.mean(t_new), 1)
            delta = round(nm - om, 1)
            sign = "+" if delta >= 0 else ""
            print(
                f"  {tier_name}:  "
                f"old_mean={om} -> new_mean={nm}  "
                f"(Delta={sign}{delta})"
            )

    # --- Apply recommendation counts ---
    rec_counts: dict[str, int] = {r: 0 for r in _VALID_RECOMMENDATIONS}
    for e in evaluated:
        if e["new"] is not None:
            rec = e["new"].get("apply_recommendation")
            if rec in rec_counts:
                rec_counts[rec] += 1

    # --- Deal-breaker count ---
    deal_breaker_count = sum(
        1 for e in evaluated
        if e["new"] is not None and e["new"].get("deal_breakers")
    )

    # --- Listing quality stats ---
    quality_scores = [
        e["new"]["listing_quality"]
        for e in evaluated
        if e["new"] is not None
        and isinstance(e["new"].get("listing_quality"), (int, float))
    ]

    print("\n" + "=" * 50)
    print("=== SUMMARY ===")
    print(f"Listings evaluated: {total}")
    print(f"Provider/model used: {provider_label}")
    print()

    if old_scores:
        om = round(statistics.mean(old_scores), 1)
        omed = round(statistics.median(old_scores), 1)
        ostd = (
            round(statistics.stdev(old_scores), 1)
            if len(old_scores) > 1 else 0.0
        )
        print(
            f"Old prompt:  mean={om}  median={omed}  std={ostd}  "
            f"range=[{min(old_scores)}, {max(old_scores)}]"
        )
    else:
        print("Old prompt:  no successful scores")

    if new_scores:
        nm = round(statistics.mean(new_scores), 1)
        nmed = round(statistics.median(new_scores), 1)
        nstd = (
            round(statistics.stdev(new_scores), 1)
            if len(new_scores) > 1 else 0.0
        )
        print(
            f"New rubric:  mean={nm}  median={nmed}  std={nstd}  "
            f"range=[{min(new_scores)}, {max(new_scores)}]"
        )
    else:
        print("New rubric:  no successful scores")

    if old_scores and new_scores:
        delta_mean = round(
            statistics.mean(new_scores) - statistics.mean(old_scores), 1
        )
        sign = "+" if delta_mean >= 0 else ""
        print(f"Delta mean: {sign}{delta_mean}")

    print()
    print("Score shifts by tier:")
    _tier_stats("High (old>=8) ", 8, 11)
    _tier_stats("Mid (5<=old<8)", 5, 8)
    _tier_stats("Low (old<5)   ", 0, 5)

    new_count = sum(
        1 for e in evaluated if e["new"] is not None
    )
    deal_pct = (
        round(deal_breaker_count / new_count * 100)
        if new_count > 0 else 0
    )
    print()
    print(
        f"Deal-breakers flagged: "
        f"{deal_breaker_count}/{new_count} ({deal_pct}%)"
    )

    rec_parts = [
        f"{r}={rec_counts[r]}"
        for r in ("Strong Yes", "Yes", "Maybe", "No", "Hard No")
    ]
    print(f"Apply recommendations: {', '.join(rec_parts)}")

    if quality_scores:
        qm = round(statistics.mean(quality_scores), 1)
        print(
            f"Listing quality: mean={qm}  "
            f"min={min(quality_scores)}  "
            f"max={max(quality_scores)}"
        )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def _parse_args() -> argparse.Namespace:
    """Parse command-line arguments.

    Returns:
        Parsed argument namespace.
    """
    parser = argparse.ArgumentParser(
        description=(
            "A/B evaluation: current scoring prompt vs. new rubric-based "
            "prompt. Reads from PostgreSQL; never writes to the database."
        ),
    )
    parser.add_argument(
        "--count",
        type=int,
        default=25,
        metavar="N",
        help="Total listings to evaluate (default: 25).",
    )
    parser.add_argument(
        "--provider",
        metavar="NAME",
        help=(
            "Force a specific provider by name (e.g. 'anthropic'). "
            "Uses the first available provider by default."
        ),
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print full LLM responses for each listing.",
    )
    return parser.parse_args()


def main() -> None:
    """Run the A/B evaluation and print the comparison report."""
    args = _parse_args()

    # --- Compute tier targets proportionally from --count ---
    total_target = args.count
    # Proportions: 8/25, 9/25, 8/25
    ratio_high = 8 / 25
    ratio_mid = 9 / 25
    ratio_low = 8 / 25
    high_n = max(1, round(total_target * ratio_high))
    mid_n = max(1, round(total_target * ratio_mid))
    low_n = max(1, round(total_target * ratio_low))

    # --- Load profile ---
    profile = _load_profile(_DEFAULT_PROFILE_PATH)
    scoring_profile = _prepare_scoring_profile(profile)
    profile_json = json.dumps(scoring_profile, indent=2)

    # --- Build provider chain ---
    chain = _build_chain(force_provider=args.provider)
    provider = _first_available_provider(chain)
    pname = _provider_name(provider)
    pmodel = _provider_model(provider)
    provider_label = f"{pname}/{pmodel}"

    print(f"Provider: {provider_label}")

    # --- Connect to database and fetch sample ---
    conn = _connect()
    try:
        listings = _fetch_stratified_sample(conn, high_n, mid_n, low_n)
    finally:
        conn.close()

    if not listings:
        print(
            "ERROR: No eligible listings found. "
            "Make sure the database has listings with description and seen=1.",
            file=sys.stderr,
        )
        sys.exit(1)

    total = len(listings)
    print(
        f"Evaluating {total} listings "
        f"(target: ~{high_n} high / ~{mid_n} mid / ~{low_n} low)"
    )
    print()

    # --- Score each listing with both prompts ---
    evaluated: list[dict] = []

    for i, listing in enumerate(listings, start=1):
        title = listing.get("title") or "(no title)"
        progress_label = _truncate(title, 55)
        print(f"[{i}/{total}] Scoring \"{progress_label}\"...", end=" ")
        sys.stdout.flush()

        description = listing.get("description") or ""

        old_result = _score_old(
            description=description,
            profile_json=profile_json,
            provider=provider,
            verbose=args.verbose,
        )

        new_result = _score_rubric(
            description=description,
            profile_json=profile_json,
            provider=provider,
            verbose=args.verbose,
        )

        status_parts = []
        if old_result is not None:
            status_parts.append(f"old={old_result.get('score')}")
        else:
            status_parts.append("old=FAIL")
        if new_result is not None:
            status_parts.append(
                f"new={new_result.get('match_score')}"
            )
        else:
            status_parts.append("new=FAIL")

        print(", ".join(status_parts))

        evaluated.append({
            "listing": listing,
            "old": old_result,
            "new": new_result,
        })

        _print_listing_comparison(
            listing=listing,
            old_result=old_result,
            new_result=new_result,
            index=i,
            total=total,
        )

    # --- Print summary ---
    _print_summary(evaluated=evaluated, provider_label=provider_label)


if __name__ == "__main__":
    main()
