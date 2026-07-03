"""Backend for the live showcase demo (real producer → real Discord).

The dashboard's demo is fed by the *separate* ``producer`` service, which POSTs
real crypto ``price.tick`` / ``price.alert`` events to the platform's public
``POST /events`` API. This module owns the platform side of that showcase:

- **seeding** one shared, stable "showcase" project with two endpoints — a real
  **Discord** endpoint (``payload_format=discord``) that receives ``price.alert``
  events so meaningful moves land in a real Discord channel, and a **controllable
  receiver** endpoint that receives every ``price.tick`` (and alert) and can be
  toggled down to demonstrate retries → backoff → dead-letter → redrive;
- the **receiver** side of that controllable endpoint: HMAC verification, the
  received-request inbox, and health toggling (``DemoReceiverHealth`` /
  ``DemoReceivedRequest``, unchanged);
- a **dead-letter fast-forward** so the reliability story is watchable without
  waiting ~5 minutes for real exponential backoff to exhaust.

Everything runs the real, unmodified production paths — ``ingest_event``,
``process_delivery``, HMAC signing, the Discord transform — so what a visitor
watches is the actual engine, not a scripted animation.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy import delete, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, selectinload

from app.core.config import Settings, get_settings
from app.models.api_key import ApiKey, hash_api_key
from app.models.delivery import Delivery, DeliveryStatus
from app.models.demo import DemoReceivedRequest, DemoReceiverHealth, DemoVisitorActivity
from app.models.endpoint import Endpoint, EndpointStatus, PayloadFormat
from app.models.event import Event
from app.models.project import Project
from app.services.crypto import encrypt_secret, generate_endpoint_secret
from app.services.event_ingestion import ingest_event
from app.worker.delivery_worker import process_delivery

logger = logging.getLogger(__name__)

# The event types the showcase producer emits. Kept as plain strings here (the
# app does not import the `producer` package) so fan-out matches them normally.
PRICE_TICK = "price.tick"
PRICE_ALERT = "price.alert"
PRICE_EVENT_TYPES: tuple[str, ...] = (PRICE_TICK, PRICE_ALERT)
TRADE_EXECUTED = "trade.executed"

# Reserved marker carried in the controllable receiver's event_types. Schemas
# reject any ``__``-prefixed event_type on real, user-registered endpoints, so
# this can never collide with a customer endpoint — and the receiver route uses
# it to be certain it only ever serves the showcase receiver.
SHOWCASE_MARKER = "__showcase__"

# Markers for the two-banks ledger demo endpoints (same reserved-prefix rules).
# Both banks subscribe to ``trade.executed``; the marker tells the bank routes
# — and seeding/resolution — which endpoint is which.
BANK_NAIVE_MARKER = "__showcase_bank_naive__"
BANK_SAFE_MARKER = "__showcase_bank_safe__"

# Advisory lock namespace for seeding; arbitrary, only ever used by this module.
_ADVISORY_LOCK_NAMESPACE = 87_412_502

# How many received requests to retain on the receiver (a live tail, not a log).
_INBOX_KEEP = 20

# How many recent deliveries the lifecycle timeline returns (a live tail).
_TIMELINE_KEEP = 12

# Cap the stored request body so the inbox can't grow unbounded.
_MAX_BODY_CHARS = 8_192


@dataclass(frozen=True)
class ShowcaseHandles:
    """Resolved ids for the seeded showcase project and its endpoints."""

    project_id: uuid.UUID
    receiver_endpoint_id: uuid.UUID
    discord_endpoint_id: uuid.UUID | None
    bank_naive_endpoint_id: uuid.UUID
    bank_safe_endpoint_id: uuid.UUID


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------


def seed_showcase(session: Session, settings: Settings | None = None) -> ShowcaseHandles:
    """Idempotently seed the shared showcase project, endpoints, and API key.

    Safe to call repeatedly (on startup or via ``python -m app.seed_showcase``):
    a transaction-scoped advisory lock guards the check-then-insert so concurrent
    callers can't create duplicates, and every sub-resource is found-or-created.
    The caller commits.

    Creates:
    - the project (by the stable ``settings.showcase_project_name``);
    - a controllable **receiver** endpoint (self-referential URL, tagged with
      :data:`SHOWCASE_MARKER`, subscribing to tick + alert) plus its health row;
    - a **Discord** endpoint subscribing to alerts only, when
      ``settings.showcase_discord_webhook_url`` is set;
    - an API key whose hash matches ``settings.showcase_api_key``, when set, so
      the external producer can authenticate with that same shared secret.
    """
    settings = settings or get_settings()

    session.execute(
        text("SELECT pg_advisory_xact_lock(:ns, hashtext(:key))"),
        {"ns": _ADVISORY_LOCK_NAMESPACE, "key": settings.showcase_project_name},
    )

    project = session.execute(
        select(Project).where(Project.name == settings.showcase_project_name)
    ).scalar_one_or_none()
    if project is None:
        project = Project(name=settings.showcase_project_name)
        session.add(project)
        session.flush()

    receiver = _find_or_create_receiver(session, project, settings.public_base_url)
    discord = _find_or_create_discord(session, project, settings.showcase_discord_webhook_url)
    bank_naive = _find_or_create_bank(
        session, project, settings.public_base_url, BANK_NAIVE_MARKER, "naive"
    )
    bank_safe = _find_or_create_bank(
        session, project, settings.public_base_url, BANK_SAFE_MARKER, "safe"
    )
    _ensure_api_key(session, project, settings.showcase_api_key)
    session.flush()

    return ShowcaseHandles(
        project_id=project.id,
        receiver_endpoint_id=receiver.id,
        discord_endpoint_id=discord.id if discord is not None else None,
        bank_naive_endpoint_id=bank_naive.id,
        bank_safe_endpoint_id=bank_safe.id,
    )


def _find_or_create_receiver(session: Session, project: Project, public_base_url: str) -> Endpoint:
    """Find or create the controllable receiver endpoint (+ its health row)."""
    existing = session.execute(
        select(Endpoint).where(
            Endpoint.project_id == project.id,
            Endpoint.event_types.contains([SHOWCASE_MARKER]),
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    endpoint_id = uuid.uuid4()
    endpoint = Endpoint(
        id=endpoint_id,
        project_id=project.id,
        # Server-constructed URL, not attacker input.
        url=f"{public_base_url.rstrip('/')}/showcase/receiver/{endpoint_id}",
        event_types=[SHOWCASE_MARKER, *PRICE_EVENT_TYPES],
        secret_enc=encrypt_secret(generate_endpoint_secret()),
        status=EndpointStatus.active,
        payload_format=PayloadFormat.raw,
        rate_limit_rps=None,
    )
    session.add(endpoint)
    # Flush the endpoint before the health row: they are linked only by a foreign
    # key (no ORM relationship), so the unit of work won't order the inserts.
    session.flush()
    session.add(DemoReceiverHealth(endpoint_id=endpoint_id, healthy=True))
    session.flush()
    return endpoint


def _find_or_create_bank(
    session: Session, project: Project, public_base_url: str, marker: str, kind: str
) -> Endpoint:
    """Find or create one bank endpoint of the two-banks ledger demo.

    Both banks subscribe to ``trade.executed`` so every trade fans out to each
    of them through the unmodified delivery path. *kind* is the URL path
    segment (``naive`` or ``safe``) the bank's receiver route lives under.
    """
    existing = session.execute(
        select(Endpoint).where(
            Endpoint.project_id == project.id,
            Endpoint.event_types.contains([marker]),
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    endpoint_id = uuid.uuid4()
    endpoint = Endpoint(
        id=endpoint_id,
        project_id=project.id,
        # Server-constructed URL, not attacker input.
        url=f"{public_base_url.rstrip('/')}/showcase/ledger/{kind}/{endpoint_id}",
        event_types=[marker, TRADE_EXECUTED],
        secret_enc=encrypt_secret(generate_endpoint_secret()),
        status=EndpointStatus.active,
        payload_format=PayloadFormat.raw,
        rate_limit_rps=None,
    )
    session.add(endpoint)
    # Flush before the health row: linked only by FK, no ORM relationship.
    session.flush()
    session.add(DemoReceiverHealth(endpoint_id=endpoint_id, healthy=True, mode="healthy"))
    session.flush()
    return endpoint


def _find_or_create_discord(
    session: Session, project: Project, webhook_url: str
) -> Endpoint | None:
    """Find or create the real Discord endpoint (alerts only). None if unconfigured."""
    if not webhook_url:
        return None

    existing = session.execute(
        select(Endpoint).where(
            Endpoint.project_id == project.id,
            Endpoint.payload_format == PayloadFormat.discord,
        )
    ).scalar_one_or_none()
    if existing is not None:
        # Keep the destination in sync if the configured webhook changed.
        if existing.url != webhook_url:
            existing.url = webhook_url
            session.flush()
        return existing

    endpoint = Endpoint(
        project_id=project.id,
        url=webhook_url,
        event_types=[PRICE_ALERT],
        secret_enc=encrypt_secret(generate_endpoint_secret()),
        status=EndpointStatus.active,
        payload_format=PayloadFormat.discord,
        rate_limit_rps=None,
    )
    session.add(endpoint)
    session.flush()
    return endpoint


def _ensure_api_key(session: Session, project: Project, plaintext: str) -> None:
    """Ensure an ApiKey whose hash matches *plaintext* exists for the project."""
    if not plaintext:
        return
    key_hash = hash_api_key(plaintext)
    existing = session.execute(
        select(ApiKey).where(ApiKey.key_hash == key_hash)
    ).scalar_one_or_none()
    if existing is not None:
        return
    session.add(
        ApiKey(
            project_id=project.id,
            name="showcase-producer",
            key_prefix=plaintext[:12],
            key_hash=key_hash,
        )
    )
    session.flush()


def resolve_showcase(session: Session, settings: Settings | None = None) -> ShowcaseHandles | None:
    """Read-only lookup of the seeded showcase handles; None if not yet seeded.

    Cheap (plain SELECTs, no advisory lock), so it is safe to call per request.
    Callers that must guarantee existence use :func:`seed_showcase` instead.
    """
    settings = settings or get_settings()
    project = session.execute(
        select(Project).where(Project.name == settings.showcase_project_name)
    ).scalar_one_or_none()
    if project is None:
        return None
    receiver = session.execute(
        select(Endpoint).where(
            Endpoint.project_id == project.id,
            Endpoint.event_types.contains([SHOWCASE_MARKER]),
        )
    ).scalar_one_or_none()
    if receiver is None:
        return None

    def _bank(marker: str) -> Endpoint | None:
        return session.execute(
            select(Endpoint).where(
                Endpoint.project_id == project.id,
                Endpoint.event_types.contains([marker]),
            )
        ).scalar_one_or_none()

    # Banks missing (e.g. a deployment seeded before the ledger demo existed)
    # → not resolved; callers fall back to seed_showcase, which backfills them.
    bank_naive = _bank(BANK_NAIVE_MARKER)
    bank_safe = _bank(BANK_SAFE_MARKER)
    if bank_naive is None or bank_safe is None:
        return None
    discord = session.execute(
        select(Endpoint).where(
            Endpoint.project_id == project.id,
            Endpoint.payload_format == PayloadFormat.discord,
        )
    ).scalar_one_or_none()
    return ShowcaseHandles(
        project_id=project.id,
        receiver_endpoint_id=receiver.id,
        discord_endpoint_id=discord.id if discord is not None else None,
        bank_naive_endpoint_id=bank_naive.id,
        bank_safe_endpoint_id=bank_safe.id,
    )


def latest_dead_lettered_id(session: Session, project_id: uuid.UUID) -> uuid.UUID | None:
    """Return the most recently updated dead-lettered delivery in the project."""
    return session.execute(
        select(Delivery.id)
        .join(Endpoint, Delivery.endpoint_id == Endpoint.id)
        .where(
            Endpoint.project_id == project_id,
            Delivery.status == DeliveryStatus.dead_lettered,
        )
        .order_by(Delivery.updated_at.desc())
        .limit(1)
    ).scalar_one_or_none()


def get_scoped_delivery(
    session: Session, project_id: uuid.UUID, delivery_id: uuid.UUID
) -> Delivery | None:
    """Return a delivery by id only if it belongs to *project_id* (else None)."""
    return session.execute(
        select(Delivery)
        .join(Endpoint, Delivery.endpoint_id == Endpoint.id)
        .where(Delivery.id == delivery_id, Endpoint.project_id == project_id)
    ).scalar_one_or_none()


def list_recent_deliveries(
    session: Session, endpoint_id: uuid.UUID, limit: int = _TIMELINE_KEEP
) -> list[Delivery]:
    """Return the receiver endpoint's most recent deliveries, newest first.

    Attempts and the source event are eagerly loaded so the dashboard's
    lifecycle timeline (attempt history, measured backoff gaps, live lease /
    next-attempt countdowns) renders from one read.
    """
    return list(
        session.execute(
            select(Delivery)
            .options(selectinload(Delivery.attempts), selectinload(Delivery.event))
            .where(Delivery.endpoint_id == endpoint_id)
            .order_by(Delivery.created_at.desc(), Delivery.id.desc())
            .limit(limit)
        ).scalars()
    )


def list_recent_events(session: Session, project_id: uuid.UUID, limit: int = 25) -> list[Event]:
    """Return the project's most recent events (the live producer feed), newest first."""
    return list(
        session.execute(
            select(Event)
            .where(Event.project_id == project_id)
            .order_by(Event.created_at.desc(), Event.id.desc())
            .limit(limit)
        ).scalars()
    )


