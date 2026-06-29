"""Tests for delivery and delivery_attempt persistence and inspection APIs.

Integration tests require a live Postgres instance (skipped automatically when
Postgres is unreachable).  Each test runs inside a savepoint-based transaction
that is rolled back on teardown.
"""

from __future__ import annotations

import uuid
from collections.abc import Generator
from datetime import UTC, datetime

import pytest
from app.db.base import Base
from app.db.session import get_session
from app.main import app
from app.models.api_key import ApiKey, generate_api_key
from app.models.delivery import Delivery, DeliveryStatus
from app.models.delivery_attempt import DeliveryAttempt
from app.models.endpoint import Endpoint, EndpointStatus
from app.models.event import Event
from app.models.project import Project
from app.services.crypto import encrypt_secret, generate_endpoint_secret
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def dl_db_session(db_engine: Engine) -> Generator[Session, None, None]:
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


def _make_endpoint(session: Session, project_id: object) -> Endpoint:
    ep = Endpoint(
        project_id=project_id,
        url="https://receiver.example.com/hook",
        event_types=["order.created"],
        secret_enc=encrypt_secret(generate_endpoint_secret()),
        status=EndpointStatus.active,
    )
    session.add(ep)
    session.flush()
    return ep


def _make_event(session: Session, project_id: object) -> Event:
    event = Event(
        project_id=project_id,
        type="order.created",
        payload={"order_id": "abc123"},
    )
    session.add(event)
    session.flush()
    return event


def _make_delivery(session: Session, event: Event, endpoint: Endpoint) -> Delivery:
    delivery = Delivery(
        event_id=event.id,
        endpoint_id=endpoint.id,
        status=DeliveryStatus.pending,
        next_attempt_at=datetime.now(UTC),
    )
    session.add(delivery)
    session.flush()
    return delivery


def _make_attempt(
    session: Session, delivery: Delivery, number: int = 1, status: int = 200
) -> DeliveryAttempt:
    attempt = DeliveryAttempt(
        delivery_id=delivery.id,
        attempt_number=number,
        response_status=status,
        response_body='{"ok": true}',
        duration_ms=123,
    )
    session.add(attempt)
    session.flush()
    return attempt


@pytest.fixture()
def client(dl_db_session: Session) -> Generator[TestClient, None, None]:
    def override() -> Generator[Session, None, None]:
        yield dl_db_session

    app.dependency_overrides[get_session] = override
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c
    app.dependency_overrides.pop(get_session, None)


def _auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


# ---------------------------------------------------------------------------
# Model persistence tests
# ---------------------------------------------------------------------------


def test_delivery_persists(dl_db_session: Session) -> None:
    project, _ = _make_project_and_key(dl_db_session, "project-dl-persist")
    endpoint = _make_endpoint(dl_db_session, project.id)
    event = _make_event(dl_db_session, project.id)
    delivery = _make_delivery(dl_db_session, event, endpoint)

    fetched = dl_db_session.get(Delivery, delivery.id)
    assert fetched is not None
    assert fetched.status == DeliveryStatus.pending
    assert fetched.event_id == event.id
    assert fetched.endpoint_id == endpoint.id
    assert fetched.attempt_count == 0
    assert fetched.leased_until is None


def test_delivery_attempt_persists(dl_db_session: Session) -> None:
    project, _ = _make_project_and_key(dl_db_session, "project-da-persist")
    endpoint = _make_endpoint(dl_db_session, project.id)
    event = _make_event(dl_db_session, project.id)
    delivery = _make_delivery(dl_db_session, event, endpoint)
    attempt = _make_attempt(dl_db_session, delivery)

    fetched = dl_db_session.get(DeliveryAttempt, attempt.id)
    assert fetched is not None
    assert fetched.delivery_id == delivery.id
    assert fetched.attempt_number == 1
    assert fetched.response_status == 200
    assert fetched.response_body == '{"ok": true}'
    assert fetched.duration_ms == 123
    assert fetched.error is None


