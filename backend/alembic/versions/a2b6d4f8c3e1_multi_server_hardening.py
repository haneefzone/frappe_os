"""Multi-server hardening: per-server SSH pool cap + moved-backup provenance (2.6)

Adds `servers.ssh_pool_limit` (nullable per-server override for the concurrent
AsyncSSH session cap; NULL = platform default) and two provenance columns on
`backups` for a cross-server moved copy: `moved_from_backup_id` (self-FK, SET
NULL) and `source_server_id` (FK servers, SET NULL).

Revision ID: a2b6d4f8c3e1
Revises: a3f1c2b4d5e6
Create Date: 2026-07-11 16:30:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'a2b6d4f8c3e1'
# Chained after the current main head (a3f1c2b4d5e6, DOO-256 reports suite) so
# the merge keeps a single alembic head (was f7a2c9e1b3d4 at branch-cut; main
# advanced through 2.3/branding/drift/compliance/reports since).
down_revision: str | Sequence[str] | None = 'a3f1c2b4d5e6'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'servers', sa.Column('ssh_pool_limit', sa.Integer(), nullable=True)
    )

    op.add_column(
        'backups', sa.Column('moved_from_backup_id', sa.Integer(), nullable=True)
    )
    op.create_index(
        op.f('ix_backups_moved_from_backup_id'),
        'backups',
        ['moved_from_backup_id'],
        unique=False,
    )
    op.create_foreign_key(
        op.f('fk_backups_moved_from_backup_id_backups'),
        'backups',
        'backups',
        ['moved_from_backup_id'],
        ['id'],
        ondelete='SET NULL',
    )

    op.add_column(
        'backups', sa.Column('source_server_id', sa.Integer(), nullable=True)
    )
    op.create_index(
        op.f('ix_backups_source_server_id'),
        'backups',
        ['source_server_id'],
        unique=False,
    )
    op.create_foreign_key(
        op.f('fk_backups_source_server_id_servers'),
        'backups',
        'servers',
        ['source_server_id'],
        ['id'],
        ondelete='SET NULL',
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint(
        op.f('fk_backups_source_server_id_servers'), 'backups', type_='foreignkey'
    )
    op.drop_index(op.f('ix_backups_source_server_id'), table_name='backups')
    op.drop_column('backups', 'source_server_id')

    op.drop_constraint(
        op.f('fk_backups_moved_from_backup_id_backups'), 'backups', type_='foreignkey'
    )
    op.drop_index(op.f('ix_backups_moved_from_backup_id'), table_name='backups')
    op.drop_column('backups', 'moved_from_backup_id')

    op.drop_column('servers', 'ssh_pool_limit')
