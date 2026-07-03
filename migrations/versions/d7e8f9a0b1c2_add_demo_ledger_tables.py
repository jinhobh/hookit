"""add demo ledger tables and per-endpoint receiver mode

Revision ID: d7e8f9a0b1c2
Revises: c8d9e0f1a2b3
Create Date: 2026-07-02 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d7e8f9a0b1c2"
down_revision: str | Sequence[str] | None = "c8d9e0f1a2b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the two-banks ledger tables; extend receiver health with a mode.

    ``demo_ledger_accounts`` holds each bank's per-account balance (a bank is
    identified by its showcase endpoint). ``demo_ledger_processed`` is the safe
    bank's processed-events table — its primary key is what makes duplicate
    deliveries a no-op. ``demo_receiver_health.mode`` extends the boolean
    toggle with the banks' tri-state health (healthy / flaky / down).
    """
    op.create_table(
        "demo_ledger_accounts",
        sa.Column("endpoint_id", sa.Uuid(), nullable=False),
        sa.Column("account", sa.Text(), nullable=False),
        sa.Column("balance", sa.Numeric(18, 2), nullable=False),
        sa.Column("status", sa.Text(), nullable=True),
        sa.Column("status_as_of", sa.DateTime(timezone=True), nullable=True),
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
            name="fk_demo_ledger_accounts_endpoint_id",
        ),
        sa.PrimaryKeyConstraint("endpoint_id", "account"),
    )
    op.create_table(
        "demo_ledger_processed",
        sa.Column("endpoint_id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column(
            "processed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["endpoint_id"],
            ["endpoints.id"],
            ondelete="CASCADE",
            name="fk_demo_ledger_processed_endpoint_id",
        ),
        sa.PrimaryKeyConstraint("endpoint_id", "event_id"),
    )
    op.add_column(
        "demo_receiver_health",
        sa.Column("mode", sa.Text(), server_default=sa.text("'healthy'"), nullable=False),
    )


def downgrade() -> None:
    """Drop the ledger tables and the receiver mode column."""
    op.drop_column("demo_receiver_health", "mode")
    op.drop_table("demo_ledger_processed")
    op.drop_table("demo_ledger_accounts")
