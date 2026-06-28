"""Tests for the POST /events endpoint.

Integration tests require a live Postgres instance (skipped automatically when
Postgres is unreachable).  Each test runs inside a savepoint-based transaction
that is rolled back on teardown, providing full isolation.
"""

from __future__ import annotations

from collections.abc import Generator

import pytest
from app.db.base import Base
from app.db.session import get_session
from app.main import app
from app.models.api_key import ApiKey, generate_api_key
from app.models.endpoint import Endpoint, EndpointStatus
from app.models.project import Project
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def ev_db_session(db_engine: Engine) -> Generator[Session, None, None]:
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


def _make_endpoint(
    session: Session,
    project_id: object,
    event_types: list[str],
    status: EndpointStatus = EndpointStatus.active,
) -> Endpoint:
    from app.services.crypto import encrypt_secret, generate_endpoint_secret

    ep = Endpoint(
        project_id=project_id,
        url="https://receiver.example.com/hook",
        event_types=event_types,
        secret_enc=encrypt_secret(generate_endpoint_secret()),
        status=status,
    )
    session.add(ep)
    session.flush()
    return ep


@pytest.fixture()
def client(ev_db_session: Session) -> Generator[TestClient, None, None]:
    def override() -> Generator[Session, None, None]:
        yield ev_db_session

    app.dependency_overrides[get_session] = override
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c
    app.dependency_overrides.pop(get_session, None)


@pytest.fixture()
def project_key(ev_db_session: Session) -> str:
    _, plaintext = _make_project_and_key(ev_db_session, "project-events")
    return plaintext


@pytest.fixture()
def project_with_endpoint(ev_db_session: Session) -> tuple[Project, str]:
    project, key = _make_project_and_key(ev_db_session, "project-events-ep")
    _make_endpoint(ev_db_session, project.id, ["order.created", "order.updated"])
    return project, key


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


_VALID_BODY = {"type": "order.created", "payload": {"order_id": "abc123"}}

# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


def test_publish_event_requires_auth(client: TestClient) -> None:
    resp = client.post("/events", json=_VALID_BODY)
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Happy path — no matching endpoints
# ---------------------------------------------------------------------------


def test_publish_event_no_endpoints_returns_201(client: TestClient, project_key: str) -> None:
    resp = client.post("/events", json=_VALID_BODY, headers=_auth(project_key))
    assert resp.status_code == 201
    data = resp.json()
    assert "event_id" in data
    assert data["queued_deliveries"] == 0


# ---------------------------------------------------------------------------
# Happy path — matching endpoints
# ---------------------------------------------------------------------------


def test_publish_event_fans_out_to_matching_endpoint(
    client: TestClient, project_with_endpoint: tuple[Project, str]
) -> None:
    _, key = project_with_endpoint
    resp = client.post("/events", json=_VALID_BODY, headers=_auth(key))
    assert resp.status_code == 201
    data = resp.json()
    assert data["queued_deliveries"] == 1


def test_publish_event_multiple_matching_endpoints(
    client: TestClient, ev_db_session: Session
) -> None:
    project, key = _make_project_and_key(ev_db_session, "project-multi-ep")
    _make_endpoint(ev_db_session, project.id, ["user.created"])
    _make_endpoint(ev_db_session, project.id, ["user.created", "order.created"])

    resp = client.post(
        "/events",
        json={"type": "user.created", "payload": {}},
        headers=_auth(key),
    )
    assert resp.status_code == 201
    assert resp.json()["queued_deliveries"] == 2


def test_publish_event_inactive_endpoint_not_queued(
    client: TestClient, ev_db_session: Session
) -> None:
    project, key = _make_project_and_key(ev_db_session, "project-inactive-ep")
    _make_endpoint(ev_db_session, project.id, ["order.created"], EndpointStatus.inactive)

    resp = client.post("/events", json=_VALID_BODY, headers=_auth(key))
    assert resp.status_code == 201
    assert resp.json()["queued_deliveries"] == 0


def test_publish_event_unmatched_type_not_queued(
    client: TestClient, ev_db_session: Session
) -> None:
    project, key = _make_project_and_key(ev_db_session, "project-unmatched")
    _make_endpoint(ev_db_session, project.id, ["payment.received"])

    resp = client.post("/events", json=_VALID_BODY, headers=_auth(key))
    assert resp.status_code == 201
    assert resp.json()["queued_deliveries"] == 0


# ---------------------------------------------------------------------------
# Idempotency — replay (same key + same body)
# ---------------------------------------------------------------------------


