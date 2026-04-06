"""
db.py — PostgreSQL database layer for Job Matcher.

All interactions with the database go through this module. No other module
should import psycopg2 or open a database connection directly.

JSON array columns (matched_skills, missing_skills, concerns) are
serialised to strings on write and deserialised to Python lists on read.
Rows returned from read helpers are plain dicts, not cursor row objects.
"""

import json
import os

import psycopg2
import psycopg2.extras

_DATABASE_URL: str = os.environ.get(
    "DATABASE_URL",
    "postgresql://jobmatcher:jobmatcher@localhost:5432/jobmatcher"
)
_USING_DEFAULT_DATABASE_URL: bool = "DATABASE_URL" not in os.environ

if _USING_DEFAULT_DATABASE_URL:
    import logging as _logging
    _logging.getLogger(__name__).warning(
        "DATABASE_URL is not set — using default local development credentials. "
        "Set DATABASE_URL in your environment or .env file for production."
    )

# Explicit allowlist for user-supplied sort keys → safe SQL ORDER BY clauses.
# Never interpolate a raw user value into SQL; look it up here instead.
_ALLOWED_SORT_COLUMNS: dict[str, str] = {
    "date_posted": "posted_at DESC",
}
_DEFAULT_SORT_CLAUSE = "score DESC"

# ---------------------------------------------------------------------------
# Pricing fallback constants — Haiku pricing used when the caller does not
# supply per-model rates.  Kept here only for backward compatibility with
# code paths that call get_usage_stats() without pricing arguments.
# The authoritative pricing table lives in providers/anthropic_provider.py.
# ---------------------------------------------------------------------------

_FALLBACK_INPUT_COST_PER_MTOK  = 0.80   # USD per million input tokens (Haiku)
_FALLBACK_OUTPUT_COST_PER_MTOK = 4.00   # USD per million output tokens (Haiku)

# ---------------------------------------------------------------------------
# Per-provider/model pricing lookup used by get_usage_stats().
# Mirrors the tables in providers/*_provider.py so that db.py has no import
# dependency on the providers package.
# Format: { provider_name: [ (model_prefix_or_id, input_mtok, output_mtok), ... ] }
# For Anthropic, prefix matching is used; for OpenAI/Gemini, exact matching.
# ---------------------------------------------------------------------------

_PRICING_TABLE: dict[str, list[tuple[str, float, float]]] = {
    "anthropic": [
        # prefix-matched (longest prefix wins in order)
        ("claude-opus-",   15.00, 75.00),
        ("claude-sonnet-",  3.00, 15.00),
        ("claude-haiku-",   0.80,  4.00),
    ],
    "openai": [
        # exact-matched
        ("gpt-4o-mini", 0.15,  0.60),
        ("gpt-4o",      2.50, 10.00),
    ],
    "gemini": [
        # exact-matched
        ("gemini-1.5-flash", 0.075,  0.30),
        ("gemini-1.5-pro",   3.50,  10.50),
    ],
}


def _lookup_pricing(model_used: str | None) -> tuple[float, float] | None:
    """Return ``(input_cost_per_mtok, output_cost_per_mtok)`` for a ``model_used`` string.

    *model_used* is stored as ``"provider/model"`` (e.g.
    ``"anthropic/claude-haiku-4-5-20251001"`` or ``"openai/gpt-4o-mini"``).
    Returns ``None`` when the model is unknown or the string cannot be parsed,
    so that callers can display ``"N/A"`` rather than a wrong number.

    Args:
        model_used: Value of the ``model_used`` DB column.

    Returns:
        ``(input_rate, output_rate)`` tuple, or ``None`` if unknown.
    """
    if not model_used:
        return None
    parts = model_used.split("/", 1)
    if len(parts) != 2:
        return None
    provider, model = parts[0].lower(), parts[1]
    entries = _PRICING_TABLE.get(provider)
    if entries is None:
        return None
    if provider == "anthropic":
        # prefix match
        for prefix, inp, out in entries:
            if model.startswith(prefix):
                return inp, out
        return None
    else:
        # exact match
        for name, inp, out in entries:
            if model == name:
                return inp, out
        return None


# ---------------------------------------------------------------------------
# Connection wrapper
# ---------------------------------------------------------------------------

