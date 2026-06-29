"""Pydantic schemas for project and API key provisioning endpoints."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field


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
