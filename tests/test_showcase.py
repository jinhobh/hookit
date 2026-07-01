"""Tests for the live showcase demo (real producer → real Discord).

Two tiers, deliberately:

- **Service** (savepoint-isolated session): seeding, resolution, health get/set,
  inbox record/prune, and delivery-scoping helpers. An injected ``Settings`` with
  a unique project name keeps each test independent; these never invoke the
  receiver route, so no cross-connection commit is involved.
- **Integration** (real, independent sessions with real commits + cleanup): the
  ``/showcase/*`` routes, the receiver route, and the dead-letter → redrive →
  recovery loop. These commit for real — the receiver resolves its own session
  exactly like production — so each test isolates itself with a unique showcase
  project name (via env) and deletes that project (ON DELETE CASCADE) at teardown.

All tests require a live Postgres instance; skipped automatically when unreachable.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Generator

import httpx
import pytest
from app.core.config import Settings, get_settings
from app.db.base import Base
from app.main import app
from app.models.api_key import ApiKey, hash_api_key
from app.models.delivery import Delivery, DeliveryStatus
from app.models.demo import DemoReceivedRequest
from app.models.endpoint import Endpoint, PayloadFormat
from app.models.project import Project
from app.routers.showcase import get_simulate_http_client
from app.services.crypto import decrypt_secret
from app.services.showcase import (
    _INBOX_KEEP,
    PRICE_ALERT,
    PRICE_TICK,
    SHOWCASE_MARKER,
    get_health,
    get_scoped_delivery,
    latest_dead_lettered_id,
    list_inbox,
    record_received_request,
    resolve_showcase,
    seed_showcase,
    set_health,
)
from app.worker.delivery_worker import process_delivery
from app.worker.signing import build_signature_header
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, selectinload


def _settings(**overrides: object) -> Settings:
    """A Settings instance with a unique showcase name and Discord disabled."""
    base = get_settings()
    defaults: dict[str, object] = {
        "showcase_project_name": f"__showcase_test_{uuid.uuid4().hex[:10]}__",
        "showcase_discord_webhook_url": "",
        "showcase_api_key": "",
        "database_url": base.database_url,
        "public_base_url": "http://localhost:8000",
    }
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


# ===========================================================================
# Service tier: savepoint-isolated session
# ===========================================================================


@pytest.fixture()
def sc_session(db_engine: Engine) -> Generator[Session, None, None]:
    """Savepoint-isolated session; rolls back after every test."""
    Base.metadata.create_all(db_engine)
    connection = db_engine.connect()
    outer_tx = connection.begin()
    session = Session(connection, join_transaction_mode="create_savepoint")
    yield session
    session.close()
    outer_tx.rollback()
    connection.close()


def test_seed_creates_project_receiver_and_health(sc_session: Session) -> None:
    settings = _settings()
    handles = seed_showcase(sc_session, settings)

    project = sc_session.get(Project, handles.project_id)
    assert project is not None and project.name == settings.showcase_project_name

    receiver = sc_session.get(Endpoint, handles.receiver_endpoint_id)
    assert receiver is not None
    assert receiver.event_types[0] == SHOWCASE_MARKER
    assert {PRICE_TICK, PRICE_ALERT} <= set(receiver.event_types)
    assert receiver.url.endswith(f"/showcase/receiver/{receiver.id}")
    assert get_health(sc_session, receiver.id) is True
    # Discord disabled when no webhook configured.
    assert handles.discord_endpoint_id is None


def test_seed_with_discord_and_api_key(sc_session: Session) -> None:
    settings = _settings(
        showcase_discord_webhook_url="https://discord.com/api/webhooks/1/abc",
        showcase_api_key="whk_showcase_test_key",
    )
    handles = seed_showcase(sc_session, settings)

    assert handles.discord_endpoint_id is not None
    discord = sc_session.get(Endpoint, handles.discord_endpoint_id)
    assert discord is not None
    assert discord.payload_format == PayloadFormat.discord
    assert discord.event_types == [PRICE_ALERT]
    assert discord.url == "https://discord.com/api/webhooks/1/abc"

    # An API key whose hash matches the shared secret is provisioned so the
    # external producer can authenticate with that same value.
    key = sc_session.execute(
        select(ApiKey).where(ApiKey.key_hash == hash_api_key("whk_showcase_test_key"))
    ).scalar_one_or_none()
    assert key is not None and key.project_id == handles.project_id


def test_seed_is_idempotent(sc_session: Session) -> None:
    settings = _settings()
    h1 = seed_showcase(sc_session, settings)
    h2 = seed_showcase(sc_session, settings)
    assert h1 == h2
    rows = (
        sc_session.execute(select(Endpoint).where(Endpoint.project_id == h1.project_id))
        .scalars()
        .all()
    )
    assert len(rows) == 1  # only the receiver (discord disabled)


def test_resolve_returns_none_when_unseeded(sc_session: Session) -> None:
    settings = _settings()
    assert resolve_showcase(sc_session, settings) is None
    seed_showcase(sc_session, settings)
    assert resolve_showcase(sc_session, settings) is not None


def test_get_and_set_health(sc_session: Session) -> None:
    handles = seed_showcase(sc_session, _settings())
    eid = handles.receiver_endpoint_id
    assert get_health(sc_session, eid) is True
    set_health(sc_session, eid, False)
    assert get_health(sc_session, eid) is False
    set_health(sc_session, eid, True)
    assert get_health(sc_session, eid) is True
    assert get_health(sc_session, uuid.uuid4()) is True  # unknown defaults healthy


def test_record_received_request_prunes_to_keep(sc_session: Session) -> None:
    handles = seed_showcase(sc_session, _settings())
    eid = handles.receiver_endpoint_id
    for i in range(_INBOX_KEEP + 5):
        record_received_request(
            sc_session,
            endpoint_id=eid,
            event_type=PRICE_TICK,
            attempt=1,
            verified=True,
            response_status=200,
            signature_header=f"t=1,v1=sig{i}",
            timestamp_header="1",
            body='{"type":"price.tick"}',
        )
    total = sc_session.execute(
        select(func.count())
        .select_from(DemoReceivedRequest)
        .where(DemoReceivedRequest.endpoint_id == eid)
    ).scalar_one()
    assert total == _INBOX_KEEP
    assert len(list_inbox(sc_session, eid)) == _INBOX_KEEP


def test_scoping_helpers_reject_foreign_delivery(sc_session: Session) -> None:
    handles = seed_showcase(sc_session, _settings())
    # No dead-lettered deliveries yet.
    assert latest_dead_lettered_id(sc_session, handles.project_id) is None
    assert get_scoped_delivery(sc_session, handles.project_id, uuid.uuid4()) is None


# ===========================================================================
# Integration tier: real sessions, real commits, explicit cleanup
# ===========================================================================


@pytest.fixture()
def isolated_showcase(
    db_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> Generator[str, None, None]:
    """Point the app at a unique, disposable showcase project for one test.

    Overrides the showcase name via env (cache-cleared so the routes pick it up),
    yields that name, and deletes the project — with everything ON DELETE CASCADE
    takes — at teardown.
    """
    Base.metadata.create_all(db_engine)
    name = f"__showcase_it_{uuid.uuid4().hex[:10]}__"
    monkeypatch.setenv("SHOWCASE_PROJECT_NAME", name)
    monkeypatch.setenv("SHOWCASE_DISCORD_WEBHOOK_URL", "")
    monkeypatch.setenv("SHOWCASE_API_KEY", "")
    get_settings.cache_clear()
    yield name
    get_settings.cache_clear()
    with Session(db_engine) as session:
        session.execute(delete(Project).where(Project.name == name))
        session.commit()
    get_settings.cache_clear()


def _receiver(db_engine: Engine) -> tuple[uuid.UUID, uuid.UUID, str]:
    """Return (project_id, receiver_endpoint_id, secret) for the seeded showcase."""
    with Session(db_engine) as session:
        handles = resolve_showcase(session)
        assert handles is not None
        ep = session.get(Endpoint, handles.receiver_endpoint_id)
        assert ep is not None
        return handles.project_id, ep.id, decrypt_secret(ep.secret_enc)


def _set_health(db_engine: Engine, endpoint_id: uuid.UUID, healthy: bool) -> None:
    with Session(db_engine) as session:
        set_health(session, endpoint_id, healthy)
        session.commit()


def _process_pending(db_engine: Engine, endpoint_id: uuid.UUID, worker_client: TestClient) -> int:
    """Process this endpoint's pending deliveries in-process (endpoint-scoped)."""
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


