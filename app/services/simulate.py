"""Backend for the interactive dashboard demo ("Ops Console").

Everything here runs the real, unmodified production paths — ``ingest_event``,
``process_delivery``, HMAC signing — so a visitor watching the dashboard sees
the actual reliability engine at work, not a scripted animation. The demo lets
a visitor:

- **emit** realistic GitHub/CI events (``push`` / ``pull_request`` /
  ``workflow_run``) that fan out to a reserved, self-referential demo endpoint
  standing in for their "deploy pipeline";
- **toggle that endpoint's health** (``DemoReceiverHealth``) to take the
  downstream up or down and watch retries, backoff, and dead-lettering happen
  on real data, then redrive to recover;
- **inspect the inbox** (``DemoReceivedRequest``) of requests that actually
  arrived, with their real signed headers and bodies.

The one deliberate shortcut is ``_fast_forward_to_dead_letter``: it drives the
"always fails" delivery through its attempts back-to-back instead of waiting for
real exponential backoff between them (base=10s, cap=1h would otherwise take
~5 minutes to exhaust), so the dead-letter → redrive story is watchable.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any

import httpx
from sqlalchemy import delete, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, selectinload

from app.core.config import Settings, get_settings
from app.models.delivery import Delivery, DeliveryStatus
from app.models.demo import DemoReceivedRequest, DemoReceiverHealth
from app.models.endpoint import Endpoint, EndpointStatus, PayloadFormat
from app.models.project import Project
from app.services.crypto import encrypt_secret, generate_endpoint_secret
from app.services.demo_events import DEMO_EVENT_TYPES, build_demo_event
from app.services.event_ingestion import ingest_event
from app.worker.delivery_worker import process_delivery

logger = logging.getLogger(__name__)

# Reserved marker carried in the demo endpoint's event_types. app/schemas/
# endpoint.py rejects any ``__``-prefixed event_type on real, user-registered
# endpoints, so this can never collide with a customer endpoint — and the
# receiver route uses it to be certain it only ever serves demo endpoints.
DEMO_MARKER = "__demo__"

# Advisory lock namespace for find_or_create_demo_endpoint; arbitrary, only ever
# used by this module.
_ADVISORY_LOCK_NAMESPACE = 87_412_501

# How many received requests to retain per demo endpoint (the inbox is a live
# tail, not an audit log).
_INBOX_KEEP = 20

# Cap the stored request body so the demo inbox can't grow unbounded.
_MAX_BODY_CHARS = 8_192


@dataclass(frozen=True)
class EmitResult:
    """Summary returned after emitting one or more demo events."""

    endpoint_id: uuid.UUID
    queued_events: int
    queued_deliveries: int
    event_type: str
    sample_payload: dict[str, Any]


# ---------------------------------------------------------------------------
# Demo endpoint + health
# ---------------------------------------------------------------------------


def find_or_create_demo_endpoint(
    session: Session, project: Project, public_base_url: str
) -> Endpoint:
    """Find or create the reserved demo ``Endpoint`` for *project*.

    A transaction-scoped Postgres advisory lock keyed by project id guards the
    check-then-insert: two concurrent first actions for the same project must
    not both create a duplicate demo endpoint. There is no unique constraint to
    catch that race after the fact, so the lock is the only guard. It releases
    automatically at the caller's next commit or rollback.

    The endpoint subscribes to the reserved :data:`DEMO_MARKER` (used to
    identify it) plus the real demo event types, so ordinary event fan-out
    routes ``push`` / ``pull_request`` / ``workflow_run`` events to it.
    """
    session.execute(
        text("SELECT pg_advisory_xact_lock(:ns, hashtext(:key))"),
        {"ns": _ADVISORY_LOCK_NAMESPACE, "key": str(project.id)},
    )
    existing = session.execute(
        select(Endpoint).where(
            Endpoint.project_id == project.id,
            Endpoint.event_types.contains([DEMO_MARKER]),
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    endpoint_id = uuid.uuid4()
    endpoint = Endpoint(
        id=endpoint_id,
        project_id=project.id,
        # Server-constructed URL, not attacker input — built directly rather
        # than routed through POST /endpoints' SSRF-validated path.
        url=f"{public_base_url.rstrip('/')}/simulate/receiver/{endpoint_id}",
        event_types=[DEMO_MARKER, *DEMO_EVENT_TYPES],
        secret_enc=encrypt_secret(generate_endpoint_secret()),
        status=EndpointStatus.active,
        payload_format=PayloadFormat.raw,
        rate_limit_rps=None,
    )
    session.add(endpoint)
    # Flush the endpoint before the health row: the two are linked only by a
    # foreign key (no ORM relationship), so the unit of work won't order the
    # inserts for us, and demo_receiver_health.endpoint_id references it.
    session.flush()
    session.add(DemoReceiverHealth(endpoint_id=endpoint_id, healthy=True))
    session.flush()
    return endpoint


def get_health(session: Session, endpoint_id: uuid.UUID) -> bool:
    """Return the demo receiver's current health (defaults healthy)."""
    row = session.get(DemoReceiverHealth, endpoint_id)
    return row.healthy if row is not None else True


