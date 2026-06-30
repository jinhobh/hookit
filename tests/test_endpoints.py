"""Tests for the /endpoints CRUD API.

Integration tests require a live Postgres instance (skipped automatically
when Postgres is unreachable).  Each test runs inside a savepoint-based
transaction that is rolled back on teardown, providing full isolation.
"""

from __future__ import annotations

from collections.abc import Generator
from typing import Any

import pytest
from app.db.base import Base
from app.db.session import get_session
from app.main import app
from app.models.api_key import ApiKey, generate_api_key
from app.models.project import Project
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def ep_db_session(db_engine: Engine) -> Generator[Session, None, None]:
    """Transactional session with savepoints for rollback-based isolation."""
    Base.metadata.create_all(db_engine)
    connection = db_engine.connect()
    outer_tx = connection.begin()
    session = Session(connection, join_transaction_mode="create_savepoint")
    yield session
    session.close()
    outer_tx.rollback()
    connection.close()


def _make_project_and_key(session: Session, name: str) -> tuple[Project, str]:
    """Insert a project + active API key; return (project, plaintext_key)."""
    project = Project(name=name)
    session.add(project)
    session.flush()
    plaintext, prefix, key_hash = generate_api_key()
    api_key = ApiKey(
        project_id=project.id,
        name="test-key",
        key_prefix=prefix,
        key_hash=key_hash,
    )
    session.add(api_key)
    session.flush()
    return project, plaintext


@pytest.fixture()
def client_a(ep_db_session: Session) -> Generator[TestClient, None, None]:
    """TestClient authenticated as project A."""

    def override() -> Generator[Session, None, None]:
        yield ep_db_session

    app.dependency_overrides[get_session] = override
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c
    app.dependency_overrides.pop(get_session, None)


@pytest.fixture()
def project_a_key(ep_db_session: Session) -> str:
    _, plaintext = _make_project_and_key(ep_db_session, "project-a-ep")
    return plaintext


@pytest.fixture()
def project_b_key(ep_db_session: Session) -> str:
    _, plaintext = _make_project_and_key(ep_db_session, "project-b-ep")
    return plaintext


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

_VALID_URL = "https://example.com/webhook"
_VALID_TYPES = ["invoice.created", "payment.failed"]


def _auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


# ---------------------------------------------------------------------------
# POST /endpoints
# ---------------------------------------------------------------------------


