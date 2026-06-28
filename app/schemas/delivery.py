"""Pydantic schemas for delivery and delivery attempt API responses."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel

from app.models.delivery import DeliveryStatus


class DeliveryAttemptResponse(BaseModel):
    """Single HTTP attempt record."""

    id: uuid.UUID
    delivery_id: uuid.UUID
    attempt_number: int
    response_status: int | None
    response_body: str | None
    error: str | None
    duration_ms: int | None
    created_at: datetime

    model_config = {"from_attributes": True}


class DeliveryResponse(BaseModel):
    """Delivery row returned by list and detail endpoints."""

    id: uuid.UUID
    event_id: uuid.UUID
    endpoint_id: uuid.UUID
    status: DeliveryStatus
    attempt_count: int
    next_attempt_at: datetime
    leased_until: datetime | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class EventDetailResponse(BaseModel):
    """Event with its associated deliveries, returned by GET /events/{id}."""

    id: uuid.UUID
    project_id: uuid.UUID
    type: str
    payload: dict[str, Any]
    idempotency_key: str | None
    created_at: datetime
    deliveries: list[DeliveryResponse]

    model_config = {"from_attributes": True}
