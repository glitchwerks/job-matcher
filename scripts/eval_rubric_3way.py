"""
scripts/eval_rubric_3way.py -- 3-way comparison: career ops scores vs old
prompt vs new rubric.

Parses career ops markdown reports from an external directory to extract
job metadata (title, company, URL, global score, deal-breaker status),
scrapes the full job description from each URL, then scores each listing
with both the old prompt and the new rubric prompt.  Outputs a per-listing
comparison block and an aggregate summary with Pearson correlations.

This script is READ-ONLY -- it never modifies the database or the reports.

Usage (from repo root):
    python scripts/eval_rubric_3way.py --reports-dir <path>
    python scripts/eval_rubric_3way.py --reports-dir <path> --count 30
    python scripts/eval_rubric_3way.py --reports-dir <path> --provider anthropic
    python scripts/eval_rubric_3way.py --reports-dir <path> --verbose

Environment:
    DATABASE_URL -- required (same as the rest of the app).  Descriptions are
    scraped live rather than pulled from the database, but the import chain
    (ingest -> db) checks for DATABASE_URL at import time.  Set it to any
    valid connection string before running (it does not need to point at a
    live database for this script to function).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
import time
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Ensure repo root is on sys.path so local modules resolve when running
# the script directly (e.g. ``python scripts/eval_rubric_3way.py``).
# ---------------------------------------------------------------------------
_REPO_ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.abspath(_REPO_ROOT))

from ingest import _provider_model, _provider_name, scrape_description

from scripts.eval_rubric import (
    _build_chain,
    _first_available_provider,
    _load_profile,
    _prepare_scoring_profile,
    _score_old,
    _score_rubric,
    _truncate,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_CONFIG_DIR = os.path.join(_REPO_ROOT, "config")
_DEFAULT_PROFILE_PATH = os.path.join(_CONFIG_DIR, "profile.json")

# Career ops score tiers (1-5 scale)
_HIGH_FLOOR = 4.0   # high tier: 4.0 - 5.0
_LOW_CEILING = 2.5  # low tier:  1.0 - 2.5
# mid tier: 2.5 - 4.0

# Scrape delay between requests (seconds)
_SCRAPE_DELAY = 1.0

# Career ops 1-5 scale -> our 0-10 scale
# 1 -> 0, 3 -> 5, 5 -> 10   (linear: (raw - 1) * 2.5)
_OPS_SCALE_FACTOR = 2.5
_OPS_SCALE_OFFSET = 1.0


# ---------------------------------------------------------------------------
# Career ops report parsing
# ---------------------------------------------------------------------------


def _normalize_ops_score(raw: float) -> float:
    """Convert a career ops 1-5 score to the 0-10 scale used by our prompts.

    The mapping is linear: 1->0, 3->5, 5->10, computed as
    ``(raw - 1) * 2.5``.

    Args:
        raw: Career ops score on the 1-5 scale.

    Returns:
        Equivalent score on the 0-10 scale, rounded to one decimal place.
    """
    return round((raw - _OPS_SCALE_OFFSET) * _OPS_SCALE_FACTOR, 1)


def _parse_report(path: Path) -> Optional[dict]:
    """Parse a career ops markdown report and extract key fields.

    Extracts:
    - ``title``: job title parsed from the H1 header
    - ``company``: company name parsed from the H1 header
    - ``url``: job posting URL from the ``**URL:**`` field
    - ``ops_score_raw``: global score on the 1-5 scale
    - ``ops_score_norm``: global score normalised to 0-10
    - ``deal_breaker``: True if a deal-breaker was detected
    - ``deal_breaker_reason``: brief string describing the trigger, or None

    The parser handles both English headers
    (``# Evaluation: Company -- Title``) and Spanish headers
    (``# Evaluacion: Company -- Title`` with em-dash or double-dash).

    Args:
        path: Path to the ``.md`` report file.

    Returns:
        Parsed dict on success, or None if any required field is missing.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        print(
            f"  WARNING: Could not read {path.name}: {exc}",
            file=sys.stderr,
        )
        return None

    lines = text.splitlines()

    # --- Title / company from H1 ---
    # Matches:  # Evaluation: Company -- Title
    #           # Evaluacion: Company -- Title     (Spanish, no accent)
    #           # Evaluacion: Company - Title      (single dash)
    #           # Evaluacion: Company - Title      (em-dash U+2014)
    title = None
    company = None
    h1_pattern = re.compile(
        r"^#\s+Evaluac?i[oe]n[^:]*:\s*(.+?)\s*(?:--|--|--|-|--|—|--)\s*(.+)$",
        re.IGNORECASE | re.UNICODE,
    )
    for line in lines[:5]:
        m = h1_pattern.match(line)
        if m:
            company = m.group(1).strip()
            title = m.group(2).strip()
            break

    if not title or not company:
        # Fallback: grab whatever is after the first ':'
        for line in lines[:5]:
            if line.startswith("#"):
                after_colon = line.split(":", 1)[-1].strip()
                # Split on common separators: --, --, em-dash
                for sep in (" -- ", " - ", " -- ", " - ", " -- ", " -- ", " — "):
                    if sep in after_colon:
                        parts = after_colon.split(sep, 1)
                        company = parts[0].strip()
                        title = parts[1].strip()
                        break
                else:
                    title = after_colon
                    company = ""
                break

    # --- Global score ---
    ops_score_raw = None
    score_pattern = re.compile(
        r"^\*\*Score:\*\*\s*([\d.]+)\s*/\s*5",
        re.IGNORECASE,
    )
    # Also handle Spanish "Score:" variant without bold
    score_pattern_plain = re.compile(
        r"^Score:\s*([\d.]+)\s*/\s*5",
        re.IGNORECASE,
    )
    for line in lines:
        m = score_pattern.match(line) or score_pattern_plain.match(line)
        if m:
            try:
                ops_score_raw = float(m.group(1))
                break
            except ValueError:
                pass

    if ops_score_raw is None:
        print(
            f"  WARNING: No score found in {path.name} -- skipping.",
            file=sys.stderr,
        )
        return None

    # --- URL ---
    url = None
    url_pattern = re.compile(
        r"^\*\*(?:URL|Url):\*\*\s*(https?://\S+)",
        re.IGNORECASE,
    )
    for line in lines:
        m = url_pattern.match(line)
        if m:
            url = m.group(1).strip()
            break

    if not url:
        print(
            f"  WARNING: No URL found in {path.name} -- skipping.",
            file=sys.stderr,
        )
        return None

    # --- Deal-breaker detection ---
    # Primary signal: Location row in score breakdown table with score 1.0
    # Secondary signal: explicit DEAL-BREAKER section or negative Red Flags
    deal_breaker = False
    deal_breaker_reason = None

    full_text = text.lower()

    # Check for location score of 1.0 in the breakdown table
    location_row_pattern = re.compile(
        r"\|\s*(?:location[^|]*)\|\s*1\.0\s*\|",
        re.IGNORECASE,
    )
    if location_row_pattern.search(text):
        deal_breaker = True
        deal_breaker_reason = "location"

    # Check for explicit DEAL-BREAKER section
    if not deal_breaker and "deal-breaker" in full_text:
        # Look for a ## DEAL-BREAKER heading
        if re.search(r"^##\s+deal-breaker", text, re.IGNORECASE | re.MULTILINE):
            deal_breaker = True
            deal_breaker_reason = "explicit deal-breaker section"

    # Check for negative Red Flags value in the score breakdown
    if not deal_breaker:
        neg_flags_pattern = re.compile(
            r"\|\s*(?:red flags?)[^|]*\|\s*-[\d.]+\s*\|",
            re.IGNORECASE,
        )
        if neg_flags_pattern.search(text):
            deal_breaker = True
            deal_breaker_reason = "negative red flags"

    return {
        "path": str(path),
        "filename": path.name,
        "title": title or "(no title)",
        "company": company or "",
        "url": url,
        "ops_score_raw": ops_score_raw,
        "ops_score_norm": _normalize_ops_score(ops_score_raw),
        "deal_breaker": deal_breaker,
        "deal_breaker_reason": deal_breaker_reason,
    }


def _load_reports(reports_dir: str) -> list[dict]:
    """Scan a directory for career ops markdown reports and parse each one.

    Files are sorted by filename (which follows the ``NNN-company-date.md``
    convention) so the run order is deterministic.

    Args:
        reports_dir: Path to the directory containing ``.md`` report files.

    Returns:
        List of successfully parsed report dicts, sorted by filename.
    """
    dir_path = Path(reports_dir)
    if not dir_path.is_dir():
        print(
            f"ERROR: --reports-dir '{reports_dir}' is not a directory.",
            file=sys.stderr,
        )
        sys.exit(1)

    md_files = sorted(dir_path.glob("*.md"))
    if not md_files:
        print(
            f"ERROR: No .md files found in '{reports_dir}'.",
            file=sys.stderr,
        )
        sys.exit(1)

    reports = []
    for path in md_files:
        parsed = _parse_report(path)
        if parsed is not None:
            reports.append(parsed)

    return reports


def _stratified_sample(
    reports: list[dict],
    count: int,
) -> list[dict]:
    """Select a stratified sample from the parsed reports.

    Stratification tiers (career ops 1-5 scale):
    - High: score >= 4.0
    - Mid:  2.5 <= score < 4.0
    - Low:  score < 2.5

    Target proportions: roughly 1/3 each, but adjusted to fill gaps when a
    tier has fewer reports than its target.  Reports are sorted by score
    within each tier (descending) for deterministic selection.

    Args:
        reports: All parsed report dicts.
        count:   Total number of reports to select.

    Returns:
        Selected list of report dicts.
    """
    high = [r for r in reports if r["ops_score_raw"] >= _HIGH_FLOOR]
    mid = [
        r for r in reports
        if _LOW_CEILING <= r["ops_score_raw"] < _HIGH_FLOOR
    ]
    low = [r for r in reports if r["ops_score_raw"] < _LOW_CEILING]

    # Sort each tier: high/mid descending, low ascending (most interesting first)
    high.sort(key=lambda r: r["ops_score_raw"], reverse=True)
    mid.sort(key=lambda r: r["ops_score_raw"], reverse=True)
    low.sort(key=lambda r: r["ops_score_raw"])

    high_target = count // 3
    low_target = count // 3
    mid_target = count - high_target - low_target

    selected: list[dict] = []
    selected.extend(high[:high_target])
    selected.extend(mid[:mid_target])
    selected.extend(low[:low_target])

    # If we came up short (tiers had fewer items than targets), backfill
    # from whichever tiers have surplus, in mid -> high -> low priority.
    if len(selected) < count:
        remaining = count - len(selected)
        used_ids = {id(r) for r in selected}
        pool = [r for r in reports if id(r) not in used_ids]
        # Sort pool by score descending for deterministic backfill
        pool.sort(key=lambda r: r["ops_score_raw"], reverse=True)
        selected.extend(pool[:remaining])

    return selected[:count]


# ---------------------------------------------------------------------------
# Pearson correlation
# ---------------------------------------------------------------------------


def _pearson(xs: list[float], ys: list[float]) -> Optional[float]:
    """Compute the Pearson correlation coefficient for two equal-length lists.

    Uses only the stdlib ``statistics`` module -- no numpy dependency.

    Args:
        xs: First list of numeric values.
        ys: Second list of numeric values; must have the same length as xs.

    Returns:
        Pearson r rounded to 2 decimal places, or None if the lists have
        fewer than 2 paired values or either list has zero variance.
    """
    if len(xs) != len(ys) or len(xs) < 2:
        return None
    # Use statistics.correlation if Python >= 3.10; otherwise compute manually
    if hasattr(statistics, "correlation"):
        try:
            return round(statistics.correlation(xs, ys), 2)
        except statistics.StatisticsError:
            return None
    # Manual fallback (Python 3.8 / 3.9)
    n = len(xs)
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    cov = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    std_x = (sum((x - mean_x) ** 2 for x in xs) / n) ** 0.5
    std_y = (sum((y - mean_y) ** 2 for y in ys) / n) ** 0.5
    if std_x == 0 or std_y == 0:
        return None
    return round(cov / (n * std_x * std_y), 2)


# ---------------------------------------------------------------------------
# Score tier classification
# ---------------------------------------------------------------------------

_TIER_HIGH = "High"
_TIER_MID = "Mid"
_TIER_LOW = "Low"


def _score_tier(score: float) -> str:
    """Classify a 0-10 score into a High / Mid / Low tier.

    Boundaries:
    - High: score >= 6.5
    - Mid:  3.5 <= score < 6.5
    - Low:  score < 3.5

    Args:
        score: Numeric score on the 0-10 scale.

    Returns:
        One of ``"High"``, ``"Mid"``, or ``"Low"``.
    """
    if score >= 6.5:
        return _TIER_HIGH
    if score >= 3.5:
        return _TIER_MID
    return _TIER_LOW


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------


def _print_listing_block(
    report: dict,
    old_result: Optional[dict],
    rubric_result: Optional[dict],
    index: int,
    total: int,
) -> None:
    """Print the 3-way comparison block for one listing.

    Args:
        report:        Parsed career ops report dict.
        old_result:    Old prompt result dict, or None on failure.
        rubric_result: New rubric result dict, or None on failure.
        index:         1-based position in the current run.
        total:         Total listings being processed.
    """
    company = report.get("company") or ""
    title = report.get("title") or "(no title)"
    label = f"{title} at {company}" if company else title
    label = _truncate(label, 55)

    ops_raw = report["ops_score_raw"]
    ops_norm = report["ops_score_norm"]
    db_str = "YES" if report["deal_breaker"] else "NO"
    db_reason = (
        f" ({report['deal_breaker_reason']})" if report["deal_breaker_reason"]
        else ""
    )

    print(
        f"\n--- #{index:03d} \"{label}\" [{index}/{total}] ---"
    )
    print(
        f"  Career Ops:  score={ops_raw}/5  "
        f"(normalized={ops_norm}/10)  "
        f"deal_breaker={db_str}{db_reason}"
    )

    if old_result is not None:
        old_score = old_result.get("score", "?")
        print(f"  Old Prompt:  score={old_score}")
    else:
        print("  Old Prompt:  [FAILED]")

    if rubric_result is not None:
        match_score = rubric_result.get("match_score", "?")
        quality = rubric_result.get("listing_quality", "?")
        rec = rubric_result.get("apply_recommendation", "?")
        deal_breakers = rubric_result.get("deal_breakers") or []
        print(
            f"  New Rubric:  match={match_score}  "
            f"quality={quality}  "
            f"rec={rec}  "
            f'deal_breakers={deal_breakers}'
        )
    else:
        print("  New Rubric:  [FAILED]")

    # Deltas (ops normalised vs each scorer)
    if old_result is not None and isinstance(
        old_result.get("score"), (int, float)
    ):
        delta_ops_old = round(
            old_result["score"] - ops_norm, 1
        )
        sign = "+" if delta_ops_old >= 0 else ""
        print(
            f"  Delta (ops vs old): {sign}{delta_ops_old}",
            end="",
        )
    else:
        print("  Delta (ops vs old): N/A", end="")

    if rubric_result is not None and isinstance(
        rubric_result.get("match_score"), (int, float)
    ):
        delta_ops_rubric = round(
            rubric_result["match_score"] - ops_norm, 1
        )
        sign = "+" if delta_ops_rubric >= 0 else ""
        print(f"   Delta (ops vs rubric): {sign}{delta_ops_rubric}")
    else:
        print("   Delta (ops vs rubric): N/A")


def _print_summary(
    results: list[dict],
    scrape_failures: int,
    provider_label: str,
) -> None:
    """Print the 3-way aggregate summary statistics.

    Args:
        results:         List of result dicts, one per processed report.
                         Each has keys: ``report``, ``old``, ``rubric``.
        scrape_failures: Number of reports skipped due to scrape failures.
        provider_label:  The ``provider/model`` string used for scoring.
    """
    total = len(results)

    ops_scores = [
        r["report"]["ops_score_norm"]
        for r in results
    ]
    old_scores = [
        r["old"]["score"]
        for r in results
        if r["old"] is not None
        and isinstance(r["old"].get("score"), (int, float))
    ]
    rubric_scores = [
        r["rubric"]["match_score"]
        for r in results
        if r["rubric"] is not None
        and isinstance(r["rubric"].get("match_score"), (int, float))
    ]

    def _stats_line(label: str, scores: list[float]) -> str:
        if not scores:
            return f"  {label:<20} N/A"
        m = round(statistics.mean(scores), 1)
        med = round(statistics.median(scores), 1)
        std = (
            round(statistics.stdev(scores), 1)
            if len(scores) > 1 else 0.0
        )
        return f"  {label:<20} mean={m}  median={med}  std={std}"

    # Pearson correlations -- only where both scorers succeeded for the same listing
    def _paired(
        key_a: str,
        sub_a: Optional[str],
        key_b: str,
        sub_b: Optional[str],
    ) -> tuple[list[float], list[float]]:
        """Extract paired score lists for correlation."""
        xs, ys = [], []
        for r in results:
            if key_a == "ops":
                val_a = r["report"]["ops_score_norm"]
            else:
                src = r.get(key_a)
                val_a = src.get(sub_a) if src else None
            if key_b == "ops":
                val_b = r["report"]["ops_score_norm"]
            else:
                src = r.get(key_b)
                val_b = src.get(sub_b) if src else None
            if isinstance(val_a, (int, float)) and isinstance(
                val_b, (int, float)
            ):
                xs.append(float(val_a))
                ys.append(float(val_b))
        return xs, ys

    ops_vs_old_xs, ops_vs_old_ys = _paired("ops", None, "old", "score")
    ops_vs_rub_xs, ops_vs_rub_ys = _paired("ops", None, "rubric", "match_score")
    old_vs_rub_xs, old_vs_rub_ys = _paired("old", "score", "rubric", "match_score")

    r_ops_old = _pearson(ops_vs_old_xs, ops_vs_old_ys)
    r_ops_rub = _pearson(ops_vs_rub_xs, ops_vs_rub_ys)
    r_old_rub = _pearson(old_vs_rub_xs, old_vs_rub_ys)

    def _r_str(r: Optional[float]) -> str:
        return f"r={r:.2f}" if r is not None else "r=N/A"

    # Deal-breaker agreement
    ops_db = sum(1 for r in results if r["report"]["deal_breaker"])
    rubric_db = sum(
        1 for r in results
        if r["rubric"] is not None and r["rubric"].get("deal_breakers")
    )
    both_db = sum(
        1 for r in results
        if r["report"]["deal_breaker"]
        and r["rubric"] is not None
        and r["rubric"].get("deal_breakers")
    )

    # Tier agreement
    def _tier_agree_count(
        score_a_fn,
        score_b_fn,
    ) -> tuple[int, int]:
        """Return (agree_count, comparable_count) for two score functions."""
        agree = 0
        comparable = 0
        for r in results:
            a = score_a_fn(r)
            b = score_b_fn(r)
            if a is not None and b is not None:
                comparable += 1
                if _score_tier(a) == _score_tier(b):
                    agree += 1
        return agree, comparable

    def _ops_score(r: dict) -> float:
        return r["report"]["ops_score_norm"]

    def _old_score(r: dict) -> Optional[float]:
        if r["old"] is not None and isinstance(
            r["old"].get("score"), (int, float)
        ):
            return float(r["old"]["score"])
        return None

    def _rubric_score(r: dict) -> Optional[float]:
        if r["rubric"] is not None and isinstance(
            r["rubric"].get("match_score"), (int, float)
        ):
            return float(r["rubric"]["match_score"])
        return None

    agree_ops_old, comp_ops_old = _tier_agree_count(_ops_score, _old_score)
    agree_ops_rub, comp_ops_rub = _tier_agree_count(_ops_score, _rubric_score)

    def _pct(n: int, d: int) -> str:
        return f"{round(n / d * 100)}%" if d > 0 else "N/A"

    print("\n" + "=" * 50)
    print("=== 3-WAY SUMMARY ===")
    print(f"Reports processed: {total}")
    print(f"Scrape failures:   {scrape_failures}")
    print(f"Provider:          {provider_label}")
    print()
    print("Scores (0-10 scale):")
    print(_stats_line("Career Ops (norm):", ops_scores))
    print(_stats_line("Old Prompt:", old_scores))
    print(_stats_line("New Rubric:", rubric_scores))
    print()
    print("Correlation (Pearson):")
    print(f"  Career Ops vs Old Prompt:  {_r_str(r_ops_old)}")
    print(f"  Career Ops vs New Rubric:  {_r_str(r_ops_rub)}")
    print(f"  Old Prompt vs New Rubric:  {_r_str(r_old_rub)}")
    print()
    print("Deal-breaker agreement:")
    print(f"  Career Ops flagged:  {ops_db}/{total}")
    print(f"  Rubric flagged:      {rubric_db}/{total}")
    print(f"  Both agree:          {both_db}/{total}")
    print()
    print("Score tier agreement (High/Mid/Low):")
    print(
        f"  Career Ops vs Old:    "
        f"{agree_ops_old}/{comp_ops_old} agree "
        f"({_pct(agree_ops_old, comp_ops_old)})"
    )
    print(
        f"  Career Ops vs Rubric: "
        f"{agree_ops_rub}/{comp_ops_rub} agree "
        f"({_pct(agree_ops_rub, comp_ops_rub)})"
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args() -> argparse.Namespace:
    """Parse command-line arguments.

    Returns:
        Parsed argument namespace.
    """
    parser = argparse.ArgumentParser(
        description=(
            "3-way comparison: career ops scores vs old scoring prompt vs "
            "new rubric prompt.  Reads career ops reports from a directory; "
            "never modifies the database or the reports."
        ),
    )
    parser.add_argument(
        "--reports-dir",
        required=True,
        metavar="PATH",
        help="Directory containing career ops markdown report files.",
    )
    parser.add_argument(
        "--count",
        type=int,
        default=30,
        metavar="N",
        help=(
            "Max reports to process, stratified across score tiers "
            "(default: 30)."
        ),
    )
    parser.add_argument(
        "--provider",
        metavar="NAME",
        help=(
            "Force a specific LLM provider by name (e.g. 'anthropic'). "
            "Uses the first available provider by default."
        ),
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print full LLM responses for each listing.",
    )
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Run the 3-way evaluation and print the comparison report."""
    args = _parse_args()

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

    # --- Load and sample reports ---
    all_reports = _load_reports(args.reports_dir)
    print(
        f"Found {len(all_reports)} parseable reports in "
        f"'{args.reports_dir}'"
    )

    sample = _stratified_sample(all_reports, args.count)
    total = len(sample)
    print(f"Selected {total} reports (stratified sample)")
    print()

    # --- Process each report ---
    results: list[dict] = []
    scrape_failures = 0

    for i, report in enumerate(sample, start=1):
        company = report.get("company") or ""
        title = report.get("title") or "(no title)"
        label = f"{title} at {company}" if company else title
        print(
            f"[{i}/{total}] Scraping \"{_truncate(label, 50)}\"...",
            end=" ",
            flush=True,
        )

        # --- Scrape full job description ---
        description, scraped_ok = scrape_description(url=report["url"])

        if not scraped_ok or not description:
            print("SCRAPE FAILED -- skipping.")
            scrape_failures += 1
            continue

        print(f"ok ({len(description)} chars)")

        # Polite delay between requests
        if i < total:
            time.sleep(_SCRAPE_DELAY)

        # --- Score with old prompt ---
        old_result = _score_old(
            description=description,
            profile_json=profile_json,
            provider=provider,
            verbose=args.verbose,
        )

        # --- Score with new rubric ---
        rubric_result = _score_rubric(
            description=description,
            profile_json=profile_json,
            provider=provider,
            verbose=args.verbose,
        )

        results.append({
            "report": report,
            "old": old_result,
            "rubric": rubric_result,
        })

        _print_listing_block(
            report=report,
            old_result=old_result,
            rubric_result=rubric_result,
            index=i,
            total=total,
        )

    if not results:
        print(
            "\nERROR: No listings could be scraped and scored.",
            file=sys.stderr,
        )
        sys.exit(1)

    # --- Print summary ---
    _print_summary(
        results=results,
        scrape_failures=scrape_failures,
        provider_label=provider_label,
    )


if __name__ == "__main__":
    main()