def test_feed_and_health_toggle(isolated_showcase: str) -> None:
    with TestClient(app) as client:
        feed = client.get("/showcase/feed").json()
        assert feed["healthy"] is True
        assert feed["discord_enabled"] is False
        assert feed["events"] == []
        assert feed["inbox"] == []

        down = client.post("/showcase/health", json={"healthy": False})
        assert down.status_code == 200 and down.json()["healthy"] is False
        assert client.get("/showcase/feed").json()["healthy"] is False


def test_summary_is_public(isolated_showcase: str) -> None:
    with TestClient(app) as client:
        resp = client.get("/showcase/summary")
        assert resp.status_code == 200
        data = resp.json()
        assert "success_rate" in data and "dlq_depth" in data and "totals" in data


def test_receiver_404_for_unknown_endpoint(isolated_showcase: str) -> None:
    with TestClient(app) as client:
        assert client.post(f"/showcase/receiver/{uuid.uuid4()}", content=b"{}").status_code == 404


def test_receiver_401_on_bad_signature_and_records_it(
    isolated_showcase: str, db_engine: Engine
) -> None:
    with TestClient(app) as client:
        client.get("/showcase/feed")  # trigger seeding
        _, endpoint_id, _secret = _receiver(db_engine)
        resp = client.post(
            f"/showcase/receiver/{endpoint_id}",
            content=b'{"type":"price.tick"}',
            headers={"X-Webhook-Signature": "t=1,v1=deadbeef", "X-Webhook-Attempt": "1"},
        )
        assert resp.status_code == 401
        inbox = client.get("/showcase/feed").json()["inbox"]
        assert len(inbox) == 1
        assert inbox[0]["verified"] is False
        assert inbox[0]["response_status"] == 401


