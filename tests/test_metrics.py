"""Tests for the Prometheus metrics endpoint and delivery worker instrumentation."""

from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, datetime

import httpx
import pytest
from app.db.base import Base
from app.main import app
from app.models.delivery import Delivery, DeliveryStatus
from app.models.endpoint import Endpoint, EndpointStatus
from app.models.event import Event
from app.models.project import Project
from app.services.crypto import encrypt_secret
from app.worker.delivery_worker import process_delivery
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY
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
    assert response.headers["content-type"].startswith("text/plain")


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
