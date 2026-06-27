"""ApiKey ORM model and key-generation helper."""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.project import Project

_KEY_PREFIX = "whk_"
# Characters of the random portion to include in the display prefix.
_PREFIX_RANDOM_CHARS = 8


def _utcnow() -> datetime:
    return datetime.now(UTC)


def generate_api_key() -> tuple[str, str, str]:
    """Generate a high-entropy API key.

    Returns ``(plaintext, key_prefix, key_hash)``.

    - ``plaintext``: the full secret; returned once, never stored.
    - ``key_prefix``: a short, human-readable prefix (e.g. ``whk_aBcDeFgH``)
      stored in the database for display and soft-lookup.
    - ``key_hash``: SHA-256 hex digest of the plaintext, stored in the database
      for verification.
    """
    raw = secrets.token_urlsafe(32)  # 256 bits of entropy
    plaintext = f"{_KEY_PREFIX}{raw}"
    key_prefix = plaintext[: len(_KEY_PREFIX) + _PREFIX_RANDOM_CHARS]
    key_hash = hashlib.sha256(plaintext.encode()).hexdigest()
    return plaintext, key_prefix, key_hash


class ApiKey(Base):
    """An API key scoped to a Project.

    Only ``key_prefix`` and ``key_hash`` are persisted; the plaintext is
    returned once at creation and never stored or logged.
    """

    __tablename__ = "api_keys"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    key_prefix: Mapped[str] = mapped_column(String(16), nullable=False)
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )

    project: Mapped[Project] = relationship("Project")

    def __repr__(self) -> str:
        return f"ApiKey(id={self.id!r}, key_prefix={self.key_prefix!r})"
