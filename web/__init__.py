"""Web layer — Flask application factory and filter/hook registration.

``create_app()`` is the single authoritative place where the Flask
``app`` object is constructed and all startup-time guards, template
filters, context processors, and before-request hooks are wired up.

After Phase 1 of the refactor, ``app.py`` delegates to this factory:

.. code-block:: python

    from web import create_app
    app = create_app()

All route definitions remain in ``app.py`` for now and are imported
as a side-effect of the ``from app import ...`` call inside
``create_app()``.  Subsequent phases will migrate routes into
blueprint modules under ``web/``.
"""

from __future__ import annotations

import os
import re
import sys as _sys
from typing import Optional

from dotenv import load_dotenv
from flask import Flask

import db
from web.filters import parse_iso, salary_fmt, timeago
from web.security import (
    _is_trusted_host,  # noqa: F401 — re-exported for test-import contract
    csrf_localhost_guard,
    inject_demo_mode,
)

# Module-level singleton so repeated calls to create_app() return the
# same Flask instance.  app.py stores the result at module scope; any
# call after the first (e.g. from the smoke test) must return that
# exact object rather than constructing a new one.
_app_instance: Optional[Flask] = None


def create_app() -> Flask:
    """Build, configure, and return the Flask application instance.

    Performs all startup-time validation (``SECRET_KEY``, prod
    ``DATABASE_URL`` placeholder check), registers Jinja filters and
    the CSRF before-request hook, initialises the database schema, and
    ensures job-source plugins are registered.

    Subsequent calls return the same singleton instance — construction
    and registration happen exactly once per process.

    Returns:
        A fully configured :class:`flask.Flask` instance.

    Raises:
        RuntimeError: If ``SECRET_KEY`` is absent or starts with
            ``"changeme"``, or if ``APP_ENV=prod`` and ``DATABASE_URL``
            contains a ``changeme_*`` placeholder.
    """
    global _app_instance
    if _app_instance is not None:
        return _app_instance

    # ------------------------------------------------------------------
    # 1. Load .env (no-op when the parent process already set the vars).
    # ------------------------------------------------------------------
    # Precedence: parent-process env (shell, VSCode task, docker
    # env_file) always wins.  This covers the native ``python app.py``
    # path where no external env loader exists; under Docker,
    # ``env_file:`` has already populated os.environ before this runs.
    load_dotenv(override=False)

    # ------------------------------------------------------------------
    # 2. Validate SECRET_KEY before creating the Flask instance.
    # ------------------------------------------------------------------
    # A stable secret key is required for session-based CSRF tokens.
    # Refuse to start with an empty or placeholder value — a fresh
    # random key on every restart invalidates session cookies and
    # breaks CSRF protection.
    _secret_key_env = os.environ.get("SECRET_KEY", "")
    if not _secret_key_env or _secret_key_env.startswith("changeme"):
        raise RuntimeError(
            "SECRET_KEY must be set to a secure random value. "
            "Generate one with: "
            'python -c "import secrets; print(secrets.token_hex(32))" '
            "and set it in .env.dev / .env.prod."
        )

    # ------------------------------------------------------------------
    # 3. Prod-only env placeholder guard.
    # ------------------------------------------------------------------
    # In prod the stack must not start with example ``changeme_*``
    # values coming straight out of ``.env.prod.example``.  The most
    # common failure mode is a server that was provisioned once and
    # never had its live ``.env.prod`` edited — the stack would come up
    # with a known-default Postgres password and a literal changeme
    # DATABASE_URL.  Refuse to start so this is caught immediately.
    #
    # We scope this to APP_ENV=prod so that local dev (where
    # ``changeme_dev`` is the documented default password) continues to
    # work untouched.
    #
    # The regex matches ``changeme`` only when bounded by ``:`` or
    # ``/`` on the left and ``_`` or ``@`` on the right, eliminating
    # false positives on legitimate passwords that happen to contain
    # ``changeme`` as a substring.
    if os.environ.get("APP_ENV", "").lower() == "prod":
        _database_url = os.environ.get("DATABASE_URL", "")
        if re.search(r"[:/]changeme[_@]", _database_url, re.IGNORECASE):
            raise RuntimeError(
                "DATABASE_URL contains a 'changeme_*' placeholder. "
                "Edit .env.prod and set a real POSTGRES_PASSWORD, "
                "then recreate the db container: "
                "docker compose -p job-matcher-pr-prod "
                "-f docker-compose.prod.yml down -v && ... up -d"
            )

    # ------------------------------------------------------------------
    # 4. Construct the Flask application.
    # ------------------------------------------------------------------
    # __name__ here resolves to "web" — Flask uses this to locate
    # templates and static files.  Because templates/ and static/ sit
    # next to app.py (not inside web/), we pass explicit paths so
    # Flask resolves them relative to the project root, not this
    # package directory.
    _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    app = Flask(
        __name__,
        template_folder=os.path.join(_root, "templates"),
        static_folder=os.path.join(_root, "static"),
    )
    app.secret_key = _secret_key_env

    # ------------------------------------------------------------------
    # 5. Inject Jinja globals (available in every template).
    # ------------------------------------------------------------------
    app.jinja_env.globals["APP_ENV"] = os.environ.get("APP_ENV", "local")
    app.jinja_env.globals["APP_VERSION"] = os.environ.get(
        "APP_VERSION", "local"
    )

    # ------------------------------------------------------------------
    # 6. Register Jinja template filters.
    # ------------------------------------------------------------------
    app.add_template_filter(salary_fmt, "salary_fmt")
    app.add_template_filter(parse_iso, "parse_iso")
    app.add_template_filter(timeago, "timeago")

    # ------------------------------------------------------------------
    # 7. Register the CSRF before-request hook.
    # ------------------------------------------------------------------
    app.before_request(csrf_localhost_guard)

    # ------------------------------------------------------------------
    # 8. Register the demo-mode context processor.
    # ------------------------------------------------------------------
    # DEMO_MODE lives in app.py at module scope.  We cannot import
    # ``app`` here at create_app() call-time because create_app() is
    # itself called from inside app.py's module initialisation, which
    # would produce a circular import with a partially-constructed
    # module.  Instead we look up the module via sys.modules at
    # *request* time — by then app.py is fully initialised and
    # sys.modules["app"] is stable.  This also means that when the
    # __main__ block sets DEMO_MODE=True after create_app() returns,
    # every subsequent request sees the updated value.
    def _demo_mode_processor() -> dict:
        """Return demo_mode for injection into every template context."""
        # Deliberate: look up "app" by its canonical module name. app.py
        # is always imported as `app` throughout this codebase — tests
        # use `from app import app as flask_app`, scripts use
        # `python app.py`, and no caller imports it under an alias. If
        # that ever changes, this processor will silently fall back to
        # DEMO_MODE=False, which is the safe default.
        _mod = _sys.modules.get("app")
        demo = getattr(_mod, "DEMO_MODE", False) if _mod else False
        return inject_demo_mode(demo)

    app.context_processor(_demo_mode_processor)

    # ------------------------------------------------------------------
    # 9. Register blueprints (Phase 5a + 5b).
    # ------------------------------------------------------------------
    # All blueprints are registered with url_prefix="" so every URL path
    # is byte-identical to the pre-refactor monolithic layout.
    #
    # Phase 5a: feed_bp, ingest_bp
    # Phase 5b: settings_bp, profile_bp, admin_bp
    #
    # Endpoint naming: blueprint-qualified names ("settings_bp.settings",
    # "profile_bp.profile", etc.) — Flask always prepends the blueprint
    # name even when the route uses an explicit endpoint= override (the
    # override only renames the local function-name component, not the
    # blueprint prefix). Inner url_for() calls in the moved handlers use
    # the prefixed names.
    #
    # Templates were unaffected by the move: every template uses
    # hard-coded URL paths in href/hx-get/hx-post/action attributes
    # (~80 references, all unchanged because no URL changed). No
    # template ever called url_for("settings") / url_for("profile") /
    # etc., so the absence of bare-name endpoints is invisible to the
    # rendered UI.
    from web.admin import admin_bp  # noqa: PLC0415
    from web.feed import feed_bp  # noqa: PLC0415
    from web.ingest import ingest_bp  # noqa: PLC0415
    from web.profile import profile_bp  # noqa: PLC0415
    from web.settings import settings_bp  # noqa: PLC0415

    app.register_blueprint(feed_bp, url_prefix="")
    app.register_blueprint(ingest_bp, url_prefix="")
    app.register_blueprint(settings_bp, url_prefix="")
    app.register_blueprint(profile_bp, url_prefix="")
    app.register_blueprint(admin_bp, url_prefix="")

    # ------------------------------------------------------------------
    # 10. Database initialisation and plugin registration.
    # ------------------------------------------------------------------
    db.init_db()

    from job_sources.auto_register import (  # noqa: PLC0415
        ensure_plugins_registered,
    )
    # _PROVIDERS_PATH is defined in app.py, but importing from app here
    # would create a circular import (app.py -> create_app() -> app.py).
    # Duplicating this one-line join is the lesser evil until Phase 2
    # relocates the path constants to services/profile_store.py.
    _providers_path = os.path.join(_root, "config", "providers.json")
    ensure_plugins_registered(_providers_path)

    _app_instance = app
    return app
