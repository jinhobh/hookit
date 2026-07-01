"""Pydantic schemas for project and API key provisioning endpoints."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ProjectCreate(BaseModel):
    """Body for POST /projects."""

    name: str = Field(min_length=1, max_length=255)


class ProjectResponse(BaseModel):
    """Project representation returned by POST /projects."""

    id: uuid.UUID
    name: str
    created_at: datetime

    model_config = {"from_attributes": True}


class ApiKeyCreate(BaseModel):
    """Body for POST /projects/{project_id}/api-keys."""

    name: str = Field(min_length=1, max_length=255)


class ApiKeyCreateResponse(BaseModel):
    """Returned by POST /projects/{project_id}/api-keys only.

    The plaintext key is returned exactly once and never stored.
    """

    id: uuid.UUID
    key: str
    prefix: str
    name: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class ApiKeyListItem(BaseModel):
    """Summary of an API key returned by GET /projects/{project_id}/api-keys.

    key_hash is intentionally absent from this schema.
    """

    id: uuid.UUID
    prefix: str = Field(validation_alias="key_prefix")
    name: str | None
    created_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


class ApiKeyPageResponse(BaseModel):
    """Paginated response for GET /projects/{project_id}/api-keys."""

    items: list[ApiKeyListItem]
    next_cursor: str | None = None