def test_receiver_200_when_healthy_503_when_down(isolated_showcase: str, db_engine: Engine) -> None:
    with TestClient(app) as client:
        client.get("/showcase/feed")  # trigger seeding
    _, endpoint_id, secret = _receiver(db_engine)
    body = b'{"type":"price.tick","payload":{}}'

    with TestClient(app) as client:
        ok = client.post(
            f"/showcase/receiver/{endpoint_id}", content=body, headers=_signed_headers(secret, body)
        )
        assert ok.status_code == 200

    _set_health(db_engine, endpoint_id, False)
    with TestClient(app) as client:
        down = client.post(
            f"/showcase/receiver/{endpoint_id}", content=body, headers=_signed_headers(secret, body)
        )
        assert down.status_code == 503


def test_dead_letter_requires_pipeline_down(isolated_showcase: str) -> None:
    with TestClient(app) as client:
        # Healthy by default → refuses to fabricate a dead-letter.
        assert client.post("/showcase/dead-letter").status_code == 409


def test_dead_letter_then_redrive_recovers(isolated_showcase: str, db_engine: Engine) -> None:
    with TestClient(app, raise_server_exceptions=True) as inner_client:

        def _override_http_client() -> Generator[httpx.Client, None, None]:
            yield inner_client

        app.dependency_overrides[get_simulate_http_client] = _override_http_client
        try:
            with TestClient(app) as client:
                client.get("/showcase/feed")  # trigger seeding
                client.post("/showcase/health", json={"healthy": False})
                dead = client.post("/showcase/dead-letter").json()
                dead_id = dead["delivery_id"]
                assert dead_id is not None and dead["healthy"] is False

                # Bring the pipeline back up and redrive (latest dead-letter).
                client.post("/showcase/health", json={"healthy": True})
                redrive = client.post("/showcase/redrive", json={})
                assert redrive.status_code == 200
                assert redrive.json()["status"] == "pending"
                assert redrive.json()["delivery_id"] == dead_id
        finally:
            app.dependency_overrides.pop(get_simulate_http_client, None)

    # Let the worker reprocess the redriven delivery against a healthy receiver.
    with Session(db_engine) as session:
        endpoint_id = session.execute(
            select(Delivery.endpoint_id).where(Delivery.id == uuid.UUID(dead_id))
        ).scalar_one()
    with TestClient(app) as worker_client:
        _process_pending(db_engine, endpoint_id, worker_client)

    with Session(db_engine) as session:
        final = session.get(Delivery, uuid.UUID(dead_id))
        assert final is not None and final.status == DeliveryStatus.succeeded