class _Conn:
    """Thin wrapper around a psycopg2 connection that mimics the sqlite3.Connection
    interface used throughout this module (execute, commit, close, context manager).

    This keeps all ~28 call sites unchanged while swapping the underlying driver.
    """

    def __init__(self, conn):
        self._conn = conn
        self._cursor = conn.cursor()

    def execute(self, sql, params=None):
        self._cursor.execute(sql, params or ())
        return self._cursor

    def commit(self):
        self._conn.commit()

    def close(self):
        self._cursor.close()
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

def get_connection(db_path=None):  # db_path kept for signature compat, ignored
    """Return an open database connection wrapping a psycopg2 connection.

    The ``db_path`` parameter is accepted for API compatibility with call sites
    that previously passed a SQLite file path; it is silently ignored.
    The actual connection target is determined by the ``DATABASE_URL``
    environment variable (default: ``postgresql://jobmatcher:jobmatcher@localhost:5432/jobmatcher``).

    The caller is responsible for closing the connection (or using it as a
    context manager for transactions).
    """
    raw = psycopg2.connect(
        _DATABASE_URL,
        cursor_factory=psycopg2.extras.RealDictCursor,
        connect_timeout=5,
    )
    return _Conn(raw)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def init_db(db_path=None) -> None:
    """Create the listings and location_geocache tables if they do not exist.

    This is a clean CREATE TABLE IF NOT EXISTS approach — no migration paths.
    Data is disposable in the PostgreSQL deployment context. All calls are
    idempotent and safe to run on every startup.

    The ``db_path`` parameter is accepted for API compatibility; it is ignored.
    """
    conn = get_connection(db_path)
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS listings (
                id                  SERIAL PRIMARY KEY,
                source              TEXT NOT NULL DEFAULT 'adzuna',
                source_id           TEXT NOT NULL,
                title               TEXT,
                company             TEXT,
                location            TEXT,
                salary_min          REAL,
                salary_max          REAL,
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
                model_used          TEXT,
                tokens_input        INTEGER,
                tokens_output       INTEGER,
                applied             INTEGER DEFAULT 0,
                job_type            TEXT,
                posted_at           TEXT,
                opened_at           TEXT DEFAULT NULL,
                description_source  TEXT NOT NULL DEFAULT 'full',
                UNIQUE(source, source_id)
            )
        """)

        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_listings_redirect_url ON listings (redirect_url)"
        )

        # Geocache table — stores resolved lat/lon for location strings so that
        # repeated ingest runs do not re-call Nominatim for the same location.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS location_geocache (
                location_text TEXT PRIMARY KEY,
                lat REAL NOT NULL,
                lon REAL NOT NULL,
                cached_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Geocache helpers
# ---------------------------------------------------------------------------

def geocache_get_many(
    conn,
    location_texts: list[str],
) -> dict[str, tuple[float, float]]:
    """Return cached (lat, lon) pairs for all location_text values that exist in the cache.

    Uses a single query with an IN clause to minimise round-trips.

    Args:
        conn:            Open database connection.
        location_texts:  List of raw location strings to look up.

    Returns:
        Dict mapping location_text → (lat, lon) for cache hits only.
        Absent keys are cache misses.
    """
    if not location_texts:
        return {}
    # Build "%s,%s,..." placeholder string — safe because only the count varies,
    # not the values; all user data is passed via the parameterised tuple.
    placeholders = ",".join(["%s"] * len(location_texts))
    rows = conn.execute(
        f"SELECT location_text, lat, lon FROM location_geocache WHERE location_text IN ({placeholders})",
        location_texts,
    ).fetchall()
    return {row["location_text"]: (row["lat"], row["lon"]) for row in rows}


def geocache_put(
    conn,
    location_text: str,
    lat: float,
    lon: float,
) -> None:
    """Insert or update a geocache entry.

    Uses INSERT ... ON CONFLICT DO UPDATE so subsequent calls with the same
    location_text update the cached_at timestamp rather than raising a UNIQUE conflict.

    Args:
        conn:           Open database connection.
        location_text:  Raw location string used as the cache key.
        lat:            Latitude of the resolved location.
        lon:            Longitude of the resolved location.
    """
    conn.execute(
        """
        INSERT INTO location_geocache (location_text, lat, lon)
        VALUES (%s, %s, %s)
        ON CONFLICT (location_text) DO UPDATE
            SET lat = EXCLUDED.lat,
                lon = EXCLUDED.lon,
                cached_at = CURRENT_TIMESTAMP
        """,
        (location_text, lat, lon),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Write helpers
# ---------------------------------------------------------------------------

def listing_exists(conn, source: str, source_id: str) -> bool:
    """Return True if a row with the given (source, source_id) pair already exists.

    The caller is responsible for opening and closing the connection.  This
    avoids repeated open/close overhead when run() chains multiple dedup checks
    for the same listing.

    Args:
        conn:      Open database connection.
        source:    Source identifier string, e.g. ``"adzuna"``.
        source_id: Source-specific listing ID string.
    """
    row = conn.execute(
        "SELECT 1 FROM listings WHERE source = %s AND source_id = %s",
        (source, source_id),
    ).fetchone()
    return row is not None


def listing_exists_by_url(conn, redirect_url: str) -> bool:
    """Return True if a listing with this redirect_url already exists (cross-source dedup).

    Used as a secondary dedup check after (source, source_id) to catch the same
    job posted across multiple sources under different IDs.

    Args:
        conn:         Open database connection.
        redirect_url: The canonical job URL to check.
    """
    row = conn.execute(
        "SELECT 1 FROM listings WHERE redirect_url = %s",
        (redirect_url,)
    ).fetchone()
    return row is not None


def insert_listing(listing: dict, db_path=None) -> None:
    """Insert a new listing row.

    `listing` must contain all columns except `id`. The JSON array columns
    (matched_skills, missing_skills, concerns) may be supplied as Python
    lists — they will be serialised to JSON strings before insertion. They
    may also be supplied as strings or None, both of which are passed through
    unchanged.

    The ``db_path`` parameter is accepted for API compatibility; it is ignored.
    """
    # Serialise array columns if the caller passed Python lists.
    row = dict(listing)
    for col in ("matched_skills", "missing_skills", "concerns"):
        val = row.get(col)
        if isinstance(val, list):
            row[col] = json.dumps(val)
        elif val is None:
            row[col] = json.dumps([])

    # Ensure all optional columns are present even if absent from the source dict.
    row.setdefault("salary_min", None)
    row.setdefault("salary_max", None)
    row.setdefault("salary_is_predicted", None)
    row.setdefault("contract_type", None)
    row.setdefault("contract_time", None)
    row.setdefault("bookmarked", 0)
    row.setdefault("dismissed", 0)
    row.setdefault("seen", 0)
    row.setdefault("tokens_input", None)
    row.setdefault("tokens_output", None)
    row.setdefault("applied", 0)
    row.setdefault("job_type", None)
    row.setdefault("model_used", None)
    row.setdefault("source", "adzuna")
    row.setdefault("source_id", None)
    row.setdefault("posted_at", None)
    row.setdefault("description_source", "full")

    conn = get_connection(db_path)
    try:
        conn.execute(
            """
            INSERT INTO listings (
                source, source_id,
                title, company, location,
                salary_min, salary_max, salary_is_predicted,
                contract_type, contract_time,
                description, redirect_url,
                created_at, fetched_at,
                score, matched_skills, missing_skills, concerns, verdict,
                bookmarked, dismissed, seen,
                tokens_input, tokens_output,
                applied,
                job_type,
                model_used,
                posted_at,
                description_source
            ) VALUES (
                %(source)s, %(source_id)s,
                %(title)s, %(company)s, %(location)s,
                %(salary_min)s, %(salary_max)s, %(salary_is_predicted)s,
                %(contract_type)s, %(contract_time)s,
                %(description)s, %(redirect_url)s,
                %(created_at)s, %(fetched_at)s,
                %(score)s, %(matched_skills)s, %(missing_skills)s, %(concerns)s, %(verdict)s,
                %(bookmarked)s, %(dismissed)s, %(seen)s,
                %(tokens_input)s, %(tokens_output)s,
                %(applied)s,
                %(job_type)s,
                %(model_used)s,
                %(posted_at)s,
                %(description_source)s
            )
            """,
            row,
        )
        conn.commit()
    finally:
        conn.close()


def update_score(
    source: str,
    source_id: str,
    score_data: dict,
    db_path=None,
) -> None:
    """Write scoring results back to an existing row and mark it seen.

    Addresses the target row by (source, source_id) rather than adzuna_id so
    that listings from any source can be rescored without special-casing.

    Also updates ``description_source`` when provided in *score_data*, so that
    a listing that was initially stored as ``'snippet'`` can be promoted to
    ``'full'`` if a subsequent re-scrape succeeds.

    Args:
        source:     Source identifier string, e.g. ``"adzuna"``.
        source_id:  Source-specific listing ID string.
        score_data: Dict with keys: score, matched_skills, missing_skills,
                    concerns, verdict. Array fields may be Python lists or
                    JSON strings.  Optionally includes ``description_source``.
        db_path:    Ignored — kept for API compatibility.
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
            SET score              = %(score)s,
                matched_skills     = %(matched_skills)s,
                missing_skills     = %(missing_skills)s,
                concerns           = %(concerns)s,
                verdict            = %(verdict)s,
                seen               = 1,
                tokens_input       = %(tokens_input)s,
                tokens_output      = %(tokens_output)s,
                model_used         = %(model_used)s,
                description_source = COALESCE(%(description_source)s, description_source)
            WHERE source = %(source)s AND source_id = %(source_id)s
            """,
            {
                **data,
                "source": source,
                "source_id": source_id,
                "tokens_input": data.get("tokens_input"),
                "tokens_output": data.get("tokens_output"),
                "model_used": data.get("model_used"),
                "description_source": data.get("description_source"),
            },
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Admin helpers
# ---------------------------------------------------------------------------

def get_listing_count(db_path=None) -> int:
    """Return the total number of rows in the listings table.

    Used by the Clear Database UI to show the user how many records will be
    deleted before they confirm.

    Args:
        db_path: Ignored — kept for API compatibility.

    Returns:
        Integer row count (0 when the table is empty).
    """
    conn = get_connection(db_path)
    try:
        row = conn.execute("SELECT COUNT(*) AS cnt FROM listings").fetchone()
        return row["cnt"] if row else 0
    finally:
        conn.close()


def clear_all_listings(conn) -> int:
    """Delete all rows from the listings table.  Schema and other tables are
    left intact (e.g. ``location_geocache`` is not touched).

    The DELETE is issued inside the caller-supplied connection so that the
    caller controls transaction scope (e.g. can wrap this in a try/finally
    that always commits).  ``conn.commit()`` is called here so that an
    implicit transaction started by the DELETE is flushed.

    Args:
        conn: An open database connection.

    Returns:
        The number of rows deleted.
    """
    cursor = conn.execute("DELETE FROM listings")
    conn.commit()
    return cursor.rowcount


# ---------------------------------------------------------------------------
# Read helpers
# ---------------------------------------------------------------------------

def _deserialise_row(row) -> dict:
    """Convert a RealDictCursor row to a plain dict and deserialise JSON array columns."""
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
    sort: str | None = None,
    db_path=None,
) -> list[dict]:
    """Return listings with score >= effective threshold and dismissed = 0.

    Default ordering is by score DESC. Pass ``sort='date_posted'`` to order by
    ``posted_at DESC`` instead (newest listings first).

    Listings whose score is NULL are excluded (they have not been scored yet).

    Args:
        threshold:   Default score floor used when min_score is not provided.
        min_score:   If provided, overrides threshold as the score floor.
        remote_only: If True, restricts to listings whose location contains "remote".
        search:      If provided, filters by title or company containing the search string.
        job_type:    If provided, restricts to listings whose job_type matches (case-insensitive).
        sort:        Optional sort key. ``'date_posted'`` orders by posted_at DESC;
                     any other value (or None) falls back to score DESC.
        db_path:     Ignored — kept for API compatibility.
    """
    effective = min_score if min_score is not None else threshold

    conditions = ["score >= %s", "dismissed = 0", "applied = 0", "description_source = 'full'"]
    params: list = [effective]

    if remote_only:
        conditions.append("LOWER(location) LIKE '%remote%'")

    if search:
        conditions.append("(LOWER(title) LIKE %s OR LOWER(company) LIKE %s)")
        term = f"%{search.lower()}%"
        params.extend([term, term])

    if job_type:
        conditions.append("LOWER(job_type) = LOWER(%s)")
        params.append(job_type)

    where_clause = " AND ".join(conditions)

    order_clause = _ALLOWED_SORT_COLUMNS.get(sort, _DEFAULT_SORT_CLAUSE)

    conn = get_connection(db_path)
    try:
        rows = conn.execute(
            f"SELECT * FROM listings WHERE {where_clause} ORDER BY {order_clause}",
            params,
        ).fetchall()
        return [_deserialise_row(r) for r in rows]
    finally:
        conn.close()


def get_snippet_feed(
    threshold: float = 7.0,
    min_score: float | None = None,
    remote_only: bool = False,
    search: str | None = None,
    job_type: str | None = None,
    sort: str | None = None,
    db_path=None,
) -> list[dict]:
    """Return scored, non-dismissed listings whose description came from an API snippet.

    Snippet-scored listings are separated from the main feed because a score
    derived from a 200–400 character API snippet is a weaker signal than one
    derived from a full scraped job description.  This function returns them in
    their own dedicated view so the user can review them separately.

    Listings whose score is NULL are excluded (not yet scored).  Dismissed
    listings are excluded.  Only listings with ``score >= effective threshold``
    are returned, mirroring the behaviour of :func:`get_feed`.

    Args:
        threshold:   Default score floor used when min_score is not provided.
        min_score:   If provided, overrides threshold as the score floor.
        remote_only: If True, restricts to listings whose location contains "remote".
        search:      If provided, filters by title or company containing the search string.
        job_type:    If provided, restricts to listings whose job_type matches (case-insensitive).
        sort:        Optional sort key.  ``'date_posted'`` orders by posted_at DESC;
                     any other value (or None) falls back to score DESC.
        db_path:     Ignored — kept for API compatibility.
    """
    effective = min_score if min_score is not None else threshold

    conditions = ["score >= %s", "dismissed = 0", "applied = 0", "description_source = 'snippet'"]
    params: list = [effective]

    if remote_only:
        conditions.append("LOWER(location) LIKE '%remote%'")

    if search:
        conditions.append("(LOWER(title) LIKE %s OR LOWER(company) LIKE %s)")
        term = f"%{search.lower()}%"
        params.extend([term, term])

    if job_type:
        conditions.append("LOWER(job_type) = LOWER(%s)")
        params.append(job_type)

    where_clause = " AND ".join(conditions)
    order_clause = _ALLOWED_SORT_COLUMNS.get(sort, _DEFAULT_SORT_CLAUSE)

    conn = get_connection(db_path)
    try:
        rows = conn.execute(
            f"SELECT * FROM listings WHERE {where_clause} ORDER BY {order_clause}",
            params,
        ).fetchall()
        return [_deserialise_row(r) for r in rows]
    finally:
        conn.close()


def get_job_types(db_path=None) -> list[str]:
    """Return a sorted list of distinct non-null job_type values present in the listings table.

    Used to populate the filter dropdown dynamically so it only shows types
    that actually exist in the database.

    Args:
        db_path: Ignored — kept for API compatibility.

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


