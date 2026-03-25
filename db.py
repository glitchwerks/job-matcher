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
                bookmarked, dismissed, seen
            ) VALUES (
                :adzuna_id, :title, :company, :location,
                :salary_min, :salary_max, :salary_is_predicted,
                :contract_type, :contract_time,
                :description, :redirect_url,
                :created_at, :fetched_at,
                :score, :matched_skills, :missing_skills, :concerns, :verdict,
                :bookmarked, :dismissed, :seen
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
                seen           = 1
            WHERE adzuna_id = :adzuna_id
            """,
            {**data, "adzuna_id": adzuna_id},
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
