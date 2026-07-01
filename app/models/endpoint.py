"""Endpoint ORM model."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import DateTime, Enum, Float, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.project import Project


def _utcnow() -> datetime:
    return datetime.now(UTC)


class EndpointStatus(StrEnum):
    active = "active"
    inactive = "inactive"


class PayloadFormat(StrEnum):
    """How the worker shapes the outbound body for this endpoint.

    ``raw`` sends the platform's native envelope; ``discord`` transforms the
    event into a Discord webhook message (embed) so deliveries render as chat
    messages in a Discord channel.
    """

    raw = "raw"
    discord = "discord"


class Endpoint(Base):
    """A webhook endpoint belonging to a Project.

    ``secret_enc`` holds a Fernet-encrypted signing secret; the plaintext is
    returned once on creation and never persisted or logged in plaintext.
    """

    __tablename__ = "endpoints"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    event_types: Mapped[list[str]] = mapped_column(ARRAY(Text()), nullable=False)
    secret_enc: Mapped[str] = mapped_column(Text(), nullable=False)
    status: Mapped[EndpointStatus] = mapped_column(
        Enum(EndpointStatus, name="endpoint_status"), nullable=False
    )
    payload_format: Mapped[PayloadFormat] = mapped_column(
        Enum(PayloadFormat, name="payload_format"),
        default=PayloadFormat.raw,
        server_default=PayloadFormat.raw.value,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )
    rate_limit_rps: Mapped[float | None] = mapped_column(Float, nullable=True)

    project: Mapped[Project] = relationship("Project")

    def __repr__(self) -> str:
        return f"Endpoint(id={self.id!r}, url={self.url!r}, status={self.status!r})"
