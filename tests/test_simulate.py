"""Tests for the interactive dashboard demo ("Ops Console").

Three tiers, deliberately:

- **Unit** — ``build_demo_event`` payload shapes; no database.
- **Service** (savepoint-isolated session): ``find_or_create_demo_endpoint``,
  health get/set, ``emit_demo_events`` fan-out, inbox record/prune/list. These
  never invoke the receiver route, so no cross-connection commit is involved.
- **Integration** (real, independent sessions with real commits + cleanup): the
  receiver route, the emit/health/inbox/dead-letter endpoints, and the
  dead-letter → redrive → recovery loop. These commit for real — the receiver
  resolves its own session exactly like production — so they can't run on the
  shared savepoint session; each creates a throwaway project and deletes it
  (ON DELETE CASCADE) at teardown.

All tests require a live Postgres instance; skipped automatically when
unreachable.
"""

from __future__ import annotations

import random
import time
import uuid
from collections.abc import Callable, Generator

import httpx
import pytest
from app.db.base import Base
from app.main import app
from app.models.api_key import ApiKey, generate_api_key
from app.models.delivery import Delivery, DeliveryStatus
from app.models.demo import DemoReceivedRequest
from app.models.endpoint import Endpoint
from app.models.project import Project
from app.routers.simulate import get_simulate_http_client
from app.services.crypto import decrypt_secret
from app.services.demo_events import DEMO_EVENT_TYPES, build_demo_event
from app.services.simulate import (
    _INBOX_KEEP,
    DEMO_MARKER,
    emit_demo_events,
    find_or_create_demo_endpoint,
    get_health,
    list_inbox,
    record_received_request,
    set_health,
)
from app.worker.delivery_worker import process_delivery
from app.worker.signing import build_signature_header
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, selectinload

_BASE = "http://localhost:8000"


def _auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


# ===========================================================================
# Unit: demo event generator
# ===========================================================================


@pytest.mark.parametrize("event_type", DEMO_EVENT_TYPES)
def test_build_demo_event_shapes(event_type: str) -> None:
    etype, payload = build_demo_event(event_type, rng=random.Random(1))
    assert etype == event_type
    assert isinstance(payload, dict)
    assert payload["repository"]["full_name"].startswith("acme/")
    if event_type == "push":
        assert payload["ref"].startswith("refs/heads/")
        assert len(payload["after"]) == 40
    elif event_type == "pull_request":
        assert payload["action"] in {"opened", "synchronize", "closed"}
        assert "title" in payload["pull_request"]
    else:
        assert payload["workflow_run"]["conclusion"] in {"success", "failure"}


def test_build_demo_event_random_type_is_valid() -> None:
    etype, _ = build_demo_event(rng=random.Random(7))
    assert etype in DEMO_EVENT_TYPES


def test_build_demo_event_is_deterministic_with_seed() -> None:
    assert build_demo_event(rng=random.Random(42)) == build_demo_event(rng=random.Random(42))


def test_build_demo_event_rejects_unknown_type() -> None:
    with pytest.raises(ValueError):
        build_demo_event("deploy.finished")


# ===========================================================================
# Service tier: savepoint-isolated session
# ===========================================================================


@pytest.fixture()
def sim_db_session(db_engine: Engine) -> Generator[Session, None, None]:
    """Savepoint-isolated session; rolls back after every test."""
    Base.metadata.create_all(db_engine)
    connection = db_engine.connect()
    outer_tx = connection.begin()
    session = Session(connection, join_transaction_mode="create_savepoint")
    yield session
    session.close()
    outer_tx.rollback()
    connection.close()


def _make_project(session: Session, name: str) -> Project:
    project = Project(name=name)
    session.add(project)
    session.flush()
    return project


def test_find_or_create_demo_endpoint_creates_one(sim_db_session: Session) -> None:
    project = _make_project(sim_db_session, "sim-create-once")
    ep = find_or_create_demo_endpoint(sim_db_session, project, _BASE)

    assert ep.event_types[0] == DEMO_MARKER
    assert set(DEMO_EVENT_TYPES) <= set(ep.event_types)
    assert ep.url == f"{_BASE}/simulate/receiver/{ep.id}"
    assert ep.rate_limit_rps is None
    # A health row is provisioned alongside, defaulting to healthy.
    assert get_health(sim_db_session, ep.id) is True


