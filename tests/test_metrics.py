"""Tests for the Prometheus metrics endpoint and delivery worker instrumentation."""

from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, datetime

import httpx
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
from app.services.crypto import encrypt_secret
from app.worker.delivery_worker import process_delivery
from fastapi.testclient import TestClient
from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

client = TestClient(app)


# ---------------------------------------------------------------------------
# /metrics endpoint tests (no DB required)
# ---------------------------------------------------------------------------


def test_metrics_returns_200() -> None:
    response = client.get("/metrics")
    assert response.status_code == 200


def test_metrics_content_type() -> None:
    response = client.get("/metrics")
    assert response.headers["content-type"] == CONTENT_TYPE_LATEST


# ---------------------------------------------------------------------------
# Counter and histogram instrumentation tests (require Postgres)
# ---------------------------------------------------------------------------


@pytest.fixture()
def met_db_session(db_engine: Engine) -> Generator[Session, None, None]:
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
    url: str = "http://receiver.test/hook",
) -> Endpoint:
    ep = Endpoint(
        project_id=project_id,
        url=url,
        event_types=["order.created"],
        secret_enc=encrypt_secret("test-secret"),
        status=EndpointStatus.active,
    )
    session.add(ep)
    session.flush()
    return ep


def _make_event(session: Session, project_id: object) -> Event:
    event = Event(
        project_id=project_id,
        type="order.created",
        payload={"order_id": "test"},
    )
    session.add(event)
    session.flush()
    return event


def _make_inflight_delivery(session: Session, event: Event, endpoint: Endpoint) -> Delivery:
    delivery = Delivery(
        event_id=event.id,
        endpoint_id=endpoint.id,
        status=DeliveryStatus.in_flight,
        attempt_count=0,
        next_attempt_at=datetime.now(UTC),
    )
    session.add(delivery)
    session.flush()
    return delivery


class _MockTransport(httpx.BaseTransport):
    def __init__(self, status_code: int) -> None:
        self._status = status_code

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(self._status, text='{"ok":true}')


def _sample(name: str, labels: dict[str, str] | None = None) -> float:
    """Return the current registry value for a sample, defaulting to 0."""
    return REGISTRY.get_sample_value(name, labels) or 0.0


def test_succeeded_counter_increments(met_db_session: Session) -> None:
    project = _make_project(met_db_session, "metrics-succeeded")
    ep = _make_endpoint(met_db_session, project.id)
    event = _make_event(met_db_session, project.id)
    delivery = _make_inflight_delivery(met_db_session, event, ep)

    before = _sample("webhook_deliveries_total", {"outcome": "succeeded"})

    with httpx.Client(transport=_MockTransport(200)) as http:
        process_delivery(delivery, met_db_session, http)

    after = _sample("webhook_deliveries_total", {"outcome": "succeeded"})
    assert after - before == 1.0


def test_failed_counter_increments(met_db_session: Session) -> None:
    project = _make_project(met_db_session, "metrics-failed")
    ep = _make_endpoint(met_db_session, project.id)
    event = _make_event(met_db_session, project.id)
    delivery = _make_inflight_delivery(met_db_session, event, ep)

    before = _sample("webhook_deliveries_total", {"outcome": "failed"})

    with httpx.Client(transport=_MockTransport(503)) as http:
        process_delivery(delivery, met_db_session, http)

    after = _sample("webhook_deliveries_total", {"outcome": "failed"})
    assert after - before == 1.0


def test_dead_lettered_counter_increments(met_db_session: Session) -> None:
    from app.core.config import get_settings

    settings = get_settings()
    project = _make_project(met_db_session, "metrics-dead-letter")
    ep = _make_endpoint(met_db_session, project.id)
    event = _make_event(met_db_session, project.id)
    delivery = _make_inflight_delivery(met_db_session, event, ep)
    delivery.attempt_count = settings.max_delivery_attempts - 1
    met_db_session.flush()

    before = _sample("webhook_deliveries_total", {"outcome": "dead_lettered"})

    with httpx.Client(transport=_MockTransport(503)) as http:
        process_delivery(delivery, met_db_session, http)

    after = _sample("webhook_deliveries_total", {"outcome": "dead_lettered"})
    assert after - before == 1.0


def test_ssrf_dead_lettered_counter_increments(met_db_session: Session) -> None:
    project = _make_project(met_db_session, "metrics-ssrf-dead-letter")
    ep = _make_endpoint(met_db_session, project.id, url="http://169.254.169.254/hook")
    event = _make_event(met_db_session, project.id)
    delivery = _make_inflight_delivery(met_db_session, event, ep)

    before = _sample("webhook_deliveries_total", {"outcome": "dead_lettered"})

    with httpx.Client() as http:
        process_delivery(delivery, met_db_session, http)

    after = _sample("webhook_deliveries_total", {"outcome": "dead_lettered"})
    assert after - before == 1.0


def test_duration_histogram_populated(met_db_session: Session) -> None:
    project = _make_project(met_db_session, "metrics-duration")
    ep = _make_endpoint(met_db_session, project.id)
    event = _make_event(met_db_session, project.id)
    delivery = _make_inflight_delivery(met_db_session, event, ep)

    before = _sample("webhook_delivery_attempt_duration_seconds_count")

    with httpx.Client(transport=_MockTransport(200)) as http:
        process_delivery(delivery, met_db_session, http)

    after = _sample("webhook_delivery_attempt_duration_seconds_count")
    assert after - before == 1.0


