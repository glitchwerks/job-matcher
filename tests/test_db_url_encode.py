"""tests/test_db_url_encode.py — Unit tests for db._encode_database_url_password.

Exercises the pure URL-rewriting function in isolation — no live database,
no env-var coupling, no subprocess.  Covers:

* Passwords that contain URI-reserved characters and need encoding.
* Passwords that are already correctly percent-encoded (idempotency).
* URLs with no password component (pass-through).
* Edge-case and malformed inputs that must not raise.

Issue: #288 — DATABASE_URL password containing URI-reserved characters
(e.g. ``@``, ``:``, ``/``, ``#``) silently breaks libpq URL parsing.
"""
from __future__ import annotations

from db import _encode_database_url_password


# ---------------------------------------------------------------------------
# Happy path: passwords that contain reserved chars must be encoded
# ---------------------------------------------------------------------------


class TestReservedCharactersAreEncoded:
    """Passwords with URI-reserved characters must be percent-encoded."""

    def test_at_sign_is_encoded(self):
        """``@`` in a password is encoded to ``%40``."""
        result = _encode_database_url_password(
            "postgresql://user:p@ss@localhost:5432/db"
        )
        assert "p%40ss" in result
        assert result.endswith("@localhost:5432/db")

    def test_colon_in_password_is_encoded(self):
        """``:`  in a password is encoded to ``%3A``."""
        result = _encode_database_url_password(
            "postgresql://user:p:ass@localhost:5432/db"
        )
        assert "p%3Aass" in result

    def test_slash_cannot_be_auto_encoded_must_be_preencoded(self):
        """``/`` in a password breaks urlsplit before we can extract the password.

        ``/`` is a URL path delimiter — ``urlsplit`` misparses the netloc when
        the raw password contains it.  The function returns the URL unchanged
        rather than guessing.  Users must pre-encode: ``p/ss`` → ``p%2Fss``.
        """
        url = "postgresql://user:p%2Fass@localhost:5432/db"
        # The pre-encoded form is passed through unchanged (idempotent).
        result = _encode_database_url_password(url)
        assert "p%2Fass" in result

    def test_hash_cannot_be_auto_encoded_must_be_preencoded(self):
        """``#`` in a password breaks urlsplit before we can extract the password.

        ``#`` is the fragment delimiter — everything after it is treated as the
        fragment, not the password.  Users must pre-encode: ``p#ss`` → ``p%23ss``.
        """
        url = "postgresql://user:p%23ass@localhost:5432/db"
        # The pre-encoded form is passed through unchanged (idempotent).
        result = _encode_database_url_password(url)
        assert "p%23ass" in result

    def test_question_mark_cannot_be_auto_encoded_must_be_preencoded(self):
        """``?`` in a password breaks urlsplit before we can extract the password.

        ``?`` is the query-string delimiter — everything after it is treated as
        the query, not part of the password.  Users must pre-encode:
        ``p?ss`` → ``p%3Fss``.
        """
        url = "postgresql://user:p%3Fass@localhost:5432/db"
        # The pre-encoded form is passed through unchanged (idempotent).
        result = _encode_database_url_password(url)
        assert "p%3Fass" in result

    def test_multiple_reserved_chars_at_and_colon_in_password(self):
        """A password with ``@`` and ``:`` encodes both automatically."""
        result = _encode_database_url_password(
            "postgresql://user:p@s:sword@localhost:5432/db"
        )
        assert "p%40s%3Asword" in result
        # Host/port/db path must be preserved unchanged.
        assert result.endswith("@localhost:5432/db")

    def test_postgres_scheme_alias_supported(self):
        """Both ``postgresql://`` and ``postgres://`` schemes are handled."""
        result = _encode_database_url_password(
            "postgres://user:p@ss@localhost:5432/db"
        )
        assert "p%40ss" in result
        assert result.startswith("postgres://")

    def test_encoded_url_is_parseable_by_urlsplit(self):
        """The returned URL can be re-parsed by urlsplit without error."""
        from urllib.parse import urlsplit
        url = "postgresql://user:p@ss:w0rd@host:5432/testdb"
        result = _encode_database_url_password(url)
        parsed = urlsplit(result)
        assert parsed.hostname == "host"
        assert parsed.port == 5432
        assert parsed.path == "/testdb"


# ---------------------------------------------------------------------------
# Idempotency: already-encoded passwords must not be double-encoded
# ---------------------------------------------------------------------------


