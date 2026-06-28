"""Delivery ORM model."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Enum, ForeignKey, Index, Integer
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.endpoint import Endpoint
from app.models.event import Event

if TYPE_CHECKING:
    from app.models.delivery_attempt import DeliveryAttempt


def _utcnow() -> datetime:
    return datetime.now(UTC)


class DeliveryStatus(StrEnum):
    pending = "pending"
    in_flight = "in_flight"
    succeeded = "succeeded"
    failed = "failed"
    dead_lettered = "dead_lettered"


class Delivery(Base):
    """A unit of work: deliver one Event to one Endpoint.

    Created during event ingestion fan-out (status=PENDING); transitions are
    driven by the delivery worker.
    """

    __tablename__ = "deliveries"
    __table_args__ = (Index("ix_deliveries_status_next_attempt_at", "status", "next_attempt_at"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    event_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("events.id", ondelete="CASCADE"), nullable=False
    )
    endpoint_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("endpoints.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[DeliveryStatus] = mapped_column(
        Enum(DeliveryStatus, name="delivery_status"),
        default=DeliveryStatus.pending,
        nullable=False,
    )
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    leased_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )

    event: Mapped[Event] = relationship("Event", back_populates="deliveries")
    endpoint: Mapped[Endpoint] = relationship("Endpoint")
    attempts: Mapped[list[DeliveryAttempt]] = relationship(
        "DeliveryAttempt",
        back_populates="delivery",
        order_by="DeliveryAttempt.attempt_number",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return f"Delivery(id={self.id!r}, status={self.status!r})"