# ---------------------------------------------------------------------------
# Receiver health
# ---------------------------------------------------------------------------


def get_health(session: Session, endpoint_id: uuid.UUID) -> bool:
    """Return the receiver's current health (defaults healthy)."""
    row = session.get(DemoReceiverHealth, endpoint_id)
    return row.healthy if row is not None else True


def set_health(session: Session, endpoint_id: uuid.UUID, healthy: bool) -> None:
    """Upsert the receiver's health flag."""
    row = session.get(DemoReceiverHealth, endpoint_id)
    if row is None:
        session.add(DemoReceiverHealth(endpoint_id=endpoint_id, healthy=healthy))
    else:
        row.healthy = healthy
    session.flush()


# ---------------------------------------------------------------------------
# Visitor activity (idle watchdog)
# ---------------------------------------------------------------------------


def touch_visitor_seen(session: Session, project_id: uuid.UUID) -> None:
    """Upsert "now" as the project's last real-visitor timestamp.

    Called only from the dashboard's own read routes — never from the
    ``producer`` service's self-generated traffic — so it is a reliable
    signal for the idle watchdog. The caller commits.
    """
    row = session.get(DemoVisitorActivity, project_id)
    if row is None:
        session.add(DemoVisitorActivity(project_id=project_id))
    else:
        row.last_seen_at = datetime.now(UTC)
    session.flush()


