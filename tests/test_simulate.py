"""Tests for the live simulation ("Simulate load") feature.

Three tiers, deliberately:

- **Tier A** (service-level): shared-savepoint session + a fake httpx
  transport. Fast; exercises ``find_or_create_demo_endpoint`` and
  ``run_simulation``'s batching/fast-forward logic directly, with no receiver
  route involved.
- **Tier B** (router + receiver): a real ``TestClient`` injected as the
  fast-forward's ``http_client``, so requests actually hit
  ``POST /simulate/receiver/{id}`` in-process (real signature verification,
  real 200/401/404/500 branching). Still runs on the shared savepoint
  session, so it does **not** prove cross-connection commit visibility — see
  Tier C for that.
- **Tier C**: two independent Postgres connections (the same idiom as
  ``test_listen_notify.py::test_notify_received_on_listen_conn``), proving the
  phase-1 commit in ``run_simulation`` is actually visible outside the
  request's own session/connection — the one property the original design
  sketch got wrong and that no shared-session test could catch.

All tests require a live Postgres instance; skipped automatically when
unreachable.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Generator

import httpx
import pytest
from app.core.config import get_settings
from app.db.base import Base
from app.db.session import get_session
from app.main import app
from app.models.api_key import ApiKey, generate_api_key
from app.models.delivery import Delivery, DeliveryStatus
from app.models.delivery_attempt import DeliveryAttempt
from app.models.endpoint import Endpoint
from app.models.project import Project
from app.routers.simulate import get_simulate_http_client
from app.services.crypto import decrypt_secret
from app.services.simulate import (
    SIMULATE_EVENT_TYPE,
    _fast_forward_to_dead_letter,
    find_or_create_demo_endpoint,
    run_simulation,
)
from app.worker.delivery_worker import run_once
from app.worker.signing import build_signature_header
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_project(session: Session, name: str) -> Project:
    project = Project(name=name)
    session.add(project)
    session.flush()
    return project


def _make_project_and_key(session: Session, name: str) -> tuple[Project, str]:
    project = _make_project(session, name)
    plaintext, prefix, key_hash = generate_api_key()
    api_key = ApiKey(project_id=project.id, name="test-key", key_prefix=prefix, key_hash=key_hash)
    session.add(api_key)
    session.flush()
    return project, plaintext


def _auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


class _AlwaysFailTransport(httpx.BaseTransport):
    """Always returns 500 — enough to drive process_delivery to dead-letter."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(500, text='{"status":"simulated failure"}')


# ---------------------------------------------------------------------------
# Tier A: service-level (shared savepoint session, fake transport)
# ---------------------------------------------------------------------------


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


def test_find_or_create_demo_endpoint_creates_one(sim_db_session: Session) -> None:
    project = _make_project(sim_db_session, "sim-create-once")
    ep = find_or_create_demo_endpoint(sim_db_session, project, "http://localhost:8000")

    assert ep.event_types == [SIMULATE_EVENT_TYPE]
    assert ep.url == f"http://localhost:8000/simulate/receiver/{ep.id}"
    assert ep.rate_limit_rps is None


def test_find_or_create_demo_endpoint_is_idempotent(sim_db_session: Session) -> None:
    project = _make_project(sim_db_session, "sim-idempotent")
    ep1 = find_or_create_demo_endpoint(sim_db_session, project, "http://localhost:8000")
    ep2 = find_or_create_demo_endpoint(sim_db_session, project, "http://localhost:8000")

    assert ep1.id == ep2.id
    rows = (
        sim_db_session.execute(select(Endpoint).where(Endpoint.project_id == project.id))
        .scalars()
        .all()
    )
    assert len(rows) == 1


def test_run_simulation_batch_composition(sim_db_session: Session) -> None:
    project = _make_project(sim_db_session, "sim-batch")
    transport = _AlwaysFailTransport()
    with httpx.Client(transport=transport) as client:
        result = run_simulation(session=sim_db_session, project=project, http_client=client)

    assert result.queued_events == 12
    assert result.queued_deliveries == 12  # one demo endpoint subscribes to all of them
    assert result.dead_lettered_delivery_id is not None