def test_find_or_create_demo_endpoint_is_idempotent(sim_db_session: Session) -> None:
    project = _make_project(sim_db_session, "sim-idempotent")
    ep1 = find_or_create_demo_endpoint(sim_db_session, project, _BASE)
    ep2 = find_or_create_demo_endpoint(sim_db_session, project, _BASE)

    assert ep1.id == ep2.id
    rows = (
        sim_db_session.execute(select(Endpoint).where(Endpoint.project_id == project.id))
        .scalars()
        .all()
    )
    assert len(rows) == 1


def test_get_and_set_health(sim_db_session: Session) -> None:
    project = _make_project(sim_db_session, "sim-health")
    ep = find_or_create_demo_endpoint(sim_db_session, project, _BASE)

    assert get_health(sim_db_session, ep.id) is True
    set_health(sim_db_session, ep.id, False)
    assert get_health(sim_db_session, ep.id) is False
    set_health(sim_db_session, ep.id, True)
    assert get_health(sim_db_session, ep.id) is True
    # Unknown endpoint defaults to healthy.
    assert get_health(sim_db_session, uuid.uuid4()) is True


def test_emit_demo_events_fans_out(sim_db_session: Session) -> None:
    project = _make_project(sim_db_session, "sim-emit")
    result = emit_demo_events(
        session=sim_db_session, project=project, public_base_url=_BASE, event_type="push", count=3
    )

    assert result.queued_events == 3
    assert result.queued_deliveries == 3  # one demo endpoint subscribes
    assert result.event_type == "push"
    assert result.sample_payload["ref"].startswith("refs/heads/")

    delivered = sim_db_session.execute(
        select(func.count()).select_from(Delivery).where(Delivery.endpoint_id == result.endpoint_id)
    ).scalar_one()
    assert delivered == 3


def test_record_received_request_prunes_to_keep(sim_db_session: Session) -> None:
    project = _make_project(sim_db_session, "sim-inbox-prune")
    ep = find_or_create_demo_endpoint(sim_db_session, project, _BASE)

    for i in range(_INBOX_KEEP + 5):
        record_received_request(
            sim_db_session,
            endpoint_id=ep.id,
            event_type="push",
            attempt=1,
            verified=True,
            response_status=200,
            signature_header=f"t=1,v1=sig{i}",
            timestamp_header="1",
            body='{"type":"push"}',
        )

    total = sim_db_session.execute(
        select(func.count())
        .select_from(DemoReceivedRequest)
        .where(DemoReceivedRequest.endpoint_id == ep.id)
    ).scalar_one()
    assert total == _INBOX_KEEP
    assert len(list_inbox(sim_db_session, ep.id)) == _INBOX_KEEP


# ===========================================================================
# Integration tier: real sessions, real commits, explicit cleanup
# ===========================================================================


@pytest.fixture()
def make_project(db_engine: Engine) -> Generator[Callable[[], tuple[uuid.UUID, str]], None, None]:
    """Factory that creates a real, committed project + API key.

    Every project it hands out is deleted at teardown; ON DELETE CASCADE takes
    the endpoints, deliveries, attempts, events, and demo rows with it.
    """
    Base.metadata.create_all(db_engine)
    created: list[uuid.UUID] = []

    def _factory() -> tuple[uuid.UUID, str]:
        with Session(db_engine) as session:
            project = Project(name=f"sim-int-{uuid.uuid4().hex[:12]}")
            session.add(project)
            session.flush()
            plaintext, prefix, key_hash = generate_api_key()
            session.add(
                ApiKey(project_id=project.id, name="k", key_prefix=prefix, key_hash=key_hash)
            )
            session.commit()
            created.append(project.id)
            return project.id, plaintext

    yield _factory

    with Session(db_engine) as session:
        for pid in created:
            session.execute(delete(Project).where(Project.id == pid))
        session.commit()


