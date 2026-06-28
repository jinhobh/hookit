"""IdempotencyRecord ORM model."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def _utcnow() -> datetime:
    return datetime.now(UTC)


class IdempotencyRecord(Base):
    """Durable record of a completed POST /events request.

    Keyed by ``(project_id, idempotency_key)``.  Stores a SHA-256 hash of the
    original request body so that a key reused with a different payload can be
    rejected (409).  The cached ``event_id`` and ``queued_deliveries`` allow
    returning the identical response on replay without creating duplicates.
    """

    __tablename__ = "idempotency_records"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "idempotency_key", name="uq_idempotency_records_project_key"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    event_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("events.id", ondelete="CASCADE"), nullable=False
    )
    queued_deliveries: Mapped[int] = mapped_column(Integer, nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    def __repr__(self) -> str:
        return f"IdempotencyRecord(project_id={self.project_id!r}, key={self.idempotency_key!r})"
