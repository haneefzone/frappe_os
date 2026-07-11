"""ai agents module — scoped agent configs + sessions (session 5.1)

Adds the AI Agents module: `ai_agent_configs` (registered scoped CLI agents),
`ai_agent_allowed_servers` (the per-agent server whitelist), and
`ai_agent_sessions` (one jailed session and its pre-change snapshot +
diff/apply/rollback lifecycle).

Chained off the 2.4 domains head. Idempotent: `upgrade()` creates the three
tables + indexes, `downgrade()` drops them cleanly (up/down/up verified).

Revision ID: f7a2c9e1b3d5
Revises: f5a2c9d1e7b4
Create Date: 2026-07-10 20:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'f7a2c9e1b3d5'
down_revision: str | Sequence[str] | None = 'f5a2c9d1e7b4'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'ai_agent_configs',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=120), nullable=False),
        sa.Column('kind', sa.String(length=20), nullable=False),
        sa.Column('command_template', sa.String(length=500), nullable=False),
        sa.Column('working_dir', sa.String(length=300), nullable=False),
        sa.Column('read_only', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            'pre_change_backup', sa.Boolean(), nullable=False, server_default=sa.true()
        ),
        sa.Column(
            'created_at', sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column(
            'updated_at', sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        op.f('ix_ai_agent_configs_name'), 'ai_agent_configs', ['name'], unique=True
    )

    op.create_table(
        'ai_agent_allowed_servers',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('agent_id', sa.Integer(), nullable=False),
        sa.Column('server_id', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ['agent_id'], ['ai_agent_configs.id'], ondelete='CASCADE'
        ),
        sa.ForeignKeyConstraint(['server_id'], ['servers.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('agent_id', 'server_id', name='uq_ai_agent_server'),
    )
    op.create_index(
        op.f('ix_ai_agent_allowed_servers_agent_id'),
        'ai_agent_allowed_servers', ['agent_id'], unique=False,
    )
    op.create_index(
        op.f('ix_ai_agent_allowed_servers_server_id'),
        'ai_agent_allowed_servers', ['server_id'], unique=False,
    )

    op.create_table(
        'ai_agent_sessions',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('agent_id', sa.Integer(), nullable=True),
        sa.Column('server_id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=True),
        sa.Column('ssh_username', sa.String(length=120), nullable=False),
        sa.Column('working_dir', sa.String(length=300), nullable=False),
        sa.Column('read_only', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            'pre_change_backup', sa.Boolean(), nullable=False, server_default=sa.true()
        ),
        sa.Column('status', sa.String(length=20), nullable=False, server_default='starting'),
        sa.Column('base_commit', sa.String(length=64), nullable=True),
        sa.Column('snapshot_ref', sa.String(length=64), nullable=True),
        sa.Column('diff_text', sa.Text(), nullable=True),
        sa.Column('disposition', sa.String(length=20), nullable=True),
        sa.Column('snapshot_job_id', sa.Integer(), nullable=True),
        sa.Column('diff_job_id', sa.Integer(), nullable=True),
        sa.Column('resolve_job_id', sa.Integer(), nullable=True),
        sa.Column('close_reason', sa.String(length=80), nullable=True),
        sa.Column(
            'started_at', sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column('ended_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('duration_seconds', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(
            ['agent_id'], ['ai_agent_configs.id'], ondelete='SET NULL'
        ),
        sa.ForeignKeyConstraint(['server_id'], ['servers.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(
            ['snapshot_job_id'], ['command_jobs.id'], ondelete='SET NULL'
        ),
        sa.ForeignKeyConstraint(
            ['diff_job_id'], ['command_jobs.id'], ondelete='SET NULL'
        ),
        sa.ForeignKeyConstraint(
            ['resolve_job_id'], ['command_jobs.id'], ondelete='SET NULL'
        ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        op.f('ix_ai_agent_sessions_agent_id'), 'ai_agent_sessions', ['agent_id'],
        unique=False,
    )
    op.create_index(
        op.f('ix_ai_agent_sessions_server_id'), 'ai_agent_sessions', ['server_id'],
        unique=False,
    )
    op.create_index(
        op.f('ix_ai_agent_sessions_user_id'), 'ai_agent_sessions', ['user_id'],
        unique=False,
    )
    op.create_index(
        op.f('ix_ai_agent_sessions_status'), 'ai_agent_sessions', ['status'],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_ai_agent_sessions_status'), table_name='ai_agent_sessions')
    op.drop_index(op.f('ix_ai_agent_sessions_user_id'), table_name='ai_agent_sessions')
    op.drop_index(op.f('ix_ai_agent_sessions_server_id'), table_name='ai_agent_sessions')
    op.drop_index(op.f('ix_ai_agent_sessions_agent_id'), table_name='ai_agent_sessions')
    op.drop_table('ai_agent_sessions')

    op.drop_index(
        op.f('ix_ai_agent_allowed_servers_server_id'),
        table_name='ai_agent_allowed_servers',
    )
    op.drop_index(
        op.f('ix_ai_agent_allowed_servers_agent_id'),
        table_name='ai_agent_allowed_servers',
    )
    op.drop_table('ai_agent_allowed_servers')

    op.drop_index(op.f('ix_ai_agent_configs_name'), table_name='ai_agent_configs')
    op.drop_table('ai_agent_configs')
