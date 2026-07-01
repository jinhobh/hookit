"""Router for the interactive dashboard demo ("Ops Console").

Authenticated, project-scoped control routes power the dashboard's demo panel:

- ``POST /simulate/events``     — emit realistic GitHub/CI events.
- ``POST /simulate/health``     — take the demo "deploy pipeline" up or down.
- ``GET  /simulate/inbox``      — current health + the received-request tail.
- ``POST /simulate/dead-letter``— fast-forward one delivery to the DLQ (requires
  the pipeline to be down) so redrive recovery is watchable without a ~5 min wait.

Plus the public, unauthenticated ``POST /simulate/receiver/{endpoint_id}`` — the
self-referential receiver those deliveries are sent to. See
``app/services/simulate.py`` for the mechanics.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Generator

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_project
from app.core.config import get_settings
from app.db.session import get_session
from app.models.endpoint import Endpoint
from app.models.project import Project
from app.schemas.simulate import (
    DeadLetterResponse,
    EmitRequest,
    EmitResponse,
    HealthRequest,
    HealthResponse,
    InboxResponse,
    ReceivedRequestItem,
)
from app.services.crypto import decrypt_secret
from app.services.simulate import (
    DEMO_MARKER,
    emit_and_dead_letter,
    emit_demo_events,
    find_or_create_demo_endpoint,
    get_health,
    list_inbox,
    record_received_request,
    set_health,
)
from app.worker.signing import verify_signature

router = APIRouter(prefix="/simulate", tags=["simulate"])


def get_simulate_http_client() -> Generator[httpx.Client, None, None]:
    """Real, socket-based ``httpx.Client`` for the dead-letter fast-forward call.

    Injected as a dependency (rather than constructed ad hoc in the service) so
    tests can override it with an in-process ``TestClient`` instead of needing a
    live bound port.
    """
    settings = get_settings()
    with httpx.Client(timeout=settings.delivery_timeout_seconds) as client:
        yield client


@router.post("/events", response_model=EmitResponse)
def emit_events(
    body: EmitRequest,
    project: Project = Depends(get_current_project),
    session: Session = Depends(get_session),
) -> EmitResponse:
    """Publish one or more realistic demo events through the real pipeline."""
    result = emit_demo_events(
        session=session,
        project=project,
        public_base_url=get_settings().public_base_url,
        event_type=body.event_type,
        count=body.count,
    )
    session.commit()
    return EmitResponse(
        endpoint_id=result.endpoint_id,
        queued_events=result.queued_events,
        queued_deliveries=result.queued_deliveries,
        event_type=result.event_type,
        sample_payload=result.sample_payload,
    )


@router.post("/health", response_model=HealthResponse)
def set_receiver_health(
    body: HealthRequest,
    project: Project = Depends(get_current_project),
    session: Session = Depends(get_session),
) -> HealthResponse:
    """Take the demo "deploy pipeline" up (200) or down (503)."""
    endpoint = find_or_create_demo_endpoint(session, project, get_settings().public_base_url)
    set_health(session, endpoint.id, body.healthy)
    session.commit()
    return HealthResponse(endpoint_id=endpoint.id, healthy=body.healthy)


@router.get("/inbox", response_model=InboxResponse)
def get_inbox(
    project: Project = Depends(get_current_project),
    session: Session = Depends(get_session),
) -> InboxResponse:
    """Return the demo receiver's current health and its received-request tail."""
    endpoint = find_or_create_demo_endpoint(session, project, get_settings().public_base_url)
    healthy = get_health(session, endpoint.id)
    items = [ReceivedRequestItem.model_validate(row) for row in list_inbox(session, endpoint.id)]
    session.commit()
    return InboxResponse(endpoint_id=endpoint.id, healthy=healthy, items=items)


@router.post("/dead-letter", response_model=DeadLetterResponse)
def force_dead_letter(
    project: Project = Depends(get_current_project),
    session: Session = Depends(get_session),
    http_client: httpx.Client = Depends(get_simulate_http_client),
) -> DeadLetterResponse:
    """Fast-forward one delivery to the dead-letter queue.

    Requires the demo pipeline to be *down* — that is what makes every attempt
    fail and reach the DLQ, and it means the delivery recovers naturally when
    the visitor brings the pipeline back up and redrives.
    """
    endpoint = find_or_create_demo_endpoint(session, project, get_settings().public_base_url)
    if get_health(session, endpoint.id):
        raise HTTPException(
            status_code=409,
            detail="Bring the pipeline down first, then force a dead-letter.",
        )
    delivery_id = emit_and_dead_letter(
        session=session, project=project, endpoint=endpoint, http_client=http_client
    )
    return DeadLetterResponse(delivery_id=delivery_id, healthy=False)


@router.post("/receiver/{endpoint_id}")
async def simulate_receiver(
    endpoint_id: uuid.UUID,
    request: Request,
    session: Session = Depends(get_session),
) -> JSONResponse:
    """Self-referential receiver for demo deliveries ("your deploy pipeline").

    Public and unauthenticated — it's a sink, not a data source: it can only
    accept a request and answer 200/401/404/503, never initiate one, and only
    ever looks up demo endpoints (tagged with the reserved ``__demo__`` marker),
    never a real customer endpoint. It verifies the HMAC signature like a real
    receiver would, records the request in the endpoint inbox, then answers
    200 when the pipeline is healthy or 503 when the visitor has taken it down.
    """
    endpoint = session.execute(
        select(Endpoint).where(
            Endpoint.id == endpoint_id,
            Endpoint.event_types.contains([DEMO_MARKER]),
        )
    ).scalar_one_or_none()
    if endpoint is None:
        raise HTTPException(status_code=404, detail="Unknown demo endpoint")

    body = await request.body()
    sig_header = request.headers.get("x-webhook-signature")
    ts_header = request.headers.get("x-webhook-timestamp")
    secret = decrypt_secret(endpoint.secret_enc)
    verified, reason = verify_signature(secret, sig_header, body)

    try:
        event_type = str(json.loads(body).get("type", "unknown"))
    except (json.JSONDecodeError, AttributeError, TypeError):
        event_type = "unknown"
    try:
        attempt = int(request.headers.get("x-webhook-attempt", "1"))
    except ValueError:
        attempt = 1

    healthy = get_health(session, endpoint_id)
    if not verified:
        response_status = 401
    elif healthy:
        response_status = 200
    else:
        response_status = 503

    record_received_request(
        session,
        endpoint_id=endpoint_id,
        event_type=event_type,
        attempt=attempt,
        verified=verified,
        response_status=response_status,
        signature_header=sig_header,
        timestamp_header=ts_header,
        body=body.decode("utf-8", errors="replace"),
    )
    session.commit()

    if not verified:
        return JSONResponse({"error": reason}, status_code=401)
    if not healthy:
        return JSONResponse({"status": "pipeline unavailable"}, status_code=503)
    return JSONResponse({"status": "accepted"}, status_code=200)