def test_delivery_attempts_relationship(dl_db_session: Session) -> None:
    project, _ = _make_project_and_key(dl_db_session, "project-dl-rel")
    endpoint = _make_endpoint(dl_db_session, project.id)
    event = _make_event(dl_db_session, project.id)
    delivery = _make_delivery(dl_db_session, event, endpoint)
    _make_attempt(dl_db_session, delivery, number=1, status=503)
    _make_attempt(dl_db_session, delivery, number=2, status=200)

    dl_db_session.expire(delivery)
    assert len(delivery.attempts) == 2
    assert delivery.attempts[0].attempt_number == 1
    assert delivery.attempts[1].attempt_number == 2


def test_delivery_attempt_cascade_deleted_with_delivery(dl_db_session: Session) -> None:
    project, _ = _make_project_and_key(dl_db_session, "project-dl-cascade")
    endpoint = _make_endpoint(dl_db_session, project.id)
    event = _make_event(dl_db_session, project.id)
    delivery = _make_delivery(dl_db_session, event, endpoint)
    attempt = _make_attempt(dl_db_session, delivery)
    attempt_id = attempt.id

    dl_db_session.delete(delivery)
    dl_db_session.flush()
    dl_db_session.expire_all()

    assert dl_db_session.get(DeliveryAttempt, attempt_id) is None


def test_event_deliveries_relationship(dl_db_session: Session) -> None:
    project, _ = _make_project_and_key(dl_db_session, "project-ev-dl-rel")
    endpoint = _make_endpoint(dl_db_session, project.id)
    event = _make_event(dl_db_session, project.id)
    _make_delivery(dl_db_session, event, endpoint)

    dl_db_session.expire(event)
    assert len(event.deliveries) == 1
    assert event.deliveries[0].event_id == event.id


# ---------------------------------------------------------------------------
# GET /events/{id} tests
# ---------------------------------------------------------------------------


def test_get_event_returns_event_with_deliveries(
    client: TestClient, dl_db_session: Session
) -> None:
    project, key = _make_project_and_key(dl_db_session, "project-get-event")
    endpoint = _make_endpoint(dl_db_session, project.id)
    event = _make_event(dl_db_session, project.id)
    delivery = _make_delivery(dl_db_session, event, endpoint)

    resp = client.get(f"/events/{event.id}", headers=_auth(key))
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == str(event.id)
    assert data["type"] == "order.created"
    assert len(data["deliveries"]) == 1
    assert data["deliveries"][0]["id"] == str(delivery.id)
    assert data["deliveries"][0]["status"] == "pending"


def test_get_event_requires_auth(client: TestClient, dl_db_session: Session) -> None:
    project, _ = _make_project_and_key(dl_db_session, "project-get-event-auth")
    event = _make_event(dl_db_session, project.id)
    resp = client.get(f"/events/{event.id}")
    assert resp.status_code == 401


def test_get_event_not_found(client: TestClient, dl_db_session: Session) -> None:
    _, key = _make_project_and_key(dl_db_session, "project-get-event-404")
    resp = client.get(f"/events/{uuid.uuid4()}", headers=_auth(key))
    assert resp.status_code == 404


def test_get_event_scoped_to_project(client: TestClient, dl_db_session: Session) -> None:
    _, key1 = _make_project_and_key(dl_db_session, "project-scoped-1")
    project2, _ = _make_project_and_key(dl_db_session, "project-scoped-2")
    event2 = _make_event(dl_db_session, project2.id)

    resp = client.get(f"/events/{event2.id}", headers=_auth(key1))
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /deliveries tests
# ---------------------------------------------------------------------------


def test_list_deliveries_returns_empty_for_no_deliveries(
    client: TestClient, dl_db_session: Session
) -> None:
    _, key = _make_project_and_key(dl_db_session, "project-list-dl-empty")
    resp = client.get("/deliveries", headers=_auth(key))
    assert resp.status_code == 200
    data = resp.json()
    assert data["items"] == []
    assert data["next_cursor"] is None


def test_list_deliveries_returns_project_deliveries(
    client: TestClient, dl_db_session: Session
) -> None:
    project, key = _make_project_and_key(dl_db_session, "project-list-dl")
    endpoint = _make_endpoint(dl_db_session, project.id)
    event = _make_event(dl_db_session, project.id)
    delivery = _make_delivery(dl_db_session, event, endpoint)

    resp = client.get("/deliveries", headers=_auth(key))
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["items"]) == 1
    assert data["items"][0]["id"] == str(delivery.id)
    assert data["next_cursor"] is None


