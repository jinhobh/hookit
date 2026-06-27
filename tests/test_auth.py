"""Tests for API key authentication dependency and the /me probe endpoint.

Integration tests require a live Postgres instance (skipped automatically
when Postgres is unreachable).  The session fixture uses
``join_transaction_mode="create_savepoint"`` so that the auth dependency's
``session.commit()`` releases a savepoint rather than committing the outer
transaction — enabling full rollback-based test isolation.
"""

from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, datetime

import pytest
from app.db.base import Base
from app.db.session import get_session
from app.main import app
from app.models.api_key import ApiKey, generate_api_key
from app.models.project import Project
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session


@pytest.fixture()
def auth_db_session(db_engine: Engine) -> Generator[Session, None, None]:
    """Transactional session using savepoints so commit() inside tests is safe."""
    Base.metadata.create_all(db_engine)
    connection = db_engine.connect()
    outer_tx = connection.begin()
    session = Session(connection, join_transaction_mode="create_savepoint")
    yield session
    session.close()
    outer_tx.rollback()
    connection.close()


@pytest.fixture()
def client(auth_db_session: Session) -> Generator[TestClient, None, None]:
    """TestClient with get_session overridden to use the transactional session."""

    def override_get_session() -> Generator[Session, None, None]:
        yield auth_db_session

    app.dependency_overrides[get_session] = override_get_session
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c
    app.dependency_overrides.pop(get_session, None)


@pytest.fixture()
def project_and_plaintext(auth_db_session: Session) -> tuple[Project, str]:
    """Insert a project + active API key; return (project, plaintext_key)."""
    project = Project(name="test-project-auth")
    auth_db_session.add(project)
    auth_db_session.flush()

    plaintext, prefix, key_hash = generate_api_key()
    api_key = ApiKey(
        project_id=project.id,
        name="test-key",
        key_prefix=prefix,
        key_hash=key_hash,
    )
    auth_db_session.add(api_key)
    auth_db_session.flush()
    return project, plaintext


@pytest.fixture()
def revoked_key_plaintext(auth_db_session: Session) -> tuple[Project, str]:
    """Insert a project + revoked API key; return (project, plaintext_key)."""
    project = Project(name="test-project-revoked")
    auth_db_session.add(project)
    auth_db_session.flush()

    plaintext, prefix, key_hash = generate_api_key()
    api_key = ApiKey(
        project_id=project.id,
        name="revoked-key",
        key_prefix=prefix,
        key_hash=key_hash,
        revoked_at=datetime.now(UTC),
    )
    auth_db_session.add(api_key)
    auth_db_session.flush()
    return project, plaintext


# ---------------------------------------------------------------------------
# /me endpoint tests
# ---------------------------------------------------------------------------


def test_me_valid_key_returns_project_id(
    client: TestClient, project_and_plaintext: tuple[Project, str]
) -> None:
    project, plaintext = project_and_plaintext
    resp = client.get("/me", headers={"Authorization": f"Bearer {plaintext}"})
    assert resp.status_code == 200
    assert resp.json() == {"project_id": str(project.id)}


def test_me_missing_auth_header_returns_401(client: TestClient) -> None:
    resp = client.get("/me")
    assert resp.status_code == 401


def test_me_malformed_scheme_returns_401(client: TestClient) -> None:
    # "Token" scheme instead of "Bearer"
    resp = client.get("/me", headers={"Authorization": "Token notabearer"})
    assert resp.status_code == 401


def test_me_unknown_key_returns_401(client: TestClient) -> None:
    _, unknown_key, _ = generate_api_key()
    resp = client.get("/me", headers={"Authorization": f"Bearer {unknown_key}"})
    assert resp.status_code == 401


def test_me_revoked_key_returns_401(
    client: TestClient, revoked_key_plaintext: tuple[Project, str]
) -> None:
    _, plaintext = revoked_key_plaintext
    resp = client.get("/me", headers={"Authorization": f"Bearer {plaintext}"})
    assert resp.status_code == 401


def test_me_updates_last_used_at(
    client: TestClient,
    project_and_plaintext: tuple[Project, str],
    auth_db_session: Session,
) -> None:
    """Successful auth must stamp last_used_at on the key row."""
    from sqlalchemy import select

    project, plaintext = project_and_plaintext
    # last_used_at is NULL before first use
    api_key_before = auth_db_session.execute(
        select(ApiKey).where(ApiKey.name == "test-key")
    ).scalar_one()
    assert api_key_before.last_used_at is None

    resp = client.get("/me", headers={"Authorization": f"Bearer {plaintext}"})
    assert resp.status_code == 200

    # Expire the cached object so the next access reloads from DB
    auth_db_session.expire(api_key_before)
    assert api_key_before.last_used_at is not None


def test_me_401_response_includes_www_authenticate_header(
    client: TestClient,
) -> None:
    resp = client.get("/me")
    assert "www-authenticate" in {k.lower() for k in resp.headers}
