"""maintenance_windows table (session 3.5)

Revision ID: e1a2b3c4d5e6
Revises: d4e5f6a7b8c9
Create Date: 2026-09-11 00:00:00.000000

Idempotent: uses `if_not_exists=True` on create and `IF EXISTS` on drop.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e1a2b3c4d5e6"
down_revision = "f1a2b3c4d5e6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "maintenance_windows",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("server_id", sa.Integer(), nullable=False),
        sa.Column("cron", sa.String(100), nullable=False),
        sa.Column("duration_minutes", sa.Integer(), nullable=False, server_default="120"),
        sa.Column("timezone", sa.String(64), nullable=False, server_default="Asia/Dubai"),
        sa.Column("blocked_danger_classes", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("TRUE")),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.ForeignKeyConstraint(
            ["server_id"], ["servers.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        if_not_exists=True,
    )
    op.create_index(
        "ix_maintenance_windows_server_id",
        "maintenance_windows",
        ["server_id"],
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_maintenance_windows_server_id",
        table_name="maintenance_windows",
        if_exists=True,
    )
    op.drop_table("maintenance_windows", if_exists=True)
