"""add demo visitor activity table

Revision ID: a3b4c5d6e7f8
Revises: d7e8f9a0b1c2
Create Date: 2026-07-03 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a3b4c5d6e7f8"
down_revision: str | Sequence[str] | None = "d7e8f9a0b1c2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the demo visitor activity table (idle watchdog last-seen tracking)."""
    op.create_table(
        "demo_visitor_activity",
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            ondelete="CASCADE",
            name="fk_demo_visitor_activity_project_id",
        ),
        sa.PrimaryKeyConstraint("project_id"),
    )


def downgrade() -> None:
    """Drop the demo visitor activity table."""
    op.drop_table("demo_visitor_activity")
