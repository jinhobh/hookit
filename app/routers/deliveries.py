"""Router for delivery inspection endpoints."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_project
from app.db.session import get_session
from app.models.delivery import Delivery, DeliveryStatus
from app.models.delivery_attempt import DeliveryAttempt
from app.models.endpoint import Endpoint
from app.models.project import Project
from app.routers._pagination import decode_cursor as _decode_cursor
from app.routers._pagination import encode_cursor as _encode_cursor
from app.schemas.delivery import DeliveryAttemptResponse, DeliveryPageResponse, DeliveryResponse

router = APIRouter(prefix="/deliveries", tags=["deliveries"])

_DEFAULT_LIMIT = 50
_MAX_LIMIT = 200


def _get_delivery_or_404(delivery_id: uuid.UUID, project: Project, session: Session) -> Delivery:
    """Return the delivery belonging to *project* or raise 404."""
    delivery = session.execute(
        select(Delivery)
        .join(Endpoint, Delivery.endpoint_id == Endpoint.id)
        .where(
            Delivery.id == delivery_id,
            Endpoint.project_id == project.id,
        )
    ).scalar_one_or_none()
    if delivery is None:
        raise HTTPException(status_code=404, detail="Delivery not found")
    return delivery


@router.get("", response_model=DeliveryPageResponse)
def list_deliveries(
    limit: Annotated[int, Query(ge=1, le=_MAX_LIMIT)] = _DEFAULT_LIMIT,
    cursor: str | None = None,
    status: DeliveryStatus | None = None,
    endpoint_id: uuid.UUID | None = None,
    project: Project = Depends(get_current_project),
    session: Session = Depends(get_session),
) -> DeliveryPageResponse:
    """List deliveries for the authenticated project with keyset cursor pagination."""
    stmt = (
        select(Delivery)
        .join(Endpoint, Delivery.endpoint_id == Endpoint.id)
        .where(Endpoint.project_id == project.id)
    )

    if status is not None:
        stmt = stmt.where(Delivery.status == status)

    if endpoint_id is not None:
        stmt = stmt.where(Delivery.endpoint_id == endpoint_id)

    if cursor is not None:
        cursor_created_at, cursor_id = _decode_cursor(cursor)
        stmt = stmt.where(
            or_(
                Delivery.created_at < cursor_created_at,
                and_(
                    Delivery.created_at == cursor_created_at,
                    Delivery.id < cursor_id,
                ),
            )
        )

    stmt = stmt.order_by(Delivery.created_at.desc(), Delivery.id.desc()).limit(limit + 1)
    rows = list(session.execute(stmt).scalars())

    has_next = len(rows) > limit
    page = rows[:limit]

    next_cursor: str | None = None
    if has_next and page:
        last = page[-1]
        next_cursor = _encode_cursor(last.created_at, last.id)

    items = [DeliveryResponse.model_validate(d) for d in page]
    return DeliveryPageResponse(items=items, next_cursor=next_cursor)


@router.get("/{delivery_id}", response_model=DeliveryResponse)
def get_delivery(
    delivery_id: uuid.UUID,
    project: Project = Depends(get_current_project),
    session: Session = Depends(get_session),
) -> Delivery:
    """Return a single delivery owned by the authenticated project."""
    return _get_delivery_or_404(delivery_id, project, session)


@router.get("/{delivery_id}/attempts", response_model=list[DeliveryAttemptResponse])
def list_delivery_attempts(
    delivery_id: uuid.UUID,
    project: Project = Depends(get_current_project),
    session: Session = Depends(get_session),
) -> list[DeliveryAttempt]:
    """Return all attempts for a delivery owned by the authenticated project."""
    _get_delivery_or_404(delivery_id, project, session)
    return list(
        session.execute(
            select(DeliveryAttempt)
            .where(DeliveryAttempt.delivery_id == delivery_id)
            .order_by(DeliveryAttempt.attempt_number)
        ).scalars()
    )


@router.post("/{delivery_id}/redrive", response_model=DeliveryResponse)
def redrive_delivery(
    delivery_id: uuid.UUID,
    project: Project = Depends(get_current_project),
    session: Session = Depends(get_session),
) -> Delivery:
    """Re-queue a dead-lettered delivery for immediate retry."""
    delivery = _get_delivery_or_404(delivery_id, project, session)
    if delivery.status != DeliveryStatus.dead_lettered:
        raise HTTPException(status_code=409, detail="Delivery is not dead-lettered")
    delivery.status = DeliveryStatus.pending
    delivery.next_attempt_at = datetime.now(UTC)
    delivery.leased_until = None
    session.commit()
    session.refresh(delivery)
    return delivery
