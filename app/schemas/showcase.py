"""Pydantic schemas for the public live-showcase API (/showcase/*)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel


class HealthRequest(BaseModel):
    """Body for POST /showcase/health."""

    healthy: bool


class HealthResponse(BaseModel):
    """Returned by POST /showcase/health."""

    receiver_endpoint_id: uuid.UUID
    healthy: bool


class ReceivedRequestItem(BaseModel):
    """One request the controllable receiver accepted (an inbox entry)."""

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


class FeedEventItem(BaseModel):
    """A recent event published by the producer (the live price feed)."""

    id: uuid.UUID
    type: str
    payload: dict[str, Any]
    created_at: datetime

    model_config = {"from_attributes": True}


class FeedResponse(BaseModel):
    """Returned by GET /showcase/feed — the live producer feed + receiver state."""

    healthy: bool
    discord_enabled: bool
    discord_widget_server_id: str | None = None
    discord_widget_channel_id: str | None = None
    events: list[FeedEventItem]
    inbox: list[ReceivedRequestItem]


class DeadLetterResponse(BaseModel):
    """Returned by POST /showcase/dead-letter."""

    delivery_id: uuid.UUID | None
    healthy: bool


class RedriveRequest(BaseModel):
    """Body for POST /showcase/redrive; delivery_id optional (defaults to latest DLQ)."""

    delivery_id: uuid.UUID | None = None


class RedriveResponse(BaseModel):
    """Returned by POST /showcase/redrive."""

    delivery_id: uuid.UUID | None
    status: str | None


class BurstResponse(BaseModel):
    """Returned by POST /showcase/burst."""

    published: int
