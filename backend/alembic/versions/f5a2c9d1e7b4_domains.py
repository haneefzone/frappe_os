"""site domains & SSL (session 2.4)

Adds the `domains` table: a hostname bound to a site with its DNS check result
(dns_ok / last_checked), TLS state (ssl_enabled / cert_status / cert_expires_at)
and a primary flag. Feeds the nginx vhost generator, certbot issue/renew jobs
and the dashboard "SSL expiring ≤30d" KPI. The certificate's private key never
lives here — only its notAfter timestamp.

Revision ID: f5a2c9d1e7b4
Revises: f4c6e8a0b2d1
Create Date: 2026-07-10 18:20:00.000000
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = 'f5a2c9d1e7b4'
down_revision: str | Sequence[str] | None = 'f4c6e8a0b2d1'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'domains',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('site_id', sa.Integer(), nullable=False),
        sa.Column('domain', sa.String(length=253), nullable=False),
        sa.Column(
            'is_primary', sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column(
            'ssl_enabled', sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column(
            'cert_status', sa.String(length=20), nullable=False,
            server_default='none',
        ),
        sa.Column('cert_expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('dns_ok', sa.Boolean(), nullable=True),
        sa.Column('last_checked', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_error', sa.String(length=500), nullable=True),
        sa.Column(
            'created_at', sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column(
            'updated_at', sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.ForeignKeyConstraint(['site_id'], ['sites.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('domain', name='uq_domains_domain'),
    )
    op.create_index(op.f('ix_domains_site_id'), 'domains', ['site_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_domains_site_id'), table_name='domains')
    op.drop_table('domains')
