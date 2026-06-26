"""Shared pytest fixtures and configuration.

The ``db_engine`` fixture skips any test that depends on it when no Postgres
service is reachable, so the suite still passes in environments where Docker is
not running.  In CI, a Postgres service container is spun up before the suite.
"""

from __future__ import annotations

import socket

import pytest
from app.core.config import get_settings
from sqlalchemy.engine import Engine


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
