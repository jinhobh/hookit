"""API key management service."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.api_key import ApiKey


def revoke_api_key(
    *,
    session: Session,
    project_id: uuid.UUID,
    key_id: uuid.UUID,
) -> bool:
    """Set revoked_at on an ApiKey scoped to the given project.

    Returns True if the key was found (and either already revoked or just
    revoked now); raises LookupError if the key does not exist or belongs
    to a different project.
    """
    api_key = session.execute(
        select(ApiKey).where(
            ApiKey.id == key_id,
            ApiKey.project_id == project_id,
        )
    ).scalar_one_or_none()

    if api_key is None:
        raise LookupError(key_id)

    if api_key.revoked_at is None:
        api_key.revoked_at = datetime.now(UTC)
        session.commit()

    return True
