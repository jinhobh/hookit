"""Shared pytest fixtures and configuration.

The ``db_engine`` fixture skips any test that depends on it when no Postgres
service is reachable, so the suite still passes in environments where Docker is
not running.  In CI, a Postgres service container is spun up before the suite.

``sc_session`` / ``isolated_showcase`` are the two showcase test tiers shared
by ``test_showcase.py`` and ``test_showcase_ledger.py``: a savepoint-isolated
session for service-level tests, and a disposable, uniquely named showcase
project (with real commits + cascade cleanup) for route-level tests.
"""

from __future__ import annotations

import socket
import uuid
from collections.abc import Generator

import pytest
from app.core.config import get_settings
from app.db.base import Base
from app.models.project import Project
from sqlalchemy import delete
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session


def _postgres_is_reachable() -> bool:
    """Return True if the configured Postgres host:port accepts a TCP connection."""
    from urllib.parse import urlparse

    parsed = urlparse(get_settings().database_url)
    host = parsed.hostname or "localhost"
    port = parsed.port or 5432
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(2)
    try:
        return sock.connect_ex((host, port)) == 0
    finally:
        sock.close()


@pytest.fixture(scope="session")
def db_engine() -> Engine:
    """Session-scoped SQLAlchemy engine; skips if Postgres is unreachable."""
    if not _postgres_is_reachable():
        pytest.skip("Postgres not reachable — start 'docker compose up -d postgres' first")
    from app.db.session import engine

    return engine


@pytest.fixture()
def sc_session(db_engine: Engine) -> Generator[Session, None, None]:
    """Savepoint-isolated session; rolls back after every test."""
    Base.metadata.create_all(db_engine)
    connection = db_engine.connect()
    outer_tx = connection.begin()
    session = Session(connection, join_transaction_mode="create_savepoint")
    yield session
    session.close()
    outer_tx.rollback()
    connection.close()


@pytest.fixture()
def isolated_showcase(
    db_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> Generator[str, None, None]:
    """Point the app at a unique, disposable showcase project for one test.

    Overrides the showcase name via env (cache-cleared so the routes pick it up),
    yields that name, and deletes the project — with everything ON DELETE CASCADE
    takes — at teardown.
    """
    Base.metadata.create_all(db_engine)
    name = f"__showcase_it_{uuid.uuid4().hex[:10]}__"
    monkeypatch.setenv("SHOWCASE_PROJECT_NAME", name)
    monkeypatch.setenv("SHOWCASE_DISCORD_WEBHOOK_URL", "")
    monkeypatch.setenv("SHOWCASE_API_KEY", "")
    get_settings.cache_clear()
    yield name
    get_settings.cache_clear()
    with Session(db_engine) as session:
        session.execute(delete(Project).where(Project.name == name))
        session.commit()
    get_settings.cache_clear()
