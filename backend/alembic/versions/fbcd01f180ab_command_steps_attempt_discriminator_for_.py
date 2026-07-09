"""command_steps: attempt discriminator for auto-retries

Adds `command_steps.attempt` (1-based) so steps that reuse `step_order` across
idempotent auto-retries of the same job can be told apart by the step-timeline
UI (DOO-96). Existing rows default to attempt 1, which is the correct reading:
before this change every step was recorded as if it belonged to a single run.
A composite index backs the timeline fetch (one job, ordered by attempt+order).

Revision ID: fbcd01f180ab
Revises: b7f1a9c2d3e4
Create Date: 2026-07-10 00:45:44.723963

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'fbcd01f180ab'
down_revision: str | Sequence[str] | None = 'b7f1a9c2d3e4'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'command_steps',
        sa.Column('attempt', sa.Integer(), server_default='1', nullable=False),
    )
    op.create_index(
        'ix_command_steps_job_attempt_order',
        'command_steps',
        ['job_id', 'attempt', 'step_order'],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_command_steps_job_attempt_order', table_name='command_steps')
    op.drop_column('command_steps', 'attempt')
