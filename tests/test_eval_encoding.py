"""tests/test_eval_encoding.py — Unit tests for scripts/_utf8_stdout.py.

Covers three scenarios:
  1. Positive: after calling the helper, sys.stdout is UTF-8.
  2. No-op safety: calling the helper when stdout lacks ``reconfigure``
     must not raise.
  3. Replacement behavior: printing a cp1252-incompatible character after
     reconfigure succeeds (no UnicodeEncodeError).
"""

from __future__ import annotations

import io
import sys

import pytest

# The helper lives in scripts/, which may not be on sys.path when pytest
# collects from the repo root.  Add the scripts directory explicitly so
# the import resolves without depending on the eval_rubric import chain
# (which requires DATABASE_URL and pulls in psycopg2 + the whole ingest
# module).
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from _utf8_stdout import reconfigure_utf8_stdout  # noqa: E402


# ---------------------------------------------------------------------------
# Positive test: reconfigure sets UTF-8 encoding on sys.stdout
# ---------------------------------------------------------------------------


def test_reconfigure_sets_utf8_encoding(monkeypatch: pytest.MonkeyPatch) -> None:
    """After calling the helper, sys.stdout encoding is utf-8.

    We replace sys.stdout with a real ``open()``-backed stream so that
    ``reconfigure`` is available, then call the helper and assert the
    resulting encoding is UTF-8.
    """
    # Use a real file-like stream that supports reconfigure (TextIOWrapper).
    buf = io.BytesIO()
    fake_stdout = io.TextIOWrapper(buf, encoding="cp1252")
    fake_stderr = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")

    monkeypatch.setattr(sys, "stdout", fake_stdout)
    monkeypatch.setattr(sys, "stderr", fake_stderr)

    reconfigure_utf8_stdout()

    actual = sys.stdout.encoding.lower().replace("-", "")
    assert actual == "utf8", (
        f"Expected sys.stdout encoding 'utf8' after reconfigure, got {actual!r}"
    )


# ---------------------------------------------------------------------------
# No-op safety: helper must not raise when stdout lacks reconfigure
# ---------------------------------------------------------------------------


def test_reconfigure_noop_when_no_reconfigure_method(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Calling the helper on a stream without reconfigure must not raise.

    ``io.StringIO`` does not expose a ``reconfigure`` method, which
    simulates environments where stdout is already a simple in-memory
    stream (e.g. inside pytest's capsys capture).
    """
    fake_stdout = io.StringIO()
    fake_stderr = io.StringIO()

    # Confirm our precondition: StringIO has no reconfigure.
    assert not hasattr(fake_stdout, "reconfigure")

    monkeypatch.setattr(sys, "stdout", fake_stdout)
    monkeypatch.setattr(sys, "stderr", fake_stderr)

    # Must complete without raising any exception.
    reconfigure_utf8_stdout()


# ---------------------------------------------------------------------------
# Replacement behavior: non-cp1252 chars print without UnicodeEncodeError
# ---------------------------------------------------------------------------


def test_em_dash_prints_without_error_after_reconfigure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Printing an em dash after reconfigure succeeds instead of crashing.

    This reproduces the exact failure mode from Issue #244: on Windows,
    sys.stdout is cp1252 when redirected; U+2014 EM DASH is not encodable
    in cp1252 and would raise UnicodeEncodeError without the helper.

    We verify:
      - No exception is raised when printing the em dash.
      - The output either contains the literal em dash (UTF-8 path) or
        the replacement character "?" (errors="replace" path) — never an
        exception.
    """
    buf = io.BytesIO()
    # Start with cp1252 to simulate the Windows-redirect failure scenario.
    fake_stdout = io.TextIOWrapper(buf, encoding="cp1252")
    fake_stderr = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")

    monkeypatch.setattr(sys, "stdout", fake_stdout)
    monkeypatch.setattr(sys, "stderr", fake_stderr)

    reconfigure_utf8_stdout()

    em_dash = "\u2014"
    chinese_char = "\u4e2d"

    # Both of these must not raise.
    print(em_dash, file=sys.stdout, end="")
    print(chinese_char, file=sys.stdout, end="")
    sys.stdout.flush()

    written = buf.getvalue().decode("utf-8", errors="replace")
    # The written bytes should contain the characters or their replacements
    # — the key assertion is that we reached this line without an exception.
    assert em_dash in written or "?" in written
    assert chinese_char in written or "?" in written
