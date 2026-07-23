"""Add report:generate permission to Developer role (session 4.4).

No schema changes — purely a data migration: adds the new ``report:generate``
action-class to the Developer role's ``permissions`` JSON array if it was
seeded from the default. Admin already holds ``*`` and is unaffected.

Revision ID: a2b6d4f8c3e1
Revises: f8b3d1c6a2e9
Create Date: 2026-07-23 10:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a2b6d4f8c3e1"
down_revision: str | None = "f8b3d1c6a2e9"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

PERMISSION = "report:generate"


def upgrade() -> None:
    conn = op.get_bind()
    # Fetch the Developer role's current permissions.
    row = conn.execute(
        sa.text("SELECT id, permissions FROM roles WHERE name = 'Developer'")
    ).fetchone()
    if row is None:
        return  # Role doesn't exist (fresh install seeds from DEFAULT_ROLES which already has it).
    perms = list(row.permissions or [])
    if PERMISSION not in perms and "*" not in perms:
        perms.append(PERMISSION)
        conn.execute(
            sa.text("UPDATE roles SET permissions = :p WHERE id = :id"),
            {"p": sa.JSON().bind_processor(conn.dialect)(perms), "id": row.id},
        )


def downgrade() -> None:
    conn = op.get_bind()
    row = conn.execute(
        sa.text("SELECT id, permissions FROM roles WHERE name = 'Developer'")
    ).fetchone()
    if row is None:
        return
    perms = [p for p in (row.permissions or []) if p != PERMISSION]
    conn.execute(
        sa.text("UPDATE roles SET permissions = :p WHERE id = :id"),
        {"p": sa.JSON().bind_processor(conn.dialect)(perms), "id": row.id},
    )
