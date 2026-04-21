"""
db.py — PostgreSQL database layer for Job Matcher.

All interactions with the database go through this module. No other module
should import psycopg2 or open database connections directly.

JSON array columns (matched_skills, missing_skills, concerns) are
serialised to strings on write and deserialised to Python lists on read.
Rows returned from read helpers are plain dicts (psycopg2 RealDictCursor
already returns dict-like rows; we convert them to plain dicts for
consistency).

Connection pooling
------------------
A module-level ``ThreadedConnectionPool`` (minconn=1, maxconn=10) is
initialised at import time.  Every call to ``get_connection()`` checks out
a connection from the pool; ``_Conn.close()`` returns it.  This avoids
the TCP handshake overhead of opening a fresh connection per request.

``DATABASE_URL`` environment variable is required — the module raises
``RuntimeError`` at import time if it is absent so the error surfaces
immediately rather than at the first database call.
"""

import json
import logging
import os
import threading
from urllib.parse import quote, unquote, urlsplit, urlunsplit

import psycopg2
import psycopg2.extras
import psycopg2.pool

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# DATABASE_URL — required; no fallback to avoid committing credentials.
# ---------------------------------------------------------------------------

def _encode_database_url_password(url: str) -> str:
    """Return *url* with its password component percent-encoded.

    libpq's URI parser treats ``@``, ``:``, ``/``, ``#``, ``?``, and other
    RFC 3986 reserved characters as structural delimiters, so a raw password
    like ``p@ss:word`` silently breaks the connection string.

    This function uses ``urllib.parse.urlsplit`` to extract the password, then
    decodes it (in case it is already encoded) and re-encodes it.  The
    decode-then-encode round-trip makes the operation **idempotent**: calling
    it twice on the same URL always produces the same result.

    **Limitation — ``/``, ``#``, ``?`` in passwords:**  These characters act
    as URL structural delimiters and cause ``urlsplit`` itself to misparse the
    URL *before* the password component can be extracted.  When the password
    contains them the URL is already broken at the point this function runs
    and cannot be automatically repaired.  Users must percent-encode such
    characters manually in their ``.env`` file:
    ``p/ss`` → ``p%2Fss``, ``p#ss`` → ``p%23ss``, ``p?ss`` → ``p%3Fss``.

    Characters handled automatically: ``@`` (→ ``%40``), ``:`` (→ ``%3A``),
    and any other non-delimiter reserved character that ``urlsplit`` can still
    extract from the password field.

    If the URL has no password component, or cannot be parsed (e.g. a DSN
    key=value string), it is returned unchanged.

    Args:
        url: A ``postgresql://`` (or ``postgres://``) connection string.

    Returns:
        The same URL with its password component safely percent-encoded,
        or the original string if no password was found / parsing failed.
    """
    try:
        parsed = urlsplit(url)
    except (ValueError, AttributeError) as exc:
        logger.warning(
            "Failed to URL-encode DATABASE_URL password (%s);"
            " using raw value",
            type(exc).__name__,
        )
        return url

    if not parsed.password:
        return url

    # Decode first so that an already-encoded password is not double-encoded,
    # then re-encode with an empty safe set so every reserved char is escaped.
    raw_password = unquote(parsed.password)
    encoded_password = quote(raw_password, safe="")

    if encoded_password == parsed.password:
        # Password was already correctly encoded — avoid reconstructing URL
        # (preserves any unusual but valid original formatting).
        return url

    # Reconstruct netloc with the encoded password.
    # urlsplit stores netloc as "user:password@host:port".  We rebuild it
    # rather than using urlunsplit on parsed directly because SplitResult
    # does not expose a setter for individual netloc parts.
    user = parsed.username or ""
    host = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port else ""
    netloc = f"{user}:{encoded_password}@{host}{port}"

    return urlunsplit((
        parsed.scheme,
        netloc,
        parsed.path,
        parsed.query,
        parsed.fragment,
    ))


_DATABASE_URL: str | None = os.environ.get("DATABASE_URL")
if not _DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL environment variable is required. "
        "Set it in your .env file (see .env.example)."
    )

# Percent-encode the password so URI-reserved characters (@ : / # ?) do not
# confuse libpq's URL parser.  This is idempotent — already-encoded URLs are
# left unchanged.
_DATABASE_URL = _encode_database_url_password(_DATABASE_URL)

