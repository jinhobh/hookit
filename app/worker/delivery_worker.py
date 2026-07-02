"""Core delivery worker: claim due deliveries and POST signed payloads to endpoints."""

from __future__ import annotations

import logging
import os
import socket
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
from app.services.metrics import (
    DELIVERIES_CLAIMED_TOTAL,
    DELIVERIES_TOTAL,
    DELIVERY_DURATION_SECONDS,
)
from app.services.ssrf import SSRFError, validate_url_not_ssrf
from app.services.transform import build_delivery_body
from app.worker.backoff import compute_next_attempt_at
from app.worker.signing import build_signature_header

logger = logging.getLogger(__name__)


def default_worker_name() -> str:
    """Return this process's default worker name: ``<hostname>:<pid>``."""
    return f"{socket.gethostname()}:{os.getpid()}"


def _resolve_worker_name(worker_name: str | None) -> str:
    """Resolve the effective worker name: explicit arg → settings → hostname:pid."""
    return worker_name or get_settings().worker_name or default_worker_name()


def claim_due_deliveries(
    session: Session,
    batch_size: int | None = None,
    worker_name: str | None = None,
) -> list[Delivery]:
    """Claim up to *batch_size* pending, due deliveries with FOR UPDATE SKIP LOCKED.

    Each claimed delivery transitions from PENDING → IN_FLIGHT, receives a
    time-bounded lease, and is stamped with the claiming worker's name.
    Concurrent workers skip locked rows rather than block.
    """
    settings = get_settings()
    if batch_size is None:
        batch_size = settings.worker_batch_size
    claimer = _resolve_worker_name(worker_name)
    now = datetime.now(UTC)
    lease_until = now + timedelta(seconds=settings.worker_lease_seconds)

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
        delivery.claimed_by = claimer

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
    worker_name: str | None = None,
) -> None:
    """POST the signed event payload to the endpoint and record the attempt.

    On 2xx response → SUCCEEDED.  On failure, schedules a retry (PENDING with
    next_attempt_at via exponential backoff) if under the attempt limit, or
    transitions to DEAD_LETTERED when the limit is reached.  Each attempt row is
    stamped with the processing worker's name.
    """
    settings = get_settings()
    endpoint: Endpoint = delivery.endpoint
    event: Event = delivery.event
    worker = _resolve_worker_name(worker_name)

    secret = decrypt_secret(endpoint.secret_enc)

    body = build_delivery_body(endpoint.payload_format, event.id, event.type, event.payload)

    attempt_number = delivery.attempt_count + 1
    ts = int(time.time())
    headers = {
        "Content-Type": "application/json",
        "X-Webhook-Timestamp": str(ts),
        "X-Webhook-Signature": build_signature_header(secret, ts, body),
        "X-Webhook-Attempt": str(attempt_number),
    }

    # SSRF check: dead-letter immediately if the URL targets a non-public address
    try:
        validate_url_not_ssrf(endpoint.url)
    except SSRFError as exc:
        session.add(
            DeliveryAttempt(
                delivery_id=delivery.id,
                attempt_number=attempt_number,
                response_status=None,
                response_body=None,
                error=str(exc),
                duration_ms=0,
                worker_id=worker,
            )
        )
        delivery.attempt_count = attempt_number
        delivery.status = DeliveryStatus.dead_lettered
        logger.warning(
            "delivery dead-lettered: SSRF protection blocked URL delivery_id=%s url=%s",
            delivery.id,
            endpoint.url,
        )
        DELIVERIES_TOTAL.labels(outcome="dead_lettered").inc()
        session.flush()
        return

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
            worker_id=worker,
        )
    )

    delivery.attempt_count = attempt_number
    DELIVERY_DURATION_SECONDS.observe(duration_ms / 1000.0)

    if succeeded:
        delivery.status = DeliveryStatus.succeeded
        DELIVERIES_TOTAL.labels(outcome="succeeded").inc()
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
        DELIVERIES_TOTAL.labels(outcome="dead_lettered").inc()
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
        DELIVERIES_TOTAL.labels(outcome="failed").inc()
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


def sleep_for_rate_limit(rate_limit_rps: float | None) -> None:
    """Sleep 1/rate_limit_rps seconds when a rate limit is set.

    Single-process MVP throttle: no distributed coordination across workers.
    Applied between consecutive deliveries to the same endpoint within one batch.
    """
    if rate_limit_rps is not None:
        time.sleep(1.0 / rate_limit_rps)


def run_once(session: Session, http_client: httpx.Client, worker_name: str | None = None) -> int:
    """Claim and process one batch of due deliveries.  Returns the number processed."""
    _recover_expired_leases(session)
    deliveries = claim_due_deliveries(session, worker_name=worker_name)
    DELIVERIES_CLAIMED_TOTAL.inc(len(deliveries))
    seen_endpoint_ids: set[object] = set()
    for delivery in deliveries:
        endpoint_id = delivery.endpoint_id
        if endpoint_id in seen_endpoint_ids:
            sleep_for_rate_limit(delivery.endpoint.rate_limit_rps)
        else:
            seen_endpoint_ids.add(endpoint_id)
        process_delivery(delivery, session, http_client, worker_name=worker_name)
    return len(deliveries)
