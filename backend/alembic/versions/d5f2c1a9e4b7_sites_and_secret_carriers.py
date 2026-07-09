"""sites inventory + secret carriers (session 1.8)

Adds:
- the `sites` table — one row per Frappe site under a discovered bench
  (upserts key on (bench_id, name); active|missing lifecycle like benches);
- `command_jobs.secrets_enc` — a Fernet token carrying a job's user-supplied
  secret params (e.g. a new site's admin password) to the worker, never stored
  in the clear;
- `servers.mariadb_root_password_enc` — the host's MariaDB root password,
  Fernet-encrypted, pulled server-side by `bench new-site` (gotcha #4).

Revision ID: d5f2c1a9e4b7
Revises: c4e21a7f9b03
Create Date: 2026-07-10 06:30:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'd5f2c1a9e4b7'
down_revision: str | Sequence[str] | None = 'c4e21a7f9b03'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'sites',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('bench_id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=200), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False, server_default='active'),
        sa.Column('scheduler_enabled', sa.Boolean(), nullable=True),
        sa.Column('maintenance_mode', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('health', sa.String(length=20), nullable=False, server_default='unknown'),
        sa.Column('discovered_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            'created_at', sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column(
            'updated_at', sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.ForeignKeyConstraint(['bench_id'], ['benches.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('bench_id', 'name', name='uq_sites_bench_name'),
    )
    op.create_index('ix_sites_bench_id', 'sites', ['bench_id'])
    op.create_index('ix_sites_status', 'sites', ['status'])

    op.add_column('command_jobs', sa.Column('secrets_enc', sa.Text(), nullable=True))
    op.add_column(
        'servers', sa.Column('mariadb_root_password_enc', sa.Text(), nullable=True)
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('servers', 'mariadb_root_password_enc')
    op.drop_column('command_jobs', 'secrets_enc')
    op.drop_index('ix_sites_status', table_name='sites')
    op.drop_index('ix_sites_bench_id', table_name='sites')
    op.drop_table('sites')
