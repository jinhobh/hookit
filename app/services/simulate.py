"""Live simulation: publish a demo batch and fast-forward one delivery to dead-lettered.

Backs the dashboard's "Simulate load" button. Everything here reuses real,
unmodified production code paths (``ingest_event``, ``process_delivery``,
HMAC signing) so a visitor watching the dashboard sees the actual reliability
engine at work, not a scripted animation. The one deliberate shortcut: the
"always fails" delivery is driven through its attempts back-to-back instead
of waiting for real exponential backoff between them (base=10s, cap=1h would
otherwise take ~5 minutes to exhaust) — see ``_fast_forward_to_dead_letter``.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

import httpx
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, selectinload

from app.core.config import Settings, get_settings
from app.models.delivery import Delivery, DeliveryStatus
from app.models.endpoint import Endpoint, EndpointStatus, PayloadFormat
from app.models.project import Project
from app.services.crypto import encrypt_secret, generate_endpoint_secret
from app.services.event_ingestion import ingest_event
from app.worker.delivery_worker import process_delivery

logger = logging.getLogger(__name__)

# Reserved event type for the demo endpoint; app/schemas/endpoint.py rejects
# this prefix on real, user-registered endpoints so it can never collide.
SIMULATE_EVENT_TYPE = "__simulate__"

# Advisory lock namespace for find_or_create_demo_endpoint; arbitrary, only
# ever used by this module.
_ADVISORY_LOCK_NAMESPACE = 87_412_501

# 9 succeed immediately, 2 fail once then succeed on a real (~10s) retry, 1 is
# the "redrive me" sentinel (0): it fails through every fast-forwarded attempt
# and gets dead-lettered, but its *effective* fail_until_attempt is set to
# exactly max_delivery_attempts + 1 (see run_simulation) so that redriving it
# lands on an attempt_number that finally clears the threshold and succeeds —
# redrive does not reset attempt_count, so without this the redriven delivery
# would just fail once more and immediately dead-letter again. Fixed batch
# size, not a request parameter, to bound the cost of each simulate call.
_BATCH_SPEC: tuple[int, ...] = (1, 1, 1, 1, 1, 1, 1, 1, 1, 2, 2, 0)
_REDRIVE_SENTINEL = 0


@dataclass(frozen=True)
class SimulationResult:
    """Summary returned to the router after a simulate run."""

    endpoint_id: uuid.UUID
    queued_events: int
    queued_deliveries: int
    dead_lettered_delivery_id: uuid.UUID | None


def find_or_create_demo_endpoint(
    session: Session, project: Project, public_base_url: str
) -> Endpoint:
    """Find or create the reserved demo ``Endpoint`` for *project*.

    A transaction-scoped Postgres advisory lock keyed by project id guards the
    check-then-insert: two concurrent first clicks of "Simulate load" for the
    same project must not both create a duplicate demo endpoint. There is no
    unique constraint to catch that race after the fact (unlike
    ``ingest_event``'s idempotency-key savepoint trick), so the lock is the
    only guard. It releases automatically at the caller's next commit or
    rollback.
    """
    session.execute(
        text("SELECT pg_advisory_xact_lock(:ns, hashtext(:key))"),
        {"ns": _ADVISORY_LOCK_NAMESPACE, "key": str(project.id)},
    )
    existing = session.execute(
        select(Endpoint).where(
            Endpoint.project_id == project.id,
            Endpoint.event_types.contains([SIMULATE_EVENT_TYPE]),
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
        event_types=[SIMULATE_EVENT_TYPE],
        secret_enc=encrypt_secret(generate_endpoint_secret()),
        status=EndpointStatus.active,
        payload_format=PayloadFormat.raw,
        rate_limit_rps=None,
    )
    session.add(endpoint)
    session.flush()
    return endpoint


def run_simulation(
    *, session: Session, project: Project, http_client: httpx.Client
) -> SimulationResult:
    """Publish the demo batch and fast-forward the "always fails" delivery.

    Two commits, deliberately (a deviation from the usual "router commits
    once" convention — see the module docstring and phase comments below):

    Phase 1 creates the demo endpoint (if needed) and ingests the whole batch,
    then commits. This commit is required, not stylistic: the
    ``/simulate/receiver`` route and the real, out-of-process worker each
    resolve their own DB session/connection, so under Postgres' default
    READ COMMITTED isolation neither would see these rows until they're
    committed.

    Phase 2 fast-forwards the one guaranteed-to-fail delivery to
    dead_lettered and commits again.
    """
    settings = get_settings()
    endpoint = find_or_create_demo_endpoint(session, project, settings.public_base_url)

    # One attempt past the dead-letter threshold: fails every fast-forwarded
    # attempt (all <= max_delivery_attempts), but succeeds the moment it's
    # redriven (attempt_number == max_delivery_attempts + 1).
    redrive_fail_until = settings.max_delivery_attempts + 1

    fail_hard_event_id: uuid.UUID | None = None
    queued_deliveries = 0
    for fail_until in _BATCH_SPEC:
        is_redrive_candidate = fail_until == _REDRIVE_SENTINEL
        event_id, count = ingest_event(
            session=session,
            project_id=project.id,
            event_type=SIMULATE_EVENT_TYPE,
            payload={
                "fail_until_attempt": redrive_fail_until if is_redrive_candidate else fail_until
            },
            idempotency_key=None,
        )
        queued_deliveries += count
        if is_redrive_candidate:
            fail_hard_event_id = event_id
    session.commit()  # phase-1 boundary: rows now visible cross-connection

    dead_lettered_delivery_id: uuid.UUID | None = None
    if fail_hard_event_id is not None:
        dead_lettered_delivery_id = _fast_forward_to_dead_letter(
            session, http_client, endpoint.id, fail_hard_event_id, settings
        )

    return SimulationResult(
        endpoint_id=endpoint.id,
        queued_events=len(_BATCH_SPEC),
        queued_deliveries=queued_deliveries,
        dead_lettered_delivery_id=dead_lettered_delivery_id,
    )


def _fast_forward_to_dead_letter(
    session: Session,
    http_client: httpx.Client,
    endpoint_id: uuid.UUID,
    event_id: uuid.UUID,
    settings: Settings,
) -> uuid.UUID | None:
    """Drive the "always fails" delivery straight to dead_lettered.

    Calls the unmodified production ``process_delivery()`` back-to-back with
    no wait on ``next_attempt_at`` — real HMAC-signed HTTP calls, real
    ``DeliveryAttempt`` rows, real dead-lettering logic; only the wait
    *between* attempts is skipped.

    Loads the target delivery with a *blocking* ``SELECT ... FOR UPDATE``
    (not ``SKIP LOCKED`` like the worker's batch claim — this path wants that
    one specific row, not "any due row"), because the real worker process may
    be racing to claim the same row the instant phase 1 commits. Bounded by a
    short ``lock_timeout`` so a pathological stall fails gracefully instead of
    hanging the request; if the worker wins the race for an attempt or two
    before we acquire the lock, ``process_delivery`` picks up wherever
    ``attempt_count`` was left and still reaches dead_lettered correctly.
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
