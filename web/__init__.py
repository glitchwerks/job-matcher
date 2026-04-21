"""Web layer — Flask app factory and blueprint registration.

Phase 0 scaffold: create_app() re-exports the existing monolithic app
from app.py. Subsequent refactor phases will migrate code here.
"""


def create_app():
    """Return the Flask application instance.

    Phase 0 stub: currently re-exports the app from the monolithic
    app.py module. This indirection exists so later phases can invert
    the import direction (build the app here; re-export from app.py)
    without touching callers.
    """
    from app import app
    return app
