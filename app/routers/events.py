"""Router for event ingestion and inspection."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.auth.dependencies import get_current_project
from app.db.session import get_session
from app.models.event import Event
from app.models.project import Project
from app.schemas.delivery import EventDetailResponse
from app.schemas.event import EventCreate, EventIngestResponse
from app.services.event_ingestion import ingest_event

router = APIRouter(prefix="/events", tags=["events"])


@router.post("", status_code=201, response_model=EventIngestResponse)
def publish_event(
    body: EventCreate,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    project: Project = Depends(get_current_project),
    session: Session = Depends(get_session),
) -> EventIngestResponse:
    """Publish an event and fan it out to matching active endpoints.

    Returns the event id and the number of delivery rows queued.
    Supports ``Idempotency-Key`` for safe client retries.
    """
    event_id, queued_deliveries = ingest_event(
        session=session,
        project_id=project.id,
        event_type=body.type,
        payload=body.payload,
        idempotency_key=idempotency_key,
    )
    session.commit()
    return EventIngestResponse(event_id=event_id, queued_deliveries=queued_deliveries)


@router.get("/{event_id}", response_model=EventDetailResponse)
def get_event(
    event_id: uuid.UUID,
    project: Project = Depends(get_current_project),
    session: Session = Depends(get_session),
) -> Event:
    """Return a single event with its associated deliveries.

    Scoped to the authenticated project.
    """
    event = session.execute(
        select(Event)
        .options(selectinload(Event.deliveries))
        .where(Event.id == event_id, Event.project_id == project.id)
    ).scalar_one_or_none()
    if event is None:
        raise HTTPException(status_code=404, detail="Event not found")
    return event
