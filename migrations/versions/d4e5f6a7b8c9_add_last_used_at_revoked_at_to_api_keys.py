"""add last_used_at and revoked_at to api_keys

Revision ID: d4e5f6a7b8c9
Revises: 3a8f1c2d4e9b
Create Date: 2026-06-27 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d4e5f6a7b8c9"
down_revision: str | Sequence[str] | None = "3a8f1c2d4e9b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add last_used_at and revoked_at nullable columns to api_keys."""
    op.add_column(
        "api_keys",
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "api_keys",
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    """Remove last_used_at and revoked_at from api_keys."""
    op.drop_column("api_keys", "revoked_at")
    op.drop_column("api_keys", "last_used_at")
