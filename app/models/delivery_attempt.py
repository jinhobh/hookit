"""DeliveryAttempt ORM model."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.delivery import Delivery


def _utcnow() -> datetime:
    return datetime.now(UTC)


class DeliveryAttempt(Base):
    """A single HTTP delivery attempt for one Delivery.

    Written by the worker after every attempt (successful or not).
    ``response_body`` is truncated to avoid storing large payloads.
    """

    __tablename__ = "delivery_attempts"
    __table_args__ = (
        UniqueConstraint(
            "delivery_id",
            "attempt_number",
            name="uq_delivery_attempts_delivery_id_attempt_number",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    delivery_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("deliveries.id", ondelete="CASCADE"), nullable=False
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    response_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_body: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Which worker loop made this attempt (observability only).
    worker_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    delivery: Mapped[Delivery] = relationship("Delivery", back_populates="attempts")

    def __repr__(self) -> str:
        return (
            f"DeliveryAttempt(id={self.id!r}, delivery_id={self.delivery_id!r}, "
            f"attempt_number={self.attempt_number!r})"
        )
