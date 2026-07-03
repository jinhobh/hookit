"""Public router for the live showcase demo.

These routes are **unauthenticated but hard-scoped to the single seeded showcase
project** (see ``app.services.showcase``), so the static dashboard can drive them
with no API key while no real customer data is ever exposed:

- ``GET  /showcase/summary``     — delivery health metrics for the showcase project.
- ``GET  /showcase/feed``        — the live producer event feed + receiver inbox/health.
- ``GET  /showcase/deliveries``  — the receiver's recent deliveries with their full
  attempt history, lease state, and the live retry configuration (the dashboard's
  delivery lifecycle timeline).
- ``POST /showcase/health``      — take the controllable receiver up or down.
- ``POST /showcase/dead-letter`` — fast-forward one delivery to the DLQ (receiver
  must be down) so redrive recovery is watchable without a ~5 min backoff wait.
- ``POST /showcase/redrive``     — redrive a dead-lettered delivery back to pending.
- ``POST /showcase/burst``       — proxy a load-spike request to the producer.
- ``POST /showcase/duplicate``   — proxy to the producer: fire the same payload
  twice concurrently with one ``Idempotency-Key`` (a real race on the ingestion
  unique constraint) and return both responses.

Plus the public ``POST /showcase/receiver/{endpoint_id}`` — the controllable
receiver those tick deliveries are sent to.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Generator
from datetime import UTC, datetime

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.session import get_session
from app.models.delivery import DeliveryStatus
from app.models.endpoint import Endpoint
from app.schemas.metrics import MetricsSummaryResponse
from app.schemas.showcase import (
    BurstRequest,
    BurstResponse,
    DeadLetterResponse,
    DeliveriesResponse,
    DuplicateResponse,
    FeedEventItem,
    FeedResponse,
    HealthRequest,
    HealthResponse,
    ReceivedRequestItem,
    RedriveRequest,
    RedriveResponse,
    TimelineAttemptItem,
    TimelineDeliveryItem,
    WorkerStat,
)
from app.services.crypto import decrypt_secret
from app.services.metrics import delivery_summary
from app.services.showcase import (
    SHOWCASE_MARKER,
    ShowcaseHandles,
    emit_and_dead_letter,
    get_health,
    get_scoped_delivery,
    latest_dead_lettered_id,
    list_inbox,
    list_recent_deliveries,
    list_recent_events,
    record_received_request,
    resolve_showcase,
    seed_showcase,
    set_health,
    touch_visitor_seen,
)
from app.worker.signing import verify_signature

router = APIRouter(prefix="/showcase", tags=["showcase"])


def get_simulate_http_client() -> Generator[httpx.Client, None, None]:
    """Real, socket-based ``httpx.Client`` for the dead-letter fast-forward call.

    Injected as a dependency (rather than constructed ad hoc) so tests can
    override it with an in-process ``TestClient`` instead of a live bound port.
    """
    settings = get_settings()
    with httpx.Client(timeout=settings.delivery_timeout_seconds) as client:
        yield client


def get_showcase(session: Session = Depends(get_session)) -> ShowcaseHandles:
    """Resolve the seeded showcase, self-healing by seeding once if absent."""
    handles = resolve_showcase(session)
    if handles is None:
        handles = seed_showcase(session)
        session.commit()
    return handles


def record_visitor_activity(
    handles: ShowcaseHandles = Depends(get_showcase),
    session: Session = Depends(get_session),
) -> None:
    """Mark that a real dashboard visitor is active right now.

    Applied only to the GET routes the dashboard's own polling loop calls
    (never to producer-triggered or click-triggered POSTs) so the idle
    watchdog (``app.services.idle_watchdog``) can tell a visitor apart from
    the ``producer`` service's self-generated traffic. Commits on its own —
    some of the routes it's attached to (``summary``, ``deliveries``) are
    otherwise read-only and never commit themselves.
    """
    touch_visitor_seen(session, handles.project_id)
    session.commit()


@router.get("/summary", response_model=MetricsSummaryResponse)
def summary(
    handles: ShowcaseHandles = Depends(get_showcase),
    session: Session = Depends(get_session),
    _visitor: None = Depends(record_visitor_activity),
) -> MetricsSummaryResponse:
    """Aggregate delivery health for the showcase project (public read)."""
    return delivery_summary(session, handles.project_id)


@router.get("/feed", response_model=FeedResponse)
def feed(
    handles: ShowcaseHandles = Depends(get_showcase),
    session: Session = Depends(get_session),
    _visitor: None = Depends(record_visitor_activity),
) -> FeedResponse:
    """Return the live producer event feed, receiver inbox, and current health."""
    settings = get_settings()
    events = [
        FeedEventItem.model_validate(e) for e in list_recent_events(session, handles.project_id)
    ]
    inbox = [
        ReceivedRequestItem.model_validate(r)
        for r in list_inbox(session, handles.receiver_endpoint_id)
    ]
    healthy = get_health(session, handles.receiver_endpoint_id)
    session.commit()
    return FeedResponse(
        healthy=healthy,
        discord_enabled=handles.discord_endpoint_id is not None,
        discord_widget_server_id=settings.discord_widget_server_id or None,
        discord_widget_channel_id=settings.discord_widget_channel_id or None,
        events=events,
        inbox=inbox,
    )


@router.get("/deliveries", response_model=DeliveriesResponse)
def deliveries(
    handles: ShowcaseHandles = Depends(get_showcase),
    session: Session = Depends(get_session),
    _visitor: None = Depends(record_visitor_activity),
) -> DeliveriesResponse:
    """Return the receiver's recent deliveries with attempts + retry config.

    Public read backing the dashboard's delivery lifecycle timeline: real
    ``Delivery`` / ``DeliveryAttempt`` rows written by the production worker,
    plus the live retry settings and server clock so measured backoff gaps can
    be compared against the nominal ``min(base·2^(n−1), cap)`` schedule.
    """
    settings = get_settings()
    items = [
        TimelineDeliveryItem(
            id=d.id,
            event_id=d.event_id,
            event_type=d.event.type,
            status=d.status.value,
            attempt_count=d.attempt_count,
            next_attempt_at=d.next_attempt_at,
            leased_until=d.leased_until,
            claimed_by=d.claimed_by,
            created_at=d.created_at,
            attempts=[TimelineAttemptItem.model_validate(a) for a in d.attempts],
        )
        for d in list_recent_deliveries(session, handles.receiver_endpoint_id)
    ]
    attempts_by_worker: dict[str, int] = {}
    for item in items:
        for attempt in item.attempts:
            if attempt.worker_id is not None:
                attempts_by_worker[attempt.worker_id] = (
                    attempts_by_worker.get(attempt.worker_id, 0) + 1
                )
    workers = [
        WorkerStat(name=name, attempts=count)
        for name, count in sorted(attempts_by_worker.items(), key=lambda kv: (-kv[1], kv[0]))
    ]
    return DeliveriesResponse(
        server_time=datetime.now(UTC),
        retry_base_seconds=settings.retry_base_seconds,
        retry_cap_seconds=settings.retry_cap_seconds,
        max_delivery_attempts=settings.max_delivery_attempts,
        receiver_endpoint_id=handles.receiver_endpoint_id,
        workers=workers,
        deliveries=items,
    )


@router.post("/health", response_model=HealthResponse)
def set_receiver_health(
    body: HealthRequest,
    handles: ShowcaseHandles = Depends(get_showcase),
    session: Session = Depends(get_session),
) -> HealthResponse:
    """Take the controllable receiver ("your pipeline") up (200) or down (503)."""
    set_health(session, handles.receiver_endpoint_id, body.healthy)
    session.commit()
    return HealthResponse(receiver_endpoint_id=handles.receiver_endpoint_id, healthy=body.healthy)


@router.post("/dead-letter", response_model=DeadLetterResponse)
def force_dead_letter(
    handles: ShowcaseHandles = Depends(get_showcase),
    session: Session = Depends(get_session),
    http_client: httpx.Client = Depends(get_simulate_http_client),
) -> DeadLetterResponse:
    """Fast-forward one delivery to the dead-letter queue.

    Requires the receiver to be *down* — that is what makes every attempt fail
    and reach the DLQ, and it means the delivery recovers naturally when the
    visitor brings the pipeline back up and redrives.
    """
    if get_health(session, handles.receiver_endpoint_id):
        raise HTTPException(
            status_code=409,
            detail="Bring the pipeline down first, then force a dead-letter.",
        )
    receiver = session.get(Endpoint, handles.receiver_endpoint_id)
    project = receiver.project if receiver is not None else None
    if receiver is None or project is None:
        raise HTTPException(status_code=404, detail="Showcase receiver not found")
    delivery_id = emit_and_dead_letter(
        session=session, project=project, endpoint=receiver, http_client=http_client
    )
    return DeadLetterResponse(delivery_id=delivery_id, healthy=False)


@router.post("/redrive", response_model=RedriveResponse)
def redrive(
    body: RedriveRequest,
    handles: ShowcaseHandles = Depends(get_showcase),
    session: Session = Depends(get_session),
) -> RedriveResponse:
    """Redrive a dead-lettered showcase delivery back to pending.

    With no ``delivery_id`` it redrives the most recent dead-lettered delivery.
    Scoped to the showcase project — a foreign id is treated as not found.
    """
    delivery_id = body.delivery_id or latest_dead_lettered_id(session, handles.project_id)
    if delivery_id is None:
        return RedriveResponse(delivery_id=None, status=None)

    delivery = get_scoped_delivery(session, handles.project_id, delivery_id)
    if delivery is None:
        raise HTTPException(status_code=404, detail="Delivery not found")
    if delivery.status != DeliveryStatus.dead_lettered:
        raise HTTPException(status_code=409, detail="Delivery is not dead-lettered")

    delivery.status = DeliveryStatus.pending
    delivery.next_attempt_at = datetime.now(UTC)
    delivery.leased_until = None
    session.commit()
    session.refresh(delivery)
    return RedriveResponse(delivery_id=delivery.id, status=delivery.status.value)


@router.post("/burst", response_model=BurstResponse)
def burst(body: BurstRequest | None = None) -> BurstResponse:
    """Proxy a load-spike request to the producer's control server.

    Keeps the producer private (no public port needed) and gives the dashboard a
    single same-origin surface. With ``same_account=true`` the producer fires
    concurrent ``trade.executed`` events against one account (the ledger demo's
    two-writers scenario) instead of a tick spike. Returns 0 published if the
    producer is unreachable rather than failing the request.
    """
    settings = get_settings()
    url = f"{settings.producer_base_url.rstrip('/')}/burst"
    try:
        with httpx.Client(timeout=settings.delivery_timeout_seconds) as client:
            resp = client.post(url, json={"same_account": body.same_account if body else False})
            resp.raise_for_status()
            published = int(resp.json().get("published", 0))
    except (httpx.HTTPError, ValueError, TypeError):
        published = 0
    return BurstResponse(published=published)


@router.post("/duplicate", response_model=DuplicateResponse)
def duplicate() -> DuplicateResponse:
    """Proxy an idempotency-race request to the producer's control server.

    The producer fires the same payload twice **concurrently** with one
    ``Idempotency-Key``; both responses come back so the dashboard can show
    they carry the same ``event_id`` and only one delivery was created.
    Returns empty ``results`` if the producer is unreachable, mirroring
    ``POST /showcase/burst``.
    """
    settings = get_settings()
    url = f"{settings.producer_base_url.rstrip('/')}/duplicate"
    try:
        with httpx.Client(timeout=settings.delivery_timeout_seconds) as client:
            resp = client.post(url)
            resp.raise_for_status()
            return DuplicateResponse.model_validate(resp.json())
    except (httpx.HTTPError, ValueError):
        return DuplicateResponse()


@router.post("/receiver/{endpoint_id}")
async def showcase_receiver(
    endpoint_id: uuid.UUID,
    request: Request,
    session: Session = Depends(get_session),
) -> JSONResponse:
    """Controllable receiver for showcase tick deliveries ("your pipeline").

    Public and unauthenticated — a sink, not a data source: it can only accept a
    request and answer 200/401/404/503, never initiate one, and only ever looks
    up the showcase receiver (tagged with the reserved ``__showcase__`` marker),
    never a real customer endpoint. It verifies the HMAC signature like a real
    receiver, records the request in the inbox, then answers 200 when healthy or
    503 when the visitor has taken it down.
    """
    endpoint = session.execute(
        select(Endpoint).where(
            Endpoint.id == endpoint_id,
            Endpoint.event_types.contains([SHOWCASE_MARKER]),
        )
    ).scalar_one_or_none()
    if endpoint is None:
        raise HTTPException(status_code=404, detail="Unknown showcase endpoint")

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