def get_bookmarks(db_path=None) -> list[dict]:
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


def get_all_scored(db_path=None) -> list[dict]:
    """Return all listings that have been scored (seen = 1), ordered by fetched_at DESC.

    Uses a subquery to pick the row with the highest id per (source, source_id)
    pair so that any accidental duplicate rows are collapsed to a single entry
    before the caller iterates them.
    """
    conn = get_connection(db_path)
    try:
        rows = conn.execute(
            """
            SELECT * FROM listings
            WHERE seen = 1
              AND id IN (
                  SELECT MAX(id)
                  FROM listings
                  WHERE seen = 1
                  GROUP BY source, source_id
              )
            ORDER BY fetched_at DESC
            """
        ).fetchall()
        return [_deserialise_row(r) for r in rows]
    finally:
        conn.close()


def get_listing_by_id(listing_id: int, db_path=None) -> dict | None:
    """Return a single listing by internal id, or None if not found.

    JSON array columns are deserialised to Python lists, consistent with the
    other read helpers.
    """
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM listings WHERE id = %s", (listing_id,)
        ).fetchone()
        if row is None:
            return None
        return _deserialise_row(row)
    finally:
        conn.close()


def get_last_fetch_time(db_path=None):
    """Return the most recent fetched_at timestamp across all listings, or None.

    Used by the web UI to display how fresh the data is (e.g. "Last updated
    3 hours ago"). Returns a :class:`datetime.datetime` in UTC if any listings
    exist, or ``None`` when the table is empty.

    Args:
        db_path: Ignored — kept for API compatibility.
    """
    import datetime

    conn = get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT MAX(fetched_at) AS last_fetch FROM listings"
        ).fetchone()
        raw = row["last_fetch"] if row else None
        if raw is None:
            return None
        # fetched_at is stored as an ISO 8601 string (e.g. "2026-01-02T12:34:56Z"
        # or "2026-01-02T12:34:56").  fromisoformat() handles both; strip the
        # trailing "Z" which Python < 3.11 does not accept.
        if isinstance(raw, datetime.datetime):
            return raw
        raw = str(raw).rstrip("Z")
        return datetime.datetime.fromisoformat(raw)
    finally:
        conn.close()


