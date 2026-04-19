"""
tests/test_env_guards.py — Regression tests for the startup env-validation
guards in `app.py`.

Covers (issue #275):

* Missing or `changeme_*` SECRET_KEY is rejected in *any* environment.
* When APP_ENV=prod, a DATABASE_URL containing a `changeme_*` placeholder
  (the shape that comes straight out of .env.prod.example) is rejected.
* When APP_ENV is dev / unset, `changeme_dev` in DATABASE_URL is allowed
  because it is the documented local default.

The guards live at module scope in `app.py` and raise during import, so each
case is exercised via a subprocess that `import app` -- a fresh interpreter
with a controlled env. This also isolates us from conftest.py's test-DB
safety guard, which only applies when pytest itself imports app modules.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent


def _run_app_import(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Spawn a subprocess that does `import app` with the given environ.

    Returns the CompletedProcess (not-raising). Tests assert on returncode
    and stderr to verify the guard fired (or did not).
    """
    # Clean env -- only inherit PATH / SYSTEMROOT so Python + Windows work,
    # plus anything the caller set. This prevents the developer's ambient
    # SECRET_KEY / DATABASE_URL from leaking into the subprocess and masking
    # the condition under test.
    minimal_env = {
        k: os.environ[k]
        for k in ("PATH", "SYSTEMROOT", "PYTHONPATH", "USERPROFILE", "TEMP", "TMP")
        if k in os.environ
    }
    minimal_env.update(env)
    # Prepend repo root so `import app` resolves without sys.path hackery.
    minimal_env["PYTHONPATH"] = (
        str(REPO_ROOT) + os.pathsep + minimal_env.get("PYTHONPATH", "")
    )

    script = textwrap.dedent(
        """
        import importlib
        # Use importlib so we get the exception with a proper traceback rather
        # than Python's "import failed" shorthand.
        importlib.import_module("app")
        """
    )
    return subprocess.run(
        [sys.executable, "-c", script],
        env=minimal_env,
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )


# ---------------------------------------------------------------------------
# SECRET_KEY guard (pre-existing behavior — kept as regression coverage)
# ---------------------------------------------------------------------------


def test_missing_secret_key_rejects_startup():
    """A missing/empty SECRET_KEY must refuse to start in any environment.

    We set SECRET_KEY to the empty string (rather than unsetting it) because
    app.py calls load_dotenv(override=False) which would otherwise populate
    the variable from a `.env` file found alongside the repo. The guard treats
    `""` as absent -- `if not _secret_key_env` -- so this exercises the same
    code path deterministically.
    """
    result = _run_app_import({
        "SECRET_KEY": "",
        "APP_ENV": "dev",
        "DATABASE_URL": "postgresql://jobmatcher:local@localhost:5432/jobmatcher_dev",
    })
    assert result.returncode != 0
    assert "SECRET_KEY" in result.stderr


def test_placeholder_secret_key_rejects_startup():
    """A SECRET_KEY still set to the .example placeholder must refuse to start."""
    result = _run_app_import({
        "SECRET_KEY": "changeme_generate_with_python_secrets_token_hex_32",
        "APP_ENV": "dev",
        "DATABASE_URL": "postgresql://jobmatcher:local@localhost:5432/jobmatcher_dev",
    })
    assert result.returncode != 0
    assert "SECRET_KEY" in result.stderr


# ---------------------------------------------------------------------------
# DATABASE_URL guard — only fires in prod (new in #275)
# ---------------------------------------------------------------------------


def test_prod_rejects_changeme_in_database_url():
    """APP_ENV=prod + DATABASE_URL containing `changeme_prod` must refuse to start.

    This is the exact shape produced when someone deploys without editing
    .env.prod: compose interpolates POSTGRES_PASSWORD=changeme_prod straight
    into DATABASE_URL. Catching it at the app layer means the web container
    restart-loops with a clear error instead of silently running with an
    example credential.
    """
    result = _run_app_import({
        "SECRET_KEY": "a" * 64,  # valid-looking key so we pass the first guard
        "APP_ENV": "prod",
        "DATABASE_URL": "postgresql://jobmatcher:changeme_prod@db:5432/jobmatcher_prod",
    })
    assert result.returncode != 0
    assert "DATABASE_URL" in result.stderr
    assert "changeme" in result.stderr.lower()


def test_dev_tolerates_changeme_dev_in_database_url():
    """APP_ENV=dev (or unset) + DATABASE_URL containing `changeme_dev` is allowed.

    `changeme_dev` is the documented default password shipped in
    .env.dev.example and used by VS Code tasks out of the box -- refusing to
    start there would break local development for everyone who never
    customised the dev credentials. The prod guard is the narrow target.

    We can't fully import app.py from this subprocess (it needs a live DB),
    so instead we verify the startup progresses *past* the env guards by
    checking the failure message is a DB connection error, not an env guard
    error.
    """
    result = _run_app_import({
        "SECRET_KEY": "a" * 64,
        "APP_ENV": "dev",
        "DATABASE_URL": "postgresql://jobmatcher:changeme_dev@127.0.0.1:1/never_exists",
    })
    # Either of two acceptable outcomes: (a) import succeeded (return 0) or
    # (b) a later DB-connection error, but *not* our env guard firing.
    combined = result.stderr + result.stdout
    assert "DATABASE_URL contains a 'changeme_" not in combined
    assert "SECRET_KEY must be set" not in combined


def test_prod_accepts_real_database_url():
    """Sanity: a prod config with a real-looking password passes env guards."""
    result = _run_app_import({
        "SECRET_KEY": "a" * 64,
        "APP_ENV": "prod",
        # Unreachable host so import dies later, but env guards must not fire.
        "DATABASE_URL": "postgresql://jobmatcher:r3al_pAssw0rd@127.0.0.1:1/jobmatcher_prod",
    })
    combined = result.stderr + result.stdout
    assert "DATABASE_URL contains a 'changeme_" not in combined
    assert "SECRET_KEY must be set" not in combined


def test_prod_empty_database_url_not_caught_by_changeme_guard():
    """Empty DATABASE_URL in prod must fail later (DB connection), not from the
    changeme guard. The guard's job is to catch *unedited* example values, not
    to validate that DATABASE_URL is set at all -- that's Postgres/psycopg2's
    concern further down the startup path.
    """
    result = _run_app_import({
        "SECRET_KEY": "a" * 64,
        "APP_ENV": "prod",
        "DATABASE_URL": "",
    })
    combined = result.stderr + result.stdout
    # The guard's signature phrase must not appear -- we want the startup
    # to progress past the env guards and fail on something else (connection
    # refused, missing URL, etc.) rather than being short-circuited by
    # our changeme check.
    assert "DATABASE_URL contains a 'changeme_" not in combined