def test_list_deliveries_scoped_to_project(client: TestClient, dl_db_session: Session) -> None:
    _, key1 = _make_project_and_key(dl_db_session, "project-dl-scope-1")
    project2, _ = _make_project_and_key(dl_db_session, "project-dl-scope-2")
    endpoint2 = _make_endpoint(dl_db_session, project2.id)
    event2 = _make_event(dl_db_session, project2.id)
    _make_delivery(dl_db_session, event2, endpoint2)

    resp = client.get("/deliveries", headers=_auth(key1))
    assert resp.status_code == 200
    data = resp.json()
    assert data["items"] == []
    assert data["next_cursor"] is None


def test_list_deliveries_requires_auth(client: TestClient) -> None:
    resp = client.get("/deliveries")
    assert resp.status_code == 401


def test_list_deliveries_default_page_structure(client: TestClient, dl_db_session: Session) -> None:
    project, key = _make_project_and_key(dl_db_session, "project-dl-page-struct")
    endpoint = _make_endpoint(dl_db_session, project.id)
    event = _make_event(dl_db_session, project.id)
    _make_delivery(dl_db_session, event, endpoint)

    resp = client.get("/deliveries", headers=_auth(key))
    assert resp.status_code == 200
    data = resp.json()
    assert "items" in data
    assert "next_cursor" in data
    assert isinstance(data["items"], list)


def test_list_deliveries_custom_limit(client: TestClient, dl_db_session: Session) -> None:
    project, key = _make_project_and_key(dl_db_session, "project-dl-limit")
    endpoint = _make_endpoint(dl_db_session, project.id)
    for _ in range(5):
        event = _make_event(dl_db_session, project.id)
        _make_delivery(dl_db_session, event, endpoint)

    resp = client.get("/deliveries?limit=3", headers=_auth(key))
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["items"]) == 3
    assert data["next_cursor"] is not None


def test_list_deliveries_cursor_pagination(client: TestClient, dl_db_session: Session) -> None:
    project, key = _make_project_and_key(dl_db_session, "project-dl-cursor")
    endpoint = _make_endpoint(dl_db_session, project.id)
    for _ in range(5):
        event = _make_event(dl_db_session, project.id)
        _make_delivery(dl_db_session, event, endpoint)

    page1 = client.get("/deliveries?limit=3", headers=_auth(key)).json()
    assert len(page1["items"]) == 3
    cursor = page1["next_cursor"]
    assert cursor is not None

    page2 = client.get(f"/deliveries?limit=3&cursor={cursor}", headers=_auth(key)).json()
    assert len(page2["items"]) == 2
    assert page2["next_cursor"] is None

    page1_ids = {item["id"] for item in page1["items"]}
    page2_ids = {item["id"] for item in page2["items"]}
    assert page1_ids.isdisjoint(page2_ids)
    assert len(page1_ids | page2_ids) == 5


def test_list_deliveries_filter_by_status(client: TestClient, dl_db_session: Session) -> None:
    project, key = _make_project_and_key(dl_db_session, "project-dl-filter-status")
    endpoint = _make_endpoint(dl_db_session, project.id)

    event1 = _make_event(dl_db_session, project.id)
    pending_delivery = _make_delivery(dl_db_session, event1, endpoint)

    event2 = _make_event(dl_db_session, project.id)
    succeeded_delivery = _make_delivery(dl_db_session, event2, endpoint)
    succeeded_delivery.status = DeliveryStatus.succeeded
    dl_db_session.flush()

    resp = client.get("/deliveries?status=pending", headers=_auth(key))
    assert resp.status_code == 200
    data = resp.json()
    ids = {item["id"] for item in data["items"]}
    assert str(pending_delivery.id) in ids
    assert str(succeeded_delivery.id) not in ids

    resp2 = client.get("/deliveries?status=succeeded", headers=_auth(key))
    assert resp2.status_code == 200
    data2 = resp2.json()
    ids2 = {item["id"] for item in data2["items"]}
    assert str(succeeded_delivery.id) in ids2
    assert str(pending_delivery.id) not in ids2


