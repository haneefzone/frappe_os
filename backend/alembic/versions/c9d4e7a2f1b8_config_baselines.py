"""Config drift baselines (session 6.7)

Adds `config_baselines`: one row per tracked config artefact per server, holding
the secret-stripped hash captured by the last managed change and its drift state
(baseline | drifted | accepted). See app.models.drift / app.core.drift.

Revision ID: c9d4e7a2f1b8
Revises: f8b3d1c6a2e9
Create Date: 2026-07-23 10:30:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'c9d4e7a2f1b8'
down_revision: str | Sequence[str] | None = 'f8b3d1c6a2e9'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "config_baselines",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("server_id", sa.Integer(), nullable=False),
        sa.Column("bench_id", sa.Integer(), nullable=True),
        sa.Column("site_id", sa.Integer(), nullable=True),
        sa.Column("artifact_key", sa.String(length=60), nullable=False),
        sa.Column("path", sa.String(length=500), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=True),
        sa.Column("size", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("sanitized_content", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=12), nullable=False, server_default="baseline"),
        sa.Column("current_sha256", sa.String(length=64), nullable=True),
        sa.Column("current_content", sa.Text(), nullable=True),
        sa.Column("captured_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("captured_by_job_id", sa.Integer(), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("drift_detected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["server_id"], ["servers.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["bench_id"], ["benches.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["site_id"], ["sites.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["captured_by_job_id"], ["command_jobs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "server_id", "artifact_key", "path", name="uq_config_baselines_identity"
        ),
    )
    op.create_index(
        "ix_config_baselines_server_id", "config_baselines", ["server_id"]
    )
    op.create_index("ix_config_baselines_bench_id", "config_baselines", ["bench_id"])
    op.create_index("ix_config_baselines_site_id", "config_baselines", ["site_id"])
    op.create_index(
        "ix_config_baselines_artifact_key", "config_baselines", ["artifact_key"]
    )
    op.create_index("ix_config_baselines_status", "config_baselines", ["status"])


def downgrade() -> None:
    op.drop_index("ix_config_baselines_status", table_name="config_baselines")
    op.drop_index("ix_config_baselines_artifact_key", table_name="config_baselines")
    op.drop_index("ix_config_baselines_site_id", table_name="config_baselines")
    op.drop_index("ix_config_baselines_bench_id", table_name="config_baselines")
    op.drop_index("ix_config_baselines_server_id", table_name="config_baselines")
    op.drop_table("config_baselines")
