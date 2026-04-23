"""app.py — Job Matcher entry point.

Constructs the Flask application via ``web.create_app()`` and runs the
dev server when invoked as a script.  WSGI servers (waitress in Docker,
gunicorn) import ``app`` directly: ``from app import app``.

Routes, helpers, filters, and Flask wiring live under ``web/`` and
``services/``.  See the Architecture section of CLAUDE.md for a full
map of what lives where.
"""

import os

from web import create_app

app = create_app()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Job Matcher web server")
    parser.add_argument(
        "--demo",
        action="store_true",
        help=(
            "Run in demo mode "
            "(env flag only — full demo support not yet wired)."
        ),
    )
    args = parser.parse_args()

    if args.demo:
        os.environ["DEMO_MODE"] = "1"
        print("Demo mode flag set.")

    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    # threaded=True is required for SSE (/ingest/stream); waitress (Docker)
    # is multi-threaded and never executes this code path.
    app.run(debug=debug, port=5000, threaded=True)
