"""Pydantic schemas for webhook endpoint API requests and responses."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import AnyHttpUrl, BaseModel, Field, field_validator

from app.models.endpoint import EndpointStatus, PayloadFormat

# Event types starting with this prefix are reserved for internal use (the
# /simulate/run live-demo feature) so a real endpoint can never accidentally
# subscribe to simulated traffic.
_RESERVED_EVENT_TYPE_PREFIX = "__"


def _check_event_types(v: list[str]) -> list[str]:
    for item in v:
        if not item.strip():
            raise ValueError("each event_type must be a non-empty string")
        if item.startswith(_RESERVED_EVENT_TYPE_PREFIX):
            raise ValueError(
                f"event_type must not start with reserved prefix {_RESERVED_EVENT_TYPE_PREFIX!r}"
            )
    return v


class EndpointCreate(BaseModel):
    """Body for POST /endpoints."""

    url: AnyHttpUrl
    event_types: list[str] = Field(min_length=1)
    status: EndpointStatus = EndpointStatus.active
    payload_format: PayloadFormat = PayloadFormat.raw
    rate_limit_rps: float | None = Field(default=None, gt=0, le=1000.0)

    @field_validator("event_types")
    @classmethod
    def event_types_nonempty_strings(cls, v: list[str]) -> list[str]:
        return _check_event_types(v)


class EndpointUpdate(BaseModel):
    """Body for PATCH /endpoints/{id}.  All fields are optional."""

    url: AnyHttpUrl | None = None
    event_types: list[str] | None = Field(default=None, min_length=1)
    status: EndpointStatus | None = None
    payload_format: PayloadFormat | None = None
    rate_limit_rps: float | None = Field(default=None, gt=0, le=1000.0)

    @field_validator("event_types")
    @classmethod
    def event_types_nonempty_strings(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return v
        return _check_event_types(v)


class EndpointResponse(BaseModel):
    """Endpoint representation returned by GET /endpoints and PATCH /endpoints/{id}."""

    id: uuid.UUID
    project_id: uuid.UUID
    url: str
    event_types: list[str]
    status: EndpointStatus
    payload_format: PayloadFormat = PayloadFormat.raw
    created_at: datetime
    updated_at: datetime
    rate_limit_rps: float | None = None

    model_config = {"from_attributes": True}


class EndpointCreateResponse(EndpointResponse):
    """Returned by POST /endpoints only — includes the plaintext signing secret.

    The secret is returned exactly once and never stored in plaintext.
    """

    secret: str


class EndpointPageResponse(BaseModel):
    """Paginated envelope returned by GET /endpoints."""

    items: list[EndpointResponse]
    next_cursor: str | None = None


class RotateSecretResponse(BaseModel):
    """Returned by POST /endpoints/{id}/rotate-secret — the new plaintext secret only."""

    secret: str
