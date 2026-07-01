"""Pydantic schemas for the interactive dashboard demo ("Ops Console")."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

# The event types the demo can emit — mirrors app.services.demo_events.
DemoEventType = Literal["push", "pull_request", "workflow_run"]


class EmitRequest(BaseModel):
    """Body for POST /simulate/events."""

    event_type: DemoEventType | None = None
    count: int = Field(default=1, ge=1, le=25)


class EmitResponse(BaseModel):
    """Returned by POST /simulate/events."""

    endpoint_id: uuid.UUID
    queued_events: int
    queued_deliveries: int
    event_type: str
    sample_payload: dict[str, Any]


class HealthRequest(BaseModel):
    """Body for POST /simulate/health."""

    healthy: bool


class HealthResponse(BaseModel):
    """Returned by POST /simulate/health."""

    endpoint_id: uuid.UUID
    healthy: bool


class ReceivedRequestItem(BaseModel):
    """One request the demo receiver accepted (an inbox entry)."""

    id: uuid.UUID
    event_type: str
    attempt: int
    verified: bool
    response_status: int
    signature_header: str | None = None
    timestamp_header: str | None = None
    body: str
    received_at: datetime

    model_config = {"from_attributes": True}


class InboxResponse(BaseModel):
    """Returned by GET /simulate/inbox — current health plus the received-request tail."""

    endpoint_id: uuid.UUID
    healthy: bool
    items: list[ReceivedRequestItem]


class DeadLetterResponse(BaseModel):
    """Returned by POST /simulate/dead-letter."""

    delivery_id: uuid.UUID | None
    healthy: bool
