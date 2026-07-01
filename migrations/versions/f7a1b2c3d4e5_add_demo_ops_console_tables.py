"""add demo ops console tables

Revision ID: f7a1b2c3d4e5
Revises: a1b2c3d4e5f6
Create Date: 2026-07-01 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f7a1b2c3d4e5"
down_revision: str | Sequence[str] | None = "a1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the demo receiver health toggle and received-request inbox tables."""
    op.create_table(
        "demo_receiver_health",
        sa.Column("endpoint_id", sa.Uuid(), nullable=False),
        sa.Column("healthy", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["endpoint_id"],
            ["endpoints.id"],
            ondelete="CASCADE",
            name="fk_demo_receiver_health_endpoint_id",
        ),
        sa.PrimaryKeyConstraint("endpoint_id"),
    )
    op.create_table(
        "demo_received_requests",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("endpoint_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("verified", sa.Boolean(), nullable=False),
        sa.Column("response_status", sa.Integer(), nullable=False),
        sa.Column("signature_header", sa.Text(), nullable=True),
        sa.Column("timestamp_header", sa.Text(), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["endpoint_id"],
            ["endpoints.id"],
            ondelete="CASCADE",
            name="fk_demo_received_requests_endpoint_id",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_demo_received_requests_endpoint_received",
        "demo_received_requests",
        ["endpoint_id", "received_at"],
    )


def downgrade() -> None:
    """Drop the demo Ops Console tables."""
    op.drop_index(
        "ix_demo_received_requests_endpoint_received",
        table_name="demo_received_requests",
    )
    op.drop_table("demo_received_requests")
    op.drop_table("demo_receiver_health")
