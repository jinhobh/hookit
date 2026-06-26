"""SQLAlchemy engine and session factory.

Import ``engine`` for raw Core usage or use ``get_session`` as a FastAPI
dependency to obtain a scoped ``Session`` that is automatically closed.
"""

from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings

# Module-level engine — created once and reused across the process lifetime.
# pool_pre_ping=True avoids silent stale-connection errors after idle periods.
engine = create_engine(get_settings().database_url, pool_pre_ping=True)

# Session factory bound to the module-level engine.
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


def get_session() -> Generator[Session, None, None]:
    """FastAPI dependency that yields a database session and closes it afterward."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
