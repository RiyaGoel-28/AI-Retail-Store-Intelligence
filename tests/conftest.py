"""
Shared pytest fixtures for the Store Intelligence test suite.

Provides automatic database cleanup between tests to prevent
SQLite "database is locked" errors on Windows.

PROMPT: "Create a pytest conftest.py fixture that automatically closes
any orphaned SQLite connections between tests to prevent 'database is
locked' errors on Windows when running the full test suite."

CHANGES MADE: Added gc.collect() both before and after each test to
ensure garbage collection runs. Added scanning of gc.get_objects() to
find and force-close any leaked sqlite3.Connection instances. Wrapped
close() in try/except to handle already-closed connections gracefully.
"""

import pytest
import sqlite3
import os
import gc


@pytest.fixture(autouse=True)
def cleanup_db():
    """Auto cleanup to close any lingering DB connections between tests."""
    gc.collect()
    yield
    gc.collect()
    # Close any open sqlite connections
    for obj in gc.get_objects():
        if isinstance(obj, sqlite3.Connection):
            try:
                obj.close()
            except Exception:
                pass
