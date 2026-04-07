"""
conftest.py — Pytest fixtures and hooks for test suite.

Mocks database initialization to allow testing without a live PostgreSQL instance.
"""

import os
import sys
from unittest.mock import MagicMock

# Set DATABASE_URL before any module imports
if not os.environ.get("DATABASE_URL"):
    os.environ["DATABASE_URL"] = "postgresql://test:test@localhost:5432/test"

# Pre-create a mock db module so when app.py imports it, it gets our mock
db_mock = MagicMock()
db_mock.init_db = MagicMock()  # Mock the init_db call
sys.modules["db"] = db_mock
