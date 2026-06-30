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

    event = Event(
        id=event_id,
        project_id=project_id,
        type=event_type,
        payload=payload,
        idempotency_key=idempotency_key,
    )
    session.add(event)

    active_endpoints = list(
        session.execute(
            select(Endpoint).where(
                Endpoint.project_id == project_id,
                Endpoint.status == EndpointStatus.active,
                Endpoint.event_types.contains([event_type]),
            )
        ).scalars()
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
    session.add_all(deliveries)

    queued_count = len(deliveries)

    if idempotency_key is not None:
        # Savepoint wraps all three inserts.  If a concurrent request already
        # committed the same (project_id, idempotency_key), the unique constraint
        # raises IntegrityError.  Rolling back to the savepoint keeps the outer
        # transaction alive so we can re-read the winning record and return it.
        nested = session.begin_nested()
        try:
            record = IdempotencyRecord(
                project_id=project_id,
                idempotency_key=idempotency_key,
                event_id=event_id,
                queued_deliveries=queued_count,
                request_hash=request_hash,
            )
            session.add(record)
            nested.commit()  # flush Event + Deliveries + IdempotencyRecord; release savepoint
        except IntegrityError:
            # Only uq_idempotency_records_project_key can fire here; FK constraints
            # reference rows already committed before this savepoint opened.
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
        session.flush()

    if queued_count > 0:
        channel = get_settings().worker_listen_channel
        session.execute(text(f"NOTIFY {channel}"))

    return event_id, queued_count
