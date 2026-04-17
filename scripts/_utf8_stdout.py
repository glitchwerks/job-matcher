"""scripts/_utf8_stdout.py — Reconfigure stdout/stderr to UTF-8.

On Windows with Python 3.14+, sys.stdout defaults to the active code page
(typically cp1252) when stdout is redirected to a file.  Any non-Latin-1
character (em dash, accented letters, CJK, etc.) then raises
UnicodeEncodeError and crashes the script mid-run.

Call ``reconfigure_utf8_stdout()`` as one of the first statements in any
script that may print arbitrary text from LLM responses or job titles.
"""

from __future__ import annotations

import sys


def reconfigure_utf8_stdout() -> None:
    """Reconfigure stdout and stderr to use UTF-8 with replacement fallback.

    Uses the ``reconfigure()`` method available on
    ``io.TextIOWrapper``-backed streams (CPython 3.7+).  The call is a
    no-op on streams that do not expose ``reconfigure`` (e.g. in-memory
    ``StringIO`` objects used in tests), so it is safe to call
    unconditionally.

    The ``errors="replace"`` policy means that any character that still
    cannot be encoded after switching to UTF-8 (e.g. certain surrogate
    pairs) is substituted with ``"?"`` rather than raising an exception.
    This is intentional — a partial run with a placeholder is far
    preferable to a crash that wastes LLM API spend.
    """
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
