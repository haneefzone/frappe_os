"""restic weekly snapshots: retention policy + integrity-check evidence (4.2)

Extends `restic_repos` (session 4.1) with the columns session 4.2 needs to drive
weekly full-system snapshots, retention pruning and periodic integrity checks:

- `last_check_ok` / `last_check_summary` — the result of the most recent
  `restic check` (a False raises the breach alert); the summary is restic's own
  credential-free verdict line only (golden rule 6).
- `last_forget_at` — when the last `restic forget --prune` retention sweep ran.
- `retention_keep_last/daily/weekly/monthly` — the per-repo retention policy the
  forget action turns into restic `--keep-*` flags. All NULL = no policy, and the
  action refuses to prune (an empty policy would delete every snapshot).

Idempotent + reversible: `batch_alter_table` so both PostgreSQL and the SQLite
test fallback add on upgrade and drop on downgrade cleanly (golden rule 10).

Revision ID: d4e2f7a9c6b1
Revises: e2f4a6c8d1b3
Create Date: 2026-09-11 00:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'd4e2f7a9c6b1'
down_revision: str | Sequence[str] | None = 'f0e1d2c3b4a5'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('restic_repos') as batch_op:
        batch_op.add_column(sa.Column('last_check_ok', sa.Boolean(), nullable=True))
        batch_op.add_column(
            sa.Column('last_check_summary', sa.String(length=500), nullable=True)
        )
        batch_op.add_column(
            sa.Column('last_forget_at', sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(sa.Column('retention_keep_last', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('retention_keep_daily', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('retention_keep_weekly', sa.Integer(), nullable=True))
        batch_op.add_column(
            sa.Column('retention_keep_monthly', sa.Integer(), nullable=True)
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('restic_repos') as batch_op:
        batch_op.drop_column('retention_keep_monthly')
        batch_op.drop_column('retention_keep_weekly')
        batch_op.drop_column('retention_keep_daily')
        batch_op.drop_column('retention_keep_last')
        batch_op.drop_column('last_forget_at')
        batch_op.drop_column('last_check_summary')
        batch_op.drop_column('last_check_ok')
