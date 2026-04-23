"""CSRF and host-trust security helpers for the Job Matcher web layer.

Contains pure-function guards and Jinja context processors that are
registered with the Flask app by ``web.__init__.create_app()``.
None of these functions import the ``app`` object directly — they
use ``flask.current_app`` or ``flask.request`` when they need
request-scoped state, keeping this module independently testable.

Module-level state
------------------
``DEMO_MODE`` is intentionally NOT re-exported from this module. The
canonical value lives at ``app`` module scope (set by the
``__main__`` block in ``app.py``).
``web/__init__.py::create_app()`` wires up a context processor that
reads it at request time via ``sys.modules["app"]``, which preserves
the ability to toggle ``DEMO_MODE`` *after* ``create_app()`` has
returned.
"""

from __future__ import annotations

import ipaddress
import re

from flask import jsonify, request


def _is_trusted_host(host: str) -> bool:
    """Return ``True`` if *host* is localhost or any private/non-routable address.

    Uses :func:`ipaddress.ip_address` ``is_private`` which covers
    RFC 1918 (10.x, 172.16–31.x, 192.168.x), loopback (127.x.x.x,
    ::1), link-local (169.254.x.x, fe80::/10), and other
    non-routable ranges — all stdlib, no new dependency.

    ``host`` is the bare hostname or IP string extracted from a URL —
    brackets have already been stripped from IPv6 addresses (e.g.
    ``::1``, not ``[::1]``).

    Args:
        host: Bare hostname or IP string from a parsed ``Origin`` or
            ``Referer`` header value.

    Returns:
        ``True`` when the host resolves to a private/loopback
        address or is the literal string ``"localhost"``.
    """
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_private
    except ValueError:
        return False


def _is_localhost_request() -> bool:
    """Return ``True`` if the request originates from a private network address.

    Checks the ``Origin`` header first (set by most browsers on
    same-origin XHR/fetch), then falls back to ``Referer``.  If
    neither header is present the request is allowed through — curl
    and other CLI tools do not send ``Origin``/``Referer``, so
    blocking headerless requests would break admin scripts and
    testing.

    The loop logic is:

    * Header present **and** regex matches **and** host is trusted
      → return ``True``
    * Header present **and** regex matches **and** host is NOT
      trusted → return ``False``
    * Header present **but** regex does not match (e.g. ``"null"``)
      → continue to next header
    * No usable header found after the loop → return ``True``
      (allow CLI/test clients)

    Returns:
        ``True`` when the request is from a trusted private-network
        origin, or when no ``Origin``/``Referer`` header is present.
    """
    for header in ("Origin", "Referer"):
        value = request.headers.get(header, "").strip()
        if not value:
            continue
        # Parse just the host portion.
        # The IPv6 alternative captures "[::1]" from "http://[::1]:5000".
        match = re.match(r"https?://(\[[^\]]+\]|[^/:]+)", value)
        if not match:
            # Header present but unparseable (e.g. "null") — try next.
            continue
        host = match.group(1).lower()
        # Strip brackets from IPv6 before passing to ipaddress module.
        if host.startswith("[") and host.endswith("]"):
            host = host[1:-1]
        if _is_trusted_host(host):
            return True
        return False  # Non-private origin found — block
    return True  # No Origin/Referer — allow (CLI tools, tests)


def inject_demo_mode(demo_mode: bool) -> dict:
    """Return a Jinja context dict containing the current demo-mode flag.

    This function is **not** directly registered as a context
    processor — ``create_app()`` wraps it in a zero-argument closure
    so it can capture the ``DEMO_MODE`` variable from ``app``'s
    module scope.

    Args:
        demo_mode: Current value of the ``DEMO_MODE`` flag.

    Returns:
        A dict ``{"demo_mode": demo_mode}`` for injection into every
        template context.
    """
    return {"demo_mode": demo_mode}


def csrf_localhost_guard() -> None:
    """Reject state-mutating requests not originating from a private network.

    This tool is designed for local/LAN use only.  Any POST, PUT,
    PATCH, or DELETE request whose ``Origin`` or ``Referer`` header
    resolves to a publicly-routable host is rejected with 403 to
    prevent cross-site request forgery.  Private addresses
    (localhost, RFC 1918, link-local) are allowed.

    Requests with no ``Origin``/``Referer`` (e.g. curl, test clients)
    are allowed through so that automated scripts and the pytest test
    suite are unaffected.

    Returns:
        A Flask ``(response, 403)`` tuple when the request is blocked,
        or ``None`` to allow the request to proceed.
    """
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        if not _is_localhost_request():
            return (
                jsonify(
                    {"error": "Forbidden: requests must originate "
                              "from a private network"}
                ),
                403,
            )
    return None
