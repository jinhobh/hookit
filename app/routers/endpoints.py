"""Router for webhook endpoint registration and management."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_project
from app.db.session import get_session
from app.models.endpoint import Endpoint, EndpointStatus
from app.models.project import Project
from app.routers._pagination import decode_cursor as _decode_cursor
from app.routers._pagination import encode_cursor as _encode_cursor
from app.schemas.endpoint import (
    EndpointCreate,
    EndpointCreateResponse,
    EndpointPageResponse,
    EndpointResponse,
    EndpointUpdate,
    RotateSecretResponse,
)
from app.services.crypto import encrypt_secret, generate_endpoint_secret
from app.services.ssrf import SSRFError, validate_url_not_ssrf

router = APIRouter(prefix="/endpoints", tags=["endpoints"])

_DEFAULT_LIMIT = 20
_MAX_LIMIT = 100


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
        payload_format=body.payload_format,
        rate_limit_rps=body.rate_limit_rps,
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
        payload_format=endpoint.payload_format,
        created_at=endpoint.created_at,
        updated_at=endpoint.updated_at,
        rate_limit_rps=endpoint.rate_limit_rps,
        secret=plaintext_secret,
    )


@router.get("", response_model=EndpointPageResponse)
def list_endpoints(
    limit: Annotated[int, Query(ge=1, le=_MAX_LIMIT)] = _DEFAULT_LIMIT,
    cursor: str | None = None,
    status: EndpointStatus | None = None,
    project: Project = Depends(get_current_project),
    session: Session = Depends(get_session),
) -> EndpointPageResponse:
    """List endpoints for the authenticated project with keyset cursor pagination."""
    stmt = select(Endpoint).where(Endpoint.project_id == project.id)

    if status is not None:
        stmt = stmt.where(Endpoint.status == status)

    if cursor is not None:
        cursor_created_at, cursor_id = _decode_cursor(cursor)
        stmt = stmt.where(
            or_(
                Endpoint.created_at < cursor_created_at,
                and_(
                    Endpoint.created_at == cursor_created_at,
                    Endpoint.id < cursor_id,
                ),
            )
        )

    stmt = stmt.order_by(Endpoint.created_at.desc(), Endpoint.id.desc()).limit(limit + 1)
    rows = list(session.execute(stmt).scalars())

    has_next = len(rows) > limit
    page = rows[:limit]

    next_cursor: str | None = None
    if has_next and page:
        last = page[-1]
        next_cursor = _encode_cursor(last.created_at, last.id)

    items = [EndpointResponse.model_validate(ep) for ep in page]
    return EndpointPageResponse(items=items, next_cursor=next_cursor)


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
    if body.payload_format is not None:
        endpoint.payload_format = body.payload_format
    if body.rate_limit_rps is not None:
        endpoint.rate_limit_rps = body.rate_limit_rps
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


@router.post("/{endpoint_id}/rotate-secret", response_model=RotateSecretResponse)
def rotate_endpoint_secret(
    endpoint_id: uuid.UUID,
    project: Project = Depends(get_current_project),
    session: Session = Depends(get_session),
) -> RotateSecretResponse:
    """Generate a new signing secret for an endpoint, replacing the old one.

    The plaintext secret is returned exactly once and never stored.
    """
    endpoint = _get_endpoint_or_404(endpoint_id, project, session)
    plaintext_secret = generate_endpoint_secret()
    endpoint.secret_enc = encrypt_secret(plaintext_secret)
    session.commit()
    return RotateSecretResponse(secret=plaintext_secret)
