"""S3-compatible storage targets + offsite columns on backups (session 2.2)

Adds the `storage_targets` table (one S3-compatible bucket the platform uploads
backup artifacts to; access/secret keys are Fernet tokens) and three offsite
columns on `backups`: `storage_state` (local/uploading/offsite/failed),
`storage_target_id` (FK, SET NULL) and `object_keys` (JSONB: kind -> object key).

Revision ID: f7a2c9e1b3d4
Revises: f5a2c9d1e7b4
Create Date: 2026-07-10 19:10:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'f7a2c9e1b3d4'
down_revision: str | Sequence[str] | None = 'f5a2c9d1e7b4'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# JSONB on Postgres, plain JSON on the SQLite fallback (mirrors the models).
_JSON = sa.JSON().with_variant(JSONB(), "postgresql")


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'storage_targets',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=120), nullable=False),
        sa.Column(
            'provider', sa.String(length=20), nullable=False, server_default='aws'
        ),
        sa.Column('endpoint_url', sa.String(length=255), nullable=True),
        sa.Column('region', sa.String(length=64), nullable=True),
        sa.Column('bucket', sa.String(length=255), nullable=False),
        sa.Column('path_prefix', sa.String(length=255), nullable=True),
        sa.Column('access_key_enc', sa.Text(), nullable=True),
        sa.Column('secret_key_enc', sa.Text(), nullable=True),
        sa.Column(
            'use_ssl', sa.Boolean(), nullable=False, server_default=sa.true()
        ),
        sa.Column(
            'enabled', sa.Boolean(), nullable=False, server_default=sa.true()
        ),
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
    )
    op.create_index(
        op.f('ix_storage_targets_name'), 'storage_targets', ['name'], unique=True
    )

    op.add_column(
        'backups',
        sa.Column(
            'storage_state',
            sa.String(length=20),
            nullable=False,
            server_default='local',
        ),
    )
    op.create_index(
        op.f('ix_backups_storage_state'), 'backups', ['storage_state'], unique=False
    )
    op.add_column(
        'backups', sa.Column('storage_target_id', sa.Integer(), nullable=True)
    )
    op.create_index(
        op.f('ix_backups_storage_target_id'),
        'backups',
        ['storage_target_id'],
        unique=False,
    )
    op.create_foreign_key(
        op.f('fk_backups_storage_target_id_storage_targets'),
        'backups',
        'storage_targets',
        ['storage_target_id'],
        ['id'],
        ondelete='SET NULL',
    )
    op.add_column('backups', sa.Column('object_keys', _JSON, nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('backups', 'object_keys')
    op.drop_constraint(
        op.f('fk_backups_storage_target_id_storage_targets'),
        'backups',
        type_='foreignkey',
    )
    op.drop_index(op.f('ix_backups_storage_target_id'), table_name='backups')
    op.drop_column('backups', 'storage_target_id')
    op.drop_index(op.f('ix_backups_storage_state'), table_name='backups')
    op.drop_column('backups', 'storage_state')
    op.drop_index(op.f('ix_storage_targets_name'), table_name='storage_targets')
    op.drop_table('storage_targets')