# ---------------------------------------------------------------------------
# Dashboard static page (no DB required)
# ---------------------------------------------------------------------------


def test_dashboard_page_served() -> None:
    response = client.get("/dashboard/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "HookIt · Live Demo" in response.text


# ---------------------------------------------------------------------------
# /metrics/summary aggregation endpoint (require Postgres)
# ---------------------------------------------------------------------------


def _make_project_and_key(session: Session, name: str) -> tuple[Project, str]:
    project = Project(name=name)
    session.add(project)
    session.flush()
    plaintext, prefix, key_hash = generate_api_key()
    session.add(
        ApiKey(project_id=project.id, name="test-key", key_prefix=prefix, key_hash=key_hash)
    )
    session.flush()
    return project, plaintext


def _make_delivery(
    session: Session,
    event: Event,
    endpoint: Endpoint,
    status: DeliveryStatus,
    *,
    attempt_count: int = 0,
    updated_at: datetime | None = None,
) -> Delivery:
    delivery = Delivery(
        event_id=event.id,
        endpoint_id=endpoint.id,
        status=status,
        attempt_count=attempt_count,
        next_attempt_at=datetime.now(UTC),
    )
    if updated_at is not None:
        delivery.updated_at = updated_at
    session.add(delivery)
    session.flush()
    return delivery


def _make_attempt(
    session: Session, delivery: Delivery, number: int, response_status: int, duration_ms: int
) -> None:
    session.add(
        DeliveryAttempt(
            delivery_id=delivery.id,
            attempt_number=number,
            response_status=response_status,
            duration_ms=duration_ms,
        )
    )
    session.flush()


@pytest.fixture()
def summary_client(met_db_session: Session) -> Generator[TestClient, None, None]:
    def override() -> Generator[Session, None, None]:
        yield met_db_session

    app.dependency_overrides[get_session] = override
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.pop(get_session, None)


def _auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def test_summary_requires_auth(summary_client: TestClient) -> None:
    assert summary_client.get("/metrics/summary").status_code == 401


def test_summary_empty_project(summary_client: TestClient, met_db_session: Session) -> None:
    _, key = _make_project_and_key(met_db_session, "summary-empty")

    body = summary_client.get("/metrics/summary", headers=_auth(key)).json()

    assert body["totals"] == {
        "pending": 0,
        "in_flight": 0,
        "succeeded": 0,
        "failed": 0,
        "dead_lettered": 0,
        "all": 0,
    }
    assert body["success_rate"] is None
    assert body["latency_ms"] is None
    assert body["dlq_depth"] == 0
    assert body["attempts_total"] == 0
    assert body["throughput_per_min"] == 0.0


def test_summary_aggregates_counts_and_latency(
    summary_client: TestClient, met_db_session: Session
) -> None:
    project, key = _make_project_and_key(met_db_session, "summary-counts")
    ep = _make_endpoint(met_db_session, project.id)
    event = _make_event(met_db_session, project.id)

    now = datetime.now(UTC)
    # 3 succeeded (recent) + 1 dead-lettered + 1 pending.
    succeeded = [
        _make_delivery(met_db_session, event, ep, DeliveryStatus.succeeded, updated_at=now)
        for _ in range(3)
    ]
    dead = _make_delivery(met_db_session, event, ep, DeliveryStatus.dead_lettered, attempt_count=3)
    _make_delivery(met_db_session, event, ep, DeliveryStatus.pending)

    for i, d in enumerate(succeeded):
        _make_attempt(met_db_session, d, 1, 200, duration_ms=100 + i * 10)
    for n in range(1, 4):  # dead-letter had 3 failing attempts
        _make_attempt(met_db_session, dead, n, 500, duration_ms=50)

    body = summary_client.get("/metrics/summary", headers=_auth(key)).json()

    assert body["totals"]["succeeded"] == 3
    assert body["totals"]["dead_lettered"] == 1
    assert body["totals"]["pending"] == 1
    assert body["totals"]["all"] == 5
    assert body["dlq_depth"] == 1
    assert body["attempts_total"] == 6  # 3 succeeded + 3 dead-letter attempts
    assert body["success_rate"] == 0.75  # 3 succeeded / (3 + 1 terminal)
    assert body["latency_ms"] is not None
    assert 50 <= body["latency_ms"]["p50"] <= 120
    assert body["throughput_per_min"] == 3.0


def test_summary_scoped_to_project(summary_client: TestClient, met_db_session: Session) -> None:
    # Another project's deliveries must not leak into this project's summary.
    other, _ = _make_project_and_key(met_db_session, "summary-other")
    other_ep = _make_endpoint(met_db_session, other.id)
    other_event = _make_event(met_db_session, other.id)
    _make_delivery(met_db_session, other_event, other_ep, DeliveryStatus.succeeded)

    _, key = _make_project_and_key(met_db_session, "summary-mine")
    body = summary_client.get("/metrics/summary", headers=_auth(key)).json()

    assert body["totals"]["all"] == 0
