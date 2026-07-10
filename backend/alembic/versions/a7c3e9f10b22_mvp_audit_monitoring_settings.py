"""mvp: audit log + monitoring samples + platform settings (session 1.12)

Adds the three MVP-closer tables:
- `audit_logs`        — the immutable activity log (rule 2 formalized): actor,
                        action, entity refs, params-with-secrets-masked, result,
                        source IP, optional job link, timestamp.
- `monitoring_samples`— rolling per-server telemetry (CPU/RAM/disk/load + the
                        four managed services' state) written by the poller.
- `platform_settings` — the single-row white-label + defaults record.

Revision ID: a7c3e9f10b22
Revises: f3b9d7c8e21a
Create Date: 2026-07-10 14:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'a7c3e9f10b22'
down_revision: str | Sequence[str] | None = 'f3b9d7c8e21a'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# JSONB on Postgres, plain JSON elsewhere (mirrors the model modules).
_JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    """Upgrade schema."""
    # --- audit_logs -------------------------------------------------------- #
    op.create_table(
        'audit_logs',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=True),
        sa.Column('action', sa.String(length=80), nullable=False),
        sa.Column('entity_type', sa.String(length=40), nullable=True),
        sa.Column('entity_id', sa.String(length=120), nullable=True),
        sa.Column('summary', sa.String(length=300), nullable=False),
        sa.Column('params_masked', _JSON, nullable=True),
        sa.Column('result', sa.String(length=20), nullable=False, server_default='ok'),
        sa.Column('source_ip', sa.String(length=64), nullable=True),
        sa.Column('job_id', sa.Integer(), nullable=True),
        sa.Column(
            'ts', sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['job_id'], ['command_jobs.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_audit_logs_user_id', 'audit_logs', ['user_id'])
    op.create_index('ix_audit_logs_action', 'audit_logs', ['action'])
    op.create_index('ix_audit_logs_entity_type', 'audit_logs', ['entity_type'])
    op.create_index('ix_audit_logs_result', 'audit_logs', ['result'])
    op.create_index('ix_audit_logs_job_id', 'audit_logs', ['job_id'])
    op.create_index('ix_audit_logs_ts', 'audit_logs', ['ts'])
    op.create_index(
        'ix_audit_logs_entity_ts', 'audit_logs', ['entity_type', 'entity_id', 'ts']
    )

    # --- monitoring_samples ----------------------------------------------- #
    op.create_table(
        'monitoring_samples',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('server_id', sa.Integer(), nullable=False),
        sa.Column('ok', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('error', sa.String(length=300), nullable=True),
        sa.Column('cpu_pct', sa.Float(), nullable=True),
        sa.Column('mem_pct', sa.Float(), nullable=True),
        sa.Column('disk_pct', sa.Float(), nullable=True),
        sa.Column('mem_used_mb', sa.Integer(), nullable=True),
        sa.Column('mem_total_mb', sa.Integer(), nullable=True),
        sa.Column('disk_used_gb', sa.Float(), nullable=True),
        sa.Column('disk_total_gb', sa.Float(), nullable=True),
        sa.Column('load1', sa.Float(), nullable=True),
        sa.Column('services', _JSON, nullable=True),
        sa.Column(
            'ts', sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.ForeignKeyConstraint(['server_id'], ['servers.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_monitoring_samples_server_id', 'monitoring_samples', ['server_id'])
    op.create_index('ix_monitoring_samples_ts', 'monitoring_samples', ['ts'])
    op.create_index(
        'ix_monitoring_samples_server_ts', 'monitoring_samples', ['server_id', 'ts']
    )

    # --- platform_settings (single row) ----------------------------------- #
    op.create_table(
        'platform_settings',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column(
            'product_name', sa.String(length=80),
            nullable=False, server_default='FDM Platform',
        ),
        sa.Column('logo_path', sa.String(length=300), nullable=True),
        sa.Column(
            'default_tz', sa.String(length=64),
            nullable=False, server_default='Asia/Dubai',
        ),
        sa.Column(
            'bench_base_path', sa.String(length=300),
            nullable=False, server_default='/home/frappe',
        ),
        sa.Column('port_range_start', sa.Integer(), nullable=False, server_default='8000'),
        sa.Column('port_range_end', sa.Integer(), nullable=False, server_default='8999'),
        sa.Column(
            'updated_at', sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.PrimaryKeyConstraint('id'),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('platform_settings')
    op.drop_index('ix_monitoring_samples_server_ts', table_name='monitoring_samples')
    op.drop_index('ix_monitoring_samples_ts', table_name='monitoring_samples')
    op.drop_index('ix_monitoring_samples_server_id', table_name='monitoring_samples')
    op.drop_table('monitoring_samples')
    op.drop_index('ix_audit_logs_entity_ts', table_name='audit_logs')
    op.drop_index('ix_audit_logs_ts', table_name='audit_logs')
    op.drop_index('ix_audit_logs_job_id', table_name='audit_logs')
    op.drop_index('ix_audit_logs_result', table_name='audit_logs')
    op.drop_index('ix_audit_logs_entity_type', table_name='audit_logs')
    op.drop_index('ix_audit_logs_action', table_name='audit_logs')
    op.drop_index('ix_audit_logs_user_id', table_name='audit_logs')
    op.drop_table('audit_logs')
