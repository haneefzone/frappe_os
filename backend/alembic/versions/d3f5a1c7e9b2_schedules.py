"""recurring-job schedules (session 2.1)

Adds the `schedules` table: a recurring action (site.backup / backup.retention_
sweep) bound to a target, its cadence (cron OR interval, evaluated in a tz), the
retention policy, an enabled flag, and run bookkeeping (next_run_at, last_run_at,
last_run_job_id). The scheduler process fires due schedules through the JobRunner.

Revision ID: d3f5a1c7e9b2
Revises: c7e1a9f2b40d
Create Date: 2026-07-10 17:10:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'd3f5a1c7e9b2'
down_revision: str | Sequence[str] | None = 'c7e1a9f2b40d'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'schedules',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=200), nullable=False),
        sa.Column('target_type', sa.String(length=20), nullable=False),
        sa.Column('target_id', sa.Integer(), nullable=False),
        sa.Column('action_name', sa.String(length=60), nullable=False),
        sa.Column('cron', sa.String(length=100), nullable=True),
        sa.Column('interval_seconds', sa.Integer(), nullable=True),
        sa.Column('timezone', sa.String(length=64), nullable=False),
        sa.Column('priority', sa.String(length=10), nullable=False),
        sa.Column(
            'with_files', sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column('retention_keep_last', sa.Integer(), nullable=True),
        sa.Column('retention_keep_days', sa.Integer(), nullable=True),
        sa.Column('enabled', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('next_run_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_run_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_run_job_id', sa.Integer(), nullable=True),
        sa.Column('created_by', sa.Integer(), nullable=True),
        sa.Column(
            'created_at', sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column(
            'updated_at', sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ['last_run_job_id'], ['command_jobs.id'], ondelete='SET NULL'
        ),
        sa.ForeignKeyConstraint(['created_by'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        op.f('ix_schedules_target_id'), 'schedules', ['target_id'], unique=False
    )
    op.create_index(
        op.f('ix_schedules_next_run_at'), 'schedules', ['next_run_at'], unique=False
    )
    op.create_index(
        op.f('ix_schedules_last_run_job_id'),
        'schedules',
        ['last_run_job_id'],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_schedules_last_run_job_id'), table_name='schedules')
    op.drop_index(op.f('ix_schedules_next_run_at'), table_name='schedules')
    op.drop_index(op.f('ix_schedules_target_id'), table_name='schedules')
    op.drop_table('schedules')
