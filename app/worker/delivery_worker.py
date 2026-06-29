"""Core delivery worker: claim due deliveries and POST signed payloads to endpoints."""

from __future__ import annotations

import json
import logging
import time
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import select, update
from sqlalchemy.orm import Session, selectinload

from app.core.config import get_settings
from app.models.delivery import Delivery, DeliveryStatus
from app.models.delivery_attempt import DeliveryAttempt
from app.models.endpoint import Endpoint
from app.models.event import Event
from app.services.crypto import decrypt_secret
from app.worker.backoff import compute_next_attempt_at
from app.worker.signing import build_signature_header

logger = logging.getLogger(__name__)

_LEASE_SECONDS = 60
_BATCH_SIZE = 10


def claim_due_deliveries(session: Session, batch_size: int = _BATCH_SIZE) -> list[Delivery]:
    """Claim up to *batch_size* pending, due deliveries with FOR UPDATE SKIP LOCKED.

    Each claimed delivery transitions from PENDING → IN_FLIGHT and receives a
    time-bounded lease.  Concurrent workers skip locked rows rather than block.
    """
    now = datetime.now(UTC)
    lease_until = now + timedelta(seconds=_LEASE_SECONDS)

    rows = (
        session.execute(
            select(Delivery)
            .options(
                selectinload(Delivery.endpoint),
                selectinload(Delivery.event),
            )
            .where(
                Delivery.status == DeliveryStatus.pending,
                Delivery.next_attempt_at <= now,
            )
            .order_by(Delivery.next_attempt_at)
            .limit(batch_size)
            .with_for_update(skip_locked=True)
        )
        .scalars()
        .all()
    )

    for delivery in rows:
        delivery.status = DeliveryStatus.in_flight
        delivery.leased_until = lease_until

    if rows:
        session.flush()

    return list(rows)


def _recover_expired_leases(session: Session) -> None:
    """Reset IN_FLIGHT deliveries with an expired lease back to PENDING."""
    now = datetime.now(UTC)
    session.execute(
        update(Delivery)
        .where(
            Delivery.status == DeliveryStatus.in_flight,
            Delivery.leased_until < now,
        )
        .values(status=DeliveryStatus.pending, leased_until=None)
    )
    session.flush()


def process_delivery(
    delivery: Delivery,
    session: Session,
    http_client: httpx.Client,
) -> None:
    """POST the signed event payload to the endpoint and record the attempt.

    On 2xx response → SUCCEEDED.  On failure, schedules a retry (PENDING with
    next_attempt_at via exponential backoff) if under the attempt limit, or
    transitions to DEAD_LETTERED when the limit is reached.
    """
    settings = get_settings()
    endpoint: Endpoint = delivery.endpoint
    event: Event = delivery.event

    secret = decrypt_secret(endpoint.secret_enc)

    body = json.dumps(
        {
            "event_id": str(event.id),
            "type": event.type,
            "payload": event.payload,
        },
        separators=(",", ":"),
    ).encode()

    ts = int(time.time())
    headers = {
        "Content-Type": "application/json",
        "X-Webhook-Timestamp": str(ts),
        "X-Webhook-Signature": build_signature_header(secret, ts, body),
    }

    attempt_number = delivery.attempt_count + 1
    t0 = time.monotonic()

    response_status: int | None = None
    response_body: str | None = None
    error: str | None = None
    succeeded = False

    try:
        resp = http_client.post(
            endpoint.url,
            content=body,
            headers=headers,
            timeout=settings.delivery_timeout_seconds,
        )
        response_status = resp.status_code
        response_body = resp.text[:1024]
        succeeded = 200 <= resp.status_code < 300
    except Exception as exc:
        error = str(exc)[:1024]

    duration_ms = int((time.monotonic() - t0) * 1000)

    session.add(
        DeliveryAttempt(
            delivery_id=delivery.id,
            attempt_number=attempt_number,
            response_status=response_status,
            response_body=response_body,
            error=error,
            duration_ms=duration_ms,
        )
    )

    delivery.attempt_count = attempt_number

    if succeeded:
        delivery.status = DeliveryStatus.succeeded
        logger.info(
            "delivery attempt succeeded"
            " delivery_id=%s attempt_number=%d http_status=%s duration_ms=%d",
            delivery.id,
            attempt_number,
            response_status,
            duration_ms,
        )
    elif attempt_number >= settings.max_delivery_attempts:
        delivery.status = DeliveryStatus.dead_lettered
        logger.warning(
            "delivery dead-lettered after max attempts"
            " delivery_id=%s attempt_number=%d http_status=%s network_error=%s",
            delivery.id,
            attempt_number,
            response_status,
            bool(error),
        )
    else:
        delivery.status = DeliveryStatus.pending
        delivery.next_attempt_at = compute_next_attempt_at(
            attempt_number,
            settings.retry_base_seconds,
            settings.retry_cap_seconds,
        )
        delivery.leased_until = None
        logger.info(
            "delivery attempt failed; retry scheduled"
            " delivery_id=%s attempt_number=%d http_status=%s network_error=%s",
            delivery.id,
            attempt_number,
            response_status,
            bool(error),
        )

    session.flush()


def run_once(session: Session, http_client: httpx.Client) -> int:
    """Claim and process one batch of due deliveries.  Returns the number processed."""
    _recover_expired_leases(session)
    deliveries = claim_due_deliveries(session)
    for delivery in deliveries:
        process_delivery(delivery, session, http_client)
    return len(deliveries)
