"""Server.connection_type: local (localhost) execution backend (DOO-1199)

Add `servers.connection_type` ('ssh' | 'local'). A `local` server is the machine
FDM itself runs on — jobs execute as local subprocesses instead of over SSH, so an
operator can install Frappe on the FDM host without an SSH loopback (DOO-1196).

`server_default='ssh'` backfills every existing row to today's behaviour, so this
is a safe, non-breaking add. NOT NULL.

Revision ID: b1c2d3e4f5a6
Revises: c0ffee1119ab
Create Date: 2026-09-14 17:20:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'b1c2d3e4f5a6'
down_revision: str | Sequence[str] | None = 'c0ffee1119ab'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'servers',
        sa.Column(
            'connection_type',
            sa.String(length=10),
            nullable=False,
            server_default='ssh',
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('servers', 'connection_type')
