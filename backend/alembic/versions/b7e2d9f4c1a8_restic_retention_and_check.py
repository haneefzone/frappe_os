"""restic retention policy + integrity-check evidence (session 4.2 — Full-system DR)

Adds the 4.2 columns to `restic_repos`:
- keep_daily / keep_weekly / keep_monthly — the `restic forget --prune` retention
  policy (NULL = that dimension not applied; the forget action refuses to run
  when all three are NULL so a destructive prune never goes out with no policy);
- check_read_data_subset — the `restic check --read-data-subset` selector for
  large repos (NULL = structural, metadata-only check);
- last_check_ok / last_check_message / last_forget_at — integrity + retention
  evidence stamps the §6 backup-evidence view renders.

All columns are nullable adds (idempotent for an existing table with rows); the
downgrade drops them.

Revision ID: b7e2d9f4c1a8
Revises: b5e9d3c1a4f7
Create Date: 2026-08-01 03:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'b7e2d9f4c1a8'
down_revision: str | Sequence[str] | None = 'b5e9d3c1a4f7'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('restic_repos', sa.Column('keep_daily', sa.Integer(), nullable=True))
    op.add_column('restic_repos', sa.Column('keep_weekly', sa.Integer(), nullable=True))
    op.add_column('restic_repos', sa.Column('keep_monthly', sa.Integer(), nullable=True))
    op.add_column(
        'restic_repos',
        sa.Column('check_read_data_subset', sa.String(length=20), nullable=True),
    )
    op.add_column('restic_repos', sa.Column('last_check_ok', sa.Boolean(), nullable=True))
    op.add_column(
        'restic_repos',
        sa.Column('last_check_message', sa.String(length=500), nullable=True),
    )
    op.add_column(
        'restic_repos',
        sa.Column('last_forget_at', sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('restic_repos', 'last_forget_at')
    op.drop_column('restic_repos', 'last_check_message')
    op.drop_column('restic_repos', 'last_check_ok')
    op.drop_column('restic_repos', 'check_read_data_subset')
    op.drop_column('restic_repos', 'keep_monthly')
    op.drop_column('restic_repos', 'keep_weekly')
    op.drop_column('restic_repos', 'keep_daily')
