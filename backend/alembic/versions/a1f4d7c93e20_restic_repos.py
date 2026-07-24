"""restic config-tier backup repositories (session 4.1 — Full-system DR)

Adds the `restic_repos` table: one restic repository per managed server for the
OS/config backup tier (nginx/supervisor/redis/mariadb configs + dpkg selections).
Each repo reuses an existing 2.2 `storage_targets` bucket (FK, SET NULL so the
evidence record survives a target delete) under a per-server key `prefix`. The
only secret on the row is the restic repo password, stored as a Fernet token in
`password_enc`. `initialized` tracks `restic init`; `last_backup_at` /
`last_check_at` / `last_snapshot_id` are backup-evidence columns (§6).

Revision ID: a1f4d7c93e20
Revises: f8b3d1c6a2e9
Create Date: 2026-07-11 16:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'a1f4d7c93e20'
down_revision: str | Sequence[str] | None = 'f8b3d1c6a2e9'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'restic_repos',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('server_id', sa.Integer(), nullable=False),
        sa.Column('storage_target_id', sa.Integer(), nullable=True),
        sa.Column('prefix', sa.String(length=255), nullable=False),
        sa.Column('password_enc', sa.Text(), nullable=True),
        sa.Column(
            'initialized', sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column('last_backup_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_check_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_snapshot_id', sa.String(length=64), nullable=True),
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
            ['server_id'],
            ['servers.id'],
            name=op.f('fk_restic_repos_server_id_servers'),
            ondelete='CASCADE',
        ),
        sa.ForeignKeyConstraint(
            ['storage_target_id'],
            ['storage_targets.id'],
            name=op.f('fk_restic_repos_storage_target_id_storage_targets'),
            ondelete='SET NULL',
        ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        op.f('ix_restic_repos_server_id'),
        'restic_repos',
        ['server_id'],
        unique=True,
    )
    op.create_index(
        op.f('ix_restic_repos_storage_target_id'),
        'restic_repos',
        ['storage_target_id'],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_restic_repos_storage_target_id'), table_name='restic_repos')
    op.drop_index(op.f('ix_restic_repos_server_id'), table_name='restic_repos')
    op.drop_table('restic_repos')
