"""backups: artifacts + integrity metadata (session 1.11)

Adds `backups` — one row per `bench backup` run, with the artifact paths
(database, public files, private files, and the site_config_backup.json that
carries the encryption_key), each artifact's size + sha256 in a JSON column,
the total size, the source Frappe major (for the restore downgrade guard), the
lifecycle status, the producing job, and a `restore_tested` flag.

Revision ID: f3b9d7c8e21a
Revises: e6a1b2c3d4e5
Create Date: 2026-07-10 12:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'f3b9d7c8e21a'
down_revision: str | Sequence[str] | None = 'e6a1b2c3d4e5'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# JSONB on Postgres, plain JSON elsewhere (mirrors app/models/backup.py).
_ARTIFACTS = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'backups',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('site_id', sa.Integer(), nullable=False),
        sa.Column('bench_id', sa.Integer(), nullable=False),
        sa.Column('type', sa.String(length=20), nullable=False, server_default='db'),
        sa.Column('db_path', sa.String(length=500), nullable=True),
        sa.Column('public_files_path', sa.String(length=500), nullable=True),
        sa.Column('private_files_path', sa.String(length=500), nullable=True),
        sa.Column('config_path', sa.String(length=500), nullable=True),
        sa.Column('size_bytes', sa.Integer(), nullable=True),
        sa.Column('artifacts', _ARTIFACTS, nullable=True),
        sa.Column('status', sa.String(length=20), nullable=False, server_default='pending'),
        sa.Column('frappe_version', sa.String(length=20), nullable=True),
        sa.Column('taken_by_job_id', sa.Integer(), nullable=True),
        sa.Column('restore_tested', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            'created_at', sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column(
            'updated_at', sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.ForeignKeyConstraint(['site_id'], ['sites.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['bench_id'], ['benches.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(
            ['taken_by_job_id'], ['command_jobs.id'], ondelete='SET NULL'
        ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_backups_site_id', 'backups', ['site_id'])
    op.create_index('ix_backups_bench_id', 'backups', ['bench_id'])
    op.create_index('ix_backups_status', 'backups', ['status'])
    op.create_index('ix_backups_taken_by_job_id', 'backups', ['taken_by_job_id'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_backups_taken_by_job_id', table_name='backups')
    op.drop_index('ix_backups_status', table_name='backups')
    op.drop_index('ix_backups_bench_id', table_name='backups')
    op.drop_index('ix_backups_site_id', table_name='backups')
    op.drop_table('backups')
