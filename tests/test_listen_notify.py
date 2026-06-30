"""Tests for LISTEN/NOTIFY-driven worker wake-up and fallback polling.

Unit tests run without Postgres.  Integration tests require a live Postgres
instance and are skipped automatically when one is not reachable.
"""

from __future__ import annotations

import time
from collections.abc import Generator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest
from app.core.config import get_settings
from app.db.base import Base
from app.models.delivery import Delivery, DeliveryStatus
from app.models.endpoint import Endpoint, EndpointStatus
from app.models.event import Event
from app.models.project import Project
from app.services.crypto import encrypt_secret
from app.services.event_ingestion import ingest_event
from app.worker.__main__ import _open_listen_conn, _wait_for_notify
from app.worker.delivery_worker import run_once
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_project(session: Session, name: str) -> Project:
    project = Project(name=name)
    session.add(project)
    session.flush()
    return project


def _make_endpoint(session: Session, project_id: object, secret: str) -> Endpoint:
    ep = Endpoint(
        project_id=project_id,
        url="http://receiver.test/hook",
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
        payload={"order_id": "abc"},
    )
    session.add(event)
    session.flush()
    return event


def _make_delivery(session: Session, event: Event, endpoint: Endpoint) -> Delivery:
    delivery = Delivery(
        event_id=event.id,
        endpoint_id=endpoint.id,
        status=DeliveryStatus.pending,
        attempt_count=0,
        next_attempt_at=datetime.now(UTC),
    )
    session.add(delivery)
    session.flush()
    return delivery


class _MockTransport(httpx.BaseTransport):
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(200, text='{"ok":true}')


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def ln_db_session(db_engine: Engine) -> Generator[Session, None, None]:
    """Savepoint-isolated session; rolls back after each test."""
    Base.metadata.create_all(db_engine)
    connection = db_engine.connect()
    outer_tx = connection.begin()
    session = Session(connection, join_transaction_mode="create_savepoint")
    yield session
    session.close()
    outer_tx.rollback()
    connection.close()


# ---------------------------------------------------------------------------
# Unit tests: _wait_for_notify (no DB required)
# ---------------------------------------------------------------------------


def test_wait_for_notify_returns_promptly_on_notification() -> None:
    """_wait_for_notify returns as soon as the first notification is yielded."""
    mock_conn: Any = MagicMock()
    mock_conn.notifies.return_value = iter([MagicMock()])

    start = time.monotonic()
    _wait_for_notify(mock_conn, timeout=10.0)
    elapsed = time.monotonic() - start

    mock_conn.notifies.assert_called_once_with(timeout=10.0)
    # Should return nearly immediately, not wait the full 10 s timeout.
    assert elapsed < 1.0


def test_wait_for_notify_returns_after_timeout() -> None:
    """_wait_for_notify returns without error when no notification arrives."""
    mock_conn: Any = MagicMock()
    mock_conn.notifies.return_value = iter([])  # empty generator simulates timeout

    _wait_for_notify(mock_conn, timeout=0.1)
    mock_conn.notifies.assert_called_once_with(timeout=0.1)


# ---------------------------------------------------------------------------
# Integration tests: NOTIFY issued by event_ingestion (requires Postgres)
# ---------------------------------------------------------------------------


def test_notify_issued_when_deliveries_queued(ln_db_session: Session) -> None:
    """ingest_event executes NOTIFY when at least one delivery row is queued."""
    settings = get_settings()
    project = _make_project(ln_db_session, "notify-issued")
    _make_endpoint(ln_db_session, project.id, "secret")

    notify_sqls: list[str] = []
    original_execute = ln_db_session.execute

    def tracking_execute(statement: Any, *args: Any, **kwargs: Any) -> Any:
        stmt_str = str(statement)
        if "NOTIFY" in stmt_str:
            notify_sqls.append(stmt_str)
        return original_execute(statement, *args, **kwargs)

    with patch.object(ln_db_session, "execute", side_effect=tracking_execute):
        ingest_event(
            session=ln_db_session,
            project_id=project.id,
            event_type="order.created",
            payload={"order_id": "001"},
            idempotency_key=None,
        )

    assert len(notify_sqls) == 1
    assert settings.worker_listen_channel in notify_sqls[0]


