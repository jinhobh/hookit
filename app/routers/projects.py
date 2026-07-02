"""Router for project and API key provisioning.

Admin/bootstrap endpoints — no authentication required in the MVP.
These routes allow operators to create projects and mint API keys without
direct database access. They are intentionally unauthenticated in this phase.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.db.session import get_session
from app.models.api_key import ApiKey, generate_api_key
from app.models.project import Project
from app.routers._pagination import decode_cursor as _decode_cursor
from app.routers._pagination import encode_cursor as _encode_cursor
from app.schemas.project import (
    ApiKeyCreate,
    ApiKeyCreateResponse,
    ApiKeyListItem,
    ApiKeyPageResponse,
    ProjectCreate,
    ProjectPageResponse,
    ProjectResponse,
)
from app.services.api_keys import revoke_api_key

router = APIRouter(prefix="/projects", tags=["projects"])

_DEFAULT_LIMIT = 20
_MAX_LIMIT = 100


def _get_project_or_404(project_id: uuid.UUID, session: Session) -> Project:
    project = session.execute(select(Project).where(Project.id == project_id)).scalar_one_or_none()
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


@router.post("", status_code=201, response_model=ProjectResponse)
def create_project(
    body: ProjectCreate,
    session: Session = Depends(get_session),
) -> ProjectResponse:
    """Create a new project.

    Admin/bootstrap endpoint — no authentication required in the MVP.
    """
    project = Project(name=body.name)
    session.add(project)
    session.commit()
    session.refresh(project)
    return ProjectResponse.model_validate(project)


@router.get("", response_model=ProjectPageResponse)
def list_projects(
    limit: Annotated[int, Query(ge=1, le=_MAX_LIMIT)] = _DEFAULT_LIMIT,
    cursor: str | None = None,
    session: Session = Depends(get_session),
) -> ProjectPageResponse:
    """List all projects with keyset cursor pagination, ordered newest first.

    Admin/bootstrap endpoint — no authentication required in the MVP.
    """
    stmt = select(Project)

    if cursor is not None:
        cursor_created_at, cursor_id = _decode_cursor(cursor)
        stmt = stmt.where(
            or_(
                Project.created_at < cursor_created_at,
                and_(
                    Project.created_at == cursor_created_at,
                    Project.id < cursor_id,
                ),
            )
        )

    stmt = stmt.order_by(Project.created_at.desc(), Project.id.desc()).limit(limit + 1)
    rows = list(session.execute(stmt).scalars())

    has_next = len(rows) > limit
    page = rows[:limit]

    next_cursor: str | None = None
    if has_next and page:
        last = page[-1]
        next_cursor = _encode_cursor(last.created_at, last.id)

    items = [ProjectResponse.model_validate(p) for p in page]
    return ProjectPageResponse(items=items, next_cursor=next_cursor)


@router.get("/{project_id}", response_model=ProjectResponse)
def get_project(
    project_id: uuid.UUID,
    session: Session = Depends(get_session),
) -> ProjectResponse:
    """Return a single project's details.

    Admin/bootstrap endpoint — no authentication required in the MVP.
    Returns 404 if the project does not exist.
    """
    project = _get_project_or_404(project_id, session)
    return ProjectResponse.model_validate(project)


@router.post("/{project_id}/api-keys", status_code=201, response_model=ApiKeyCreateResponse)
def create_api_key(
    project_id: uuid.UUID,
    body: ApiKeyCreate,
    session: Session = Depends(get_session),
) -> ApiKeyCreateResponse:
    """Mint a new API key scoped to the given project.

    Admin/bootstrap endpoint — no authentication required in the MVP.
    The plaintext key is returned exactly once and never stored or logged.
    Returns 404 if the project does not exist.
    """
    project = _get_project_or_404(project_id, session)

    plaintext, key_prefix, key_hash = generate_api_key()
    api_key = ApiKey(
        project_id=project.id,
        name=body.name,
        key_prefix=key_prefix,
        key_hash=key_hash,
    )
    session.add(api_key)
    session.commit()
    session.refresh(api_key)
    return ApiKeyCreateResponse(
        id=api_key.id,
        key=plaintext,
        prefix=api_key.key_prefix,
        name=api_key.name,
        created_at=api_key.created_at,
    )


@router.get("/{project_id}/api-keys", response_model=ApiKeyPageResponse)
def list_api_keys(
    project_id: uuid.UUID,
    limit: Annotated[int, Query(ge=1, le=_MAX_LIMIT)] = _DEFAULT_LIMIT,
    cursor: str | None = None,
    session: Session = Depends(get_session),
) -> ApiKeyPageResponse:
    """List API keys for a project with keyset cursor pagination.

    Admin/bootstrap endpoint — no authentication required in the MVP.
    Returns 404 if the project does not exist.
    key_hash is never included in responses.
    """
    _get_project_or_404(project_id, session)

    stmt = select(ApiKey).where(ApiKey.project_id == project_id)

    if cursor is not None:
        cursor_created_at, cursor_id = _decode_cursor(cursor)
        stmt = stmt.where(
            or_(
                ApiKey.created_at < cursor_created_at,
                and_(
                    ApiKey.created_at == cursor_created_at,
                    ApiKey.id < cursor_id,
                ),
            )
        )

    stmt = stmt.order_by(ApiKey.created_at.desc(), ApiKey.id.desc()).limit(limit + 1)
    rows = list(session.execute(stmt).scalars())

    has_next = len(rows) > limit
    page = rows[:limit]

    next_cursor: str | None = None
    if has_next and page:
        last = page[-1]
        next_cursor = _encode_cursor(last.created_at, last.id)

    items = [ApiKeyListItem.model_validate(key) for key in page]
    return ApiKeyPageResponse(items=items, next_cursor=next_cursor)


@router.delete("/{project_id}/api-keys/{key_id}", status_code=204)
def delete_api_key(
    project_id: uuid.UUID,
    key_id: uuid.UUID,
    session: Session = Depends(get_session),
) -> None:
    """Revoke an API key by setting its revoked_at timestamp.

    Admin/bootstrap endpoint — no authentication required in the MVP.
    Idempotent: revoking an already-revoked key returns 204.
    Returns 404 if the key does not exist or belongs to a different project.
    """
    try:
        revoke_api_key(session=session, project_id=project_id, key_id=key_id)
    except LookupError:
        raise HTTPException(status_code=404, detail="API key not found") from None
