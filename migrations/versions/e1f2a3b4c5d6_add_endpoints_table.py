"""add endpoints table

Revision ID: e1f2a3b4c5d6
Revises: d4e5f6a7b8c9
Create Date: 2026-06-27 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY

# revision identifiers, used by Alembic.
revision: str = "e1f2a3b4c5d6"
down_revision: str | Sequence[str] | None = "d4e5f6a7b8c9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create endpoints table."""
    op.execute("CREATE TYPE endpoint_status AS ENUM ('active', 'inactive')")
    op.create_table(
        "endpoints",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("url", sa.String(2048), nullable=False),
        sa.Column("event_types", ARRAY(sa.Text()), nullable=False),
        sa.Column("secret_enc", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Enum("active", "inactive", name="endpoint_status", create_type=False),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            ondelete="CASCADE",
            name="fk_endpoints_project_id",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_endpoints_project_id", "endpoints", ["project_id"])


def downgrade() -> None:
    """Drop endpoints table and enum."""
    op.drop_index("ix_endpoints_project_id", table_name="endpoints")
    op.drop_table("endpoints")
    op.execute("DROP TYPE endpoint_status")
