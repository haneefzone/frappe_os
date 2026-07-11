"""Backup compliance policies, statuses + breach events (session 2.3)

Adds three tables:
- `backup_policies`          — per-site RPO / retention / offsite policy (unique site_id)
- `compliance_statuses`      — latest evaluated state per policied site (unique site_id)
- `compliance_breach_events` — channel-free breach records for session 3.1 to consume

Revision ID: f8b3d1c6a2e9
Revises: f7a2c9e1b3d4
Create Date: 2026-07-11 16:20:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'f8b3d1c6a2e9'
down_revision: str | Sequence[str] | None = 'f7a2c9e1b3d4'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# JSONB on Postgres, plain JSON on the SQLite fallback (mirrors the models).
_JSON = sa.JSON().with_variant(JSONB(), "postgresql")


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'backup_policies',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('site_id', sa.Integer(), nullable=False),
        sa.Column('rpo_hours', sa.Integer(), nullable=False),
        sa.Column('retention_days', sa.Integer(), nullable=True),
        sa.Column(
            'require_offsite', sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column(
            'require_restore_test',
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column(
            'enabled', sa.Boolean(), nullable=False, server_default=sa.true()
        ),
        sa.Column('created_by', sa.Integer(), nullable=True),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            'updated_at',
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(['site_id'], ['sites.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['created_by'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('site_id', name='uq_backup_policies_site'),
    )
    op.create_index(
        op.f('ix_backup_policies_site_id'), 'backup_policies', ['site_id'], unique=False
    )

    op.create_table(
        'compliance_statuses',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('site_id', sa.Integer(), nullable=False),
        sa.Column('state', sa.String(length=20), nullable=False),
        sa.Column('last_backup_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('breaches', _JSON, nullable=True),
        sa.Column('evaluated_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            'updated_at',
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(['site_id'], ['sites.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('site_id', name='uq_compliance_statuses_site'),
    )
    op.create_index(
        op.f('ix_compliance_statuses_site_id'),
        'compliance_statuses',
        ['site_id'],
        unique=False,
    )
    op.create_index(
        op.f('ix_compliance_statuses_state'),
        'compliance_statuses',
        ['state'],
        unique=False,
    )

    op.create_table(
        'compliance_breach_events',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('site_id', sa.Integer(), nullable=True),
        sa.Column('site_name', sa.String(length=200), nullable=True),
        sa.Column('breaches', _JSON, nullable=True),
        sa.Column('last_backup_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('rpo_hours', sa.Integer(), nullable=True),
        sa.Column('consumed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(['site_id'], ['sites.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        op.f('ix_compliance_breach_events_site_id'),
        'compliance_breach_events',
        ['site_id'],
        unique=False,
    )
    op.create_index(
        op.f('ix_compliance_breach_events_created_at'),
        'compliance_breach_events',
        ['created_at'],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        op.f('ix_compliance_breach_events_created_at'),
        table_name='compliance_breach_events',
    )
    op.drop_index(
        op.f('ix_compliance_breach_events_site_id'),
        table_name='compliance_breach_events',
    )
    op.drop_table('compliance_breach_events')

    op.drop_index(
        op.f('ix_compliance_statuses_state'), table_name='compliance_statuses'
    )
    op.drop_index(
        op.f('ix_compliance_statuses_site_id'), table_name='compliance_statuses'
    )
    op.drop_table('compliance_statuses')

    op.drop_index(
        op.f('ix_backup_policies_site_id'), table_name='backup_policies'
    )
    op.drop_table('backup_policies')