def get_last_visitor_seen(session: Session, project_id: uuid.UUID) -> datetime | None:
    """Return when a real visitor was last seen, or None if never recorded."""
    row = session.get(DemoVisitorActivity, project_id)
    return row.last_seen_at if row is not None else None


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
    """Append one received request to the receiver inbox, pruning old rows.

    Keeps only the most recent :data:`_INBOX_KEEP` rows so the inbox stays a
    bounded live tail. The caller commits.
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
    """Return the most recent received requests for the receiver, newest first."""
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


def _build_showcase_tick() -> tuple[str, dict[str, Any]]:
    """A minimal ``price.tick`` payload for the dead-letter demo (no real fetch)."""
    return PRICE_TICK, {
        "symbol": "BTC-USD",
        "base": "BTC",
        "quote": "USD",
        "price": "0",
        "change": "0",
        "change_pct": "0.00",
        "direction": "flat",
        "observed_at": datetime.now(UTC).isoformat(),
        "demo": "dead-letter",
    }


def emit_and_dead_letter(
    *,
    session: Session,
    project: Project,
    endpoint: Endpoint,
    http_client: httpx.Client,
) -> uuid.UUID | None:
    """Emit one tick and drive it straight to dead_lettered on the receiver.

    The caller must have verified the receiver is currently unhealthy — that is
    what makes every fast-forwarded attempt fail (503) and reach the dead-letter
    queue. Because the receiver is *down* rather than permanently broken, the
    dead-lettered delivery recovers naturally on redrive once the visitor brings
    the pipeline back up (no attempt-count seeding tricks).

    Two commits, deliberately: phase 1 makes the new event/delivery visible to
    the receiver route and worker (which resolve their own connections under
    READ COMMITTED); phase 2 persists the fast-forwarded attempts.
    """
    settings = get_settings()
    event_type, payload = _build_showcase_tick()
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
    pathological stall fails gracefully instead of hanging the request.
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
        logger.warning("showcase: could not lock target delivery within 5s; skipping fast-forward")
        return None

    if delivery is None:
        return None

    for _ in range(settings.max_delivery_attempts):
        if delivery.status == DeliveryStatus.dead_lettered:
            break
        process_delivery(delivery, session, http_client)

    session.commit()  # phase-2 boundary
    return delivery.id if delivery.status == DeliveryStatus.dead_lettered else None