def test_list_deliveries_filter_by_endpoint_id(client: TestClient, dl_db_session: Session) -> None:
    project, key = _make_project_and_key(dl_db_session, "project-dl-filter-ep")
    endpoint1 = _make_endpoint(dl_db_session, project.id)
    endpoint2 = _make_endpoint(dl_db_session, project.id)

    event1 = _make_event(dl_db_session, project.id)
    delivery1 = _make_delivery(dl_db_session, event1, endpoint1)

    event2 = _make_event(dl_db_session, project.id)
    delivery2 = _make_delivery(dl_db_session, event2, endpoint2)

    resp = client.get(f"/deliveries?endpoint_id={endpoint1.id}", headers=_auth(key))
    assert resp.status_code == 200
    data = resp.json()
    ids = {item["id"] for item in data["items"]}
    assert str(delivery1.id) in ids
    assert str(delivery2.id) not in ids


def test_list_deliveries_invalid_cursor_returns_422(
    client: TestClient, dl_db_session: Session
) -> None:
    _, key = _make_project_and_key(dl_db_session, "project-dl-bad-cursor")
    resp = client.get("/deliveries?cursor=notavalidcursor", headers=_auth(key))
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# GET /deliveries/{id} tests
# ---------------------------------------------------------------------------


def test_get_delivery_returns_delivery(client: TestClient, dl_db_session: Session) -> None:
    project, key = _make_project_and_key(dl_db_session, "project-get-dl")
    endpoint = _make_endpoint(dl_db_session, project.id)
    event = _make_event(dl_db_session, project.id)
    delivery = _make_delivery(dl_db_session, event, endpoint)

    resp = client.get(f"/deliveries/{delivery.id}", headers=_auth(key))
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == str(delivery.id)
    assert data["status"] == "pending"
    assert data["event_id"] == str(event.id)


def test_get_delivery_not_found(client: TestClient, dl_db_session: Session) -> None:
    _, key = _make_project_and_key(dl_db_session, "project-get-dl-404")
    resp = client.get(f"/deliveries/{uuid.uuid4()}", headers=_auth(key))
    assert resp.status_code == 404


def test_get_delivery_scoped_to_project(client: TestClient, dl_db_session: Session) -> None:
    _, key1 = _make_project_and_key(dl_db_session, "project-get-dl-scope-1")
    project2, _ = _make_project_and_key(dl_db_session, "project-get-dl-scope-2")
    endpoint2 = _make_endpoint(dl_db_session, project2.id)
    event2 = _make_event(dl_db_session, project2.id)
    delivery2 = _make_delivery(dl_db_session, event2, endpoint2)

    resp = client.get(f"/deliveries/{delivery2.id}", headers=_auth(key1))
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /deliveries/{id}/attempts tests
# ---------------------------------------------------------------------------


def test_list_attempts_returns_empty(client: TestClient, dl_db_session: Session) -> None:
    project, key = _make_project_and_key(dl_db_session, "project-attempts-empty")
    endpoint = _make_endpoint(dl_db_session, project.id)
    event = _make_event(dl_db_session, project.id)
    delivery = _make_delivery(dl_db_session, event, endpoint)

    resp = client.get(f"/deliveries/{delivery.id}/attempts", headers=_auth(key))
    assert resp.status_code == 200
    assert resp.json() == []


def test_list_attempts_returns_ordered_attempts(client: TestClient, dl_db_session: Session) -> None:
    project, key = _make_project_and_key(dl_db_session, "project-attempts-ordered")
    endpoint = _make_endpoint(dl_db_session, project.id)
    event = _make_event(dl_db_session, project.id)
    delivery = _make_delivery(dl_db_session, event, endpoint)
    _make_attempt(dl_db_session, delivery, number=1, status=503)
    _make_attempt(dl_db_session, delivery, number=2, status=200)

    resp = client.get(f"/deliveries/{delivery.id}/attempts", headers=_auth(key))
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2
    assert data[0]["attempt_number"] == 1
    assert data[0]["response_status"] == 503
    assert data[1]["attempt_number"] == 2
    assert data[1]["response_status"] == 200


def test_list_attempts_not_found_for_missing_delivery(
    client: TestClient, dl_db_session: Session
) -> None:
    _, key = _make_project_and_key(dl_db_session, "project-attempts-404")
    resp = client.get(f"/deliveries/{uuid.uuid4()}/attempts", headers=_auth(key))
    assert resp.status_code == 404