# ---------------------------------------------------------------------------
# Explicit allowlist for user-supplied sort keys → safe SQL ORDER BY clauses.
# Never interpolate a raw user value into SQL; look it up here instead.
# ---------------------------------------------------------------------------
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
# Connection pool — initialised once at module import.
# ---------------------------------------------------------------------------

_pool: psycopg2.pool.ThreadedConnectionPool | None = None
_pool_lock = threading.Lock()


def _get_pool() -> psycopg2.pool.ThreadedConnectionPool:
    """Return the module-level connection pool, creating it on first call."""
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = psycopg2.pool.ThreadedConnectionPool(
                    minconn=1,
                    maxconn=10,
                    dsn=_DATABASE_URL,
                    cursor_factory=psycopg2.extras.RealDictCursor,
                    connect_timeout=5,
                )
    return _pool


# ---------------------------------------------------------------------------
# _Conn — thin wrapper that makes psycopg2 connections behave like the
# sqlite3 connection interface used throughout this module.
# ---------------------------------------------------------------------------

class _Conn:
    """Wraps a psycopg2 connection checked out from the pool.

    Each call to ``execute()`` creates a **new cursor** so that callers that
    hold a cursor reference while issuing a second query do not interfere with
    each other (matching the sqlite3.Connection.execute() contract).

    On context-manager exit, any in-flight transaction is rolled back on
    exception before the connection is returned to the pool.
    """

    def __init__(self, conn, pool: psycopg2.pool.ThreadedConnectionPool):
        self._conn = conn
        self._pool = pool

    def execute(self, sql: str, params=None):
        """Execute *sql* with *params* on a fresh cursor and return that cursor."""
        cur = self._conn.cursor()
        cur.execute(sql, params or ())
        return cur

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        """Return the underlying connection to the pool."""
        self._pool.putconn(self._conn)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is not None:
            try:
                self._conn.rollback()
            # Catch-all: stale run cleanup is best-effort; swallow any rollback error
            # so the original exception is not masked.
            except Exception:
                pass
        self._pool.putconn(self._conn)


# ---------------------------------------------------------------------------
# Connection factory
# ---------------------------------------------------------------------------

def get_connection() -> _Conn:
    """Check out a connection from the pool and return it wrapped in ``_Conn``.

    The connection has ``autocommit=True`` so every statement auto-commits in
    its own transaction — matching the implicit behaviour of sqlite3.  Write
    paths that need an explicit transaction call ``conn.commit()`` directly
    after their statement(s); psycopg2 in autocommit mode treats each
    statement as its own implicit transaction unless the caller issues BEGIN.

    The caller is responsible for closing the connection (which returns it to
    the pool), either explicitly or via the ``_Conn`` context manager.
    """
    pool = _get_pool()
    raw = pool.getconn()
    raw.autocommit = True
    return _Conn(raw, pool)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def init_db() -> None:
    """Create or migrate the listings table.

    Idempotent — safe to call on every startup.  Uses ``IF NOT EXISTS`` and
    ``ADD COLUMN IF NOT EXISTS`` so repeated calls are no-ops.

    PostgreSQL migration notes vs. the legacy SQLite schema
    -------------------------------------------------------
    - ``SERIAL PRIMARY KEY`` replaces ``INTEGER PRIMARY KEY AUTOINCREMENT``.
    - ``BOOLEAN`` replaces ``INTEGER DEFAULT 0`` for flag columns.
    - ``INSERT ... ON CONFLICT DO NOTHING`` replaces ``INSERT OR IGNORE``.
    - ``ILIKE`` replaces ``LOWER(col) LIKE LOWER(?)``.
    - ``%s`` placeholders replace ``?``.
    """
    with get_connection() as conn:
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

        # Apply ADD COLUMN migrations for columns added after initial schema.
        # PostgreSQL supports ADD COLUMN IF NOT EXISTS (v9.6+).
        for column, typedef in (
            ("tokens_input",       "INTEGER"),
            ("tokens_output",      "INTEGER"),
            ("applied",            "INTEGER DEFAULT 0"),
            ("job_type",           "TEXT"),
            ("model_used",         "TEXT"),
            ("posted_at",          "TEXT"),
            ("opened_at",          "TEXT"),
            ("description_source", "TEXT NOT NULL DEFAULT 'full'"),
        ):
            conn.execute(
                f"ALTER TABLE listings ADD COLUMN IF NOT EXISTS {column} {typedef}"
            )

        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_listings_redirect_url ON listings (redirect_url)"
        )

        # --- #114: Reclassify JSearch listings with full API descriptions ---
        # JSearch provides complete job descriptions via the API, but the
        # pipeline previously marked all skip_scrape listings as "snippet".
        try:
            cur = conn.execute(
                """UPDATE listings
                   SET description_source = 'full'
                   WHERE source = 'jsearch'
                     AND LENGTH(description) >= 100
                     AND description_source = 'snippet'"""
            )
            if cur.rowcount:
                print(
                    f"Migration #114: reclassified {cur.rowcount} JSearch listings from "
                    "'snippet' to 'full'"
                )
        except (psycopg2.ProgrammingError, psycopg2.DataError) as e:
            print(f"Migration #114 (JSearch reclassify): {e}")

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

        # Ingest run tracking table — records start/end of each pipeline run
        # so the Admin UI can show run history and scheduler health.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ingest_runs (
                id              SERIAL PRIMARY KEY,
                trigger_source  TEXT NOT NULL,
                started_at      TIMESTAMPTZ NOT NULL,
                finished_at     TIMESTAMPTZ,
                status          TEXT NOT NULL,
                fetched         INTEGER DEFAULT 0,
                filtered        INTEGER DEFAULT 0,
                scored          INTEGER DEFAULT 0,
                failed_count    INTEGER DEFAULT 0,
                cost_usd        NUMERIC(10, 4) DEFAULT 0,
                log_filename    TEXT,
                error_message   TEXT
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_ingest_runs_started_at "
            "ON ingest_runs (started_at DESC)"
        )

        # Stale-row sweep: a SIGKILL'd ingest leaves status='running' forever.
        # 1 hour: longest observed ingest run is ~15 min; 1h gives 4x margin.
        conn.execute("""
            UPDATE ingest_runs
               SET status = 'failed',
                   error_message = 'process died — detected on next startup'
             WHERE status = 'running'
               AND started_at < NOW() - INTERVAL '1 hour'
        """)


