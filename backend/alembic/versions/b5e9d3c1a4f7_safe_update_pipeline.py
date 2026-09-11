"""Safe update pipeline: site environment + update_pipelines table (session 3.3)

Adds the `environment` column to `sites` (dev|staging|prod, drives the
EnvironmentBadge + prod-update guardrails) and the `update_pipelines` table that
records one clone→staging→verify→promote run and persists the checklist verdict
and the mandatory pre-update backup id so the promote gate is enforced
server-side.

Revision ID: b5e9d3c1a4f7
Revises: a1f4d7c93e20
Create Date: 2026-07-25 00:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'b5e9d3c1a4f7'
down_revision: str | Sequence[str] | None = 'a1f4d7c93e20'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# JSONB on Postgres, plain JSON on the SQLite test fallback (mirrors the model).
_JSON = sa.JSON().with_variant(JSONB(), "postgresql")


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'sites',
        sa.Column(
            'environment',
            sa.String(length=20),
            nullable=False,
            server_default='dev',
        ),
    )
    op.create_index(op.f('ix_sites_environment'), 'sites', ['environment'])

    op.create_table(
        'update_pipelines',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('source_site_id', sa.Integer(), nullable=False),
        sa.Column('source_bench_id', sa.Integer(), nullable=False),
        sa.Column('staging_bench_id', sa.Integer(), nullable=False),
        sa.Column('staging_site_name', sa.String(length=200), nullable=False),
        sa.Column('staging_site_id', sa.Integer(), nullable=True),
        sa.Column(
            'phase', sa.String(length=30), nullable=False, server_default='draft'
        ),
        sa.Column('scrub_method', sa.String(length=140), nullable=True),
        sa.Column('checklist', _JSON, nullable=True),
        sa.Column(
            'checklist_ok', sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column('pre_backup_id', sa.Integer(), nullable=True),
        sa.Column('clone_job_id', sa.Integer(), nullable=True),
        sa.Column('update_job_id', sa.Integer(), nullable=True),
        sa.Column('verify_job_id', sa.Integer(), nullable=True),
        sa.Column('promote_job_id', sa.Integer(), nullable=True),
        sa.Column('rollback_job_id', sa.Integer(), nullable=True),
        sa.Column('note', sa.String(length=500), nullable=True),
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
        sa.ForeignKeyConstraint(
            ['source_site_id'], ['sites.id'], ondelete='CASCADE'
        ),
        sa.ForeignKeyConstraint(
            ['source_bench_id'], ['benches.id'], ondelete='CASCADE'
        ),
        sa.ForeignKeyConstraint(
            ['staging_bench_id'], ['benches.id'], ondelete='CASCADE'
        ),
        sa.ForeignKeyConstraint(
            ['staging_site_id'], ['sites.id'], ondelete='SET NULL'
        ),
        sa.ForeignKeyConstraint(
            ['pre_backup_id'], ['backups.id'], ondelete='SET NULL'
        ),
        sa.ForeignKeyConstraint(
            ['clone_job_id'], ['command_jobs.id'], ondelete='SET NULL'
        ),
        sa.ForeignKeyConstraint(
            ['update_job_id'], ['command_jobs.id'], ondelete='SET NULL'
        ),
        sa.ForeignKeyConstraint(
            ['verify_job_id'], ['command_jobs.id'], ondelete='SET NULL'
        ),
        sa.ForeignKeyConstraint(
            ['promote_job_id'], ['command_jobs.id'], ondelete='SET NULL'
        ),
        sa.ForeignKeyConstraint(
            ['rollback_job_id'], ['command_jobs.id'], ondelete='SET NULL'
        ),
        sa.ForeignKeyConstraint(['created_by'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        op.f('ix_update_pipelines_source_site_id'),
        'update_pipelines',
        ['source_site_id'],
    )
    op.create_index(
        op.f('ix_update_pipelines_source_bench_id'),
        'update_pipelines',
        ['source_bench_id'],
    )
    op.create_index(
        op.f('ix_update_pipelines_staging_bench_id'),
        'update_pipelines',
        ['staging_bench_id'],
    )
    op.create_index(
        op.f('ix_update_pipelines_phase'), 'update_pipelines', ['phase']
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_update_pipelines_phase'), table_name='update_pipelines')
    op.drop_index(
        op.f('ix_update_pipelines_staging_bench_id'), table_name='update_pipelines'
    )
    op.drop_index(
        op.f('ix_update_pipelines_source_bench_id'), table_name='update_pipelines'
    )
    op.drop_index(
        op.f('ix_update_pipelines_source_site_id'), table_name='update_pipelines'
    )
    op.drop_table('update_pipelines')
    op.drop_index(op.f('ix_sites_environment'), table_name='sites')
    op.drop_column('sites', 'environment')
