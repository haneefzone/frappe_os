"""AI integration settings (session 5.0, Phase 5 — AI)

Adds the one-row `ai_settings` table: the Claude/Anthropic integration config the
5.1 copilot and 5.2 agents build on — enabled toggle, Fernet-encrypted API key,
editable model routing (deep vs high-volume) with current-model defaults, a
monthly cost cap, a per-call audit toggle, and persisted monthly token/cost
accounting.

Revision ID: e4c7b1a9f2d0
Revises: d3f5a1c7e9b2
Create Date: 2026-07-10 18:20:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'e4c7b1a9f2d0'
down_revision: str | Sequence[str] | None = 'd3f5a1c7e9b2'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'ai_settings',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column(
            'enabled', sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column('api_key_enc', sa.Text(), nullable=True),
        sa.Column(
            'model_deep',
            sa.String(length=80),
            nullable=False,
            server_default='claude-opus-4-8',
        ),
        sa.Column(
            'model_high_volume',
            sa.String(length=80),
            nullable=False,
            server_default='claude-sonnet-4-6',
        ),
        sa.Column('monthly_budget_usd', sa.Float(), nullable=True),
        sa.Column(
            'audit_calls', sa.Boolean(), nullable=False, server_default=sa.true()
        ),
        sa.Column('usage_period', sa.String(length=7), nullable=True),
        sa.Column(
            'tokens_input_month', sa.Integer(), nullable=False, server_default='0'
        ),
        sa.Column(
            'tokens_output_month', sa.Integer(), nullable=False, server_default='0'
        ),
        sa.Column(
            'cost_usd_month', sa.Float(), nullable=False, server_default='0'
        ),
        sa.Column(
            'updated_at',
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint('id'),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('ai_settings')