def test_create_endpoint_returns_201_with_secret(client_a: TestClient, project_a_key: str) -> None:
    resp = client_a.post(
        "/endpoints",
        json={"url": _VALID_URL, "event_types": _VALID_TYPES},
        headers=_auth(project_a_key),
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["url"] == _VALID_URL
    assert data["event_types"] == _VALID_TYPES
    assert data["status"] == "active"
    assert "secret" in data
    assert len(data["secret"]) > 0
    assert "id" in data


def test_create_endpoint_secret_not_in_subsequent_get(
    client_a: TestClient, project_a_key: str
) -> None:
    resp = client_a.post(
        "/endpoints",
        json={"url": _VALID_URL, "event_types": _VALID_TYPES},
        headers=_auth(project_a_key),
    )
    assert resp.status_code == 201

    resp2 = client_a.get("/endpoints", headers=_auth(project_a_key))
    assert resp2.status_code == 200
    for ep in resp2.json():
        assert "secret" not in ep


def test_create_endpoint_defaults_to_active(client_a: TestClient, project_a_key: str) -> None:
    resp = client_a.post(
        "/endpoints",
        json={"url": _VALID_URL, "event_types": ["order.placed"]},
        headers=_auth(project_a_key),
    )
    assert resp.status_code == 201
    assert resp.json()["status"] == "active"


def test_create_endpoint_with_inactive_status(client_a: TestClient, project_a_key: str) -> None:
    resp = client_a.post(
        "/endpoints",
        json={"url": _VALID_URL, "event_types": ["order.placed"], "status": "inactive"},
        headers=_auth(project_a_key),
    )
    assert resp.status_code == 201
    assert resp.json()["status"] == "inactive"


def test_create_endpoint_rejects_invalid_url(client_a: TestClient, project_a_key: str) -> None:
    resp = client_a.post(
        "/endpoints",
        json={"url": "not-a-url", "event_types": _VALID_TYPES},
        headers=_auth(project_a_key),
    )
    assert resp.status_code == 422


def test_create_endpoint_rejects_empty_event_types(
    client_a: TestClient, project_a_key: str
) -> None:
    resp = client_a.post(
        "/endpoints",
        json={"url": _VALID_URL, "event_types": []},
        headers=_auth(project_a_key),
    )
    assert resp.status_code == 422


def test_create_endpoint_rejects_blank_event_type_string(
    client_a: TestClient, project_a_key: str
) -> None:
    resp = client_a.post(
        "/endpoints",
        json={"url": _VALID_URL, "event_types": ["  "]},
        headers=_auth(project_a_key),
    )
    assert resp.status_code == 422


def test_create_endpoint_rejects_ssrf_url(client_a: TestClient, project_a_key: str) -> None:
    resp = client_a.post(
        "/endpoints",
        json={"url": "http://127.0.0.1:9999/hook", "event_types": _VALID_TYPES},
        headers=_auth(project_a_key),
    )
    assert resp.status_code == 422
    assert "non-public address" in resp.json()["detail"]


def test_create_endpoint_requires_auth(client_a: TestClient) -> None:
    resp = client_a.post(
        "/endpoints",
        json={"url": _VALID_URL, "event_types": _VALID_TYPES},
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# GET /endpoints
# ---------------------------------------------------------------------------


def test_list_endpoints_empty_initially(client_a: TestClient, project_a_key: str) -> None:
    resp = client_a.get("/endpoints", headers=_auth(project_a_key))
    assert resp.status_code == 200
    assert resp.json() == []


def test_list_endpoints_returns_created(client_a: TestClient, project_a_key: str) -> None:
    client_a.post(
        "/endpoints",
        json={"url": _VALID_URL, "event_types": _VALID_TYPES},
        headers=_auth(project_a_key),
    )
    resp = client_a.get("/endpoints", headers=_auth(project_a_key))
    assert resp.status_code == 200
    assert len(resp.json()) == 1


def test_list_endpoints_cross_project_isolation(
    client_a: TestClient, project_a_key: str, project_b_key: str
) -> None:
    """Project B cannot see Project A's endpoints."""
    client_a.post(
        "/endpoints",
        json={"url": _VALID_URL, "event_types": _VALID_TYPES},
        headers=_auth(project_a_key),
    )
    resp = client_a.get("/endpoints", headers=_auth(project_b_key))
    assert resp.status_code == 200
    assert resp.json() == []


# ---------------------------------------------------------------------------
# PATCH /endpoints/{id}
# ---------------------------------------------------------------------------


def test_patch_endpoint_updates_url(client_a: TestClient, project_a_key: str) -> None:
    create = client_a.post(
        "/endpoints",
        json={"url": _VALID_URL, "event_types": _VALID_TYPES},
        headers=_auth(project_a_key),
    )
    ep_id = create.json()["id"]
    resp = client_a.patch(
        f"/endpoints/{ep_id}",
        json={"url": "https://new.example.com/hook"},
        headers=_auth(project_a_key),
    )
    assert resp.status_code == 200
    assert resp.json()["url"] == "https://new.example.com/hook"


def test_patch_endpoint_updates_event_types(client_a: TestClient, project_a_key: str) -> None:
    create = client_a.post(
        "/endpoints",
        json={"url": _VALID_URL, "event_types": _VALID_TYPES},
        headers=_auth(project_a_key),
    )
    ep_id = create.json()["id"]
    resp = client_a.patch(
        f"/endpoints/{ep_id}",
        json={"event_types": ["order.shipped"]},
        headers=_auth(project_a_key),
    )
    assert resp.status_code == 200
    assert resp.json()["event_types"] == ["order.shipped"]


def test_patch_endpoint_updates_status(client_a: TestClient, project_a_key: str) -> None:
    create = client_a.post(
        "/endpoints",
        json={"url": _VALID_URL, "event_types": _VALID_TYPES},
        headers=_auth(project_a_key),
    )
    ep_id = create.json()["id"]
    resp = client_a.patch(
        f"/endpoints/{ep_id}",
        json={"status": "inactive"},
        headers=_auth(project_a_key),
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "inactive"


def test_patch_endpoint_404_for_wrong_project(
    client_a: TestClient, project_a_key: str, project_b_key: str
) -> None:
    """Project B cannot patch Project A's endpoint."""
    create = client_a.post(
        "/endpoints",
        json={"url": _VALID_URL, "event_types": _VALID_TYPES},
        headers=_auth(project_a_key),
    )
    ep_id = create.json()["id"]
    resp = client_a.patch(
        f"/endpoints/{ep_id}",
        json={"status": "inactive"},
        headers=_auth(project_b_key),
    )
    assert resp.status_code == 404


def test_patch_endpoint_rejects_empty_event_types(client_a: TestClient, project_a_key: str) -> None:
    create = client_a.post(
        "/endpoints",
        json={"url": _VALID_URL, "event_types": _VALID_TYPES},
        headers=_auth(project_a_key),
    )
    ep_id = create.json()["id"]
    resp = client_a.patch(
        f"/endpoints/{ep_id}",
        json={"event_types": []},
        headers=_auth(project_a_key),
    )
    assert resp.status_code == 422


def test_patch_endpoint_rejects_ssrf_url(client_a: TestClient, project_a_key: str) -> None:
    create = client_a.post(
        "/endpoints",
        json={"url": _VALID_URL, "event_types": _VALID_TYPES},
        headers=_auth(project_a_key),
    )
    ep_id = create.json()["id"]
    resp = client_a.patch(
        f"/endpoints/{ep_id}",
        json={"url": "http://10.0.0.1/hook"},
        headers=_auth(project_a_key),
    )
    assert resp.status_code == 422
    assert "non-public address" in resp.json()["detail"]


def test_patch_endpoint_not_found(client_a: TestClient, project_a_key: str) -> None:
    import uuid

    resp = client_a.patch(
        f"/endpoints/{uuid.uuid4()}",
        json={"status": "inactive"},
        headers=_auth(project_a_key),
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# DELETE /endpoints/{id}
# ---------------------------------------------------------------------------


def test_delete_endpoint_returns_204(client_a: TestClient, project_a_key: str) -> None:
    create = client_a.post(
        "/endpoints",
        json={"url": _VALID_URL, "event_types": _VALID_TYPES},
        headers=_auth(project_a_key),
    )
    ep_id = create.json()["id"]
    resp = client_a.delete(f"/endpoints/{ep_id}", headers=_auth(project_a_key))
    assert resp.status_code == 204


def test_delete_endpoint_removes_from_list(client_a: TestClient, project_a_key: str) -> None:
    create = client_a.post(
        "/endpoints",
        json={"url": _VALID_URL, "event_types": _VALID_TYPES},
        headers=_auth(project_a_key),
    )
    ep_id = create.json()["id"]
    client_a.delete(f"/endpoints/{ep_id}", headers=_auth(project_a_key))
    resp = client_a.get("/endpoints", headers=_auth(project_a_key))
    assert resp.json() == []


def test_delete_endpoint_404_for_wrong_project(
    client_a: TestClient, project_a_key: str, project_b_key: str
) -> None:
    """Project B cannot delete Project A's endpoint."""
    create = client_a.post(
        "/endpoints",
        json={"url": _VALID_URL, "event_types": _VALID_TYPES},
        headers=_auth(project_a_key),
    )
    ep_id = create.json()["id"]
    resp = client_a.delete(f"/endpoints/{ep_id}", headers=_auth(project_b_key))
    assert resp.status_code == 404


def test_delete_endpoint_not_found(client_a: TestClient, project_a_key: str) -> None:
    import uuid

    resp = client_a.delete(f"/endpoints/{uuid.uuid4()}", headers=_auth(project_a_key))
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /endpoints/{id}/rotate-secret
# ---------------------------------------------------------------------------


def _create_endpoint(client: TestClient, key: str) -> Any:
    resp = client.post(
        "/endpoints",
        json={"url": _VALID_URL, "event_types": _VALID_TYPES},
        headers=_auth(key),
    )
    assert resp.status_code == 201
    return resp.json()


def test_rotate_secret_returns_200_with_secret(client_a: TestClient, project_a_key: str) -> None:
    ep = _create_endpoint(client_a, project_a_key)
    resp = client_a.post(f"/endpoints/{ep['id']}/rotate-secret", headers=_auth(project_a_key))
    assert resp.status_code == 200
    data = resp.json()
    assert "secret" in data
    assert len(data["secret"]) > 0


def test_rotate_secret_produces_different_secret(
    client_a: TestClient, project_a_key: str, ep_db_session: Session
) -> None:
    import uuid as _uuid

    from app.models.endpoint import Endpoint
    from sqlalchemy import select

    ep = _create_endpoint(client_a, project_a_key)
    ep_id = ep["id"]

    old_enc = (
        ep_db_session.execute(select(Endpoint).where(Endpoint.id == _uuid.UUID(ep_id)))
        .scalar_one()
        .secret_enc
    )

    resp = client_a.post(f"/endpoints/{ep_id}/rotate-secret", headers=_auth(project_a_key))
    assert resp.status_code == 200

    ep_db_session.expire_all()
    new_enc = (
        ep_db_session.execute(select(Endpoint).where(Endpoint.id == _uuid.UUID(ep_id)))
        .scalar_one()
        .secret_enc
    )

    assert new_enc != old_enc


def test_rotate_secret_second_call_produces_different_secret(
    client_a: TestClient, project_a_key: str
) -> None:
    ep = _create_endpoint(client_a, project_a_key)
    ep_id = ep["id"]
    resp1 = client_a.post(f"/endpoints/{ep_id}/rotate-secret", headers=_auth(project_a_key))
    resp2 = client_a.post(f"/endpoints/{ep_id}/rotate-secret", headers=_auth(project_a_key))
    assert resp1.status_code == 200
    assert resp2.status_code == 200
    assert resp1.json()["secret"] != resp2.json()["secret"]


def test_rotate_secret_404_for_wrong_project(
    client_a: TestClient, project_a_key: str, project_b_key: str
) -> None:
    ep = _create_endpoint(client_a, project_a_key)
    resp = client_a.post(f"/endpoints/{ep['id']}/rotate-secret", headers=_auth(project_b_key))
    assert resp.status_code == 404


def test_rotate_secret_404_for_nonexistent_endpoint(
    client_a: TestClient, project_a_key: str
) -> None:
    import uuid

    resp = client_a.post(f"/endpoints/{uuid.uuid4()}/rotate-secret", headers=_auth(project_a_key))
    assert resp.status_code == 404


def test_rotate_secret_requires_auth(client_a: TestClient, project_a_key: str) -> None:
    ep = _create_endpoint(client_a, project_a_key)
    resp = client_a.post(f"/endpoints/{ep['id']}/rotate-secret")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# rate_limit_rps field
# ---------------------------------------------------------------------------


def test_create_endpoint_with_rate_limit_rps(client_a: TestClient, project_a_key: str) -> None:
    resp = client_a.post(
        "/endpoints",
        json={"url": _VALID_URL, "event_types": _VALID_TYPES, "rate_limit_rps": 10.0},
        headers=_auth(project_a_key),
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["rate_limit_rps"] == 10.0


def test_create_endpoint_rate_limit_rps_defaults_to_null(
    client_a: TestClient, project_a_key: str
) -> None:
    resp = client_a.post(
        "/endpoints",
        json={"url": _VALID_URL, "event_types": _VALID_TYPES},
        headers=_auth(project_a_key),
    )
    assert resp.status_code == 201
    assert resp.json()["rate_limit_rps"] is None


def test_list_endpoints_includes_rate_limit_rps(client_a: TestClient, project_a_key: str) -> None:
    client_a.post(
        "/endpoints",
        json={"url": _VALID_URL, "event_types": _VALID_TYPES, "rate_limit_rps": 5.0},
        headers=_auth(project_a_key),
    )
    resp = client_a.get("/endpoints", headers=_auth(project_a_key))
    assert resp.status_code == 200
    assert resp.json()[0]["rate_limit_rps"] == 5.0


def test_patch_endpoint_updates_rate_limit_rps(client_a: TestClient, project_a_key: str) -> None:
    create = client_a.post(
        "/endpoints",
        json={"url": _VALID_URL, "event_types": _VALID_TYPES},
        headers=_auth(project_a_key),
    )
    ep_id = create.json()["id"]
    resp = client_a.patch(
        f"/endpoints/{ep_id}",
        json={"rate_limit_rps": 50.0},
        headers=_auth(project_a_key),
    )
    assert resp.status_code == 200
    assert resp.json()["rate_limit_rps"] == 50.0


def test_create_endpoint_rate_limit_rps_zero_is_invalid(
    client_a: TestClient, project_a_key: str
) -> None:
    resp = client_a.post(
        "/endpoints",
        json={"url": _VALID_URL, "event_types": _VALID_TYPES, "rate_limit_rps": 0},
        headers=_auth(project_a_key),
    )
    assert resp.status_code == 422


def test_create_endpoint_rate_limit_rps_negative_is_invalid(
    client_a: TestClient, project_a_key: str
) -> None:
    resp = client_a.post(
        "/endpoints",
        json={"url": _VALID_URL, "event_types": _VALID_TYPES, "rate_limit_rps": -1.0},
        headers=_auth(project_a_key),
    )
    assert resp.status_code == 422


def test_create_endpoint_rate_limit_rps_above_max_is_invalid(
    client_a: TestClient, project_a_key: str
) -> None:
    resp = client_a.post(
        "/endpoints",
        json={"url": _VALID_URL, "event_types": _VALID_TYPES, "rate_limit_rps": 1000.1},
        headers=_auth(project_a_key),
    )
    assert resp.status_code == 422


def test_create_endpoint_rate_limit_rps_at_max_is_valid(
    client_a: TestClient, project_a_key: str
) -> None:
    resp = client_a.post(
        "/endpoints",
        json={"url": _VALID_URL, "event_types": _VALID_TYPES, "rate_limit_rps": 1000.0},
        headers=_auth(project_a_key),
    )
    assert resp.status_code == 201
    assert resp.json()["rate_limit_rps"] == 1000.0
