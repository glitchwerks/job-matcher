"""
db.py — SQLite database layer for Job Matcher.

All interactions with jobs.db go through this module. No other module
should import sqlite3 or open the database file directly.

JSON array columns (matched_skills, missing_skills, concerns) are
serialised to strings on write and deserialised to Python lists on read.
Rows returned from read helpers are plain dicts, not sqlite3.Row objects.
"""

import json
import sqlite3

# ---------------------------------------------------------------------------
# Pricing constants — Haiku pricing per million tokens (update if rates change)
# ---------------------------------------------------------------------------

_HAIKU_INPUT_COST_PER_MTOK = 0.80   # USD per million input tokens
_HAIKU_OUTPUT_COST_PER_MTOK = 4.00  # USD per million output tokens


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

def get_connection(db_path: str = "jobs.db") -> sqlite3.Connection:
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

def init_db(db_path: str = "jobs.db") -> None:
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
                seen                INTEGER DEFAULT 0
            )
        """)

        # Migrate existing databases — SQLite does not support
        # ALTER TABLE ... ADD COLUMN IF NOT EXISTS, so we catch the
        # OperationalError that is raised when the column already exists.
        for column, typedef in (
            ("tokens_input", "INTEGER"),
            ("tokens_output", "INTEGER"),
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

def listing_exists(adzuna_id: str, db_path: str = "jobs.db") -> bool:
    """Return True if a row with the given adzuna_id already exists."""
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT 1 FROM listings WHERE adzuna_id = ?", (adzuna_id,)
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def insert_listing(listing: dict, db_path: str = "jobs.db") -> None:
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

    # Ensure token columns are present even if absent from the source dict.
    row.setdefault("tokens_input", None)
    row.setdefault("tokens_output", None)

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
                tokens_input, tokens_output
            ) VALUES (
                :adzuna_id, :title, :company, :location,
                :salary_min, :salary_max, :salary_is_predicted,
                :contract_type, :contract_time,
                :description, :redirect_url,
                :created_at, :fetched_at,
                :score, :matched_skills, :missing_skills, :concerns, :verdict,
                :bookmarked, :dismissed, :seen,
                :tokens_input, :tokens_output
            )
            """,
            row,
        )
        conn.commit()
    finally:
        conn.close()


def update_score(adzuna_id: str, score_data: dict, db_path: str = "jobs.db") -> None:
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
                tokens_output  = :tokens_output
            WHERE adzuna_id = :adzuna_id
            """,
            {
                **data,
                "adzuna_id": adzuna_id,
                "tokens_input": data.get("tokens_input"),
                "tokens_output": data.get("tokens_output"),
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


def get_feed(threshold: float = 7.0, db_path: str = "jobs.db") -> list[dict]:
    """Return listings with score >= threshold and dismissed = 0, ordered by score DESC.

    Listings whose score is NULL are excluded (they have not been scored yet).
    """
    conn = get_connection(db_path)
    try:
        rows = conn.execute(
            """
            SELECT * FROM listings
            WHERE score >= ? AND dismissed = 0
            ORDER BY score DESC
            """,
            (threshold,),
        ).fetchall()
        return [_deserialise_row(r) for r in rows]
    finally:
        conn.close()


def get_bookmarks(db_path: str = "jobs.db") -> list[dict]:
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


def get_listing_by_id(listing_id: int, db_path: str = "jobs.db") -> dict | None:
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


def get_usage_stats(db_path: str = "jobs.db") -> dict:
    """Return aggregated API usage and cost statistics.

    Queries the listings table to produce totals and a per-day breakdown.
    All token columns are nullable — NULL values are treated as 0 via
    SQLite's COALESCE so the arithmetic is always well-defined.

    Returns:
        Dict with keys:
            total_scored        -- count of listings with score IS NOT NULL
            total_tokens_input  -- sum of tokens_input across all rows
            total_tokens_output -- sum of tokens_output across all rows
            estimated_cost_usd  -- float, calculated from Haiku list pricing
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
            total_tokens_input / 1_000_000 * _HAIKU_INPUT_COST_PER_MTOK
            + total_tokens_output / 1_000_000 * _HAIKU_OUTPUT_COST_PER_MTOK
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
                        tok_in / 1_000_000 * _HAIKU_INPUT_COST_PER_MTOK
                        + tok_out / 1_000_000 * _HAIKU_OUTPUT_COST_PER_MTOK
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

def set_bookmarked(listing_id: int, value: int, db_path: str = "jobs.db") -> None:
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


def set_dismissed(listing_id: int, value: int, db_path: str = "jobs.db") -> None:
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
