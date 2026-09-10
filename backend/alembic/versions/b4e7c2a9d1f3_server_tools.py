"""Server tool inventory + version-matrix overrides (session 6.1)

Adds:
- `server_tools` — observed toolchain state per (server, tool), unique on the pair
- `platform_settings.version_matrix_overrides` — the B4.17 Defaults override map

Revision ID: b4e7c2a9d1f3
Revises: c3d9a1f5e6b8
Create Date: 2026-07-19 10:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'b4e7c2a9d1f3'
down_revision: str | Sequence[str] | None = 'c3d9a1f5e6b8'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# JSONB on Postgres, plain JSON on the SQLite fallback (mirrors the models).
_JSON = sa.JSON().with_variant(JSONB(), "postgresql")


# Roles that gain (or lose) the new `tool:scan` action class. Adding it to
# DEFAULT_ROLES only affects freshly seeded installs, so existing rows are
# backfilled here — otherwise an Operator on an upgraded install could never
# scan. The wildcard-holding Admin needs nothing.
_TOOL_SCAN = "tool:scan"
_SCAN_ROLES = ("Developer", "Operator")


def _grant_tool_scan(*, add: bool) -> None:
    """Add/remove `tool:scan` on the seeded roles, leaving custom roles alone."""
    bind = op.get_bind()
    roles = sa.table(
        "roles",
        sa.column("id", sa.Integer),
        sa.column("name", sa.String),
        sa.column("permissions", _JSON),
    )
    rows = bind.execute(
        sa.select(roles.c.id, roles.c.name, roles.c.permissions).where(
            roles.c.name.in_(_SCAN_ROLES)
        )
    ).fetchall()
    for row in rows:
        perms = list(row.permissions or [])
        if add and _TOOL_SCAN not in perms:
            perms.append(_TOOL_SCAN)
        elif not add and _TOOL_SCAN in perms:
            perms.remove(_TOOL_SCAN)
        else:
            continue
        bind.execute(
            roles.update().where(roles.c.id == row.id).values(permissions=perms)
        )


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'server_tools',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('server_id', sa.Integer(), nullable=False),
        sa.Column('tool_id', sa.String(length=60), nullable=False),
        sa.Column('detected_version', sa.String(length=60), nullable=True),
        sa.Column('recommended_version', sa.String(length=60), nullable=True),
        sa.Column(
            'status', sa.String(length=20), nullable=False, server_default='unknown'
        ),
        sa.Column('last_checked_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.Column(
            'updated_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(['server_id'], ['servers.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'server_id', 'tool_id', name='uq_server_tools_server_tool'
        ),
    )
    op.create_index(
        op.f('ix_server_tools_server_id'), 'server_tools', ['server_id'], unique=False
    )
    op.create_index(
        op.f('ix_server_tools_status'), 'server_tools', ['status'], unique=False
    )

    op.add_column(
        'platform_settings',
        sa.Column(
            'version_matrix_overrides',
            _JSON,
            nullable=False,
            server_default='{}',
        ),
    )

    _grant_tool_scan(add=True)


def downgrade() -> None:
    """Downgrade schema."""
    _grant_tool_scan(add=False)
    op.drop_column('platform_settings', 'version_matrix_overrides')
    op.drop_index(op.f('ix_server_tools_status'), table_name='server_tools')
    op.drop_index(op.f('ix_server_tools_server_id'), table_name='server_tools')
    op.drop_table('server_tools')
