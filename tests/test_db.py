"""Tests for the database engine and session factory.

These tests require a live Postgres service.  They are automatically skipped
when Postgres is unreachable (see conftest.py ``db_engine`` fixture).
In CI a Postgres service container is started before this suite runs.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker


def test_engine_select_one(db_engine: Engine) -> None:
    """Engine can open a connection and execute SELECT 1."""
    with db_engine.connect() as conn:
        result = conn.execute(text("SELECT 1"))
        row = result.fetchone()
    assert row is not None
    assert row[0] == 1


def test_session_local_select_one(db_engine: Engine) -> None:
    """SessionLocal produces a usable Session that can execute a query."""
    session_factory = sessionmaker(bind=db_engine, autocommit=False, autoflush=False)
    session = session_factory()
    try:
        result = session.execute(text("SELECT 1"))
        row = result.fetchone()
    finally:
        session.close()
    assert row is not None
    assert row[0] == 1