class TestIdempotency:
    """Passwords that are already correctly percent-encoded are not changed."""

    def test_already_encoded_at_sign_unchanged(self):
        """``%40`` in a password is left as ``%40`` (not ``%2540``)."""
        url = "postgresql://user:p%40ss@localhost:5432/db"
        result = _encode_database_url_password(url)
        assert "p%40ss" in result
        # Must not double-encode: %40 → %2540.
        assert "%2540" not in result

    def test_already_encoded_colon_unchanged(self):
        """``%3A`` in a password stays ``%3A``."""
        url = "postgresql://user:p%3Aass@localhost:5432/db"
        result = _encode_database_url_password(url)
        assert "p%3Aass" in result
        assert "%253A" not in result

    def test_fully_encoded_complex_password_unchanged(self):
        """A fully-encoded password with multiple encoded chars is unchanged."""
        url = "postgresql://user:p%40s%3As%2Fw%23rd@localhost:5432/db"
        result = _encode_database_url_password(url)
        # Content must be preserved.
        assert "p%40s%3As%2Fw%23rd" in result
        # No double-encoding.
        assert "%25" not in result

    def test_applying_twice_is_idempotent(self):
        """Calling the function twice on the same URL returns the same result."""
        url = "postgresql://user:p@ss:word@localhost:5432/db"
        once = _encode_database_url_password(url)
        twice = _encode_database_url_password(once)
        assert once == twice

    def test_password_of_only_encoded_reserved_chars_is_idempotent(self):
        """A password composed entirely of encoded reserved chars is unchanged.

        Regression test: ``%40%40%40`` is already a valid percent-encoded
        sequence.  The decode-then-encode round-trip must reproduce the same
        string and must not double-encode (e.g. ``%40`` → ``%2540``).
        """
        url = "postgresql://user:%40%40%40@host:5432/db"
        result = _encode_database_url_password(url)
        assert result == url
        assert "%2540" not in result


# ---------------------------------------------------------------------------
# Pass-through: URLs with no password must be returned unchanged
# ---------------------------------------------------------------------------


class TestNoPasswordPassThrough:
    """URLs without a password component must be returned unchanged."""

    def test_url_without_password_unchanged(self):
        """No password → URL is returned as-is."""
        url = "postgresql://user@localhost:5432/db"
        assert _encode_database_url_password(url) == url

    def test_url_without_userinfo_unchanged(self):
        """No user/password section at all → URL is returned as-is."""
        url = "postgresql://localhost:5432/db"
        assert _encode_database_url_password(url) == url

    def test_url_with_normal_password_unchanged(self):
        """A password with no reserved chars is returned unchanged."""
        url = "postgresql://user:safepw@localhost:5432/db"
        assert _encode_database_url_password(url) == url

    def test_url_preserves_query_params(self):
        """Query parameters (e.g. sslmode) survive the rewrite unchanged."""
        url = "postgresql://user:p@ss@localhost:5432/db?sslmode=require"
        result = _encode_database_url_password(url)
        assert "sslmode=require" in result

    def test_url_preserves_non_default_port(self):
        """Non-standard port numbers survive the rewrite unchanged."""
        url = "postgresql://user:p@ss@localhost:5433/db"
        result = _encode_database_url_password(url)
        assert ":5433/" in result

    def test_host_at_sign_not_confused_with_password_at_sign(self):
        """The host portion's structural ``@`` is not confused with password content."""
        url = "postgresql://user:p%40ss@db.example.com:5432/db"
        result = _encode_database_url_password(url)
        # The structural @ separating credentials from host must still be there.
        assert "@db.example.com" in result


# ---------------------------------------------------------------------------
# Edge cases and graceful error handling
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Malformed or unusual inputs must never raise an exception."""

    def test_empty_string_returns_empty_string(self):
        """An empty string is returned as-is without raising."""
        assert _encode_database_url_password("") == ""

    def test_non_postgres_scheme_returned_unchanged(self):
        """Non-postgresql URLs are parsed and handled gracefully."""
        url = "sqlite:///local.db"
        # sqlite has no password; should be returned unchanged.
        result = _encode_database_url_password(url)
        assert result == url

    def test_plain_dsn_string_returned_unchanged(self):
        """A DSN key=value string (no ``://``) is returned unchanged."""
        dsn = "host=localhost dbname=mydb user=u password=p@ss"
        # urlsplit will not see a password component → pass-through.
        result = _encode_database_url_password(dsn)
        assert result == dsn

    def test_empty_password_component_unchanged(self):
        """A URL with an explicit empty password (user:@host) is unchanged."""
        url = "postgresql://user:@localhost:5432/db"
        result = _encode_database_url_password(url)
        # Empty password → no encoding needed; function should not crash.
        assert result == url
