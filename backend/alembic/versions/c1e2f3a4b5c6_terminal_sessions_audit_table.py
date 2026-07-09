"""terminal_sessions: audit table for browser SSH sessions (FDM 1.5)

Each browser terminal session creates a row on open and finalises it
(status, ended_at, duration_seconds, close_reason) on disconnect.

Revision ID: c1e2f3a4b5c6
Revises: fbcd01f180ab
Create Date: 2026-07-10 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c1e2f3a4b5c6"
down_revision: str | None = "fbcd01f180ab"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "terminal_sessions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("server_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("ssh_username", sa.String(length=120), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="open"),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_seconds", sa.Integer(), nullable=True),
        sa.Column("close_reason", sa.String(length=80), nullable=True),
        sa.ForeignKeyConstraint(["server_id"], ["servers.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_terminal_sessions_server_id", "terminal_sessions", ["server_id"])
    op.create_index("ix_terminal_sessions_user_id", "terminal_sessions", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_terminal_sessions_user_id", table_name="terminal_sessions")
    op.drop_index("ix_terminal_sessions_server_id", table_name="terminal_sessions")
    op.drop_table("terminal_sessions")
