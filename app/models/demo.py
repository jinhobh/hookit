"""ORM models backing the interactive dashboard demo ("Ops Console").

Both tables are demo-scoped and keyed to a demo ``Endpoint`` (the reserved,
self-referential receiver created by ``app.services.simulate``). They never
touch real customer endpoints.

- ``DemoReceiverHealth`` is the toggle the visitor flips to take their
  downstream "deploy pipeline" up or down. It lives in the database (not
  process memory) so it is visible across connections: the toggle write and
  the ``/simulate/receiver`` read resolve independent sessions, and the real
  out-of-process worker's delivery attempts must observe the current value.
- ``DemoReceivedRequest`` is the endpoint-side inbox: every request the demo
  receiver actually accepts is recorded with its real signed headers and body
  so the dashboard can prove genuine, HMAC-signed HTTP deliveries arrived.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def _utcnow() -> datetime:
    return datetime.now(UTC)


class DemoReceiverHealth(Base):
    """Current health of a demo receiver ("your deploy pipeline").

    One row per demo endpoint. ``healthy=False`` makes ``/simulate/receiver``
    answer 503, so the visitor can watch the platform retry, back off, and
    dead-letter — then flip it back and redrive to recover.
    """

    __tablename__ = "demo_receiver_health"

    endpoint_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("endpoints.id", ondelete="CASCADE"), primary_key=True
    )
    healthy: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )

    def __repr__(self) -> str:
        return f"DemoReceiverHealth(endpoint_id={self.endpoint_id!r}, healthy={self.healthy!r})"


class DemoReceivedRequest(Base):
    """One HTTP request the demo receiver actually accepted (the endpoint inbox).

    Captured at receive time — the signature header in particular cannot be
    reconstructed later, since it is keyed to the exact send timestamp — so the
    dashboard can display the real signed request that arrived. Pruned to the
    most recent few per endpoint by ``app.services.simulate``.
    """

    __tablename__ = "demo_received_requests"
    __table_args__ = (
        Index("ix_demo_received_requests_endpoint_received", "endpoint_id", "received_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    endpoint_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("endpoints.id", ondelete="CASCADE"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    verified: Mapped[bool] = mapped_column(Boolean, nullable=False)
    response_status: Mapped[int] = mapped_column(Integer, nullable=False)
    signature_header: Mapped[str | None] = mapped_column(Text, nullable=True)
    timestamp_header: Mapped[str | None] = mapped_column(Text, nullable=True)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    def __repr__(self) -> str:
        return (
            f"DemoReceivedRequest(id={self.id!r}, event_type={self.event_type!r}, "
            f"response_status={self.response_status!r})"
        )
