"""ORM models backing the live showcase demo's controllable receiver.

Both tables are demo-scoped and keyed to the reserved, self-referential receiver
``Endpoint`` created by ``app.services.showcase``. They never touch real customer
endpoints.

- ``DemoReceiverHealth`` is the toggle the visitor flips to take their
  downstream "pipeline" up or down. It lives in the database (not process
  memory) so it is visible across connections: the toggle write and the
  ``/showcase/receiver`` read resolve independent sessions, and the real
  out-of-process worker's delivery attempts must observe the current value.
- ``DemoReceivedRequest`` is the endpoint-side inbox: every request the receiver
  actually accepts is recorded with its real signed headers and body so the
  dashboard can prove genuine, HMAC-signed HTTP deliveries arrived.
- ``DemoLedgerAccount`` / ``DemoLedgerProcessed`` back the two-banks ledger
  demo: per-account balances kept by each bank receiver (a bank *is* a
  showcase endpoint), and the safe bank's processed-events table whose primary
  key makes duplicate deliveries a no-op.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, Numeric, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def _utcnow() -> datetime:
    return datetime.now(UTC)


class DemoReceiverHealth(Base):
    """Current health of the controllable receiver ("your pipeline").

    One row per receiver endpoint. ``healthy=False`` makes ``/showcase/receiver``
    answer 503, so the visitor can watch the platform retry, back off, and
    dead-letter — then flip it back and redrive to recover.
    """

    __tablename__ = "demo_receiver_health"

    endpoint_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("endpoints.id", ondelete="CASCADE"), primary_key=True
    )
    healthy: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Tri-state health used by the bank receivers: healthy | flaky | down.
    # "flaky" means "process the webhook, then fail to respond" — the lost-ack
    # scenario. The plumbing receiver keeps using the boolean above.
    mode: Mapped[str] = mapped_column(
        Text, default="healthy", server_default="healthy", nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )

    def __repr__(self) -> str:
        return f"DemoReceiverHealth(endpoint_id={self.endpoint_id!r}, healthy={self.healthy!r})"


class DemoReceivedRequest(Base):
    """One HTTP request the receiver actually accepted (the endpoint inbox).

    Captured at receive time — the signature header in particular cannot be
    reconstructed later, since it is keyed to the exact send timestamp — so the
    dashboard can display the real signed request that arrived. Pruned to the
    most recent few per endpoint by ``app.services.showcase``.
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


class DemoLedgerAccount(Base):
    """One bank's balance for one demo account (the two-banks ledger demo).

    A "bank" is a showcase bank endpoint; both banks keep rows here, keyed by
    their endpoint id. ``balance`` is exact ``numeric`` — money is never a
    float. ``status`` / ``status_as_of`` are the last-write-wins fields the
    time-travel scenario targets: the naive bank stamps them in arrival order,
    the safe bank guards them with the event's ``executed_at`` timestamp.
    """

    __tablename__ = "demo_ledger_accounts"

    endpoint_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("endpoints.id", ondelete="CASCADE"), primary_key=True
    )
    account: Mapped[str] = mapped_column(Text, primary_key=True)
    balance: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    status: Mapped[str | None] = mapped_column(Text, nullable=True)
    status_as_of: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )

    def __repr__(self) -> str:
        return (
            f"DemoLedgerAccount(endpoint_id={self.endpoint_id!r}, "
            f"account={self.account!r}, balance={self.balance!r})"
        )


class DemoLedgerProcessed(Base):
    """The safe bank's processed-events table — receiver-side idempotency.

    One row per (bank endpoint, platform event id). The primary key is the
    dedupe: a redelivered event's INSERT conflicts and the bank answers 200
    without touching the balance. Written in the same transaction as the
    balance update so "processed" and "applied" can never disagree.
    """

    __tablename__ = "demo_ledger_processed"

    endpoint_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("endpoints.id", ondelete="CASCADE"), primary_key=True
    )
    event_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    processed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    def __repr__(self) -> str:
        return f"DemoLedgerProcessed(endpoint_id={self.endpoint_id!r}, event_id={self.event_id!r})"