def set_health(session: Session, endpoint_id: uuid.UUID, healthy: bool) -> None:
    """Upsert the demo receiver's health flag."""
    row = session.get(DemoReceiverHealth, endpoint_id)
    if row is None:
        session.add(DemoReceiverHealth(endpoint_id=endpoint_id, healthy=healthy))
    else:
        row.healthy = healthy
    session.flush()


# ---------------------------------------------------------------------------
# Emitting events
# ---------------------------------------------------------------------------


def emit_demo_events(
    *,
    session: Session,
    project: Project,
    public_base_url: str,
    event_type: str | None = None,
    count: int = 1,
) -> EmitResult:
    """Publish *count* realistic demo events through the real ingestion path.

    Each event fans out to the demo endpoint, producing a real delivery the
    worker will sign and POST. The caller commits.
    """
    endpoint = find_or_create_demo_endpoint(session, project, public_base_url)

    queued_deliveries = 0
    last_type = event_type or DEMO_EVENT_TYPES[0]
    last_payload: dict[str, Any] = {}
    for _ in range(count):
        etype, payload = build_demo_event(event_type)
        _event_id, queued = ingest_event(
            session=session,
            project_id=project.id,
            event_type=etype,
            payload=payload,
            idempotency_key=None,
        )
        queued_deliveries += queued
        last_type, last_payload = etype, payload

    session.flush()
    return EmitResult(
        endpoint_id=endpoint.id,
        queued_events=count,
        queued_deliveries=queued_deliveries,
        event_type=last_type,
        sample_payload=last_payload,
    )


# ---------------------------------------------------------------------------
# Receiver inbox
# ---------------------------------------------------------------------------


def record_received_request(
    session: Session,
    *,
    endpoint_id: uuid.UUID,
    event_type: str,
    attempt: int,
    verified: bool,
    response_status: int,
    signature_header: str | None,
    timestamp_header: str | None,
    body: str,
) -> None:
    """Append one received request to the endpoint inbox, pruning old rows.

    Keeps only the most recent :data:`_INBOX_KEEP` rows per endpoint so the
    demo inbox stays a bounded live tail. The caller commits.
    """
    session.add(
        DemoReceivedRequest(
            endpoint_id=endpoint_id,
            event_type=event_type,
            attempt=attempt,
            verified=verified,
            response_status=response_status,
            signature_header=signature_header,
            timestamp_header=timestamp_header,
            body=body[:_MAX_BODY_CHARS],
        )
    )
    session.flush()

    stale_ids = list(
        session.execute(
            select(DemoReceivedRequest.id)
            .where(DemoReceivedRequest.endpoint_id == endpoint_id)
            .order_by(DemoReceivedRequest.received_at.desc(), DemoReceivedRequest.id.desc())
            .offset(_INBOX_KEEP)
        ).scalars()
    )
    if stale_ids:
        session.execute(delete(DemoReceivedRequest).where(DemoReceivedRequest.id.in_(stale_ids)))


