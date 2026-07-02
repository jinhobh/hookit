"""add worker attribution to deliveries and delivery_attempts

Revision ID: c8d9e0f1a2b3
Revises: f7a1b2c3d4e5
Create Date: 2026-07-02 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c8d9e0f1a2b3"
down_revision: str | Sequence[str] | None = "f7a1b2c3d4e5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Record which worker claimed a delivery and which worker made each attempt."""
    op.add_column("deliveries", sa.Column("claimed_by", sa.Text(), nullable=True))
    op.add_column("delivery_attempts", sa.Column("worker_id", sa.Text(), nullable=True))


def downgrade() -> None:
    """Drop the worker attribution columns."""
    op.drop_column("delivery_attempts", "worker_id")
    op.drop_column("deliveries", "claimed_by")
