"""Router for event ingestion and inspection."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session, selectinload

from app.auth.dependencies import get_current_project
from app.db.session import get_session
from app.models.event import Event
from app.models.project import Project
from app.routers._pagination import decode_cursor, encode_cursor
from app.schemas.delivery import EventDetailResponse
from app.schemas.event import EventCreate, EventIngestResponse, EventListItem, EventListResponse
from app.services.event_ingestion import ingest_event

router = APIRouter(prefix="/events", tags=["events"])

_DEFAULT_LIMIT = 20
_MAX_LIMIT = 100


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


@router.get("", response_model=EventListResponse)
def list_events(
    limit: Annotated[int, Query(ge=1, le=_MAX_LIMIT)] = _DEFAULT_LIMIT,
    cursor: str | None = None,
    event_type: str | None = None,
    project: Project = Depends(get_current_project),
    session: Session = Depends(get_session),
) -> EventListResponse:
    """List events for the authenticated project with keyset cursor pagination."""
    stmt = (
        select(Event).options(selectinload(Event.deliveries)).where(Event.project_id == project.id)
    )

    if event_type is not None:
        stmt = stmt.where(Event.type == event_type)

    if cursor is not None:
        cursor_created_at, cursor_id = decode_cursor(cursor)
        stmt = stmt.where(
            or_(
                Event.created_at < cursor_created_at,
                and_(
                    Event.created_at == cursor_created_at,
                    Event.id < cursor_id,
                ),
            )
        )

    stmt = stmt.order_by(Event.created_at.desc(), Event.id.desc()).limit(limit + 1)
    rows = list(session.execute(stmt).scalars())

    has_next = len(rows) > limit
    page = rows[:limit]

    next_cursor: str | None = None
    if has_next and page:
        last = page[-1]
        next_cursor = encode_cursor(last.created_at, last.id)

    items = [
        EventListItem(
            id=event.id,
            type=event.type,
            payload=event.payload,
            created_at=event.created_at,
            delivery_count=len(event.deliveries),
        )
        for event in page
    ]
    return EventListResponse(items=items, next_cursor=next_cursor)


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
