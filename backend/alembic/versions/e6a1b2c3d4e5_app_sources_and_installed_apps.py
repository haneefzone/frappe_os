"""app sources + installed apps (session 1.9)

Adds:
- `app_sources` — a repo or marketplace name the platform can `bench get-app`
  from, with a Fernet-encrypted deploy key for private repos;
- `installed_apps` — one row per (site, app): the app×site matrix, with the
  branch/version each site is on. `app_source_id` is nullable (discovered apps
  and apps whose source was later deleted).

Revision ID: e6a1b2c3d4e5
Revises: d5f2c1a9e4b7
Create Date: 2026-07-10 08:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'e6a1b2c3d4e5'
down_revision: str | Sequence[str] | None = 'd5f2c1a9e4b7'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'app_sources',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=120), nullable=False),
        sa.Column('repo_url', sa.String(length=300), nullable=False),
        sa.Column('kind', sa.String(length=20), nullable=False, server_default='marketplace'),
        sa.Column('default_branch', sa.String(length=100), nullable=True),
        sa.Column('is_private', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('deploy_key_enc', sa.Text(), nullable=True),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column(
            'created_at', sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column(
            'updated_at', sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name', name='uq_app_sources_name'),
    )

    op.create_table(
        'installed_apps',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('site_id', sa.Integer(), nullable=False),
        sa.Column('bench_id', sa.Integer(), nullable=False),
        sa.Column('app_source_id', sa.Integer(), nullable=True),
        sa.Column('app_name', sa.String(length=120), nullable=False),
        sa.Column('branch', sa.String(length=100), nullable=True),
        sa.Column('version', sa.String(length=50), nullable=True),
        sa.Column('installed_at', sa.DateTime(timezone=True), nullable=True),
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
        sa.ForeignKeyConstraint(['app_source_id'], ['app_sources.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('site_id', 'app_name', name='uq_installed_apps_site_app'),
    )
    op.create_index('ix_installed_apps_site_id', 'installed_apps', ['site_id'])
    op.create_index('ix_installed_apps_bench_id', 'installed_apps', ['bench_id'])
    op.create_index('ix_installed_apps_app_source_id', 'installed_apps', ['app_source_id'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_installed_apps_app_source_id', table_name='installed_apps')
    op.drop_index('ix_installed_apps_bench_id', table_name='installed_apps')
    op.drop_index('ix_installed_apps_site_id', table_name='installed_apps')
    op.drop_table('installed_apps')
    op.drop_table('app_sources')
