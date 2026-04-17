"""
conftest.py — project-level pytest configuration.

Patches db.init_db() and db.get_listing_count() before any test module imports
app.py so that settings and other Flask-route tests can run without a live
PostgreSQL connection.

Tests that genuinely require the database (test_db.py, test_ingest_run.py,
etc.) must set DATABASE_URL in the environment to a real Postgres instance —
they connect normally because the patches applied here only prevent the
module-level init and the listing-count query used by the settings page.

Database safety guard
---------------------
When DATABASE_URL IS set (i.e. tests will touch a real Postgres instance),
a session-start guard checks that the database name contains "test".  If it
does not, pytest exits immediately with a clear error message rather than
letting fixture teardown delete rows from a dev or production database.

Set ``ALLOW_NON_TEST_DB=1`` to bypass the guard (e.g. for CI runs against an
ephemeral non-test-named DB).  A stderr warning is printed in that case.
"""
from __future__ import annotations

import os
import sys
from unittest.mock import patch

# ---------------------------------------------------------------------------
# Suppress the module-level db.init_db() call in app.py and the per-request
# db.get_listing_count() call in the /settings GET handler so that tests
# which only exercise Flask routes (settings, reorder, security, etc.) do
# not require a live Postgres instance.
#
# The patch is applied before any test module is collected, which is when
# `import app` first runs.  Tests that intentionally exercise database
# behaviour will still work because they set DATABASE_URL to a real server
# and can re-enter psycopg2 freely — they just won't be blocked by the
# import-time init call.
# ---------------------------------------------------------------------------
# Provide a stable test secret key so that importing app.py does not raise
# RuntimeError from the SECRET_KEY validation added to guard against empty or
# placeholder values in real deployments.
os.environ.setdefault(
    "SECRET_KEY", "test-secret-key-not-for-production-use"
)

if not os.environ.get("DATABASE_URL"):
    # Provide a dummy URL so db.py's startup guard doesn't raise, then
    # immediately patch the two DB entry-points used by app.py.
    os.environ.setdefault("DATABASE_URL", "postgresql://dummy:dummy@localhost:5432/dummy")

    _init_db_patcher = patch("db.init_db", return_value=None)
    _init_db_patcher.start()

    _listing_count_patcher = patch("db.get_listing_count", return_value=0)
    _listing_count_patcher.start()

    # Patchers are intentionally never stopped — they live for the whole
    # pytest session.

else:
    # DATABASE_URL is set — a real Postgres instance will be used.
    # Refuse to run against a non-test database unless the escape hatch is
    # explicitly set, to prevent fixture teardown from wiping real data.
    # See tests/_db_name_guard.py for the pure function and its unit tests.
    # Import is deferred here because it is only needed when DATABASE_URL is
    # set (i.e. a real Postgres instance will be used).  The if-branch above
    # patches the DB entirely and never touches the guard module, so importing
    # it unconditionally at module level would be wasteful and confusing.
    import tests._db_name_guard as _guard  # noqa: E402

    _db_url = os.environ["DATABASE_URL"]
    _allow = bool(os.environ.get("ALLOW_NON_TEST_DB"))

    try:
        _guard.check_database_url_is_test(_db_url, allow_override=_allow)
    except Exception as _exc:  # pytest.UsageError or ValueError
        print(str(_exc), file=sys.stderr)
        # pytest.exit() is the cleanest way to abort collection immediately.
        import pytest as _pytest
        _pytest.exit(str(_exc), returncode=4)
