"""uptime samples + per-site uptime config (session 2.7)

Adds the `uptime_samples` rolling-buffer table (one external HTTP probe per site
per ~60s) and two site columns: `uptime_enabled` (per-site checker switch) and
`check_url` (optional check-URL override). The site health dot is now driven by
these checks; the Fleet Health uptime component reads a 30-day window here.

Revision ID: c7e1a9f2b40d
Revises: b2d4f6a8c1e3
Create Date: 2026-07-10 16:30:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'c7e1a9f2b40d'
down_revision: str | Sequence[str] | None = 'b2d4f6a8c1e3'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'sites',
        sa.Column(
            'uptime_enabled', sa.Boolean(), nullable=False, server_default=sa.true()
        ),
    )
    op.add_column('sites', sa.Column('check_url', sa.String(length=500), nullable=True))

    op.create_table(
        'uptime_samples',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('site_id', sa.Integer(), nullable=False),
        sa.Column('up', sa.Boolean(), nullable=False),
        sa.Column('status_code', sa.Integer(), nullable=True),
        sa.Column('latency_ms', sa.Float(), nullable=True),
        sa.Column('error', sa.String(length=300), nullable=True),
        sa.Column('ts', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(['site_id'], ['sites.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        op.f('ix_uptime_samples_site_id'), 'uptime_samples', ['site_id'], unique=False
    )
    op.create_index(
        op.f('ix_uptime_samples_ts'), 'uptime_samples', ['ts'], unique=False
    )
    op.create_index(
        'ix_uptime_samples_site_ts', 'uptime_samples', ['site_id', 'ts'], unique=False
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_uptime_samples_site_ts', table_name='uptime_samples')
    op.drop_index(op.f('ix_uptime_samples_ts'), table_name='uptime_samples')
    op.drop_index(op.f('ix_uptime_samples_site_id'), table_name='uptime_samples')
    op.drop_table('uptime_samples')
    op.drop_column('sites', 'check_url')
    op.drop_column('sites', 'uptime_enabled')