def test_list_attempts_scoped_to_project(client: TestClient, dl_db_session: Session) -> None:
    _, key1 = _make_project_and_key(dl_db_session, "project-att-scope-1")
    project2, _ = _make_project_and_key(dl_db_session, "project-att-scope-2")
    endpoint2 = _make_endpoint(dl_db_session, project2.id)
    event2 = _make_event(dl_db_session, project2.id)
    delivery2 = _make_delivery(dl_db_session, event2, endpoint2)
    _make_attempt(dl_db_session, delivery2, number=1)

    resp = client.get(f"/deliveries/{delivery2.id}/attempts", headers=_auth(key1))
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /deliveries/{id}/redrive tests
# ---------------------------------------------------------------------------


def test_redrive_dead_lettered_delivery_succeeds(
    client: TestClient, dl_db_session: Session
) -> None:
    project, key = _make_project_and_key(dl_db_session, "project-redrive-ok")
    endpoint = _make_endpoint(dl_db_session, project.id)
    event = _make_event(dl_db_session, project.id)
    delivery = _make_delivery(dl_db_session, event, endpoint)
    delivery.status = DeliveryStatus.dead_lettered
    delivery.attempt_count = 5
    delivery.leased_until = datetime(2026, 1, 1, tzinfo=UTC)
    dl_db_session.flush()

    resp = client.post(f"/deliveries/{delivery.id}/redrive", headers=_auth(key))
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == str(delivery.id)
    assert data["status"] == "pending"
    assert data["attempt_count"] == 5
    assert data["leased_until"] is None


def test_redrive_not_found_returns_404(client: TestClient, dl_db_session: Session) -> None:
    _, key = _make_project_and_key(dl_db_session, "project-redrive-404")
    resp = client.post(f"/deliveries/{uuid.uuid4()}/redrive", headers=_auth(key))
    assert resp.status_code == 404


def test_redrive_scoped_to_project_returns_404(client: TestClient, dl_db_session: Session) -> None:
    _, key1 = _make_project_and_key(dl_db_session, "project-redrive-scope-1")
    project2, _ = _make_project_and_key(dl_db_session, "project-redrive-scope-2")
    endpoint2 = _make_endpoint(dl_db_session, project2.id)
    event2 = _make_event(dl_db_session, project2.id)
    delivery2 = _make_delivery(dl_db_session, event2, endpoint2)
    delivery2.status = DeliveryStatus.dead_lettered
    dl_db_session.flush()

    resp = client.post(f"/deliveries/{delivery2.id}/redrive", headers=_auth(key1))
    assert resp.status_code == 404


def test_redrive_pending_delivery_returns_409(client: TestClient, dl_db_session: Session) -> None:
    project, key = _make_project_and_key(dl_db_session, "project-redrive-409-pending")
    endpoint = _make_endpoint(dl_db_session, project.id)
    event = _make_event(dl_db_session, project.id)
    delivery = _make_delivery(dl_db_session, event, endpoint)
    assert delivery.status == DeliveryStatus.pending

    resp = client.post(f"/deliveries/{delivery.id}/redrive", headers=_auth(key))
    assert resp.status_code == 409
    assert "not dead-lettered" in resp.json()["detail"]


def test_redrive_succeeded_delivery_returns_409(client: TestClient, dl_db_session: Session) -> None:
    project, key = _make_project_and_key(dl_db_session, "project-redrive-409-succeeded")
    endpoint = _make_endpoint(dl_db_session, project.id)
    event = _make_event(dl_db_session, project.id)
    delivery = _make_delivery(dl_db_session, event, endpoint)
    delivery.status = DeliveryStatus.succeeded
    dl_db_session.flush()

    resp = client.post(f"/deliveries/{delivery.id}/redrive", headers=_auth(key))
    assert resp.status_code == 409
    assert "not dead-lettered" in resp.json()["detail"]


def test_redrive_requires_auth(client: TestClient, dl_db_session: Session) -> None:
    project, _ = _make_project_and_key(dl_db_session, "project-redrive-auth")
    endpoint = _make_endpoint(dl_db_session, project.id)
    event = _make_event(dl_db_session, project.id)
    delivery = _make_delivery(dl_db_session, event, endpoint)

    resp = client.post(f"/deliveries/{delivery.id}/redrive")
    assert resp.status_code == 401