def _create_demo_endpoint(db_engine: Engine, project_id: uuid.UUID) -> tuple[uuid.UUID, str]:
    """Provision the demo endpoint for a project and return (endpoint_id, secret)."""
    with Session(db_engine) as session:
        project = session.get(Project, project_id)
        assert project is not None
        ep = find_or_create_demo_endpoint(session, project, _BASE)
        secret = decrypt_secret(ep.secret_enc)
        session.commit()
        return ep.id, secret


def _set_health(db_engine: Engine, endpoint_id: uuid.UUID, healthy: bool) -> None:
    with Session(db_engine) as session:
        set_health(session, endpoint_id, healthy)
        session.commit()


def _process_pending(db_engine: Engine, endpoint_id: uuid.UUID, worker_client: TestClient) -> int:
    """Process this endpoint's pending deliveries in-process (endpoint-scoped).

    Deliberately narrower than the worker's global claim so the test never
    touches unrelated rows that may exist in a shared database.
    """
    with Session(db_engine) as session:
        deliveries = (
            session.execute(
                select(Delivery)
                .options(selectinload(Delivery.endpoint), selectinload(Delivery.event))
                .where(
                    Delivery.endpoint_id == endpoint_id,
                    Delivery.status == DeliveryStatus.pending,
                )
                .with_for_update()
            )
            .scalars()
            .all()
        )
        for delivery in deliveries:
            process_delivery(delivery, session, worker_client)
        session.commit()
        return len(deliveries)


def _signed_headers(secret: str, body: bytes, attempt: int = 1) -> dict[str, str]:
    ts = int(time.time())
    return {
        "X-Webhook-Signature": build_signature_header(secret, ts, body),
        "X-Webhook-Timestamp": str(ts),
        "X-Webhook-Attempt": str(attempt),
    }


def test_emit_requires_auth(make_project: Callable[[], tuple[uuid.UUID, str]]) -> None:
    with TestClient(app) as client:
        assert client.post("/simulate/events", json={"count": 1}).status_code == 401


