"""Integration tests for project and API key provisioning endpoints.

Tests require a live Postgres instance (skipped automatically when Postgres
is unreachable). Uses savepoint-based rollback for test isolation.
"""

from __future__ import annotations

import hashlib
import time
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
# GET /projects/{project_id}
# ---------------------------------------------------------------------------


def test_get_project_returns_200(client: TestClient) -> None:
    create_resp = client.post("/projects", json={"name": "detail-project"})
    assert create_resp.status_code == 201
    project_id = create_resp.json()["id"]

    resp = client.get(f"/projects/{project_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == project_id
    assert data["name"] == "detail-project"
    assert "created_at" in data


def test_get_project_response_shape(client: TestClient) -> None:
    create_resp = client.post("/projects", json={"name": "detail-shape"})
    project_id = create_resp.json()["id"]

    resp = client.get(f"/projects/{project_id}")
    assert resp.status_code == 200
    assert set(resp.json().keys()) == {"id", "name", "created_at"}


def test_get_project_nonexistent_returns_404(client: TestClient) -> None:
    fake_id = str(uuid.uuid4())
    resp = client.get(f"/projects/{fake_id}")
    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"].lower()


def test_get_project_no_auth_required(client: TestClient) -> None:
    create_resp = client.post("/projects", json={"name": "detail-no-auth"})
    project_id = create_resp.json()["id"]
    resp = client.get(f"/projects/{project_id}")
    assert resp.status_code == 200


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


# ---------------------------------------------------------------------------
# DELETE /projects/{project_id}/api-keys/{key_id}
# ---------------------------------------------------------------------------


def _mint_key(
    client: TestClient, project_name: str, key_name: str = "test-key"
) -> tuple[str, str, str]:
    """Helper: create a project + API key, return (project_id, key_id, plaintext)."""
    project_resp = client.post("/projects", json={"name": project_name})
    assert project_resp.status_code == 201
    project_id = project_resp.json()["id"]

    key_resp = client.post(f"/projects/{project_id}/api-keys", json={"name": key_name})
    assert key_resp.status_code == 201
    key_data = key_resp.json()
    return project_id, key_data["id"], key_data["key"]


def test_revoke_api_key_returns_204(client: TestClient) -> None:
    project_id, key_id, _ = _mint_key(client, "revoke-happy")
    resp = client.delete(f"/projects/{project_id}/api-keys/{key_id}")
    assert resp.status_code == 204
    assert resp.content == b""


def test_revoke_api_key_idempotent(client: TestClient) -> None:
    """Revoking an already-revoked key must return 204, not an error."""
    project_id, key_id, _ = _mint_key(client, "revoke-idempotent")
    resp1 = client.delete(f"/projects/{project_id}/api-keys/{key_id}")
    assert resp1.status_code == 204
    resp2 = client.delete(f"/projects/{project_id}/api-keys/{key_id}")
    assert resp2.status_code == 204


def test_revoke_nonexistent_key_returns_404(client: TestClient) -> None:
    project_resp = client.post("/projects", json={"name": "revoke-missing"})
    project_id = project_resp.json()["id"]
    fake_key_id = str(uuid.uuid4())
    resp = client.delete(f"/projects/{project_id}/api-keys/{fake_key_id}")
    assert resp.status_code == 404


def test_revoke_key_wrong_project_returns_404(client: TestClient) -> None:
    """Key from a different project must return 404 (no cross-project info leakage)."""
    _, key_id, _ = _mint_key(client, "revoke-owner-project")
    other_project_resp = client.post("/projects", json={"name": "revoke-other-project"})
    other_project_id = other_project_resp.json()["id"]

    resp = client.delete(f"/projects/{other_project_id}/api-keys/{key_id}")
    assert resp.status_code == 404


def test_revoke_key_prevents_auth(client: TestClient) -> None:
    """After revocation, bearer requests with the revoked key must return 401."""
    project_id, key_id, plaintext = _mint_key(client, "revoke-auth-check")

    # Key works before revocation
    me_resp = client.get("/me", headers={"Authorization": f"Bearer {plaintext}"})
    assert me_resp.status_code == 200

    # Revoke
    revoke_resp = client.delete(f"/projects/{project_id}/api-keys/{key_id}")
    assert revoke_resp.status_code == 204

    # Key must now be rejected
    me_resp_after = client.get("/me", headers={"Authorization": f"Bearer {plaintext}"})
    assert me_resp_after.status_code == 401


def test_revoke_api_key_no_auth_required(client: TestClient) -> None:
    """Revocation endpoint must succeed without Authorization header."""
    project_id, key_id, _ = _mint_key(client, "revoke-no-auth")
    resp = client.delete(f"/projects/{project_id}/api-keys/{key_id}")
    assert resp.status_code == 204


# ---------------------------------------------------------------------------
# GET /projects/{project_id}/api-keys
# ---------------------------------------------------------------------------


def test_list_api_keys_multiple(client: TestClient) -> None:
    """Project with multiple keys returns envelope with items, never key_hash."""
    project_resp = client.post("/projects", json={"name": "list-multi-project"})
    project_id = project_resp.json()["id"]
    client.post(f"/projects/{project_id}/api-keys", json={"name": "key-alpha"})
    client.post(f"/projects/{project_id}/api-keys", json={"name": "key-beta"})

    resp = client.get(f"/projects/{project_id}/api-keys")
    assert resp.status_code == 200
    envelope = resp.json()
    assert set(envelope.keys()) == {"items", "next_cursor"}
    items = envelope["items"]
    assert len(items) == 2
    names = {item["name"] for item in items}
    assert names == {"key-alpha", "key-beta"}
    for item in items:
        assert set(item.keys()) == {
            "id",
            "prefix",
            "name",
            "created_at",
            "last_used_at",
            "revoked_at",
        }
        assert "key_hash" not in item
        assert "key" not in item


def test_list_api_keys_empty_project(client: TestClient) -> None:
    """Project with no keys returns 200 with an empty items list."""
    project_resp = client.post("/projects", json={"name": "list-empty-project"})
    project_id = project_resp.json()["id"]

    resp = client.get(f"/projects/{project_id}/api-keys")
    assert resp.status_code == 200
    envelope = resp.json()
    assert envelope["items"] == []
    assert envelope["next_cursor"] is None


def test_list_api_keys_nonexistent_project_returns_404(client: TestClient) -> None:
    """Non-existent project_id must return 404."""
    fake_id = str(uuid.uuid4())
    resp = client.get(f"/projects/{fake_id}/api-keys")
    assert resp.status_code == 404


def test_list_api_keys_ordered_by_created_at_desc(client: TestClient) -> None:
    """Keys are returned newest-first (descending created_at) to match all other paginated lists."""
    project_resp = client.post("/projects", json={"name": "list-order-project"})
    project_id = project_resp.json()["id"]
    resp1 = client.post(f"/projects/{project_id}/api-keys", json={"name": "first"})
    time.sleep(0.002)
    resp2 = client.post(f"/projects/{project_id}/api-keys", json={"name": "second"})
    time.sleep(0.002)
    resp3 = client.post(f"/projects/{project_id}/api-keys", json={"name": "third"})

    resp = client.get(f"/projects/{project_id}/api-keys")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 3
    ids = [item["id"] for item in items]
    # Newest first
    assert ids == [resp3.json()["id"], resp2.json()["id"], resp1.json()["id"]]


def test_list_api_keys_revoked_key_shows_revoked_at(client: TestClient) -> None:
    """A revoked key's revoked_at field is populated; active keys have null."""
    project_id, key_id, _ = _mint_key(client, "list-revoke-check")
    client.delete(f"/projects/{project_id}/api-keys/{key_id}")

    resp = client.get(f"/projects/{project_id}/api-keys")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["revoked_at"] is not None


def test_list_api_keys_active_key_has_null_revoked_at(client: TestClient) -> None:
    """Active keys return null for revoked_at."""
    project_id, _key_id, _ = _mint_key(client, "list-active-check")

    resp = client.get(f"/projects/{project_id}/api-keys")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["revoked_at"] is None


def test_list_api_keys_single_page_no_cursor(client: TestClient) -> None:
    """Single page of results returns items with next_cursor=None."""
    project_resp = client.post("/projects", json={"name": "single-page-project"})
    project_id = project_resp.json()["id"]
    client.post(f"/projects/{project_id}/api-keys", json={"name": "only-key"})

    resp = client.get(f"/projects/{project_id}/api-keys", params={"limit": 20})
    assert resp.status_code == 200
    envelope = resp.json()
    assert len(envelope["items"]) == 1
    assert envelope["next_cursor"] is None


def test_list_api_keys_pagination_across_pages(client: TestClient) -> None:
    """next_cursor is set when results exceed limit; second request returns remaining items."""
    project_resp = client.post("/projects", json={"name": "paginate-project"})
    project_id = project_resp.json()["id"]
    for i in range(3):
        client.post(f"/projects/{project_id}/api-keys", json={"name": f"key-{i}"})
        time.sleep(0.002)

    # First page: limit=2 should return 2 items and a cursor
    resp1 = client.get(f"/projects/{project_id}/api-keys", params={"limit": 2})
    assert resp1.status_code == 200
    page1 = resp1.json()
    assert len(page1["items"]) == 2
    assert page1["next_cursor"] is not None

    # Second page: use cursor, should return 1 remaining item with no next cursor
    resp2 = client.get(
        f"/projects/{project_id}/api-keys",
        params={"limit": 2, "cursor": page1["next_cursor"]},
    )
    assert resp2.status_code == 200
    page2 = resp2.json()
    assert len(page2["items"]) == 1
    assert page2["next_cursor"] is None

    # All IDs across pages are unique and no overlap
    ids1 = {item["id"] for item in page1["items"]}
    ids2 = {item["id"] for item in page2["items"]}
    assert ids1.isdisjoint(ids2)


def test_list_api_keys_invalid_cursor_returns_422(client: TestClient) -> None:
    """An invalid cursor value must return 422."""
    project_resp = client.post("/projects", json={"name": "cursor-422-project"})
    project_id = project_resp.json()["id"]

    resp = client.get(
        f"/projects/{project_id}/api-keys",
        params={"cursor": "not-a-valid-cursor"},
    )
    assert resp.status_code == 422


def test_list_api_keys_key_hash_never_in_response(client: TestClient) -> None:
    """key_hash must never appear anywhere in the paginated response."""
    project_resp = client.post("/projects", json={"name": "hash-guard-project"})
    project_id = project_resp.json()["id"]
    client.post(f"/projects/{project_id}/api-keys", json={"name": "safe-key"})

    resp = client.get(f"/projects/{project_id}/api-keys")
    assert resp.status_code == 200
    import json

    raw = json.dumps(resp.json())
    assert "key_hash" not in raw
    assert "hash" not in raw


# ---------------------------------------------------------------------------
# GET /projects
# ---------------------------------------------------------------------------


def test_list_projects_empty_returns_200(client: TestClient) -> None:
    """Empty list returns 200 with empty items and null next_cursor."""
    resp = client.get("/projects")
    assert resp.status_code == 200
    data = resp.json()
    assert data["items"] == []
    assert data["next_cursor"] is None


def test_list_projects_returns_created_project(client: TestClient) -> None:
    """Single project appears in the list with the correct shape."""
    client.post("/projects", json={"name": "list-me"})
    resp = client.get("/projects")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["items"]) == 1
    item = data["items"][0]
    assert item["name"] == "list-me"
    assert set(item.keys()) == {"id", "name", "created_at"}