def list_inbox(
    session: Session, endpoint_id: uuid.UUID, limit: int = _INBOX_KEEP
) -> list[DemoReceivedRequest]:
    """Return the most recent received requests for a demo endpoint, newest first."""
    return list(
        session.execute(
            select(DemoReceivedRequest)
            .where(DemoReceivedRequest.endpoint_id == endpoint_id)
            .order_by(DemoReceivedRequest.received_at.desc(), DemoReceivedRequest.id.desc())
            .limit(limit)
        ).scalars()
    )


# ---------------------------------------------------------------------------
# Dead-letter fast path
# ---------------------------------------------------------------------------


def emit_and_dead_letter(
    *,
    session: Session,
    project: Project,
    endpoint: Endpoint,
    http_client: httpx.Client,
) -> uuid.UUID | None:
    """Emit one event and drive it straight to dead_lettered.

    The caller must have verified the receiver is currently unhealthy — that is
    what makes every fast-forwarded attempt fail (503) and reach the dead-letter
    queue. Because the receiver is *down* rather than permanently broken, the
    resulting dead-lettered delivery recovers naturally on redrive once the
    visitor brings the pipeline back up (no attempt-count seeding tricks).

    Two commits, deliberately: phase 1 makes the new event/delivery visible to
    the receiver route and worker (which resolve their own connections under
    READ COMMITTED); phase 2 persists the fast-forwarded attempts.
    """
    settings = get_settings()
    event_type, payload = build_demo_event()
    event_id, _ = ingest_event(
        session=session,
        project_id=project.id,
        event_type=event_type,
        payload=payload,
        idempotency_key=None,
    )
    session.commit()  # phase-1 boundary: rows now visible cross-connection

    return _fast_forward_to_dead_letter(session, http_client, endpoint.id, event_id, settings)


def _fast_forward_to_dead_letter(
    session: Session,
    http_client: httpx.Client,
    endpoint_id: uuid.UUID,
    event_id: uuid.UUID,
    settings: Settings,
) -> uuid.UUID | None:
    """Drive the "always fails" delivery straight to dead_lettered.

    Calls the unmodified production ``process_delivery()`` back-to-back with no
    wait on ``next_attempt_at`` — real HMAC-signed HTTP calls, real
    ``DeliveryAttempt`` rows, real dead-lettering logic; only the wait *between*
    attempts is skipped.

    Loads the target delivery with a *blocking* ``SELECT ... FOR UPDATE`` (not
    ``SKIP LOCKED`` like the worker's batch claim — this path wants that one
    specific row), because the real worker may be racing to claim the same row
    the instant phase 1 commits. Bounded by a short ``lock_timeout`` so a
    pathological stall fails gracefully instead of hanging the request; if the
    worker wins the race for an attempt or two before we acquire the lock,
    ``process_delivery`` picks up wherever ``attempt_count`` was left and still
    reaches dead_lettered correctly.
    """
    try:
        session.execute(text("SET LOCAL lock_timeout = '5s'"))
        delivery = session.execute(
            select(Delivery)
            .options(selectinload(Delivery.endpoint), selectinload(Delivery.event))
            .where(Delivery.event_id == event_id, Delivery.endpoint_id == endpoint_id)
            .with_for_update()
        ).scalar_one_or_none()
    except OperationalError:
        session.rollback()
        logger.warning("simulate: could not lock target delivery within 5s; skipping fast-forward")
        return None

    if delivery is None:
        return None

    for _ in range(settings.max_delivery_attempts):
        if delivery.status == DeliveryStatus.dead_lettered:
            break
        process_delivery(delivery, session, http_client)

    session.commit()  # phase-2 boundary
    return delivery.id if delivery.status == DeliveryStatus.dead_lettered else None
