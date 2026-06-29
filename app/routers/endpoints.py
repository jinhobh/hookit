"""Router for webhook endpoint registration and management."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_project
from app.db.session import get_session
from app.models.endpoint import Endpoint
from app.models.project import Project
from app.schemas.endpoint import (
    EndpointCreate,
    EndpointCreateResponse,
    EndpointResponse,
    EndpointUpdate,
)
from app.services.crypto import encrypt_secret, generate_endpoint_secret
from app.services.ssrf import SSRFError, validate_url_not_ssrf

router = APIRouter(prefix="/endpoints", tags=["endpoints"])


def _get_endpoint_or_404(endpoint_id: uuid.UUID, project: Project, session: Session) -> Endpoint:
    """Return the endpoint owned by *project* or raise 404."""
    endpoint = session.execute(
        select(Endpoint).where(
            Endpoint.id == endpoint_id,
            Endpoint.project_id == project.id,
        )
    ).scalar_one_or_none()
    if endpoint is None:
        raise HTTPException(status_code=404, detail="Endpoint not found")
    return endpoint


@router.post("", status_code=201, response_model=EndpointCreateResponse)
def create_endpoint(
    body: EndpointCreate,
    project: Project = Depends(get_current_project),
    session: Session = Depends(get_session),
) -> EndpointCreateResponse:
    """Register a new webhook endpoint scoped to the authenticated project.

    The signing secret is generated server-side, encrypted at rest, and
    returned **once** in this response.  It will not appear in subsequent
    reads.
    """
    try:
        validate_url_not_ssrf(str(body.url))
    except SSRFError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    plaintext_secret = generate_endpoint_secret()
    endpoint = Endpoint(
        project_id=project.id,
        url=str(body.url),
        event_types=body.event_types,
        secret_enc=encrypt_secret(plaintext_secret),
        status=body.status,
    )
    session.add(endpoint)
    session.commit()
    session.refresh(endpoint)
    return EndpointCreateResponse(
        id=endpoint.id,
        project_id=endpoint.project_id,
        url=endpoint.url,
        event_types=endpoint.event_types,
        status=endpoint.status,
        created_at=endpoint.created_at,
        updated_at=endpoint.updated_at,
        secret=plaintext_secret,
    )


@router.get("", response_model=list[EndpointResponse])
def list_endpoints(
    project: Project = Depends(get_current_project),
    session: Session = Depends(get_session),
) -> list[Endpoint]:
    """List all endpoints belonging to the authenticated project."""
    return list(
        session.execute(select(Endpoint).where(Endpoint.project_id == project.id)).scalars()
    )


@router.patch("/{endpoint_id}", response_model=EndpointResponse)
def update_endpoint(
    endpoint_id: uuid.UUID,
    body: EndpointUpdate,
    project: Project = Depends(get_current_project),
    session: Session = Depends(get_session),
) -> Endpoint:
    """Update url, event_types, or status of an endpoint owned by the project."""
    endpoint = _get_endpoint_or_404(endpoint_id, project, session)
    if body.url is not None:
        try:
            validate_url_not_ssrf(str(body.url))
        except SSRFError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        endpoint.url = str(body.url)
    if body.event_types is not None:
        endpoint.event_types = body.event_types
    if body.status is not None:
        endpoint.status = body.status
    session.commit()
    session.refresh(endpoint)
    return endpoint


@router.delete("/{endpoint_id}", status_code=204)
def delete_endpoint(
    endpoint_id: uuid.UUID,
    project: Project = Depends(get_current_project),
    session: Session = Depends(get_session),
) -> None:
    """Delete an endpoint owned by the authenticated project."""
    endpoint = _get_endpoint_or_404(endpoint_id, project, session)
    session.delete(endpoint)
    session.commit()
