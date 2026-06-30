"""add rate_limit_rps to endpoints

Revision ID: c1d2e3f4a5b6
Revises: b2c3d4e5f6a7
Create Date: 2026-06-30 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c1d2e3f4a5b6"
down_revision: str | Sequence[str] | None = "b2c3d4e5f6a7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add nullable rate_limit_rps column to endpoints."""
    op.add_column("endpoints", sa.Column("rate_limit_rps", sa.Float(), nullable=True))


def downgrade() -> None:
    """Remove rate_limit_rps column from endpoints."""
    op.drop_column("endpoints", "rate_limit_rps")
