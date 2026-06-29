"""Shared keyset cursor helpers for paginated list endpoints."""

from __future__ import annotations

import base64
import uuid
from datetime import datetime

from fastapi import HTTPException


def encode_cursor(created_at: datetime, row_id: uuid.UUID) -> str:
    raw = f"{created_at.isoformat()}|{row_id}"
    return base64.urlsafe_b64encode(raw.encode()).decode()


def decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        created_at_str, id_str = raw.split("|", 1)
        return datetime.fromisoformat(created_at_str), uuid.UUID(id_str)
    except Exception:
        raise HTTPException(status_code=422, detail="Invalid cursor") from None
