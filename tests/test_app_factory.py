"""tests/test_app_factory.py — Smoke tests for the Phase 1 app factory.

Verifies the singleton contract of ``create_app()``, the re-export
contract between ``web.__init__`` and ``app``, and that the
``web.filters`` and ``web.security`` modules are independently
importable without going through the full application stack.

These tests are intentionally free of network calls, database access,
and Flask application setup so they run in any environment where the
project dependencies are installed.
"""

from __future__ import annotations

from datetime import datetime, timezone


# ---------------------------------------------------------------------------
# create_app() factory contract
# ---------------------------------------------------------------------------

class TestCreateAppFactory:
    """Tests for the ``create_app()`` factory function."""

    def test_create_app_returns_flask_instance(self) -> None:
        """create_app() must return a flask.Flask object.

        Verifies the most basic contract of the factory: the returned
        object is a real Flask application instance, not a mock or
        wrapper.
        """
        import flask
        from web import create_app

        result = create_app()

        assert isinstance(result, flask.Flask)

    def test_create_app_is_singleton(self) -> None:
        """Repeated calls to create_app() must return the same object.

        The factory caches its result in ``web._app_instance``.
        Two successive calls must return the identical object (``is``
        check, not just equality) so that route decorators that already
        ran against the first instance remain valid.
        """
        from web import create_app

        first = create_app()
        second = create_app()

        assert first is second

    def test_create_app_matches_app_module_app(self) -> None:
        """create_app() must return the same instance as ``app.app``.

        ``app.py`` does ``app = create_app()`` at module scope and then
        re-exports it.  Any consumer that imports ``from app import app``
        must receive the exact same object that ``create_app()`` returns.
        """
        import app as app_module
        from web import create_app

        factory_result = create_app()

        assert factory_result is app_module.app


# ---------------------------------------------------------------------------
# Direct-import smoke tests (independent loadability)
# ---------------------------------------------------------------------------

class TestWebFiltersDirectlyImportable:
    """Tests that web.filters is independently importable and functional."""

    def test_web_filters_directly_importable(self) -> None:
        """web.filters must be importable without the Flask app context.

        Importing from ``web.filters`` directly (not via ``web`` or
        ``app``) must succeed, and the three public filter functions
        must be callable.  This proves the module has no hidden
        dependency on Flask application state at import time.
        """
        from web.filters import salary_fmt, parse_iso, timeago

        assert callable(salary_fmt)
        assert callable(parse_iso)
        assert callable(timeago)

    def test_salary_fmt_returns_nonempty_string(self) -> None:
        """salary_fmt must format a min/max salary pair as a string.

        Exercises the core formatting path with a well-formed listing
        dict to confirm the function works without Flask context.
        """
        from web.filters import salary_fmt

        result = salary_fmt({"salary_min": 50000, "salary_max": 70000})

        assert isinstance(result, str)
        assert len(result) > 0

    def test_timeago_returns_nonempty_string_for_recent_datetime(
        self,
    ) -> None:
        """timeago must return a non-empty string for a recent datetime.

        Uses a datetime 10 minutes in the past so the output is in the
        "N minutes ago" range rather than an absolute date, confirming
        the relative-time path executes without Flask context.
        """
        from web.filters import timeago
        from datetime import timedelta

        recent = datetime.now(tz=timezone.utc) - timedelta(minutes=10)
        result = timeago(recent)

        assert isinstance(result, str)
        assert len(result) > 0


class TestWebSecurityDirectlyImportable:
    """Tests that web.security is independently importable and functional."""

    def test_web_security_directly_importable(self) -> None:
        """web.security must be importable without the Flask app context.

        All four public symbols must be importable directly from
        ``web.security``.  This proves the module has no hidden
        dependency on Flask application state at import time.
        """
        from web.security import (  # noqa: F401
            _is_trusted_host,
            _is_localhost_request,
            inject_demo_mode,
            csrf_localhost_guard,
        )

        assert callable(_is_trusted_host)
        assert callable(_is_localhost_request)
        assert callable(inject_demo_mode)
        assert callable(csrf_localhost_guard)

    def test_is_trusted_host_returns_true_for_localhost(self) -> None:
        """_is_trusted_host('localhost') must return True.

        The literal string "localhost" is the most common private-
        network hostname and must always be trusted, even though it
        does not parse as an IP address.
        """
        from web.security import _is_trusted_host

        assert _is_trusted_host("localhost") is True
