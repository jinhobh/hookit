"""Router for delivery inspection endpoints."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_project
from app.db.session import get_session
from app.models.delivery import Delivery
from app.models.delivery_attempt import DeliveryAttempt
from app.models.endpoint import Endpoint
from app.models.project import Project
from app.schemas.delivery import DeliveryAttemptResponse, DeliveryResponse

router = APIRouter(prefix="/deliveries", tags=["deliveries"])


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


@router.get("", response_model=list[DeliveryResponse])
def list_deliveries(
    project: Project = Depends(get_current_project),
    session: Session = Depends(get_session),
) -> list[Delivery]:
    """List all deliveries for endpoints belonging to the authenticated project."""
    return list(
        session.execute(
            select(Delivery)
            .join(Endpoint, Delivery.endpoint_id == Endpoint.id)
            .where(Endpoint.project_id == project.id)
            .order_by(Delivery.created_at.desc())
        ).scalars()
    )


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
