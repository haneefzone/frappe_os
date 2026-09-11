"""Platform self-backup + master-key escrow acknowledgement (session 6.3)

Adds the `platform_backups` table (one row per `platform.self_backup` run: the
encrypted archive's size/sha256, the plaintext sha256, the KDF salt the
backup-passphrase-derived key was built over, the storage target + object key,
the verify lifecycle, and the restore-tested stamp) and two escrow-acknowledgement
columns on the `platform_settings` singleton (`master_key_escrow_confirmed_at`
/ `_by`) driving the persistent, non-dismissable master-key escrow banner.

Revision ID: d1c3b5a7e9f2
Revises: b4e7c2a9d1f3
Create Date: 2026-07-23 00:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'd1c3b5a7e9f2'
down_revision: str | Sequence[str] | None = 'b4e7c2a9d1f3'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'platform_backups',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column(
            'status', sa.String(length=20), nullable=False, server_default='pending'
        ),
        sa.Column('size_bytes', sa.Integer(), nullable=True),
        sa.Column('sha256', sa.String(length=64), nullable=True),
        sa.Column('plaintext_sha256', sa.String(length=64), nullable=True),
        sa.Column(
            'encrypted', sa.Boolean(), nullable=False, server_default=sa.true()
        ),
        sa.Column('kdf_salt', sa.String(length=64), nullable=True),
        sa.Column('storage_target_id', sa.Integer(), nullable=True),
        sa.Column('object_key', sa.String(length=500), nullable=True),
        sa.Column(
            'verify_status',
            sa.String(length=20),
            nullable=False,
            server_default='unverified',
        ),
        sa.Column('verified_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('restore_tested_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('taken_by_job_id', sa.Integer(), nullable=True),
        sa.Column('verified_by_job_id', sa.Integer(), nullable=True),
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
            ['storage_target_id'],
            ['storage_targets.id'],
            name=op.f('fk_platform_backups_storage_target_id_storage_targets'),
            ondelete='SET NULL',
        ),
        sa.ForeignKeyConstraint(
            ['taken_by_job_id'],
            ['command_jobs.id'],
            name=op.f('fk_platform_backups_taken_by_job_id_command_jobs'),
            ondelete='SET NULL',
        ),
        sa.ForeignKeyConstraint(
            ['verified_by_job_id'],
            ['command_jobs.id'],
            name=op.f('fk_platform_backups_verified_by_job_id_command_jobs'),
            ondelete='SET NULL',
        ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        op.f('ix_platform_backups_status'),
        'platform_backups',
        ['status'],
        unique=False,
    )
    op.create_index(
        op.f('ix_platform_backups_storage_target_id'),
        'platform_backups',
        ['storage_target_id'],
        unique=False,
    )
    op.create_index(
        op.f('ix_platform_backups_verify_status'),
        'platform_backups',
        ['verify_status'],
        unique=False,
    )
    op.create_index(
        op.f('ix_platform_backups_taken_by_job_id'),
        'platform_backups',
        ['taken_by_job_id'],
        unique=False,
    )
    op.create_index(
        op.f('ix_platform_backups_verified_by_job_id'),
        'platform_backups',
        ['verified_by_job_id'],
        unique=False,
    )

    op.add_column(
        'platform_settings',
        sa.Column(
            'master_key_escrow_confirmed_at',
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        'platform_settings',
        sa.Column('master_key_escrow_confirmed_by', sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        op.f('fk_platform_settings_master_key_escrow_confirmed_by_users'),
        'platform_settings',
        'users',
        ['master_key_escrow_confirmed_by'],
        ['id'],
        ondelete='SET NULL',
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint(
        op.f('fk_platform_settings_master_key_escrow_confirmed_by_users'),
        'platform_settings',
        type_='foreignkey',
    )
    op.drop_column('platform_settings', 'master_key_escrow_confirmed_by')
    op.drop_column('platform_settings', 'master_key_escrow_confirmed_at')

    op.drop_index(
        op.f('ix_platform_backups_verified_by_job_id'), table_name='platform_backups'
    )
    op.drop_index(
        op.f('ix_platform_backups_taken_by_job_id'), table_name='platform_backups'
    )
    op.drop_index(
        op.f('ix_platform_backups_verify_status'), table_name='platform_backups'
    )
    op.drop_index(
        op.f('ix_platform_backups_storage_target_id'), table_name='platform_backups'
    )
    op.drop_index(op.f('ix_platform_backups_status'), table_name='platform_backups')
    op.drop_table('platform_backups')
