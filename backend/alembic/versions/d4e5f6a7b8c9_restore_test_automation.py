"""restore-test automation (session 3.4)

Revision ID: d4e5f6a7b8c9
Revises: b4e7c2a9d1f3
Create Date: 2026-09-11 00:00:00

Scheduled proof-of-restorability. Adds the restore-tested badge fields to
`backups` and the per-site cadence to `backup_policies`:

- backups.restore_test_status       — untested | passed | failed (NOT NULL,
                                       server_default 'untested' so existing
                                       rows migrate to "untested").
- backups.restore_tested_at         — when the last restore-test finished (UTC).
- backups.restore_test_detail       — short summary of the last result.
- backups.restore_test_job_id       — the job that produced it (FK, SET NULL).
- backup_policies.restore_test_interval_days — days between auto restore-tests
                                       (server_default '7'; NULL = on-demand only).
"""

import sqlalchemy as sa

from alembic import op

revision = "d4e5f6a7b8c9"
down_revision = "b4e7c2a9d1f3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "backups",
        sa.Column(
            "restore_test_status",
            sa.String(20),
            nullable=False,
            server_default="untested",
        ),
    )
    op.add_column(
        "backups",
        sa.Column("restore_tested_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "backups",
        sa.Column("restore_test_detail", sa.String(500), nullable=True),
    )
    op.add_column(
        "backups",
        sa.Column(
            "restore_test_job_id",
            sa.Integer(),
            sa.ForeignKey("command_jobs.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_backups_restore_test_job_id", "backups", ["restore_test_job_id"]
    )

    op.add_column(
        "backup_policies",
        sa.Column(
            "restore_test_interval_days",
            sa.Integer(),
            nullable=True,
            server_default="7",
        ),
    )


def downgrade() -> None:
    op.drop_column("backup_policies", "restore_test_interval_days")
    op.drop_index("ix_backups_restore_test_job_id", table_name="backups")
    op.drop_column("backups", "restore_test_job_id")
    op.drop_column("backups", "restore_test_detail")
    op.drop_column("backups", "restore_tested_at")
    op.drop_column("backups", "restore_test_status")
