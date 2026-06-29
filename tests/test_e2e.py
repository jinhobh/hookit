"""End-to-end integration test: publish -> worker deliver -> inspect.

Exercises the full chain in-process via the FastAPI TestClient and the
worker's run_once().  No external network access occurs — outbound HTTP is
intercepted by an in-process mock transport.

Requires a live Postgres instance (skipped automatically when unreachable).
"""

from __future__ import annotations

import hmac
import json
from collections.abc import Generator

import httpx
import pytest
from app.db.base import Base
from app.db.session import get_session
from app.main import app
from app.worker.delivery_worker import run_once
from app.worker.signing import sign_payload
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def e2e_db_session(db_engine: Engine) -> Generator[Session, None, None]:
    """Savepoint-isolated session shared between the TestClient and the worker."""
    Base.metadata.create_all(db_engine)
    connection = db_engine.connect()
    outer_tx = connection.begin()
    session = Session(connection, join_transaction_mode="create_savepoint")
    yield session
    session.close()
    outer_tx.rollback()
    connection.close()


@pytest.fixture()
def e2e_client(e2e_db_session: Session) -> Generator[TestClient, None, None]:
    """FastAPI TestClient wired to the shared e2e session."""

    def override() -> Generator[Session, None, None]:
        yield e2e_db_session

    app.dependency_overrides[get_session] = override
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c
    app.dependency_overrides.pop(get_session, None)


class _MockTransport(httpx.BaseTransport):
    """Records outbound requests and returns a canned 200 response."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(200, text='{"ok":true}')


# ---------------------------------------------------------------------------
# End-to-end test
# ---------------------------------------------------------------------------


def test_happy_path_publish_deliver_inspect(
    e2e_client: TestClient,
    e2e_db_session: Session,
) -> None:
    """Full publish -> worker deliver -> inspect flow, entirely in-process."""

    # 1. Create a project and API key
    proj_resp = e2e_client.post("/projects", json={"name": "e2e-test-project"})
    assert proj_resp.status_code == 201
    project_id = proj_resp.json()["id"]

    key_resp = e2e_client.post(f"/projects/{project_id}/api-keys", json={"name": "e2e-key"})
    assert key_resp.status_code == 201
    api_key = key_resp.json()["key"]
    auth = {"Authorization": f"Bearer {api_key}"}

    # 2. Register an endpoint and capture the signing secret (returned once)
    ep_resp = e2e_client.post(
        "/endpoints",
        json={"url": "http://receiver.test/hook", "event_types": ["order.created"]},
        headers=auth,
    )
    assert ep_resp.status_code == 201
    ep_data = ep_resp.json()
    signing_secret: str = ep_data["secret"]

    # 3. Publish an event with an Idempotency-Key; assert one delivery queued
    event_resp = e2e_client.post(
        "/events",
        json={"type": "order.created", "payload": {"order_id": "e2e-001"}},
        headers={**auth, "Idempotency-Key": "e2e-idem-key-001"},
    )
    assert event_resp.status_code == 201
    event_data = event_resp.json()
    event_id: str = event_data["event_id"]
    assert event_data["queued_deliveries"] == 1

    # 4. Run the worker once using a mock transport that returns 200
    transport = _MockTransport()
    with httpx.Client(transport=transport) as http_client:
        count = run_once(e2e_db_session, http_client)

    assert count == 1
    assert len(transport.requests) == 1

    # 5. Verify the outbound request body contains the expected event data
    req = transport.requests[0]
    body = json.loads(req.content)
    assert body["event_id"] == event_id
    assert body["type"] == "order.created"
    assert body["payload"] == {"order_id": "e2e-001"}

    # 6. Verify the HMAC-SHA256 signature using constant-time comparison
    sig_header = req.headers["x-webhook-signature"]
    ts_header = req.headers["x-webhook-timestamp"]
    parts = dict(p.split("=", 1) for p in sig_header.split(","))
    ts = int(parts["t"])
    assert str(ts) == ts_header
    expected_sig = sign_payload(signing_secret, ts, req.content)
    assert hmac.compare_digest(parts["v1"], expected_sig)

    # 7. GET /deliveries — one delivery with status=succeeded
    dl_resp = e2e_client.get("/deliveries", headers=auth)
    assert dl_resp.status_code == 200
    dl_data = dl_resp.json()
    assert len(dl_data["items"]) == 1
    delivery = dl_data["items"][0]
    assert delivery["status"] == "succeeded"
    delivery_id: str = delivery["id"]

    # 8. GET /deliveries/{id}/attempts — one attempt with response_status=200
    att_resp = e2e_client.get(f"/deliveries/{delivery_id}/attempts", headers=auth)
    assert att_resp.status_code == 200
    attempts = att_resp.json()
    assert len(attempts) == 1
    assert attempts[0]["response_status"] == 200
