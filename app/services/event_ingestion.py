"""Event ingestion service: idempotency check, fan-out, and atomic persistence."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.delivery import Delivery, DeliveryStatus
from app.models.endpoint import Endpoint, EndpointStatus
from app.models.event import Event
from app.models.idempotency import IdempotencyRecord


def _body_hash(event_type: str, payload: dict[str, Any]) -> str:
    """Stable SHA-256 hash of the canonical request body."""
    canonical = json.dumps(
        {"type": event_type, "payload": payload}, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def ingest_event(
    *,
    session: Session,
    project_id: uuid.UUID,
    event_type: str,
    payload: dict[str, Any],
    idempotency_key: str | None,
) -> tuple[uuid.UUID, int]:
    """Ingest an event, fan out to matching endpoints, return (event_id, queued_count).

    All writes (Event, Delivery rows, IdempotencyRecord) are flushed to the same
    transaction; the caller must commit.

    Idempotency semantics when *idempotency_key* is provided:
    - Same key + same body → return cached (event_id, queued_deliveries), no-op.
    - Same key + different body → raise HTTP 409.
    - First request → create, flush, and return.

    Race condition: two concurrent requests can both pass the initial lookup before
    either commits.  The losing insert hits the unique constraint on
    ``idempotency_records``.  A savepoint wraps all three inserts (Event, Deliveries,
    IdempotencyRecord) so the outer transaction survives — the savepoint is rolled
    back, the winning record is re-read, and its cached response is returned.
    """
    request_hash = _body_hash(event_type, payload)

    if idempotency_key is not None:
        existing = session.execute(
            select(IdempotencyRecord).where(
                IdempotencyRecord.project_id == project_id,
                IdempotencyRecord.idempotency_key == idempotency_key,
            )
        ).scalar_one_or_none()

        if existing is not None:
            if existing.request_hash != request_hash:
                raise HTTPException(
                    status_code=409,
                    detail="Idempotency-Key reused with a different request body",
                )
            return existing.event_id, existing.queued_deliveries

    now = datetime.now(UTC)
    # Pre-generate UUID so Delivery rows reference it without an early flush.
    event_id = uuid.uuid4()

    # Query the fan-out targets *before* adding any pending objects: this SELECT
    # autoflushes, and an autoflushed Event would land in the outer transaction,
    # escape the savepoint below, and survive its rollback as an orphan row when
    # a concurrent request wins the idempotency race.
    active_endpoints = list(
        session.execute(
            select(Endpoint).where(
                Endpoint.project_id == project_id,
                Endpoint.status == EndpointStatus.active,
                Endpoint.event_types.contains([event_type]),
            )
        ).scalars()
    )

    event = Event(
        id=event_id,
        project_id=project_id,
        type=event_type,
        payload=payload,
        idempotency_key=idempotency_key,
    )
    deliveries = [
        Delivery(
            event_id=event_id,
            endpoint_id=ep.id,
            status=DeliveryStatus.pending,
            attempt_count=0,
            next_attempt_at=now,
        )
        for ep in active_endpoints
    ]
    queued_count = len(deliveries)

    if idempotency_key is not None:
        # The savepoint must wrap all three inserts.  ``begin_nested()`` flushes
        # any *already-pending* objects to the outer transaction before emitting
        # SAVEPOINT, so the objects are added only after it opens — otherwise a
        # losing racer's Event/Delivery rows would escape the rollback and be
        # committed as an orphan event plus a duplicate delivery.
        nested = session.begin_nested()
        try:
            session.add(event)
            session.add_all(deliveries)
            session.add(
                IdempotencyRecord(
                    project_id=project_id,
                    idempotency_key=idempotency_key,
                    event_id=event_id,
                    queued_deliveries=queued_count,
                    request_hash=request_hash,
                )
            )
            nested.commit()  # flush Event + Deliveries + IdempotencyRecord; release savepoint
        except IntegrityError:
            # A concurrent request already committed the same
            # (project_id, idempotency_key): only uq_idempotency_records_project_key
            # can fire here; FK constraints reference rows committed before this
            # savepoint opened.
            nested.rollback()  # undo all three inserts; outer transaction stays alive
            existing = session.execute(
                select(IdempotencyRecord).where(
                    IdempotencyRecord.project_id == project_id,
                    IdempotencyRecord.idempotency_key == idempotency_key,
                )
            ).scalar_one()
            if existing.request_hash != request_hash:
                raise HTTPException(
                    status_code=409,
                    detail="Idempotency-Key reused with a different request body",
                ) from None
            return existing.event_id, existing.queued_deliveries
    else:
        session.add(event)
        session.add_all(deliveries)
        session.flush()

    if queued_count > 0:
        channel = get_settings().worker_listen_channel
        session.execute(text(f"NOTIFY {channel}"))

    return event_id, queued_count
