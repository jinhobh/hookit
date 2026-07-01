"""Router for the live simulation ("Simulate load") dashboard feature.

Two routes: ``POST /simulate/run`` (authenticated, project-scoped) kicks off a
demo batch; ``POST /simulate/receiver/{endpoint_id}`` is the self-referential
flaky receiver those deliveries are sent to. See ``app/services/simulate.py``
for the mechanics.
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
from app.schemas.simulate import SimulateRunResponse
from app.services.crypto import decrypt_secret
from app.services.simulate import SIMULATE_EVENT_TYPE, run_simulation
from app.worker.signing import verify_signature

router = APIRouter(prefix="/simulate", tags=["simulate"])


def get_simulate_http_client() -> Generator[httpx.Client, None, None]:
    """Real, socket-based ``httpx.Client`` for the fast-forward self-call.

    Injected as a dependency (rather than constructed ad hoc in the service)
    so tests can override it with an in-process ``TestClient`` instead of
    needing a live bound port.
    """
    settings = get_settings()
    with httpx.Client(timeout=settings.delivery_timeout_seconds) as client:
        yield client


@router.post("/run", response_model=SimulateRunResponse)
def run_simulate(
    project: Project = Depends(get_current_project),
    session: Session = Depends(get_session),
    http_client: httpx.Client = Depends(get_simulate_http_client),
) -> SimulateRunResponse:
    """Publish a demo batch and fast-forward one delivery to dead_lettered.

    Powers the dashboard's "Simulate load" button: most events succeed
    immediately, a couple genuinely retry with real backoff, and one is
    fast-forwarded to dead_lettered so it can be redriven from the dashboard.
    """
    result = run_simulation(session=session, project=project, http_client=http_client)
    return SimulateRunResponse(
        endpoint_id=result.endpoint_id,
        queued_events=result.queued_events,
        queued_deliveries=result.queued_deliveries,
        dead_lettered_delivery_id=result.dead_lettered_delivery_id,
    )


@router.post("/receiver/{endpoint_id}")
async def simulate_receiver(
    endpoint_id: uuid.UUID,
    request: Request,
    session: Session = Depends(get_session),
) -> JSONResponse:
    """Self-referential flaky receiver for simulated deliveries.

    Public, unauthenticated — it's a sink, not a data source: it can only
    accept a request and answer 200/401/404/500, never initiate one, and only
    ever looks up demo endpoints (``event_types`` tagged with the reserved
    ``__simulate__`` marker), never a real customer endpoint. Verifies the
    HMAC signature like a real receiver would, then decides pass/fail from the
    request's own ``X-Webhook-Attempt`` header and
    ``payload.fail_until_attempt`` — no server-side state needed.
    """
    endpoint = session.execute(
        select(Endpoint).where(
            Endpoint.id == endpoint_id,
            Endpoint.event_types.contains([SIMULATE_EVENT_TYPE]),
        )
    ).scalar_one_or_none()
    if endpoint is None:
        raise HTTPException(status_code=404, detail="Unknown simulate endpoint")

    body = await request.body()
    sig_header = request.headers.get("x-webhook-signature")
    secret = decrypt_secret(endpoint.secret_enc)
    ok, reason = verify_signature(secret, sig_header, body)
    if not ok:
        return JSONResponse({"error": reason}, status_code=401)

    try:
        fail_until_attempt = int(json.loads(body).get("payload", {}).get("fail_until_attempt", 1))
    except (json.JSONDecodeError, TypeError, ValueError, AttributeError):
        fail_until_attempt = 1
    try:
        attempt_number = int(request.headers.get("x-webhook-attempt", "1"))
    except ValueError:
        attempt_number = 1

    if attempt_number >= fail_until_attempt:
        return JSONResponse({"status": "accepted"}, status_code=200)
    return JSONResponse({"status": "simulated failure"}, status_code=500)
