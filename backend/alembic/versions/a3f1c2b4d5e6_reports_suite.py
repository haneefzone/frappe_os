"""Reports suite: report_runs + schedule/job wiring (session 6.2)

- `report_runs`         — one row per generated report artifact (interactive,
                          queued or scheduled); carries the evidence fields
                          (sha256, byte size, row count, requesting user).
- `schedules.params`    — action-specific config for a `report` schedule
                          (report id, format, recipients, range); + `target_id`
                          relaxed to nullable, since a report schedule has no
                          single row target.
- `command_jobs.server_id` relaxed to nullable, since a platform-local report
                          job targets no managed server.

Revision ID: a3f1c2b4d5e6
Revises: f8b3d1c6a2e9
Create Date: 2026-07-23 05:10:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'a3f1c2b4d5e6'
down_revision: str | Sequence[str] | None = 'f8b3d1c6a2e9'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# JSONB on Postgres, plain JSON on the SQLite fallback (mirrors the models).
_JSON = sa.JSON().with_variant(JSONB(), "postgresql")


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'report_runs',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('report_id', sa.String(length=60), nullable=False),
        sa.Column('params', _JSON, nullable=False),
        sa.Column('format', sa.String(length=10), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('artifact_path', sa.String(length=500), nullable=True),
        sa.Column('artifact_bytes', sa.Integer(), nullable=True),
        sa.Column('sha256', sa.String(length=64), nullable=True),
        sa.Column('row_count', sa.Integer(), nullable=True),
        sa.Column('requested_by', sa.Integer(), nullable=True),
        sa.Column('job_id', sa.Integer(), nullable=True),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['requested_by'], ['users.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['job_id'], ['command_jobs.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        op.f('ix_report_runs_report_id'), 'report_runs', ['report_id'], unique=False
    )
    op.create_index(
        op.f('ix_report_runs_status'), 'report_runs', ['status'], unique=False
    )
    op.create_index(
        op.f('ix_report_runs_job_id'), 'report_runs', ['job_id'], unique=False
    )

    # A report schedule stores its config here and points at no row target.
    op.add_column('schedules', sa.Column('params', _JSON, nullable=True))
    op.alter_column(
        'schedules', 'target_id', existing_type=sa.Integer(), nullable=True
    )

    # A platform-local report job targets no managed server.
    op.alter_column(
        'command_jobs', 'server_id', existing_type=sa.Integer(), nullable=True
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.alter_column(
        'command_jobs', 'server_id', existing_type=sa.Integer(), nullable=False
    )
    op.alter_column(
        'schedules', 'target_id', existing_type=sa.Integer(), nullable=False
    )
    op.drop_column('schedules', 'params')

    op.drop_index(op.f('ix_report_runs_job_id'), table_name='report_runs')
    op.drop_index(op.f('ix_report_runs_status'), table_name='report_runs')
    op.drop_index(op.f('ix_report_runs_report_id'), table_name='report_runs')
    op.drop_table('report_runs')
