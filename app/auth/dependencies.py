"""FastAPI authentication dependencies."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.session import get_session
from app.models.api_key import ApiKey, hash_api_key
from app.models.project import Project

_bearer = HTTPBearer(auto_error=False)

_401 = HTTPException(
    status_code=401,
    detail="Invalid or missing API key",
    headers={"WWW-Authenticate": "Bearer"},
)


def get_current_project(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    session: Session = Depends(get_session),
) -> Project:
    """Resolve a Bearer API key to its owning Project.

    Raises 401 for missing, malformed, revoked, or unknown tokens.
    Updates ``last_used_at`` on the matching ApiKey row.

    The key is never logged; comparison is hash-based so no timing oracle
    is created against the plaintext.
    """
    if credentials is None:
        raise _401

    token = credentials.credentials
    key_hash = hash_api_key(token)

    api_key = session.execute(
        select(ApiKey).where(ApiKey.key_hash == key_hash)
    ).scalar_one_or_none()

    if api_key is None or api_key.revoked_at is not None:
        raise _401

    # Load the project relationship before commit to avoid an extra round-trip
    # after SQLAlchemy expires the identity map on session.commit().
    project = api_key.project
    api_key.last_used_at = datetime.now(UTC)
    session.commit()
    return project
