"""Generated report artifacts (session 6.2).

Every report generation — interactive or scheduled — lands one `ReportRun` row,
so a report someone exported is retrievable and auditable after the fact: who
asked for it, over what range, what came out, and a sha256 proving the file on
disk is the one that was generated. That is what makes the `backup_evidence`
and `user_activity` exports usable as ISO evidence (uiux-spec A1.6) rather than
just a download.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

# JSONB on Postgres, plain JSON elsewhere (SQLite test fallback).
ParamsJSON = JSON().with_variant(JSONB(), "postgresql")

REPORT_RUN_STATUSES = ("pending", "running", "success", "failure")
REPORT_FORMATS = ("csv", "pdf")


class ReportRun(Base):
    """One generated report artifact."""

    __tablename__ = "report_runs"

    id: Mapped[int] = mapped_column(primary_key=True)

    # Registry id (app/core/reports/registry.py), e.g. "backup_evidence".
    report_id: Mapped[str] = mapped_column(String(60), index=True)
    # The resolved, validated parameters the generator ran with — already
    # sanitized (golden rule 6), so this is safe to return from the API.
    params: Mapped[dict] = mapped_column(ParamsJSON, default=dict)
    format: Mapped[str] = mapped_column(String(10), default="csv")

    # pending | running | success | failure
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    error: Mapped[str | None] = mapped_column(Text)

    # Filesystem path under settings.reports_dir; NULL until the artifact lands.
    artifact_path: Mapped[str | None] = mapped_column(String(500))
    artifact_bytes: Mapped[int | None] = mapped_column(Integer)
    # sha256 of the artifact — the evidence integrity check.
    sha256: Mapped[str | None] = mapped_column(String(64))
    row_count: Mapped[int | None] = mapped_column(Integer)

    # Who asked. NULL only if the requesting user was deleted afterwards; the
    # generating user is ALSO baked into the artifact header, so deleting the
    # user never rewrites already-issued evidence.
    requested_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    # The CommandJob that produced it, for async runs (rule 2/3 audit trail).
    job_id: Mapped[int | None] = mapped_column(
        ForeignKey("command_jobs.id", ondelete="SET NULL"), index=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