def test_list_projects_no_auth_required(client: TestClient) -> None:
    resp = client.get("/projects")
    assert resp.status_code == 200


def test_list_projects_limit_zero_returns_422(client: TestClient) -> None:
    resp = client.get("/projects", params={"limit": 0})
    assert resp.status_code == 422


def test_list_projects_limit_over_max_returns_422(client: TestClient) -> None:
    resp = client.get("/projects", params={"limit": 101})
    assert resp.status_code == 422


def test_list_projects_single_page_no_cursor(client: TestClient) -> None:
    """When results fit in one page, next_cursor is None."""
    for i in range(3):
        client.post("/projects", json={"name": f"proj-{i}"})

    resp = client.get("/projects", params={"limit": 20})
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["items"]) == 3
    assert data["next_cursor"] is None


def test_list_projects_pagination_across_pages(client: TestClient) -> None:
    """next_cursor pages through all projects without overlap."""
    for i in range(3):
        client.post("/projects", json={"name": f"page-proj-{i}"})
        time.sleep(0.002)

    resp1 = client.get("/projects", params={"limit": 2})
    assert resp1.status_code == 200
    page1 = resp1.json()
    assert len(page1["items"]) == 2
    assert page1["next_cursor"] is not None

    resp2 = client.get("/projects", params={"limit": 2, "cursor": page1["next_cursor"]})
    assert resp2.status_code == 200
    page2 = resp2.json()
    assert len(page2["items"]) == 1
    assert page2["next_cursor"] is None

    ids1 = {item["id"] for item in page1["items"]}
    ids2 = {item["id"] for item in page2["items"]}
    assert ids1.isdisjoint(ids2)


def test_list_projects_ordered_newest_first(client: TestClient) -> None:
    """Results are ordered created_at DESC."""
    for i in range(3):
        client.post("/projects", json={"name": f"order-proj-{i}"})
        time.sleep(0.002)

    resp = client.get("/projects")
    assert resp.status_code == 200
    items = resp.json()["items"]
    names = [item["name"] for item in items]
    assert names == ["order-proj-2", "order-proj-1", "order-proj-0"]


def test_list_projects_invalid_cursor_returns_422(client: TestClient) -> None:
    resp = client.get("/projects", params={"cursor": "not-a-valid-cursor"})
    assert resp.status_code == 422