def test_idempotent_replay_returns_same_response(
    client: TestClient, project_with_endpoint: tuple[Project, str]
) -> None:
    _, key = project_with_endpoint
    headers = {**_auth(key), "Idempotency-Key": "key-replay-1"}

    resp1 = client.post("/events", json=_VALID_BODY, headers=headers)
    assert resp1.status_code == 201

    resp2 = client.post("/events", json=_VALID_BODY, headers=headers)
    assert resp2.status_code == 201

    assert resp1.json()["event_id"] == resp2.json()["event_id"]
    assert resp1.json()["queued_deliveries"] == resp2.json()["queued_deliveries"]


def test_idempotent_replay_does_not_create_duplicates(
    client: TestClient, project_with_endpoint: tuple[Project, str], ev_db_session: Session
) -> None:
    from app.models.event import Event
    from sqlalchemy import select

    _, key = project_with_endpoint
    headers = {**_auth(key), "Idempotency-Key": "key-no-dup"}

    client.post("/events", json=_VALID_BODY, headers=headers)
    client.post("/events", json=_VALID_BODY, headers=headers)

    event_count = len(
        list(
            ev_db_session.execute(
                select(Event).where(Event.idempotency_key == "key-no-dup")
            ).scalars()
        )
    )
    assert event_count == 1


# ---------------------------------------------------------------------------
# Idempotency — concurrent insert race (savepoint recovery)
# ---------------------------------------------------------------------------


def test_idempotency_savepoint_recovers_on_concurrent_insert(
    client: TestClient,
    project_with_endpoint: tuple[Project, str],
    ev_db_session: Session,
) -> None:
    """Savepoint catches IntegrityError when two requests race past the initial lookup.

    The "loser" request has already passed the guard (initial lookup returned None)
    before the "winner" committed.  We simulate this by patching the first
    ``session.execute`` call to return None, forcing the savepoint recovery code path.
    The loser must return the winner's event_id rather than a 500.
    """
    from unittest.mock import MagicMock, patch

    _, key = project_with_endpoint

    # Winner: commit an IdempotencyRecord normally.
    first_resp = client.post(
        "/events",
        json=_VALID_BODY,
        headers={**_auth(key), "Idempotency-Key": "concurrent-key"},
    )
    assert first_resp.status_code == 201
    first_data = first_resp.json()

    # Loser: bypass the initial lookup (as if it ran before the winner committed)
    # so ingest_event tries a fresh insert and hits the unique constraint.
    original_execute = ev_db_session.execute
    select_calls: list[int] = [0]

    def patched_execute(stmt, *args, **kwargs):  # type: ignore[no-untyped-def]
        select_calls[0] += 1
        if select_calls[0] == 1:
            # Pretend the initial IdempotencyRecord lookup saw nothing.
            mock_result = MagicMock()
            mock_result.scalar_one_or_none.return_value = None
            return mock_result
        return original_execute(stmt, *args, **kwargs)

    with patch.object(ev_db_session, "execute", side_effect=patched_execute):
        second_resp = client.post(
            "/events",
            json=_VALID_BODY,
            headers={**_auth(key), "Idempotency-Key": "concurrent-key"},
        )

    assert second_resp.status_code == 201
    second_data = second_resp.json()
    # Savepoint recovery: loser returns the winner's cached response, not a 500.
    assert second_data["event_id"] == first_data["event_id"]
    assert second_data["queued_deliveries"] == first_data["queued_deliveries"]


# ---------------------------------------------------------------------------
# Idempotency — conflict (same key + different body → 409)
# ---------------------------------------------------------------------------


def test_idempotency_conflict_returns_409(client: TestClient, project_key: str) -> None:
    headers = {**_auth(project_key), "Idempotency-Key": "key-conflict"}

    resp1 = client.post("/events", json=_VALID_BODY, headers=headers)
    assert resp1.status_code == 201

    different_body = {"type": "order.updated", "payload": {"changed": True}}
    resp2 = client.post("/events", json=different_body, headers=headers)
    assert resp2.status_code == 409


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


def test_publish_event_missing_type_returns_422(client: TestClient, project_key: str) -> None:
    resp = client.post(
        "/events",
        json={"payload": {"x": 1}},
        headers=_auth(project_key),
    )
    assert resp.status_code == 422


def test_publish_event_blank_type_returns_422(client: TestClient, project_key: str) -> None:
    resp = client.post(
        "/events",
        json={"type": "  ", "payload": {}},
        headers=_auth(project_key),
    )
    assert resp.status_code == 422


def test_publish_event_oversized_payload_returns_422(client: TestClient, project_key: str) -> None:
    big = {"data": "x" * 70_000}
    resp = client.post(
        "/events",
        json={"type": "order.created", "payload": big},
        headers=_auth(project_key),
    )
    assert resp.status_code == 422
