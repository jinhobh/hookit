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
from app.worker.backoff import compute_next_attempt_at
from app.worker.delivery_worker import claim_due_deliveries, process_delivery, run_once
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


def test_process_delivery_non_2xx_schedules_retry(wk_db_session: Session) -> None:
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

    # Under MAX_DELIVERY_ATTEMPTS: should retry (PENDING) with a future next_attempt_at
    assert delivery.status == DeliveryStatus.pending
    assert delivery.attempt_count == 1
    assert delivery.next_attempt_at > datetime.now(UTC)


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


def test_process_delivery_network_error_writes_attempt_and_schedules_retry(
    wk_db_session: Session,
) -> None:
    project = _make_project(wk_db_session, "process-net-err")
    secret = generate_endpoint_secret()
    ep = _make_endpoint(wk_db_session, project.id, secret)
    event = _make_event(wk_db_session, project.id)
    delivery = _make_delivery(wk_db_session, event, ep)
    delivery.status = DeliveryStatus.in_flight
    wk_db_session.flush()

    with httpx.Client(transport=_ErrorTransport()) as client:
        process_delivery(delivery, wk_db_session, client)

    # Under MAX_DELIVERY_ATTEMPTS: retry is scheduled
    assert delivery.status == DeliveryStatus.pending
    assert delivery.next_attempt_at > datetime.now(UTC)
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
    # Under MAX_DELIVERY_ATTEMPTS: retry is scheduled, delivery is PENDING
    assert delivery.status == DeliveryStatus.pending

    # Simulate re-claim for retry
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


# ---------------------------------------------------------------------------
# Unit tests: compute_next_attempt_at (no DB required)
# ---------------------------------------------------------------------------


def test_compute_next_attempt_at_attempt_1() -> None:
    """Attempt 1: delay is base_seconds (before cap and jitter)."""
    base, cap = 10.0, 3600.0
    before = datetime.now(UTC)
    result = compute_next_attempt_at(1, base, cap)
    # delay = min(10 * 2^0, 3600) = 10; jitter in [0, 1.0]
    elapsed = (result - before).total_seconds()
    assert 10.0 <= elapsed <= 11.1


def test_compute_next_attempt_at_attempt_6() -> None:
    """Attempt 6: delay is 320s (10 * 2^5), still below default cap."""
    base, cap = 10.0, 3600.0
    before = datetime.now(UTC)
    result = compute_next_attempt_at(6, base, cap)
    # delay = min(10 * 2^5, 3600) = 320; jitter in [0, 32.0]
    elapsed = (result - before).total_seconds()
    assert 320.0 <= elapsed <= 352.1


def test_compute_next_attempt_at_delay_capped() -> None:
    """Delay is capped at cap_seconds regardless of attempt number."""
    base, cap = 10.0, 5.0
    before = datetime.now(UTC)
    result = compute_next_attempt_at(10, base, cap)
    # uncapped = 10 * 2^9 = 5120; capped to 5; jitter in [0, 0.5]
    elapsed = (result - before).total_seconds()
    assert 5.0 <= elapsed <= 5.6


def test_compute_next_attempt_at_jitter_within_bounds() -> None:
    """Jitter always falls within [0, delay * 0.1] across many samples."""
    base, cap = 10.0, 3600.0
    # delay = 10; jitter must be in [0, 1.0]
    for _ in range(50):
        before = datetime.now(UTC)
        result = compute_next_attempt_at(1, base, cap)
        after = datetime.now(UTC)
        assert result >= before + timedelta(seconds=10.0)
        assert result <= after + timedelta(seconds=11.0 + 0.01)


# ---------------------------------------------------------------------------
# Integration tests: retry and dead-letter flow
# ---------------------------------------------------------------------------


def test_failing_delivery_retries_then_dead_letters(wk_db_session: Session) -> None:
    """A delivery that always fails reaches DEAD_LETTERED after MAX_DELIVERY_ATTEMPTS."""
    from app.core.config import get_settings

    settings = get_settings()
    max_attempts = settings.max_delivery_attempts

    project = _make_project(wk_db_session, "retry-dead-letter")
    secret = generate_endpoint_secret()
    ep = _make_endpoint(wk_db_session, project.id, secret)
    event = _make_event(wk_db_session, project.id)
    delivery = _make_delivery(wk_db_session, event, ep)
    delivery.status = DeliveryStatus.in_flight
    wk_db_session.flush()

    transport = _MockTransport(status_code=503)
    with httpx.Client(transport=transport) as client:
        # First (max_attempts - 1) failures → PENDING with backoff
        for i in range(max_attempts - 1):
            process_delivery(delivery, wk_db_session, client)
            assert delivery.status == DeliveryStatus.pending, f"attempt {i + 1} should be PENDING"
            assert delivery.next_attempt_at > datetime.now(UTC)
            # Re-claim for next attempt
            delivery.status = DeliveryStatus.in_flight
            wk_db_session.flush()

        # Final attempt → DEAD_LETTERED
        process_delivery(delivery, wk_db_session, client)

    assert delivery.status == DeliveryStatus.dead_lettered
    assert delivery.attempt_count == max_attempts
    wk_db_session.expire(delivery)
    assert len(delivery.attempts) == max_attempts


def test_expired_lease_delivery_is_recovered(wk_db_session: Session) -> None:
    """An IN_FLIGHT delivery with an expired lease is reset and re-claimed on the next run_once."""
    project = _make_project(wk_db_session, "lease-recovery")
    ep = _make_endpoint(wk_db_session, project.id, "lease-secret")
    event = _make_event(wk_db_session, project.id)
    delivery = _make_delivery(wk_db_session, event, ep)

    # Simulate a crashed worker: delivery is IN_FLIGHT with an expired lease
    delivery.status = DeliveryStatus.in_flight
    delivery.leased_until = datetime.now(UTC) - timedelta(seconds=1)
    delivery.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
    wk_db_session.flush()

    transport = _MockTransport(status_code=200)
    with httpx.Client(transport=transport) as client:
        count = run_once(wk_db_session, client)

    assert count == 1
    wk_db_session.expire(delivery)
    assert delivery.status == DeliveryStatus.succeeded
