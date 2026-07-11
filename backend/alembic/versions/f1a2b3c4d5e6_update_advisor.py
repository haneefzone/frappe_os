"""update advisor — upstream tag cache + per-app version status (session 3.2)

Adds two read-only tables backing the update advisor:

- `upstream_tag_cache` — cached `git ls-remote --tags` output per upstream repo
  (keyed on a normalised repo_key), TTL-guarded so polls reuse a fresh fetch.
- `app_version_status` — one row per installed app: installed_ref, latest_ref,
  behind_by, optional security_update flag, checked_at. The "behind by N" chips
  and the dashboard "updates available" count read this.

No update is performed by 3.2 (that is the 3.3 safe-update pipeline).

Revision ID: f1a2b3c4d5e6
Revises: d3f5a1c7e9b2
Create Date: 2026-07-10 18:30:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'f1a2b3c4d5e6'
down_revision: str | Sequence[str] | None = 'b3c1d5e7f9a2'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'upstream_tag_cache',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('repo_key', sa.String(length=300), nullable=False),
        sa.Column('remote_url', sa.String(length=300), nullable=False),
        sa.Column('tags_json', sa.Text(), nullable=False, server_default='[]'),
        sa.Column('fetched_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_error', sa.Text(), nullable=True),
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
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('repo_key', name='uq_upstream_tag_cache_repo_key'),
    )

    op.create_table(
        'app_version_status',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('installed_app_id', sa.Integer(), nullable=False),
        sa.Column('site_id', sa.Integer(), nullable=False),
        sa.Column('app_name', sa.String(length=120), nullable=False),
        sa.Column('branch', sa.String(length=100), nullable=True),
        sa.Column('repo_key', sa.String(length=300), nullable=True),
        sa.Column('installed_ref', sa.String(length=80), nullable=True),
        sa.Column('latest_ref', sa.String(length=80), nullable=True),
        sa.Column('behind_by', sa.Integer(), nullable=True),
        sa.Column(
            'security_update', sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column('checked_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_error', sa.Text(), nullable=True),
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
        sa.ForeignKeyConstraint(
            ['installed_app_id'], ['installed_apps.id'], ondelete='CASCADE'
        ),
        sa.ForeignKeyConstraint(['site_id'], ['sites.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'installed_app_id', name='uq_app_version_status_installed_app'
        ),
    )
    op.create_index(
        op.f('ix_app_version_status_installed_app_id'),
        'app_version_status',
        ['installed_app_id'],
        unique=False,
    )
    op.create_index(
        op.f('ix_app_version_status_site_id'),
        'app_version_status',
        ['site_id'],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        op.f('ix_app_version_status_site_id'), table_name='app_version_status'
    )
    op.drop_index(
        op.f('ix_app_version_status_installed_app_id'),
        table_name='app_version_status',
    )
    op.drop_table('app_version_status')
    op.drop_table('upstream_tag_cache')
