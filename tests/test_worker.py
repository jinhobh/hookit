"""Tests for the delivery worker: signing utilities, claim logic, and delivery processing.

Integration tests require a live Postgres instance (skipped automatically when
Postgres is unreachable).  Outbound HTTP is intercepted by an in-process mock
transport — no external network access occurs.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Generator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from app.db.base import Base
from app.models.delivery import Delivery, DeliveryStatus
from app.models.delivery_attempt import DeliveryAttempt
from app.models.endpoint import Endpoint, EndpointStatus
from app.models.event import Event
from app.models.project import Project
from app.services.crypto import encrypt_secret, generate_endpoint_secret
from app.worker.delivery_worker import claim_due_deliveries, process_delivery
from app.worker.signing import build_signature_header, sign_payload
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

# ---------------------------------------------------------------------------
# Unit tests: signing (no DB required)
# ---------------------------------------------------------------------------


def test_sign_payload_produces_hmac_sha256() -> None:
    secret = "test-secret"
    timestamp = 1700000000
    body = b'{"event_id":"abc"}'
    canonical = f"{timestamp}.".encode() + body
    expected = hmac.new(secret.encode(), canonical, hashlib.sha256).hexdigest()
    assert sign_payload(secret, timestamp, body) == expected


def test_sign_payload_differs_for_different_secrets() -> None:
    body = b'{"test": true}'
    ts = 1700000000
    assert sign_payload("secret-a", ts, body) != sign_payload("secret-b", ts, body)


def test_sign_payload_differs_for_different_timestamps() -> None:
    secret = "same-secret"
    body = b'{"test": true}'
    assert sign_payload(secret, 1700000000, body) != sign_payload(secret, 1700000001, body)


def test_sign_payload_differs_for_different_bodies() -> None:
    secret = "same-secret"
    ts = 1700000000
    assert sign_payload(secret, ts, b'{"a":1}') != sign_payload(secret, ts, b'{"b":2}')


def test_build_signature_header_format() -> None:
    secret = "test-secret"
    ts = 1700000000
    body = b'{"test": true}'
    header = build_signature_header(secret, ts, body)
    assert header == f"t={ts},v1={sign_payload(secret, ts, body)}"


# ---------------------------------------------------------------------------
# Helpers for integration tests
# ---------------------------------------------------------------------------


@pytest.fixture()
def wk_db_session(db_engine: Engine) -> Generator[Session, None, None]:
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


def _make_endpoint(
    session: Session,
    project_id: object,
    secret: str,
    url: str = "http://receiver.test/hook",
) -> Endpoint:
    ep = Endpoint(
        project_id=project_id,
        url=url,
        event_types=["order.created"],
        secret_enc=encrypt_secret(secret),
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


def _make_delivery(
    session: Session,
    event: Event,
    endpoint: Endpoint,
    status: DeliveryStatus = DeliveryStatus.pending,
    next_attempt_at: datetime | None = None,
) -> Delivery:
    delivery = Delivery(
        event_id=event.id,
        endpoint_id=endpoint.id,
        status=status,
        attempt_count=0,
        next_attempt_at=next_attempt_at or datetime.now(UTC),
    )
    session.add(delivery)
    session.flush()
    return delivery


class _MockTransport(httpx.BaseTransport):
    """Records requests and returns a canned HTTP response."""

    def __init__(self, status_code: int = 200, body: str = '{"ok":true}') -> None:
        self.requests: list[httpx.Request] = []
        self._status = status_code
        self._body = body

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self._status, text=self._body)


class _ErrorTransport(httpx.BaseTransport):
    """Always raises a connection error."""

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused")


# ---------------------------------------------------------------------------
# Integration tests: claim_due_deliveries
# ---------------------------------------------------------------------------


def test_claim_pending_delivery(wk_db_session: Session) -> None:
    project = _make_project(wk_db_session, "claim-pending")
    ep = _make_endpoint(wk_db_session, project.id, "secret1")
    event = _make_event(wk_db_session, project.id)
    delivery = _make_delivery(wk_db_session, event, ep)

    claimed = claim_due_deliveries(wk_db_session)

    assert len(claimed) == 1
    assert claimed[0].id == delivery.id
    assert claimed[0].status == DeliveryStatus.in_flight
    assert claimed[0].leased_until is not None


def test_claim_skips_already_in_flight(wk_db_session: Session) -> None:
    project = _make_project(wk_db_session, "claim-skip-inflight")
    ep = _make_endpoint(wk_db_session, project.id, "secret2")
    event = _make_event(wk_db_session, project.id)
    _make_delivery(wk_db_session, event, ep, status=DeliveryStatus.in_flight)

    assert claim_due_deliveries(wk_db_session) == []


def test_claim_skips_succeeded(wk_db_session: Session) -> None:
    project = _make_project(wk_db_session, "claim-skip-succeeded")
    ep = _make_endpoint(wk_db_session, project.id, "secret3")
    event = _make_event(wk_db_session, project.id)
    _make_delivery(wk_db_session, event, ep, status=DeliveryStatus.succeeded)

    assert claim_due_deliveries(wk_db_session) == []


def test_claim_skips_future_next_attempt(wk_db_session: Session) -> None:
    project = _make_project(wk_db_session, "claim-skip-future")
    ep = _make_endpoint(wk_db_session, project.id, "secret4")
    event = _make_event(wk_db_session, project.id)
    _make_delivery(
        wk_db_session,
        event,
        ep,
        next_attempt_at=datetime.now(UTC) + timedelta(hours=1),
    )

    assert claim_due_deliveries(wk_db_session) == []


def test_claim_sets_leased_until(wk_db_session: Session) -> None:
    project = _make_project(wk_db_session, "claim-lease")
    ep = _make_endpoint(wk_db_session, project.id, "secret5")
    event = _make_event(wk_db_session, project.id)
    _make_delivery(wk_db_session, event, ep)

    before = datetime.now(UTC)
    claimed = claim_due_deliveries(wk_db_session)
    after = datetime.now(UTC)

    assert claimed[0].leased_until is not None
    assert claimed[0].leased_until > before
    # lease is ~60 s, so leased_until should be well after `after`
    assert claimed[0].leased_until > after


# ---------------------------------------------------------------------------
# Integration tests: process_delivery
# ---------------------------------------------------------------------------


def test_process_delivery_2xx_marks_succeeded(wk_db_session: Session) -> None:
    project = _make_project(wk_db_session, "process-2xx")
    secret = generate_endpoint_secret()
    ep = _make_endpoint(wk_db_session, project.id, secret)
    event = _make_event(wk_db_session, project.id)
    delivery = _make_delivery(wk_db_session, event, ep)
    delivery.status = DeliveryStatus.in_flight
    wk_db_session.flush()

    transport = _MockTransport(status_code=200)
    with httpx.Client(transport=transport) as client:
        process_delivery(delivery, wk_db_session, client)

    assert delivery.status == DeliveryStatus.succeeded
    assert delivery.attempt_count == 1


def test_process_delivery_non_2xx_marks_failed(wk_db_session: Session) -> None:
    project = _make_project(wk_db_session, "process-503")
    secret = generate_endpoint_secret()
    ep = _make_endpoint(wk_db_session, project.id, secret)
    event = _make_event(wk_db_session, project.id)
    delivery = _make_delivery(wk_db_session, event, ep)
    delivery.status = DeliveryStatus.in_flight
    wk_db_session.flush()

    transport = _MockTransport(status_code=503)
    with httpx.Client(transport=transport) as client:
        process_delivery(delivery, wk_db_session, client)

    assert delivery.status == DeliveryStatus.failed
    assert delivery.attempt_count == 1


def test_process_delivery_writes_attempt_record(wk_db_session: Session) -> None:
    project = _make_project(wk_db_session, "process-attempt")
    secret = generate_endpoint_secret()
    ep = _make_endpoint(wk_db_session, project.id, secret)
    event = _make_event(wk_db_session, project.id)
    delivery = _make_delivery(wk_db_session, event, ep)
    delivery.status = DeliveryStatus.in_flight
    wk_db_session.flush()

    transport = _MockTransport(status_code=200)
    with httpx.Client(transport=transport) as client:
        process_delivery(delivery, wk_db_session, client)

    wk_db_session.expire(delivery)
    assert len(delivery.attempts) == 1
    attempt: DeliveryAttempt = delivery.attempts[0]
    assert attempt.attempt_number == 1
    assert attempt.response_status == 200
    assert attempt.duration_ms is not None
    assert attempt.error is None


def test_process_delivery_network_error_writes_failed_attempt(wk_db_session: Session) -> None:
    project = _make_project(wk_db_session, "process-net-err")
    secret = generate_endpoint_secret()
    ep = _make_endpoint(wk_db_session, project.id, secret)
    event = _make_event(wk_db_session, project.id)
    delivery = _make_delivery(wk_db_session, event, ep)
    delivery.status = DeliveryStatus.in_flight
    wk_db_session.flush()

    with httpx.Client(transport=_ErrorTransport()) as client:
        process_delivery(delivery, wk_db_session, client)

    assert delivery.status == DeliveryStatus.failed
    wk_db_session.expire(delivery)
    assert len(delivery.attempts) == 1
    attempt = delivery.attempts[0]
    assert attempt.response_status is None
    assert attempt.error is not None
    assert "Connection refused" in attempt.error


def test_process_delivery_sends_correct_hmac_signature(wk_db_session: Session) -> None:
    """The outbound POST must carry a verifiable HMAC-SHA256 signature."""
    project = _make_project(wk_db_session, "process-signing")
    secret = "known-signing-secret"
    ep = _make_endpoint(wk_db_session, project.id, secret)
    event = _make_event(wk_db_session, project.id)
    delivery = _make_delivery(wk_db_session, event, ep)
    delivery.status = DeliveryStatus.in_flight
    wk_db_session.flush()

    transport = _MockTransport(status_code=200)
    with httpx.Client(transport=transport) as client:
        process_delivery(delivery, wk_db_session, client)

    assert len(transport.requests) == 1
    req = transport.requests[0]

    sig_header = req.headers["x-webhook-signature"]
    ts_header = req.headers["x-webhook-timestamp"]

    # Parse t=... ,v1=...
    parts = dict(p.split("=", 1) for p in sig_header.split(","))
    ts = int(parts["t"])
    assert str(ts) == ts_header

    # Recompute and verify
    body = req.content
    expected = sign_payload(secret, ts, body)
    assert parts["v1"] == expected


def test_process_delivery_payload_contains_event_data(wk_db_session: Session) -> None:
    """The POST body must include event_id, type, and payload."""
    project = _make_project(wk_db_session, "process-payload")
    secret = generate_endpoint_secret()
    ep = _make_endpoint(wk_db_session, project.id, secret)
    event = _make_event(wk_db_session, project.id)
    delivery = _make_delivery(wk_db_session, event, ep)
    delivery.status = DeliveryStatus.in_flight
    wk_db_session.flush()

    transport = _MockTransport(status_code=200)
    with httpx.Client(transport=transport) as client:
        process_delivery(delivery, wk_db_session, client)

    req = transport.requests[0]
    body = json.loads(req.content)
    assert body["event_id"] == str(event.id)
    assert body["type"] == "order.created"
    assert body["payload"] == {"order_id": "abc123"}


def test_process_delivery_increments_attempt_count_on_retry(wk_db_session: Session) -> None:
    """A second call increments attempt_count to 2 and uses attempt_number=2."""
    project = _make_project(wk_db_session, "process-retry-count")
    secret = generate_endpoint_secret()
    ep = _make_endpoint(wk_db_session, project.id, secret)
    event = _make_event(wk_db_session, project.id)
    delivery = _make_delivery(wk_db_session, event, ep)
    delivery.status = DeliveryStatus.in_flight
    wk_db_session.flush()

    transport = _MockTransport(status_code=503)
    with httpx.Client(transport=transport) as client:
        process_delivery(delivery, wk_db_session, client)

    assert delivery.attempt_count == 1
    assert delivery.status == DeliveryStatus.failed

    # Simulate a re-queue for retry
    delivery.status = DeliveryStatus.in_flight
    wk_db_session.flush()

    transport2 = _MockTransport(status_code=200)
    with httpx.Client(transport=transport2) as client2:
        process_delivery(delivery, wk_db_session, client2)

    assert delivery.attempt_count == 2
    assert delivery.status == DeliveryStatus.succeeded
    wk_db_session.expire(delivery)
    assert len(delivery.attempts) == 2
    assert delivery.attempts[1].attempt_number == 2
