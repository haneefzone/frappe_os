"""benches: discovered Frappe bench inventory

Adds the `benches` table (session 1.6). One row per plain `bench init` install
found on a server over SSH: its path, parsed versions (frappe/python/node), the
port map read from sites/common_site_config.json, production flag, and a
lifecycle status (active | missing). Upserts key on (server_id, path).

Revision ID: c4e21a7f9b03
Revises: c1e2f3a4b5c6
Create Date: 2026-07-10 02:10:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'c4e21a7f9b03'
# Chained after the terminal-sessions migration (FDM 1.5, committed as
# 218b381) so the two concurrently-authored 1.5/1.6 migrations form a single
# linear head rather than a fork.
down_revision: str | Sequence[str] | None = 'c1e2f3a4b5c6'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'benches',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('server_id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=200), nullable=False),
        sa.Column('path', sa.String(length=500), nullable=False),
        sa.Column('frappe_version', sa.String(length=50), nullable=True),
        sa.Column('python_version', sa.String(length=50), nullable=True),
        sa.Column('node_version', sa.String(length=50), nullable=True),
        sa.Column('webserver_port', sa.Integer(), nullable=True),
        sa.Column('socketio_port', sa.Integer(), nullable=True),
        sa.Column('redis_cache_port', sa.Integer(), nullable=True),
        sa.Column('redis_queue_port', sa.Integer(), nullable=True),
        sa.Column('redis_socketio_port', sa.Integer(), nullable=True),
        sa.Column('file_watcher_port', sa.Integer(), nullable=True),
        sa.Column('is_production', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('status', sa.String(length=20), nullable=False, server_default='active'),
        sa.Column('discovered_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            'created_at', sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column(
            'updated_at', sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.ForeignKeyConstraint(['server_id'], ['servers.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('server_id', 'path', name='uq_benches_server_path'),
    )
    op.create_index('ix_benches_server_id', 'benches', ['server_id'], unique=False)
    op.create_index('ix_benches_status', 'benches', ['status'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_benches_status', table_name='benches')
    op.drop_index('ix_benches_server_id', table_name='benches')
    op.drop_table('benches')
