"""initial

Revision ID: 051579833a52
Revises: 
Create Date: 2026-07-07 19:33:17.891333

"""
from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = '051579833a52'
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
