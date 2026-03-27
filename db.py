"""
db.py — SQLite database layer for Job Matcher.

All interactions with jobs.db go through this module. No other module
should import sqlite3 or open the database file directly.

JSON array columns (matched_skills, missing_skills, concerns) are
serialised to strings on write and deserialised to Python lists on read.
Rows returned from read helpers are plain dicts, not sqlite3.Row objects.
"""

import json
import os
import sqlite3

_DEFAULT_DB_PATH: str = os.environ.get("DB_PATH", "jobs.db")

# ---------------------------------------------------------------------------
# Pricing fallback constants — Haiku pricing used when the caller does not
# supply per-model rates.  Kept here only for backward compatibility with
# code paths that call get_usage_stats() without pricing arguments.
# The authoritative pricing table lives in providers/anthropic_provider.py.
# ---------------------------------------------------------------------------

_FALLBACK_INPUT_COST_PER_MTOK  = 0.80   # USD per million input tokens (Haiku)
_FALLBACK_OUTPUT_COST_PER_MTOK = 4.00   # USD per million output tokens (Haiku)


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

def get_connection(db_path: str = _DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Return an open sqlite3 connection with row_factory set to sqlite3.Row.

    The caller is responsible for closing the connection (or using it as a
    context manager for transactions).
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def init_db(db_path: str = _DEFAULT_DB_PATH) -> None:
    """Create the listings table if it does not already exist.

    Safe to call on every startup — uses CREATE TABLE IF NOT EXISTS.
    """
    conn = get_connection(db_path)
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS listings (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                adzuna_id           TEXT UNIQUE NOT NULL,
                title               TEXT,
                company             TEXT,
                location            TEXT,
                salary_min          INTEGER,
                salary_max          INTEGER,
                salary_is_predicted INTEGER,
                contract_type       TEXT,
                contract_time       TEXT,
                description         TEXT,
                redirect_url        TEXT,
                created_at          TEXT,
                fetched_at          TEXT,
                score               REAL,
                matched_skills      TEXT,
                missing_skills      TEXT,
                concerns            TEXT,
                verdict             TEXT,
                bookmarked          INTEGER DEFAULT 0,
                dismissed           INTEGER DEFAULT 0,
                seen                INTEGER DEFAULT 0,
                model_used          TEXT
            )
        """)

        # NOTE: Poor-man's migration — ALTER TABLE raises OperationalError if the column
        # already exists, which we suppress. This means a genuine DB error (e.g. disk full)
        # during ALTER would also be silently swallowed. Acceptable for a single-user local
        # tool; a proper migration table would be needed at larger scale.
        # Migrate existing databases — SQLite does not support
        # ALTER TABLE ... ADD COLUMN IF NOT EXISTS, so we catch the
        # OperationalError that is raised when the column already exists.
        for column, typedef in (
            ("tokens_input", "INTEGER"),
            ("tokens_output", "INTEGER"),
            ("applied", "INTEGER DEFAULT 0"),
            ("job_type", "TEXT"),
            ("model_used", "TEXT"),
        ):
            try:
                conn.execute(
                    f"ALTER TABLE listings ADD COLUMN {column} {typedef}"
                )
            except sqlite3.OperationalError:
                pass  # Column already present; nothing to do.

        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Write helpers
# ---------------------------------------------------------------------------

def listing_exists(adzuna_id: str, db_path: str = _DEFAULT_DB_PATH) -> bool:
    """Return True if a row with the given adzuna_id already exists."""
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT 1 FROM listings WHERE adzuna_id = ?", (adzuna_id,)
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def insert_listing(listing: dict, db_path: str = _DEFAULT_DB_PATH) -> None:
    """Insert a new listing row.

    `listing` must contain all columns except `id`. The JSON array columns
    (matched_skills, missing_skills, concerns) may be supplied as Python
    lists — they will be serialised to JSON strings before insertion. They
    may also be supplied as strings or None, both of which are passed through
    unchanged.
    """
    # Serialise array columns if the caller passed Python lists.
    row = dict(listing)
    for col in ("matched_skills", "missing_skills", "concerns"):
        val = row.get(col)
        if isinstance(val, list):
            row[col] = json.dumps(val)
        elif val is None:
            row[col] = json.dumps([])

    # Ensure token, status, and classification columns are present even if absent from the source dict.
    row.setdefault("tokens_input", None)
    row.setdefault("tokens_output", None)
    row.setdefault("applied", 0)
    row.setdefault("job_type", None)
    row.setdefault("model_used", None)

    conn = get_connection(db_path)
    try:
        conn.execute(
            """
            INSERT INTO listings (
                adzuna_id, title, company, location,
                salary_min, salary_max, salary_is_predicted,
                contract_type, contract_time,
                description, redirect_url,
                created_at, fetched_at,
                score, matched_skills, missing_skills, concerns, verdict,
                bookmarked, dismissed, seen,
                tokens_input, tokens_output,
                applied,
                job_type,
                model_used
            ) VALUES (
                :adzuna_id, :title, :company, :location,
                :salary_min, :salary_max, :salary_is_predicted,
                :contract_type, :contract_time,
                :description, :redirect_url,
                :created_at, :fetched_at,
                :score, :matched_skills, :missing_skills, :concerns, :verdict,
                :bookmarked, :dismissed, :seen,
                :tokens_input, :tokens_output,
                :applied,
                :job_type,
                :model_used
            )
            """,
            row,
        )
        conn.commit()
    finally:
        conn.close()


def update_score(adzuna_id: str, score_data: dict, db_path: str = _DEFAULT_DB_PATH) -> None:
    """Write Haiku scoring results back to an existing row and mark it seen.

    `score_data` must contain: score, matched_skills, missing_skills,
    concerns, verdict. Array fields may be Python lists or JSON strings.
    """
    data = dict(score_data)
    for col in ("matched_skills", "missing_skills", "concerns"):
        val = data.get(col)
        if isinstance(val, list):
            data[col] = json.dumps(val)
        elif val is None:
            data[col] = json.dumps([])

    conn = get_connection(db_path)
    try:
        conn.execute(
            """
            UPDATE listings
            SET score          = :score,
                matched_skills = :matched_skills,
                missing_skills = :missing_skills,
                concerns       = :concerns,
                verdict        = :verdict,
                seen           = 1,
                tokens_input   = :tokens_input,
                tokens_output  = :tokens_output,
                model_used     = :model_used
            WHERE adzuna_id = :adzuna_id
            """,
            {
                **data,
                "adzuna_id": adzuna_id,
                "tokens_input": data.get("tokens_input"),
                "tokens_output": data.get("tokens_output"),
                "model_used": data.get("model_used"),
            },
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Read helpers
# ---------------------------------------------------------------------------

def _deserialise_row(row: sqlite3.Row) -> dict:
    """Convert a sqlite3.Row to a plain dict and deserialise JSON array columns."""
    d = dict(row)
    for col in ("matched_skills", "missing_skills", "concerns"):
        raw = d.get(col)
        if raw is None:
            d[col] = []
        else:
            try:
                parsed = json.loads(raw)
                d[col] = parsed if isinstance(parsed, list) else []
            except (json.JSONDecodeError, TypeError):
                d[col] = []
    return d


def get_feed(
    threshold: float = 7.0,
    min_score: float | None = None,
    remote_only: bool = False,
    search: str | None = None,
    job_type: str | None = None,
    db_path: str = _DEFAULT_DB_PATH,
) -> list[dict]:
    """Return listings with score >= effective threshold and dismissed = 0, ordered by score DESC.

    Listings whose score is NULL are excluded (they have not been scored yet).

    Args:
        threshold:   Default score floor used when min_score is not provided.
        min_score:   If provided, overrides threshold as the score floor.
        remote_only: If True, restricts to listings whose location contains "remote".
        search:      If provided, filters by title or company containing the search string.
        job_type:    If provided, restricts to listings whose job_type matches (case-insensitive).
        db_path:     Path to the SQLite database file.
    """
    effective = min_score if min_score is not None else threshold

    conditions = ["score >= ?", "dismissed = 0", "applied = 0"]
    params: list = [effective]

    if remote_only:
        conditions.append("LOWER(location) LIKE '%remote%'")

    if search:
        conditions.append("(LOWER(title) LIKE ? OR LOWER(company) LIKE ?)")
        term = f"%{search.lower()}%"
        params.extend([term, term])

    if job_type:
        conditions.append("LOWER(job_type) = LOWER(?)")
        params.append(job_type)

    where_clause = " AND ".join(conditions)

    conn = get_connection(db_path)
    try:
        rows = conn.execute(
            f"SELECT * FROM listings WHERE {where_clause} ORDER BY score DESC",
            params,
        ).fetchall()
        return [_deserialise_row(r) for r in rows]
    finally:
        conn.close()


def get_job_types(db_path: str = _DEFAULT_DB_PATH) -> list[str]:
    """Return a sorted list of distinct non-null job_type values present in the listings table.

    Used to populate the filter dropdown dynamically so it only shows types
    that actually exist in the database.

    Args:
        db_path: Path to the SQLite database file.

    Returns:
        Sorted list of unique job_type strings, excluding NULL values.
    """
    conn = get_connection(db_path)
    try:
        rows = conn.execute(
            "SELECT DISTINCT job_type FROM listings WHERE job_type IS NOT NULL ORDER BY job_type ASC"
        ).fetchall()
        return [row["job_type"] for row in rows]
    finally:
        conn.close()


def get_bookmarks(db_path: str = _DEFAULT_DB_PATH) -> list[dict]:
    """Return all bookmarked listings ordered by score DESC."""
    conn = get_connection(db_path)
    try:
        rows = conn.execute(
            """
            SELECT * FROM listings
            WHERE bookmarked = 1
            ORDER BY score DESC
            """,
        ).fetchall()
        return [_deserialise_row(r) for r in rows]
    finally:
        conn.close()


def get_all_scored(db_path: str = _DEFAULT_DB_PATH) -> list[dict]:
    """Return all listings that have been scored (seen = 1), ordered by fetched_at DESC."""
    conn = get_connection(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM listings WHERE seen = 1 ORDER BY fetched_at DESC"
        ).fetchall()
        return [_deserialise_row(r) for r in rows]
    finally:
        conn.close()


def get_listing_by_id(listing_id: int, db_path: str = _DEFAULT_DB_PATH) -> dict | None:
    """Return a single listing by internal id, or None if not found.

    JSON array columns are deserialised to Python lists, consistent with the
    other read helpers.
    """
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM listings WHERE id = ?", (listing_id,)
        ).fetchone()
        if row is None:
            return None
        return _deserialise_row(row)
    finally:
        conn.close()


def get_usage_stats(
    db_path: str = _DEFAULT_DB_PATH,
    input_cost_per_mtok: float = _FALLBACK_INPUT_COST_PER_MTOK,
    output_cost_per_mtok: float = _FALLBACK_OUTPUT_COST_PER_MTOK,
) -> dict:
    """Return aggregated API usage and cost statistics.

    Queries the listings table to produce totals and a per-day breakdown.
    All token columns are nullable — NULL values are treated as 0 via
    SQLite's COALESCE so the arithmetic is always well-defined.

    Args:
        db_path:              Path to the SQLite database file.
        input_cost_per_mtok:  USD cost per million input tokens.  Defaults to
                              Haiku pricing for backward compatibility with
                              callers that do not supply per-model rates.
        output_cost_per_mtok: USD cost per million output tokens.  Defaults to
                              Haiku pricing for the same reason.

    Returns:
        Dict with keys:
            total_scored        -- count of listings with score IS NOT NULL
            total_tokens_input  -- sum of tokens_input across all rows
            total_tokens_output -- sum of tokens_output across all rows
            estimated_cost_usd  -- float, calculated using supplied pricing
            by_date             -- list of per-day dicts, most recent first;
                                   each dict has: date, scored, tokens_input,
                                   tokens_output, cost_usd
    """
    conn = get_connection(db_path)
    try:
        totals_row = conn.execute(
            """
            SELECT
                COUNT(CASE WHEN score IS NOT NULL THEN 1 END) AS total_scored,
                COALESCE(SUM(tokens_input), 0)                AS total_tokens_input,
                COALESCE(SUM(tokens_output), 0)               AS total_tokens_output
            FROM listings
            """
        ).fetchone()

        total_scored: int = totals_row["total_scored"]
        total_tokens_input: int = totals_row["total_tokens_input"]
        total_tokens_output: int = totals_row["total_tokens_output"]
        estimated_cost_usd: float = (
            total_tokens_input  / 1_000_000 * input_cost_per_mtok
            + total_tokens_output / 1_000_000 * output_cost_per_mtok
        )

        day_rows = conn.execute(
            """
            SELECT
                DATE(fetched_at)                                    AS date,
                COUNT(CASE WHEN score IS NOT NULL THEN 1 END)       AS scored,
                COALESCE(SUM(tokens_input), 0)                      AS tokens_input,
                COALESCE(SUM(tokens_output), 0)                     AS tokens_output
            FROM listings
            GROUP BY DATE(fetched_at)
            ORDER BY date DESC
            """
        ).fetchall()

        by_date: list[dict] = []
        for row in day_rows:
            tok_in = row["tokens_input"]
            tok_out = row["tokens_output"]
            by_date.append(
                {
                    "date": row["date"],
                    "scored": row["scored"],
                    "tokens_input": tok_in,
                    "tokens_output": tok_out,
                    "cost_usd": (
                        tok_in  / 1_000_000 * input_cost_per_mtok
                        + tok_out / 1_000_000 * output_cost_per_mtok
                    ),
                }
            )

        return {
            "total_scored": total_scored,
            "total_tokens_input": total_tokens_input,
            "total_tokens_output": total_tokens_output,
            "estimated_cost_usd": estimated_cost_usd,
            "by_date": by_date,
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Toggle helpers
# ---------------------------------------------------------------------------

def set_bookmarked(listing_id: int, value: int, db_path: str = _DEFAULT_DB_PATH) -> None:
    """Set bookmarked to 1 (save) or 0 (unsave) for the given internal id."""
    conn = get_connection(db_path)
    try:
        conn.execute(
            "UPDATE listings SET bookmarked = ? WHERE id = ?",
            (int(bool(value)), listing_id),
        )
        conn.commit()
    finally:
        conn.close()


def set_dismissed(listing_id: int, value: int, db_path: str = _DEFAULT_DB_PATH) -> None:
    """Set dismissed to 1 (hide) or 0 (restore) for the given internal id."""
    conn = get_connection(db_path)
    try:
        conn.execute(
            "UPDATE listings SET dismissed = ? WHERE id = ?",
            (int(bool(value)), listing_id),
        )
        conn.commit()
    finally:
        conn.close()


def set_applied(listing_id: int, value: int, db_path: str = _DEFAULT_DB_PATH) -> None:
    """Set applied to 1 (mark as applied) or 0 (unmark) for the given internal id."""
    conn = get_connection(db_path)
    try:
        conn.execute(
            "UPDATE listings SET applied = ? WHERE id = ?",
            (int(bool(value)), listing_id),
        )
        conn.commit()
    finally:
        conn.close()


def get_applied(db_path: str = _DEFAULT_DB_PATH) -> list[dict]:
    """Return all listings where applied = 1, ordered by fetched_at DESC."""
    conn = get_connection(db_path)
    try:
        rows = conn.execute(
            """
            SELECT * FROM listings
            WHERE applied = 1
            ORDER BY fetched_at DESC
            """,
        ).fetchall()
        return [_deserialise_row(r) for r in rows]
    finally:
        conn.close()