# ---------------------------------------------------------------------------
# Ingest run helpers
# ---------------------------------------------------------------------------

def create_ingest_run(trigger_source: str, log_filename: str | None = None) -> int:
    """Insert a new ingest_runs row at status='running' and return its id.

    Args:
        trigger_source: How this run was triggered (``'scheduled'``,
            ``'manual_ui'``, or ``'manual_cli'``).
        log_filename:   Base filename of the per-run log file, or ``None``
            if file logging is unavailable.

    Returns:
        The ``id`` of the newly created row.
    """
    with get_connection() as conn:
        cur = conn.execute(
            """INSERT INTO ingest_runs (trigger_source, started_at, status, log_filename)
               VALUES (%s, NOW(), 'running', %s)
               RETURNING id""",
            (trigger_source, log_filename),
        )
        return cur.fetchone()["id"]


def finish_ingest_run(
    run_id: int,
    status: str,
    counts: dict | None = None,
    cost_usd: float = 0,
    error_message: str | None = None,
) -> None:
    """Update an ingest_runs row with final status and metrics.

    Args:
        run_id:        The ``id`` returned by :func:`create_ingest_run`.
        status:        Final run status — ``'success'`` or ``'failed'``.
        counts:        Dict with optional keys ``fetched``, ``filtered``,
            ``scored``, ``failed``.  Missing keys default to 0.
        cost_usd:      Estimated LLM cost for the run in USD.
        error_message: Short error description when ``status='failed'``;
            truncated to 500 characters.
    """
    counts = counts or {}
    with get_connection() as conn:
        conn.execute(
            """UPDATE ingest_runs
                  SET finished_at   = NOW(),
                      status        = %s,
                      fetched       = %s,
                      filtered      = %s,
                      scored        = %s,
                      failed_count  = %s,
                      cost_usd      = %s,
                      error_message = %s
                WHERE id = %s""",
            (
                status,
                counts.get("fetched", 0),
                counts.get("filtered", 0),
                counts.get("scored", 0),
                counts.get("failed", 0),
                cost_usd,
                (error_message or "")[:500] if error_message else None,
                run_id,
            ),
        )


def get_recent_ingest_runs(limit: int = 10) -> list[dict]:
    """Return the most recent ingest runs, newest first.

    Args:
        limit: Maximum number of rows to return (default 10).

    Returns:
        List of dicts with all ``ingest_runs`` columns.
    """
    with get_connection() as conn:
        cur = conn.execute(
            "SELECT * FROM ingest_runs ORDER BY started_at DESC LIMIT %s",
            (limit,),
        )
        return [dict(row) for row in cur.fetchall()]


