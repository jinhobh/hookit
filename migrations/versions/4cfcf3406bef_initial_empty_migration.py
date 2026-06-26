"""initial empty migration

Revision ID: 4cfcf3406bef
Revises:
Create Date: 2026-06-26 21:51:16.013831

"""

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "4cfcf3406bef"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