def test_emit_events_end_to_end(make_project: Callable[[], tuple[uuid.UUID, str]]) -> None:
    _, api_key = make_project()
    with TestClient(app) as client:
        resp = client.post(
            "/simulate/events",
            json={"event_type": "push", "count": 2},
            headers=_auth(api_key),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["queued_events"] == 2
        assert data["queued_deliveries"] == 2
        assert data["event_type"] == "push"

        deliveries = client.get("/deliveries", headers=_auth(api_key)).json()["items"]
        assert len(deliveries) == 2
        assert all(d["status"] == "pending" for d in deliveries)


def test_health_toggle_reflected_in_inbox(
    make_project: Callable[[], tuple[uuid.UUID, str]],
) -> None:
    _, api_key = make_project()
    with TestClient(app) as client:
        down = client.post("/simulate/health", json={"healthy": False}, headers=_auth(api_key))
        assert down.status_code == 200
        assert down.json()["healthy"] is False

        inbox = client.get("/simulate/inbox", headers=_auth(api_key)).json()
        assert inbox["healthy"] is False
        assert inbox["items"] == []


def test_receiver_404_for_unknown_endpoint(
    make_project: Callable[[], tuple[uuid.UUID, str]],
) -> None:
    with TestClient(app) as client:
        resp = client.post(f"/simulate/receiver/{uuid.uuid4()}", content=b"{}")
        assert resp.status_code == 404


def test_receiver_401_on_bad_signature_and_records_it(
    make_project: Callable[[], tuple[uuid.UUID, str]], db_engine: Engine
) -> None:
    project_id, api_key = make_project()
    endpoint_id, _secret = _create_demo_endpoint(db_engine, project_id)

    with TestClient(app) as client:
        resp = client.post(
            f"/simulate/receiver/{endpoint_id}",
            content=b'{"type":"push"}',
            headers={"X-Webhook-Signature": "t=1,v1=deadbeef", "X-Webhook-Attempt": "1"},
        )
        assert resp.status_code == 401

        inbox = client.get("/simulate/inbox", headers=_auth(api_key)).json()
        assert len(inbox["items"]) == 1
        assert inbox["items"][0]["verified"] is False
        assert inbox["items"][0]["response_status"] == 401


def test_receiver_200_when_healthy_503_when_down(
    make_project: Callable[[], tuple[uuid.UUID, str]], db_engine: Engine
) -> None:
    project_id, _api_key = make_project()
    endpoint_id, secret = _create_demo_endpoint(db_engine, project_id)
    body = b'{"type":"workflow_run","payload":{}}'

    with TestClient(app) as client:
        ok = client.post(
            f"/simulate/receiver/{endpoint_id}", content=body, headers=_signed_headers(secret, body)
        )
        assert ok.status_code == 200

    _set_health(db_engine, endpoint_id, False)
    with TestClient(app) as client:
        down = client.post(
            f"/simulate/receiver/{endpoint_id}", content=body, headers=_signed_headers(secret, body)
        )
        assert down.status_code == 503


def test_inbox_shows_signed_request_after_delivery(
    make_project: Callable[[], tuple[uuid.UUID, str]], db_engine: Engine
) -> None:
    _, api_key = make_project()
    with TestClient(app) as client:
        emit = client.post(
            "/simulate/events", json={"event_type": "push", "count": 1}, headers=_auth(api_key)
        ).json()
        endpoint_id = uuid.UUID(emit["endpoint_id"])

        processed = _process_pending(db_engine, endpoint_id, client)
        assert processed == 1

        inbox = client.get("/simulate/inbox", headers=_auth(api_key)).json()
        assert len(inbox["items"]) == 1
        item = inbox["items"][0]
        assert item["verified"] is True
        assert item["response_status"] == 200
        assert item["event_type"] == "push"
        assert item["signature_header"].startswith("t=")


def test_dead_letter_requires_pipeline_down(
    make_project: Callable[[], tuple[uuid.UUID, str]],
) -> None:
    _, api_key = make_project()
    with TestClient(app) as client:
        # Healthy by default → refuses to fabricate a dead-letter.
        resp = client.post("/simulate/dead-letter", headers=_auth(api_key))
        assert resp.status_code == 409


def test_dead_letter_then_redrive_recovers(
    make_project: Callable[[], tuple[uuid.UUID, str]], db_engine: Engine
) -> None:
    project_id, api_key = make_project()

    # An in-process client injected as the fast-forward's http_client so the
    # self-call to /simulate/receiver runs the real route in-process.
    with TestClient(app, raise_server_exceptions=True) as inner_client:

        def _override_http_client() -> Generator[httpx.Client, None, None]:
            yield inner_client

        app.dependency_overrides[get_simulate_http_client] = _override_http_client
        try:
            with TestClient(app) as client:
                # Take the pipeline down, then fast-forward one delivery to the DLQ.
                client.post("/simulate/health", json={"healthy": False}, headers=_auth(api_key))
                dead = client.post("/simulate/dead-letter", headers=_auth(api_key)).json()
                dead_id = dead["delivery_id"]
                assert dead_id is not None
                assert dead["healthy"] is False

                dlq = client.get("/deliveries?status=dead_lettered", headers=_auth(api_key)).json()[
                    "items"
                ]
                assert [d["id"] for d in dlq] == [dead_id]

                # Bring the pipeline back up and redrive.
                client.post("/simulate/health", json={"healthy": True}, headers=_auth(api_key))
                redrive = client.post(f"/deliveries/{dead_id}/redrive", headers=_auth(api_key))
                assert redrive.json()["status"] == "pending"
        finally:
            app.dependency_overrides.pop(get_simulate_http_client, None)

    # Let the worker reprocess the redriven (now-pending) delivery against a
    # healthy receiver — it must recover to succeeded.
    with Session(db_engine) as session:
        endpoint_id = session.execute(
            select(Delivery.endpoint_id).where(Delivery.id == uuid.UUID(dead_id))
        ).scalar_one()

    with TestClient(app) as worker_client:
        _process_pending(db_engine, endpoint_id, worker_client)

    with TestClient(app) as client:
        final = client.get(f"/deliveries/{dead_id}", headers=_auth(api_key)).json()
        assert final["status"] == "succeeded"