# ---------------------------------------------------------------------------
# Geocache helpers
# ---------------------------------------------------------------------------

def geocache_get_many(
    conn: _Conn,
    location_texts: list[str],
) -> dict[str, tuple[float, float]]:
    """Return cached (lat, lon) pairs for all location_text values that exist in the cache.

    Uses a single query with an IN clause to minimise round-trips.

    Args:
        conn:            Open _Conn connection.
        location_texts:  List of raw location strings to look up.

    Returns:
        Dict mapping location_text → (lat, lon) for cache hits only.
        Absent keys are cache misses.
    """
    if not location_texts:
        return {}
    placeholders = ",".join(["%s"] * len(location_texts))
    rows = conn.execute(
        f"SELECT location_text, lat, lon FROM location_geocache WHERE location_text IN ({placeholders})",
        location_texts,
    ).fetchall()
    return {row["location_text"]: (row["lat"], row["lon"]) for row in rows}


def geocache_put(
    conn: _Conn,
    location_text: str,
    lat: float,
    lon: float,
) -> None:
    """Insert or replace a geocache entry.

    Uses INSERT ... ON CONFLICT DO UPDATE so subsequent calls with the same
    location_text update the cached_at timestamp rather than raising a UNIQUE
    conflict.

    Args:
        conn:           Open _Conn connection.
        location_text:  Raw location string used as the cache key.
        lat:            Latitude of the resolved location.
        lon:            Longitude of the resolved location.
    """
    conn.execute(
        """
        INSERT INTO location_geocache (location_text, lat, lon)
        VALUES (%s, %s, %s)
        ON CONFLICT (location_text) DO UPDATE
            SET lat = EXCLUDED.lat, lon = EXCLUDED.lon,
                cached_at = CURRENT_TIMESTAMP
        """,
        (location_text, lat, lon),
    )


# ---------------------------------------------------------------------------
# Write helpers
# ---------------------------------------------------------------------------

def listing_exists(conn: _Conn, source: str, source_id: str) -> bool:
    """Return True if a row with the given (source, source_id) pair already exists.

    The caller is responsible for opening and closing the connection.  This
    avoids repeated open/close overhead when run() chains multiple dedup checks
    for the same listing.

    Args:
        conn:      Open _Conn connection.
        source:    Source identifier string, e.g. ``"adzuna"``.
        source_id: Source-specific listing ID string.
    """
    row = conn.execute(
        "SELECT 1 FROM listings WHERE source = %s AND source_id = %s",
        (source, source_id),
    ).fetchone()
    return row is not None


def listing_exists_by_url(conn: _Conn, redirect_url: str) -> bool:
    """Return True if a listing with this redirect_url already exists (cross-source dedup).

    Used as a secondary dedup check after (source, source_id) to catch the same
    job posted across multiple sources under different IDs.

    Args:
        conn:         Open _Conn connection.
        redirect_url: The canonical job URL to check.
    """
    row = conn.execute(
        "SELECT 1 FROM listings WHERE redirect_url = %s",
        (redirect_url,)
    ).fetchone()
    return row is not None


def insert_listing(listing: dict) -> None:
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

    with get_connection() as conn:
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


