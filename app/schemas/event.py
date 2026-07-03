"""Pydantic schemas for event ingestion API requests and responses."""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator

from app.core.config import get_settings
from app.schemas._reserved import RESERVED_EVENT_TYPE_PREFIX


class EventCreate(BaseModel):
    """Body for POST /events."""

    type: str = Field(min_length=1, max_length=255)
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("type")
    @classmethod
    def type_nonempty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("type must be a non-empty string")
        if v.startswith(RESERVED_EVENT_TYPE_PREFIX):
            raise ValueError(
                f"type must not start with reserved prefix {RESERVED_EVENT_TYPE_PREFIX!r}"
            )
        return v

    @field_validator("payload")
    @classmethod
    def payload_size(cls, v: dict[str, Any]) -> dict[str, Any]:
        max_bytes = get_settings().max_event_payload_bytes
        raw = json.dumps(v, separators=(",", ":"))
        if len(raw.encode()) > max_bytes:
            raise ValueError(f"payload must not exceed {max_bytes} bytes")
        return v


class EventIngestResponse(BaseModel):
    """Response for POST /events."""

    event_id: uuid.UUID
    queued_deliveries: int


class EventListItem(BaseModel):
    """Single item in GET /events response."""

    id: uuid.UUID
    type: str
    payload: dict[str, Any]
    created_at: datetime
    delivery_count: int


class EventListResponse(BaseModel):
    """Paginated response envelope for GET /events."""

    items: list[EventListItem]
    next_cursor: str | None
