"""add payload_format to endpoints

Revision ID: a1b2c3d4e5f6
Revises: c1d2e3f4a5b6
Create Date: 2026-07-01 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ENUM

# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: str | Sequence[str] | None = "c1d2e3f4a5b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add non-null payload_format column (default 'raw') to endpoints."""
    op.execute("CREATE TYPE payload_format AS ENUM ('raw', 'discord')")
    op.add_column(
        "endpoints",
        sa.Column(
            "payload_format",
            ENUM("raw", "discord", name="payload_format", create_type=False),
            server_default="raw",
            nullable=False,
        ),
    )


def downgrade() -> None:
    """Remove payload_format column and its enum type from endpoints."""
    op.drop_column("endpoints", "payload_format")
    op.execute("DROP TYPE payload_format")
