"""Integration tests for POST /projects and POST /projects/{id}/api-keys.

Tests require a live Postgres instance (skipped automatically when Postgres
is unreachable). Uses savepoint-based rollback for test isolation.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Generator

import pytest
from app.db.base import Base
from app.db.session import get_session
from app.main import app
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session


@pytest.fixture()
def projects_db_session(db_engine: Engine) -> Generator[Session, None, None]:
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
def client(projects_db_session: Session) -> Generator[TestClient, None, None]:
    """TestClient with get_session overridden to use the transactional session."""

    def override_get_session() -> Generator[Session, None, None]:
        yield projects_db_session

    app.dependency_overrides[get_session] = override_get_session
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c
    app.dependency_overrides.pop(get_session, None)


# ---------------------------------------------------------------------------
# POST /projects
# ---------------------------------------------------------------------------


def test_create_project_returns_201(client: TestClient) -> None:
    resp = client.post("/projects", json={"name": "acme"})
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "acme"
    assert "id" in data
    assert "created_at" in data
    # Response must not include updated_at or anything beyond spec
    assert uuid.UUID(data["id"])


def test_create_project_response_shape(client: TestClient) -> None:
    resp = client.post("/projects", json={"name": "shape-test"})
    assert resp.status_code == 201
    data = resp.json()
    assert set(data.keys()) == {"id", "name", "created_at"}


def test_create_project_missing_name_returns_422(client: TestClient) -> None:
    resp = client.post("/projects", json={})
    assert resp.status_code == 422


def test_create_project_empty_name_returns_422(client: TestClient) -> None:
    resp = client.post("/projects", json={"name": ""})
    assert resp.status_code == 422


def test_create_project_no_auth_required(client: TestClient) -> None:
    """Endpoint must succeed without Authorization header."""
    resp = client.post("/projects", json={"name": "no-auth-project"})
    assert resp.status_code == 201


# ---------------------------------------------------------------------------
# POST /projects/{project_id}/api-keys
# ---------------------------------------------------------------------------


def test_create_api_key_returns_201(client: TestClient) -> None:
    project_resp = client.post("/projects", json={"name": "key-owner"})
    project_id = project_resp.json()["id"]

    resp = client.post(f"/projects/{project_id}/api-keys", json={"name": "default"})
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "default"
    assert data["key"].startswith("whk_")
    assert "prefix" in data
    assert "id" in data
    assert "created_at" in data


def test_create_api_key_response_shape(client: TestClient) -> None:
    project_resp = client.post("/projects", json={"name": "shape-key-owner"})
    project_id = project_resp.json()["id"]

    resp = client.post(f"/projects/{project_id}/api-keys", json={"name": "default"})
    assert resp.status_code == 201
    data = resp.json()
    assert set(data.keys()) == {"id", "key", "prefix", "name", "created_at"}


def test_create_api_key_plaintext_hashes_correctly(
    client: TestClient, projects_db_session: Session
) -> None:
    """The plaintext key returned must hash to the stored key_hash."""
    from app.models.api_key import ApiKey
    from sqlalchemy import select

    project_resp = client.post("/projects", json={"name": "hash-check-project"})
    project_id = project_resp.json()["id"]

    resp = client.post(f"/projects/{project_id}/api-keys", json={"name": "hash-key"})
    assert resp.status_code == 201
    plaintext = resp.json()["key"]

    api_key = projects_db_session.execute(
        select(ApiKey).where(ApiKey.name == "hash-key")
    ).scalar_one()
    expected_hash = hashlib.sha256(plaintext.encode()).hexdigest()
    assert api_key.key_hash == expected_hash


def test_create_api_key_authenticates_via_me(client: TestClient) -> None:
    """The returned plaintext key must work against GET /me."""
    project_resp = client.post("/projects", json={"name": "auth-check-project"})
    project_id = project_resp.json()["id"]

    key_resp = client.post(f"/projects/{project_id}/api-keys", json={"name": "auth-key"})
    plaintext = key_resp.json()["key"]

    me_resp = client.get("/me", headers={"Authorization": f"Bearer {plaintext}"})
    assert me_resp.status_code == 200
    assert me_resp.json()["project_id"] == project_id


def test_create_api_key_nonexistent_project_returns_404(client: TestClient) -> None:
    fake_id = str(uuid.uuid4())
    resp = client.post(f"/projects/{fake_id}/api-keys", json={"name": "orphan"})
    assert resp.status_code == 404


def test_create_api_key_no_auth_required(client: TestClient) -> None:
    """Endpoint must succeed without Authorization header."""
    project_resp = client.post("/projects", json={"name": "no-auth-key-project"})
    project_id = project_resp.json()["id"]

    resp = client.post(f"/projects/{project_id}/api-keys", json={"name": "open-key"})
    assert resp.status_code == 201


def test_create_multiple_keys_for_same_project(client: TestClient) -> None:
    project_resp = client.post("/projects", json={"name": "multi-key-project"})
    project_id = project_resp.json()["id"]

    resp1 = client.post(f"/projects/{project_id}/api-keys", json={"name": "key-one"})
    resp2 = client.post(f"/projects/{project_id}/api-keys", json={"name": "key-two"})
    assert resp1.status_code == 201
    assert resp2.status_code == 201
    # Each key must be distinct
    assert resp1.json()["key"] != resp2.json()["key"]
    assert resp1.json()["id"] != resp2.json()["id"]