def get_usage_stats(
    db_path=None,
    input_cost_per_mtok: float = _FALLBACK_INPUT_COST_PER_MTOK,
    output_cost_per_mtok: float = _FALLBACK_OUTPUT_COST_PER_MTOK,
) -> dict:
    """Return aggregated API usage and cost statistics.

    Queries the listings table to produce totals and a per-day breakdown.
    All token columns are nullable — NULL values are treated as 0 via
    COALESCE so the arithmetic is always well-defined.

    Cost estimation uses per-model pricing from ``_lookup_pricing()``.  When
    a row's ``model_used`` value maps to a known model, the actual rates for
    that provider/model are used.  When the model is unknown or absent the
    row's cost contribution is ``None`` (unknown), and the per-day and total
    ``cost_usd`` / ``estimated_cost_usd`` fields are ``None`` when *any* row
    in the aggregation has an unknown model — this signals to the UI that a
    precise estimate cannot be shown rather than silently returning a wrong
    number.

    The ``input_cost_per_mtok`` / ``output_cost_per_mtok`` parameters are
    kept for backward compatibility but are no longer used internally; all
    cost calculations go through ``_lookup_pricing()``.

    Args:
        db_path:              Ignored — kept for API compatibility.
        input_cost_per_mtok:  Ignored — kept for backward-compatible callers.
        output_cost_per_mtok: Ignored — kept for backward-compatible callers.

    Returns:
        Dict with keys:
            total_scored        -- count of listings with score IS NOT NULL
            total_tokens_input  -- sum of tokens_input across all rows
            total_tokens_output -- sum of tokens_output across all rows
            estimated_cost_usd  -- float total cost, or None if any model is unknown
            by_date             -- list of per-day dicts, most recent first;
                                   each dict has: date, scored, tokens_input,
                                   tokens_output, cost_usd (float or None)
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

        # Fetch per-model token aggregates to compute accurate per-model costs.
        model_rows = conn.execute(
            """
            SELECT
                model_used,
                COALESCE(SUM(tokens_input), 0)  AS tokens_input,
                COALESCE(SUM(tokens_output), 0) AS tokens_output
            FROM listings
            GROUP BY model_used
            """
        ).fetchall()

        total_cost: float | None = 0.0
        for mrow in model_rows:
            pricing = _lookup_pricing(mrow["model_used"])
            if pricing is None:
                # At least one model_used value is unknown — cost is indeterminate.
                total_cost = None
                break
            in_rate, out_rate = pricing
            total_cost += (
                mrow["tokens_input"]  / 1_000_000 * in_rate
                + mrow["tokens_output"] / 1_000_000 * out_rate
            )

        day_rows = conn.execute(
            """
            SELECT
                DATE(fetched_at::timestamp)                         AS date,
                COUNT(CASE WHEN score IS NOT NULL THEN 1 END)       AS scored,
                COALESCE(SUM(tokens_input), 0)                      AS tokens_input,
                COALESCE(SUM(tokens_output), 0)                     AS tokens_output,
                model_used
            FROM listings
            GROUP BY DATE(fetched_at::timestamp), model_used
            ORDER BY date DESC
            """
        ).fetchall()

        # Aggregate per-date across all models that appeared on that day.
        by_date_map: dict[str, dict] = {}
        for row in day_rows:
            date_key = str(row["date"]) if row["date"] is not None else None
            if date_key not in by_date_map:
                by_date_map[date_key] = {
                    "date": date_key,
                    "scored": 0,
                    "tokens_input": 0,
                    "tokens_output": 0,
                    "cost_usd": 0.0,
                    "_has_unknown": False,
                }
            bucket = by_date_map[date_key]
            bucket["scored"] += row["scored"]
            tok_in = row["tokens_input"]
            tok_out = row["tokens_output"]
            bucket["tokens_input"]  += tok_in
            bucket["tokens_output"] += tok_out
            pricing = _lookup_pricing(row["model_used"])
            if pricing is None:
                bucket["_has_unknown"] = True
            elif not bucket["_has_unknown"]:
                in_rate, out_rate = pricing
                bucket["cost_usd"] += (
                    tok_in  / 1_000_000 * in_rate
                    + tok_out / 1_000_000 * out_rate
                )

        by_date: list[dict] = []
        for bucket in by_date_map.values():
            has_unknown = bucket.pop("_has_unknown")
            bucket["cost_usd"] = None if has_unknown else bucket["cost_usd"]
            by_date.append(bucket)
        # Sort most-recent first (keys are ISO date strings — lexicographic DESC works).
        by_date.sort(key=lambda d: d["date"] or "", reverse=True)

        return {
            "total_scored": total_scored,
            "total_tokens_input": total_tokens_input,
            "total_tokens_output": total_tokens_output,
            "estimated_cost_usd": total_cost,
            "by_date": by_date,
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Toggle helpers
# ---------------------------------------------------------------------------

def set_bookmarked(listing_id: int, value: int, db_path=None) -> None:
    """Set bookmarked to 1 (save) or 0 (unsave) for the given internal id."""
    conn = get_connection(db_path)
    try:
        conn.execute(
            "UPDATE listings SET bookmarked = %s WHERE id = %s",
            (int(bool(value)), listing_id),
        )
        conn.commit()
    finally:
        conn.close()


def set_dismissed(listing_id: int, value: int, db_path=None) -> None:
    """Set dismissed to 1 (hide) or 0 (restore) for the given internal id."""
    conn = get_connection(db_path)
    try:
        conn.execute(
            "UPDATE listings SET dismissed = %s WHERE id = %s",
            (int(bool(value)), listing_id),
        )
        conn.commit()
    finally:
        conn.close()


def set_applied(listing_id: int, value: int, db_path=None) -> None:
    """Set applied to 1 (mark as applied) or 0 (unmark) for the given internal id."""
    conn = get_connection(db_path)
    try:
        conn.execute(
            "UPDATE listings SET applied = %s WHERE id = %s",
            (int(bool(value)), listing_id),
        )
        conn.commit()
    finally:
        conn.close()


def mark_opened(listing_id: int, db_path=None) -> None:
    """Record that the user has opened (expanded) this listing for the first time.

    Sets ``opened_at`` to the current UTC timestamp as an ISO 8601 string.
    This is idempotent — if ``opened_at`` is already set, the row is not
    updated, so repeat expansions do not overwrite the original open time.

    Args:
        listing_id: Internal integer primary key.
        db_path:    Ignored — kept for API compatibility.
    """
    import datetime

    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = get_connection(db_path)
    try:
        conn.execute(
            "UPDATE listings SET opened_at = %s WHERE id = %s AND opened_at IS NULL",
            (now, listing_id),
        )
        conn.commit()
    finally:
        conn.close()


def toggle_bookmarked(listing_id: int, db_path=None) -> dict | None:
    """Atomically flip the bookmarked flag and return the updated listing.

    Uses a single SQL statement (``1 - bookmarked``) so concurrent requests
    cannot both read the same state and both write the same flipped value —
    the race condition that the read-flip-write pattern is vulnerable to.

    Args:
        listing_id: Internal integer primary key.
        db_path:    Ignored — kept for API compatibility.

    Returns:
        The updated listing dict, or None if the id does not exist.
    """
    conn = get_connection(db_path)
    try:
        conn.execute(
            "UPDATE listings SET bookmarked = 1 - bookmarked WHERE id = %s",
            (listing_id,),
        )
        conn.commit()
    finally:
        conn.close()
    return get_listing_by_id(listing_id, db_path=db_path)


def toggle_applied(listing_id: int, db_path=None) -> dict | None:
    """Atomically flip the applied flag and return the updated listing.

    Uses a single SQL statement (``1 - applied``) so concurrent requests
    cannot both read the same state and both write the same flipped value —
    the race condition that the read-flip-write pattern is vulnerable to.

    Args:
        listing_id: Internal integer primary key.
        db_path:    Ignored — kept for API compatibility.

    Returns:
        The updated listing dict, or None if the id does not exist.
    """
    conn = get_connection(db_path)
    try:
        conn.execute(
            "UPDATE listings SET applied = 1 - applied WHERE id = %s",
            (listing_id,),
        )
        conn.commit()
    finally:
        conn.close()
    return get_listing_by_id(listing_id, db_path=db_path)


def get_applied(db_path=None) -> list[dict]:
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