def update_score(
    source: str,
    source_id: str,
    score_data: dict,
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
    """
    data = dict(score_data)
    for col in ("matched_skills", "missing_skills", "concerns"):
        val = data.get(col)
        if isinstance(val, list):
            data[col] = json.dumps(val)
        elif val is None:
            data[col] = json.dumps([])

    with get_connection() as conn:
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


# ---------------------------------------------------------------------------
# Admin helpers
# ---------------------------------------------------------------------------

def get_listing_count() -> int:
    """Return the total number of rows in the listings table.

    Used by the Clear Database UI to show the user how many records will be
    deleted before they confirm.

    Returns:
        Integer row count (0 when the table is empty).
    """
    with get_connection() as conn:
        row = conn.execute("SELECT COUNT(*) AS cnt FROM listings").fetchone()
        return row["cnt"] if row else 0


def clear_all_listings(conn: _Conn) -> int:
    """Delete all rows from the listings table.  Schema and other tables are
    left intact (e.g. ``location_geocache`` is not touched).

    The DELETE is issued inside the caller-supplied connection so that the
    caller controls transaction scope (e.g. can wrap this in a try/finally
    that always commits).

    Args:
        conn: An open _Conn connection to the database.

    Returns:
        The number of rows deleted.
    """
    cursor = conn.execute("DELETE FROM listings")
    return cursor.rowcount


# ---------------------------------------------------------------------------
# Read helpers
# ---------------------------------------------------------------------------

def _deserialise_row(row) -> dict:
    """Convert a psycopg2 RealDictRow to a plain dict and deserialise JSON array columns."""
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
    """
    effective = min_score if min_score is not None else threshold

    conditions = ["score >= %s", "dismissed = 0", "applied = 0", "description_source = 'full'"]
    params: list = [effective]

    if remote_only:
        conditions.append("location ILIKE '%%remote%%'")

    if search:
        conditions.append("(title ILIKE %s OR company ILIKE %s)")
        term = f"%{search}%"
        params.extend([term, term])

    if job_type:
        conditions.append("job_type ILIKE %s")
        params.append(job_type)

    where_clause = " AND ".join(conditions)
    order_clause = _ALLOWED_SORT_COLUMNS.get(sort, _DEFAULT_SORT_CLAUSE)

    with get_connection() as conn:
        rows = conn.execute(
            f"SELECT * FROM listings WHERE {where_clause} ORDER BY {order_clause}",
            params,
        ).fetchall()
        return [_deserialise_row(r) for r in rows]


def get_snippet_feed(
    threshold: float = 7.0,
    min_score: float | None = None,
    remote_only: bool = False,
    search: str | None = None,
    job_type: str | None = None,
    sort: str | None = None,
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
    """
    effective = min_score if min_score is not None else threshold

    conditions = ["score >= %s", "dismissed = 0", "applied = 0", "description_source = 'snippet'"]
    params: list = [effective]

    if remote_only:
        conditions.append("location ILIKE '%%remote%%'")

    if search:
        conditions.append("(title ILIKE %s OR company ILIKE %s)")
        term = f"%{search}%"
        params.extend([term, term])

    if job_type:
        conditions.append("job_type ILIKE %s")
        params.append(job_type)

    where_clause = " AND ".join(conditions)
    order_clause = _ALLOWED_SORT_COLUMNS.get(sort, _DEFAULT_SORT_CLAUSE)

    with get_connection() as conn:
        rows = conn.execute(
            f"SELECT * FROM listings WHERE {where_clause} ORDER BY {order_clause}",
            params,
        ).fetchall()
        return [_deserialise_row(r) for r in rows]


def get_job_types() -> list[str]:
    """Return a sorted list of distinct non-null job_type values present in the listings table.

    Used to populate the filter dropdown dynamically so it only shows types
    that actually exist in the database.

    Returns:
        Sorted list of unique job_type strings, excluding NULL values.
    """
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT DISTINCT job_type FROM listings WHERE job_type IS NOT NULL ORDER BY job_type ASC"
        ).fetchall()
        return [row["job_type"] for row in rows]


def get_bookmarks() -> list[dict]:
    """Return all bookmarked listings ordered by score DESC."""
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT * FROM listings
            WHERE bookmarked = 1
            ORDER BY score DESC
            """,
        ).fetchall()
        return [_deserialise_row(r) for r in rows]


def get_all_scored() -> list[dict]:
    """Return all listings that have been scored (seen = 1), ordered by fetched_at DESC.

    Uses a subquery to pick the row with the highest id per (source, source_id)
    pair so that any accidental duplicate rows (e.g. from an imperfect migration)
    are collapsed to a single entry before the caller iterates them.
    """
    with get_connection() as conn:
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


def get_listing_by_id(listing_id: int) -> dict | None:
    """Return a single listing by internal id, or None if not found.

    JSON array columns are deserialised to Python lists, consistent with the
    other read helpers.
    """
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM listings WHERE id = %s", (listing_id,)
        ).fetchone()
        if row is None:
            return None
        return _deserialise_row(row)


def get_last_fetch_time():
    """Return the most recent fetched_at timestamp across all listings, or None.

    Used by the web UI to display how fresh the data is (e.g. "Last updated
    3 hours ago"). Returns a :class:`datetime.datetime` in UTC if any listings
    exist, or ``None`` when the table is empty.
    """
    import datetime

    with get_connection() as conn:
        row = conn.execute(
            "SELECT MAX(fetched_at) AS last_fetch FROM listings"
        ).fetchone()
        raw = row["last_fetch"] if row else None
        if raw is None:
            return None
        # fetched_at is stored as an ISO 8601 string (e.g. "2026-01-02T12:34:56Z"
        # or "2026-01-02T12:34:56").  fromisoformat() handles both; strip the
        # trailing "Z" which Python < 3.11 does not accept.
        raw = raw.rstrip("Z")
        return datetime.datetime.fromisoformat(raw)


