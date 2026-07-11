"""job analyses — AI root-cause of failed jobs (session 5.2 panel copilot)

Revision ID: a1d2c3b4e5f6
Revises: f7a2c9e1b3d4
Create Date: 2026-07-11 15:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a1d2c3b4e5f6"
down_revision: str | Sequence[str] | None = "f7a2c9e1b3d4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "job_analyses",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("job_id", sa.Integer(), nullable=False),
        sa.Column(
            "status", sa.String(length=20), nullable=False, server_default="pending"
        ),
        sa.Column("model", sa.String(length=80), nullable=True),
        sa.Column("root_cause", sa.Text(), nullable=True),
        sa.Column("suggested_fix", sa.Text(), nullable=True),
        sa.Column("summary", sa.String(length=400), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("requested_by", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["job_id"], ["command_jobs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["requested_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_job_analyses_job_id"), "job_analyses", ["job_id"], unique=False
    )
    op.create_index(
        op.f("ix_job_analyses_status"), "job_analyses", ["status"], unique=False
    )
    op.create_index(
        op.f("ix_job_analyses_created_at"), "job_analyses", ["created_at"], unique=False
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_job_analyses_created_at"), table_name="job_analyses")
    op.drop_index(op.f("ix_job_analyses_status"), table_name="job_analyses")
    op.drop_index(op.f("ix_job_analyses_job_id"), table_name="job_analyses")
    op.drop_table("job_analyses")
