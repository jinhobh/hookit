"""Router for event ingestion."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_project
from app.db.session import get_session
from app.models.project import Project
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
