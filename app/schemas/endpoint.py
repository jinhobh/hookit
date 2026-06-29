"""Pydantic schemas for webhook endpoint API requests and responses."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import AnyHttpUrl, BaseModel, Field, field_validator

from app.models.endpoint import EndpointStatus


class EndpointCreate(BaseModel):
    """Body for POST /endpoints."""

    url: AnyHttpUrl
    event_types: list[str] = Field(min_length=1)
    status: EndpointStatus = EndpointStatus.active

    @field_validator("event_types")
    @classmethod
    def event_types_nonempty_strings(cls, v: list[str]) -> list[str]:
        for item in v:
            if not item.strip():
                raise ValueError("each event_type must be a non-empty string")
        return v


class EndpointUpdate(BaseModel):
    """Body for PATCH /endpoints/{id}.  All fields are optional."""

    url: AnyHttpUrl | None = None
    event_types: list[str] | None = Field(default=None, min_length=1)
    status: EndpointStatus | None = None

    @field_validator("event_types")
    @classmethod
    def event_types_nonempty_strings(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return v
        for item in v:
            if not item.strip():
                raise ValueError("each event_type must be a non-empty string")
        return v


class EndpointResponse(BaseModel):
    """Endpoint representation returned by GET /endpoints and PATCH /endpoints/{id}."""

    id: uuid.UUID
    project_id: uuid.UUID
    url: str
    event_types: list[str]
    status: EndpointStatus
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class EndpointCreateResponse(EndpointResponse):
    """Returned by POST /endpoints only — includes the plaintext signing secret.

    The secret is returned exactly once and never stored in plaintext.
    """

    secret: str


class RotateSecretResponse(BaseModel):
    """Returned by POST /endpoints/{id}/rotate-secret — the new plaintext secret only."""

    secret: str
