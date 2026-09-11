"""bootstrap_state — first-run setup flag (session 6.4)

Revision ID: a9b2c3d4e5f6
Revises: b4e7c2a9d1f3, b5e9d3c1a4f7, c9d4e7a2f1b8, e4c7b1a9f2d0, f8b3d1c6a2e9, fbcd01f180ab
Create Date: 2026-09-11 07:00:00.000000

Merges all current main-branch heads into a single new head so subsequent
sessions can chain from a clean single revision.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a9b2c3d4e5f6"
down_revision: str | Sequence[str] | None = (
    "b4e7c2a9d1f3",
    "b5e9d3c1a4f7",
    "c9d4e7a2f1b8",
    "d1c3b5a7e9f2",
    "e4c7b1a9f2d0",
    "f8b3d1c6a2e9",
    "fbcd01f180ab",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "bootstrap_state",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("setup_complete", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "completed_by_user_id",
            sa.Integer,
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("completed_from_ip", sa.String(45), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    # Insert the singleton immediately so all reads can use db.get(BootstrapState, 1).
    op.execute(
        "INSERT INTO bootstrap_state (id, setup_complete) VALUES (1, false)"
    )


def downgrade() -> None:
    op.drop_table("bootstrap_state")
