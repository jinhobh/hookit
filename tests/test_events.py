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
    before the "winner" committed.  We simulate this by patching only the first
    IdempotencyRecord SELECT to return None, forcing the savepoint recovery code path.
    The loser must return the winner's event_id rather than a 500.
    """
    from unittest.mock import MagicMock, patch

    from app.models.idempotency import IdempotencyRecord

    _, key = project_with_endpoint

    # Winner: commit an IdempotencyRecord normally.
    first_resp = client.post(
        "/events",
        json=_VALID_BODY,
        headers={**_auth(key), "Idempotency-Key": "concurrent-key"},
    )
    assert first_resp.status_code == 201
    first_data = first_resp.json()

    # Loser: bypass the initial IdempotencyRecord lookup (as if it ran before the
    # winner committed) so ingest_event tries a fresh insert and hits the unique
    # constraint.  We target only IdempotencyRecord SELECTs so auth queries pass
    # through untouched.
    original_execute = ev_db_session.execute
    idem_select_calls: list[int] = [0]

    def patched_execute(stmt, *args, **kwargs):  # type: ignore[no-untyped-def]
        descs = getattr(stmt, "column_descriptions", None)
        if descs and descs[0].get("entity") is IdempotencyRecord:
            idem_select_calls[0] += 1
            if idem_select_calls[0] == 1:
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

    # And the loser's rolled-back inserts must leave no rows behind — an
    # autoflushed Event once escaped the savepoint and leaked as an orphan.
    from app.models.delivery import Delivery
    from app.models.event import Event
    from sqlalchemy import func, select

    project, _ = project_with_endpoint
    event_count = ev_db_session.execute(
        select(func.count()).select_from(Event).where(Event.project_id == project.id)
    ).scalar_one()
    delivery_count = ev_db_session.execute(
        select(func.count())
        .select_from(Delivery)
        .join(Event, Delivery.event_id == Event.id)
        .where(Event.project_id == project.id)
    ).scalar_one()
    assert event_count == 1
    assert delivery_count == 1


def test_idempotency_genuine_concurrent_race_yields_one_event(db_engine: Engine) -> None:
    """Two truly concurrent POST /events with one Idempotency-Key create one event.

    Unlike the savepoint test above (which forces the loser's code path), this
    fires two real requests from two threads through real committed sessions —
    the same shape as the showcase's producer-driven "Publish duplicate" demo.
    Whichever interleaving occurs, both callers must get the same event_id and
    exactly one event/delivery may exist afterwards.
    """
    import threading
    import uuid as _uuid

    from app.models.delivery import Delivery
    from app.models.event import Event
    from sqlalchemy import delete, func, select

    Base.metadata.create_all(db_engine)
    project_name = f"idem-race-{_uuid.uuid4().hex[:10]}"

    with Session(db_engine) as setup:
        project, key = _make_project_and_key(setup, project_name)
        _make_endpoint(setup, project.id, ["order.created"])
        setup.commit()
        project_id = project.id

    barrier = threading.Barrier(2)
    results: list[tuple[int, dict[str, object]]] = []
    lock = threading.Lock()

    def fire() -> None:
        with TestClient(app) as thread_client:
            barrier.wait(timeout=10)
            resp = thread_client.post(
                "/events",
                json=_VALID_BODY,
                headers={**_auth(key), "Idempotency-Key": "genuine-race-key"},
            )
            with lock:
                results.append((resp.status_code, resp.json()))

    threads = [threading.Thread(target=fire) for _ in range(2)]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert len(results) == 2
        assert all(status == 201 for status, _ in results)
        first, second = results[0][1], results[1][1]
        assert first["event_id"] == second["event_id"]
        assert first["queued_deliveries"] == second["queued_deliveries"] == 1

        with Session(db_engine) as check:
            event_count = check.execute(
                select(func.count()).select_from(Event).where(Event.project_id == project_id)
            ).scalar_one()
            delivery_count = check.execute(
                select(func.count())
                .select_from(Delivery)
                .join(Event, Delivery.event_id == Event.id)
                .where(Event.project_id == project_id)
            ).scalar_one()
            assert event_count == 1
            assert delivery_count == 1
    finally:
        with Session(db_engine) as cleanup:
            cleanup.execute(delete(Project).where(Project.name == project_name))
            cleanup.commit()


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
# GET /events — list with cursor pagination
# ---------------------------------------------------------------------------


def test_list_events_requires_auth(client: TestClient) -> None:
    resp = client.get("/events")
    assert resp.status_code == 401


def test_list_events_empty_returns_empty_page(client: TestClient, project_key: str) -> None:
    resp = client.get("/events", headers=_auth(project_key))
    assert resp.status_code == 200
    data = resp.json()
    assert data == {"items": [], "next_cursor": None}


def test_list_events_happy_path(
    client: TestClient, project_with_endpoint: tuple[Project, str]
) -> None:
    project, key = project_with_endpoint
    resp1 = client.post("/events", json=_VALID_BODY, headers=_auth(key))
    assert resp1.status_code == 201

    resp = client.get("/events", headers=_auth(key))
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["items"]) == 1
    item = data["items"][0]
    assert item["type"] == _VALID_BODY["type"]
    assert item["payload"] == _VALID_BODY["payload"]
    assert "id" in item
    assert "created_at" in item
    assert item["delivery_count"] == 1
    assert data["next_cursor"] is None


def test_list_events_next_cursor_present_when_results_exceed_limit(
    client: TestClient, project_key: str
) -> None:
    for i in range(3):
        client.post(
            "/events",
            json={"type": "order.created", "payload": {"i": i}},
            headers=_auth(project_key),
        )

    resp = client.get("/events?limit=2", headers=_auth(project_key))
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["items"]) == 2
    assert data["next_cursor"] is not None


def test_list_events_next_cursor_absent_on_last_page(client: TestClient, project_key: str) -> None:
    for i in range(3):
        client.post(
            "/events",
            json={"type": "order.created", "payload": {"i": i}},
            headers=_auth(project_key),
        )

    page1 = client.get("/events?limit=2", headers=_auth(project_key)).json()
    cursor = page1["next_cursor"]
    assert cursor is not None

    page2 = client.get(f"/events?limit=2&cursor={cursor}", headers=_auth(project_key)).json()
    assert len(page2["items"]) == 1
    assert page2["next_cursor"] is None


def test_list_events_cursor_covers_all_items_without_overlap(
    client: TestClient, project_key: str
) -> None:
    for i in range(5):
        client.post(
            "/events",
            json={"type": "order.created", "payload": {"i": i}},
            headers=_auth(project_key),
        )

    page1 = client.get("/events?limit=3", headers=_auth(project_key)).json()
    assert len(page1["items"]) == 3
    cursor = page1["next_cursor"]
    assert cursor is not None

    page2 = client.get(f"/events?limit=3&cursor={cursor}", headers=_auth(project_key)).json()
    assert len(page2["items"]) == 2
    assert page2["next_cursor"] is None

    page1_ids = {item["id"] for item in page1["items"]}
    page2_ids = {item["id"] for item in page2["items"]}
    assert page1_ids.isdisjoint(page2_ids)
    assert len(page1_ids | page2_ids) == 5


def test_list_events_event_type_filter_narrows_results(
    client: TestClient, project_key: str
) -> None:
    client.post(
        "/events",
        json={"type": "order.created", "payload": {}},
        headers=_auth(project_key),
    )
    client.post(
        "/events",
        json={"type": "order.updated", "payload": {}},
        headers=_auth(project_key),
    )
    client.post(
        "/events",
        json={"type": "order.created", "payload": {}},
        headers=_auth(project_key),
    )

    resp = client.get("/events?event_type=order.created", headers=_auth(project_key))
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["items"]) == 2
    assert all(item["type"] == "order.created" for item in data["items"])


def test_list_events_scoped_to_project(client: TestClient, ev_db_session: Session) -> None:
    _, key1 = _make_project_and_key(ev_db_session, "project-list-scope-1")
    _, key2 = _make_project_and_key(ev_db_session, "project-list-scope-2")

    client.post("/events", json={"type": "order.created", "payload": {"p": 1}}, headers=_auth(key1))
    client.post("/events", json={"type": "order.created", "payload": {"p": 2}}, headers=_auth(key2))

    resp1 = client.get("/events", headers=_auth(key1))
    assert resp1.status_code == 200
    data1 = resp1.json()
    assert len(data1["items"]) == 1
    assert data1["items"][0]["payload"] == {"p": 1}

    resp2 = client.get("/events", headers=_auth(key2))
    assert resp2.status_code == 200
    data2 = resp2.json()
    assert len(data2["items"]) == 1
    assert data2["items"][0]["payload"] == {"p": 2}


def test_list_events_delivery_count_zero_when_no_endpoints(
    client: TestClient, project_key: str
) -> None:
    client.post("/events", json=_VALID_BODY, headers=_auth(project_key))

    resp = client.get("/events", headers=_auth(project_key))
    assert resp.status_code == 200
    data = resp.json()
    assert data["items"][0]["delivery_count"] == 0


def test_list_events_invalid_cursor_returns_422(client: TestClient, project_key: str) -> None:
    resp = client.get("/events?cursor=notavalidcursor", headers=_auth(project_key))
    assert resp.status_code == 422


def test_list_events_limit_too_large_returns_422(client: TestClient, project_key: str) -> None:
    resp = client.get("/events?limit=101", headers=_auth(project_key))
    assert resp.status_code == 422


def test_list_events_limit_zero_returns_422(client: TestClient, project_key: str) -> None:
    resp = client.get("/events?limit=0", headers=_auth(project_key))
    assert resp.status_code == 422


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