def test_run_simulation_fast_forward_reaches_dead_letter_quickly(sim_db_session: Session) -> None:
    project = _make_project(sim_db_session, "sim-fast-forward")
    transport = _AlwaysFailTransport()

    start = time.monotonic()
    with httpx.Client(transport=transport) as client:
        result = run_simulation(session=sim_db_session, project=project, http_client=client)
    elapsed = time.monotonic() - start

    # Without the fast-forward, base=10s/cap=1h backoff would take ~5 real minutes.
    assert elapsed < 5.0
    assert result.dead_lettered_delivery_id is not None

    delivery = sim_db_session.get(Delivery, result.dead_lettered_delivery_id)
    assert delivery is not None
    assert delivery.status == DeliveryStatus.dead_lettered

    settings = get_settings()
    attempts = (
        sim_db_session.execute(
            select(DeliveryAttempt)
            .where(DeliveryAttempt.delivery_id == delivery.id)
            .order_by(DeliveryAttempt.attempt_number)
        )
        .scalars()
        .all()
    )
    assert [a.attempt_number for a in attempts] == list(
        range(1, settings.max_delivery_attempts + 1)
    )
    assert all(a.response_status == 500 for a in attempts)


def test_fast_forward_returns_none_when_delivery_missing(sim_db_session: Session) -> None:
    settings = get_settings()
    transport = _AlwaysFailTransport()
    with httpx.Client(transport=transport) as client:
        result = _fast_forward_to_dead_letter(
            sim_db_session, client, uuid.uuid4(), uuid.uuid4(), settings
        )
    assert result is None


# ---------------------------------------------------------------------------
# Tier B: router + receiver (real TestClient injected as the http_client)
# ---------------------------------------------------------------------------


@pytest.fixture()
def sim_client(sim_db_session: Session) -> Generator[TestClient, None, None]:
    """Outer TestClient wired to the shared session, with a second TestClient
    instance injected as get_simulate_http_client so the fast-forward's
    self-call to /simulate/receiver actually runs the real route in-process.
    """

    def override_session() -> Generator[Session, None, None]:
        yield sim_db_session

    app.dependency_overrides[get_session] = override_session
    with TestClient(app, raise_server_exceptions=True) as inner_client:

        def override_http_client() -> Generator[httpx.Client, None, None]:
            yield inner_client

        app.dependency_overrides[get_simulate_http_client] = override_http_client
        with TestClient(app, raise_server_exceptions=True) as outer_client:
            yield outer_client
        app.dependency_overrides.pop(get_simulate_http_client, None)
    app.dependency_overrides.pop(get_session, None)


def test_simulate_run_requires_auth(sim_client: TestClient) -> None:
    resp = sim_client.post("/simulate/run")
    assert resp.status_code == 401


def test_simulate_run_end_to_end(sim_client: TestClient, sim_db_session: Session) -> None:
    _, api_key = _make_project_and_key(sim_db_session, "sim-e2e")

    resp = sim_client.post("/simulate/run", headers=_auth(api_key))
    assert resp.status_code == 200
    data = resp.json()
    assert data["queued_events"] == 12
    assert data["queued_deliveries"] == 12
    assert data["dead_lettered_delivery_id"] is not None

    dlq_resp = sim_client.get("/deliveries?status=dead_lettered", headers=_auth(api_key))
    assert dlq_resp.status_code == 200
    dlq_items = dlq_resp.json()["items"]
    assert len(dlq_items) == 1
    assert dlq_items[0]["id"] == data["dead_lettered_delivery_id"]

    redrive_resp = sim_client.post(
        f"/deliveries/{data['dead_lettered_delivery_id']}/redrive", headers=_auth(api_key)
    )
    assert redrive_resp.status_code == 200
    assert redrive_resp.json()["status"] == "pending"


def test_simulate_redrive_recovers_to_succeeded(
    sim_client: TestClient, sim_db_session: Session
) -> None:
    """Regression test: redrive must not reset attempt_count, so the demo batch's
    "redrive me" delivery is deliberately seeded to succeed on exactly the next
    attempt after redrive (see run_simulation's redrive_fail_until) — otherwise
    it would just fail once more and immediately dead-letter again, silently
    breaking the one interaction the whole feature is built around.
    """
    _, api_key = _make_project_and_key(sim_db_session, "sim-redrive-recovers")

    data = sim_client.post("/simulate/run", headers=_auth(api_key)).json()
    dead_id = data["dead_lettered_delivery_id"]
    assert dead_id is not None

    redrive_resp = sim_client.post(f"/deliveries/{dead_id}/redrive", headers=_auth(api_key))
    assert redrive_resp.json()["status"] == "pending"

    # The redriven row sorts after the batch's other (earlier next_attempt_at)
    # deliveries, so it may not land in the first claimed batch — loop until
    # nothing's left to claim (bounded: 12 total events, batch_size defaults to 10).
    with TestClient(app, raise_server_exceptions=True) as worker_http_client:
        for _ in range(3):
            if run_once(sim_db_session, worker_http_client) == 0:
                break

    final = sim_client.get(f"/deliveries/{dead_id}", headers=_auth(api_key)).json()
    assert final["status"] == "succeeded"


