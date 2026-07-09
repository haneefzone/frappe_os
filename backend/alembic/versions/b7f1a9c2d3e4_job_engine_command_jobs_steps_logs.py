"""job engine: command_jobs, command_steps, log_entries

Revision ID: b7f1a9c2d3e4
Revises: 0cdc7d2ec643
Create Date: 2026-07-10 06:40:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'b7f1a9c2d3e4'
down_revision: str | Sequence[str] | None = '0cdc7d2ec643'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'command_jobs',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('server_id', sa.Integer(), nullable=False),
        sa.Column('target_type', sa.String(length=20), nullable=False),
        sa.Column('target_id', sa.String(length=255), nullable=True),
        sa.Column('action_name', sa.String(length=120), nullable=False),
        sa.Column('priority', sa.String(length=10), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('rq_job_id', sa.String(length=64), nullable=True),
        sa.Column(
            'params_sanitized',
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'),
            nullable=False,
        ),
        sa.Column('lock_key', sa.String(length=255), nullable=True),
        sa.Column('retry_count', sa.Integer(), nullable=False),
        sa.Column('exit_code', sa.Integer(), nullable=True),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('ended_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_by', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'),
                  nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'),
                  nullable=False),
        sa.ForeignKeyConstraint(['server_id'], ['servers.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['created_by'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_command_jobs_server_id'), 'command_jobs', ['server_id'])
    op.create_index(op.f('ix_command_jobs_action_name'), 'command_jobs', ['action_name'])
    op.create_index(op.f('ix_command_jobs_status'), 'command_jobs', ['status'])
    op.create_index(op.f('ix_command_jobs_created_at'), 'command_jobs', ['created_at'])

    op.create_table(
        'command_steps',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('job_id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=200), nullable=False),
        sa.Column('step_order', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('ended_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('error_traceback', sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(['job_id'], ['command_jobs.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_command_steps_job_id'), 'command_steps', ['job_id'])

    op.create_table(
        'log_entries',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('job_id', sa.Integer(), nullable=False),
        sa.Column('seq', sa.Integer(), nullable=False),
        sa.Column('stream', sa.String(length=10), nullable=False),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('ts', sa.DateTime(timezone=True), server_default=sa.text('now()'),
                  nullable=False),
        sa.ForeignKeyConstraint(['job_id'], ['command_jobs.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('job_id', 'seq', name='uq_log_entries_job_seq'),
    )
    op.create_index(op.f('ix_log_entries_job_id'), 'log_entries', ['job_id'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_log_entries_job_id'), table_name='log_entries')
    op.drop_table('log_entries')
    op.drop_index(op.f('ix_command_steps_job_id'), table_name='command_steps')
    op.drop_table('command_steps')
    op.drop_index(op.f('ix_command_jobs_created_at'), table_name='command_jobs')
    op.drop_index(op.f('ix_command_jobs_status'), table_name='command_jobs')
    op.drop_index(op.f('ix_command_jobs_action_name'), table_name='command_jobs')
    op.drop_index(op.f('ix_command_jobs_server_id'), table_name='command_jobs')
    op.drop_table('command_jobs')