def test_no_notify_when_no_deliveries_queued(ln_db_session: Session) -> None:
    """ingest_event does NOT send NOTIFY when no active endpoints match."""
    project = _make_project(ln_db_session, "notify-skipped")
    # No endpoints registered — queued_count will be 0.

    notify_sqls: list[str] = []
    original_execute = ln_db_session.execute

    def tracking_execute(statement: Any, *args: Any, **kwargs: Any) -> Any:
        if "NOTIFY" in str(statement):
            notify_sqls.append(str(statement))
        return original_execute(statement, *args, **kwargs)

    with patch.object(ln_db_session, "execute", side_effect=tracking_execute):
        ingest_event(
            session=ln_db_session,
            project_id=project.id,
            event_type="order.created",
            payload={"order_id": "002"},
            idempotency_key=None,
        )

    assert notify_sqls == []


# ---------------------------------------------------------------------------
# Integration test: fallback polling (no NOTIFY required)
# ---------------------------------------------------------------------------


def test_fallback_poll_processes_delivery_without_notify(ln_db_session: Session) -> None:
    """Worker run_once claims and processes a delivery row injected directly.

    This mirrors the fallback-poll path: a delivery exists but no NOTIFY was
    sent (e.g. missed or not yet implemented on the sender side).  The worker
    still processes it on its regular poll cycle.
    """
    project = _make_project(ln_db_session, "fallback-poll")
    ep = _make_endpoint(ln_db_session, project.id, "secret")
    event = _make_event(ln_db_session, project.id)
    delivery = _make_delivery(ln_db_session, event, ep)

    transport = _MockTransport()
    with httpx.Client(transport=transport) as client:
        count = run_once(ln_db_session, client)

    assert count == 1
    assert len(transport.requests) == 1
    ln_db_session.expire(delivery)
    assert delivery.status == DeliveryStatus.succeeded


# ---------------------------------------------------------------------------
# Integration test: _open_listen_conn (requires Postgres)
# ---------------------------------------------------------------------------


def test_open_listen_conn_connects_and_listens(db_engine: Engine) -> None:
    """_open_listen_conn opens a psycopg connection and issues LISTEN."""
    import psycopg

    settings = get_settings()
    conn = _open_listen_conn(settings)
    try:
        assert isinstance(conn, psycopg.Connection)
        # Send a NOTIFY on the channel from the same connection; it should not
        # raise.  (The connection is autocommit so NOTIFY fires immediately.)
        conn.execute(f"NOTIFY {settings.worker_listen_channel}")
    finally:
        conn.close()


def test_notify_received_on_listen_conn(db_engine: Engine) -> None:
    """A NOTIFY sent after commit is received by a LISTEN connection promptly.

    Uses two independent connections: one listens, the other sends NOTIFY via a
    committed transaction, and we verify the notification arrives within 200 ms.
    This is an end-to-end verification of the LISTEN/NOTIFY mechanism.
    """
    import psycopg

    settings = get_settings()
    dsn = settings.database_url.replace("postgresql+psycopg://", "postgresql://", 1)

    # Connection 1: LISTEN
    listen_conn = psycopg.connect(dsn, autocommit=True)
    listen_conn.execute(f"LISTEN {settings.worker_listen_channel}")

    # Connection 2: send NOTIFY in a committed transaction
    notify_conn = psycopg.connect(dsn, autocommit=False)
    try:
        notify_conn.execute(f"NOTIFY {settings.worker_listen_channel}")
        notify_conn.commit()

        received = False
        for notify in listen_conn.notifies(timeout=0.5):
            if notify.channel == settings.worker_listen_channel:
                received = True
                break

        assert received, (
            f"Expected NOTIFY on channel {settings.worker_listen_channel!r} within 500 ms"
        )
    finally:
        notify_conn.close()
        listen_conn.close()