def get_usage_stats(
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
    with get_connection() as conn:
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

        # PostgreSQL: DATE(col) works identically to SQLite for ISO timestamp strings.
        day_rows = conn.execute(
            """
            SELECT
                DATE(fetched_at)                                    AS date,
                COUNT(CASE WHEN score IS NOT NULL THEN 1 END)       AS scored,
                COALESCE(SUM(tokens_input), 0)                      AS tokens_input,
                COALESCE(SUM(tokens_output), 0)                     AS tokens_output,
                model_used
            FROM listings
            GROUP BY DATE(fetched_at), model_used
            ORDER BY date DESC
            """
        ).fetchall()

    # Aggregate per-date across all models that appeared on that day.
    by_date_map: dict[str, dict] = {}
    for row in day_rows:
        date_key = str(row["date"])
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
    by_date.sort(key=lambda d: d["date"], reverse=True)

    return {
        "total_scored": total_scored,
        "total_tokens_input": total_tokens_input,
        "total_tokens_output": total_tokens_output,
        "estimated_cost_usd": total_cost,
        "by_date": by_date,
    }


# ---------------------------------------------------------------------------
# Toggle helpers
# ---------------------------------------------------------------------------

def set_bookmarked(listing_id: int, value: int) -> None:
    """Set bookmarked to 1 (save) or 0 (unsave) for the given internal id."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE listings SET bookmarked = %s WHERE id = %s",
            (int(bool(value)), listing_id),
        )


def set_dismissed(listing_id: int, value: int) -> None:
    """Set dismissed to 1 (hide) or 0 (restore) for the given internal id."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE listings SET dismissed = %s WHERE id = %s",
            (int(bool(value)), listing_id),
        )


def set_applied(listing_id: int, value: int) -> None:
    """Set applied to 1 (mark as applied) or 0 (unmark) for the given internal id."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE listings SET applied = %s WHERE id = %s",
            (int(bool(value)), listing_id),
        )


def mark_opened(listing_id: int) -> None:
    """Record that the user has opened (expanded) this listing for the first time.

    Sets ``opened_at`` to the current UTC timestamp as an ISO 8601 string.
    This is idempotent — if ``opened_at`` is already set, the row is not
    updated, so repeat expansions do not overwrite the original open time.

    Args:
        listing_id: Internal integer primary key.
    """
    import datetime

    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with get_connection() as conn:
        conn.execute(
            "UPDATE listings SET opened_at = %s WHERE id = %s AND opened_at IS NULL",
            (now, listing_id),
        )


def toggle_bookmarked(listing_id: int) -> dict | None:
    """Atomically flip the bookmarked flag and return the updated listing.

    Uses a single SQL statement (``1 - bookmarked``) so concurrent requests
    cannot both read the same state and both write the same flipped value —
    the race condition that the read-flip-write pattern is vulnerable to.

    Args:
        listing_id: Internal integer primary key.

    Returns:
        The updated listing dict, or None if the id does not exist.
    """
    with get_connection() as conn:
        conn.execute(
            "UPDATE listings SET bookmarked = 1 - bookmarked WHERE id = %s",
            (listing_id,),
        )
    return get_listing_by_id(listing_id)


def toggle_applied(listing_id: int) -> dict | None:
    """Atomically flip the applied flag and return the updated listing.

    Uses a single SQL statement (``1 - applied``) so concurrent requests
    cannot both read the same state and both write the same flipped value —
    the race condition that the read-flip-write pattern is vulnerable to.

    Args:
        listing_id: Internal integer primary key.

    Returns:
        The updated listing dict, or None if the id does not exist.
    """
    with get_connection() as conn:
        conn.execute(
            "UPDATE listings SET applied = 1 - applied WHERE id = %s",
            (listing_id,),
        )
    return get_listing_by_id(listing_id)


def get_applied() -> list[dict]:
    """Return all listings where applied = 1, ordered by fetched_at DESC."""
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT * FROM listings
            WHERE applied = 1
            ORDER BY fetched_at DESC
            """,
        ).fetchall()
        return [_deserialise_row(r) for r in rows]
