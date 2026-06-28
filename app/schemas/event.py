"""Pydantic schemas for event ingestion API requests and responses."""

from __future__ import annotations

import json
import uuid
from typing import Any

from pydantic import BaseModel, Field, field_validator

_MAX_PAYLOAD_BYTES = 65_536  # 64 KiB


class EventCreate(BaseModel):
    """Body for POST /events."""

    type: str = Field(min_length=1, max_length=255)
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("type")
    @classmethod
    def type_nonempty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("type must be a non-empty string")
        return v

    @field_validator("payload")
    @classmethod
    def payload_size(cls, v: dict[str, Any]) -> dict[str, Any]:
        raw = json.dumps(v, separators=(",", ":"))
        if len(raw.encode()) > _MAX_PAYLOAD_BYTES:
            raise ValueError(f"payload must not exceed {_MAX_PAYLOAD_BYTES} bytes")
        return v


class EventIngestResponse(BaseModel):
    """Response for POST /events."""

    event_id: uuid.UUID
    queued_deliveries: int