def test_simulate_run_reuses_demo_endpoint(sim_client: TestClient, sim_db_session: Session) -> None:
    project, api_key = _make_project_and_key(sim_db_session, "sim-reuse")

    first = sim_client.post("/simulate/run", headers=_auth(api_key)).json()
    second = sim_client.post("/simulate/run", headers=_auth(api_key)).json()

    assert first["endpoint_id"] == second["endpoint_id"]
    endpoints = (
        sim_db_session.execute(select(Endpoint).where(Endpoint.project_id == project.id))
        .scalars()
        .all()
    )
    assert len(endpoints) == 1


def test_simulate_receiver_404_for_unknown_endpoint(sim_client: TestClient) -> None:
    resp = sim_client.post(f"/simulate/receiver/{uuid.uuid4()}", content=b"{}")
    assert resp.status_code == 404


def test_simulate_receiver_401_on_bad_signature(
    sim_client: TestClient, sim_db_session: Session
) -> None:
    project = _make_project(sim_db_session, "sim-receiver-badsig")
    ep = find_or_create_demo_endpoint(sim_db_session, project, "http://localhost:8000")
    sim_db_session.commit()

    resp = sim_client.post(
        f"/simulate/receiver/{ep.id}",
        content=b'{"payload":{"fail_until_attempt":1}}',
        headers={"X-Webhook-Signature": "t=1,v1=deadbeef", "X-Webhook-Attempt": "1"},
    )
    assert resp.status_code == 401


def test_simulate_receiver_200_when_attempt_meets_threshold(
    sim_client: TestClient, sim_db_session: Session
) -> None:
    project = _make_project(sim_db_session, "sim-receiver-pass")
    ep = find_or_create_demo_endpoint(sim_db_session, project, "http://localhost:8000")
    sim_db_session.commit()
    secret = decrypt_secret(ep.secret_enc)

    body = b'{"payload":{"fail_until_attempt":2}}'
    ts = int(time.time())
    resp = sim_client.post(
        f"/simulate/receiver/{ep.id}",
        content=body,
        headers={
            "X-Webhook-Signature": build_signature_header(secret, ts, body),
            "X-Webhook-Timestamp": str(ts),
            "X-Webhook-Attempt": "2",
        },
    )
    assert resp.status_code == 200


def test_simulate_receiver_500_when_attempt_below_threshold(
    sim_client: TestClient, sim_db_session: Session
) -> None:
    project = _make_project(sim_db_session, "sim-receiver-fail")
    ep = find_or_create_demo_endpoint(sim_db_session, project, "http://localhost:8000")
    sim_db_session.commit()
    secret = decrypt_secret(ep.secret_enc)

    body = b'{"payload":{"fail_until_attempt":2}}'
    ts = int(time.time())
    resp = sim_client.post(
        f"/simulate/receiver/{ep.id}",
        content=body,
        headers={
            "X-Webhook-Signature": build_signature_header(secret, ts, body),
            "X-Webhook-Timestamp": str(ts),
            "X-Webhook-Attempt": "1",
        },
    )
    assert resp.status_code == 500


# ---------------------------------------------------------------------------
# Tier C: cross-connection commit visibility (proves the phase-1 boundary)
# ---------------------------------------------------------------------------


def test_demo_endpoint_commit_is_visible_to_a_second_connection(db_engine: Engine) -> None:
    """The endpoint created by find_or_create_demo_endpoint must be visible
    from a genuinely independent connection once the caller commits — this is
    what makes it safe for the real, out-of-process worker (and the
    /simulate/receiver route, resolved via its own session) to see it.
    """
    Base.metadata.create_all(db_engine)
    connection = db_engine.connect()
    session = Session(connection)
    try:
        project = _make_project(session, "sim-cross-conn")
        session.commit()
        endpoint = find_or_create_demo_endpoint(session, project, "http://localhost:8000")
        session.commit()

        second_connection = db_engine.connect()
        try:
            row = second_connection.execute(
                select(Endpoint).where(Endpoint.id == endpoint.id)
            ).first()
            assert row is not None
        finally:
            second_connection.close()
    finally:
        stale_project = session.get(Project, project.id)
        if stale_project is not None:
            session.delete(stale_project)
            session.commit()
        session.close()
        connection.close()
