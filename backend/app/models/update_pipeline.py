"""Safe update pipeline (session 3.3).

An `UpdatePipeline` is the durable record of one production-update run through
the safe path: clone the prod site to a staging bench → update staging → run a
verification checklist → promote to prod (with a mandatory pre-backup and a
tested rollback). It ties together the four jobs and persists the checklist
verdict + the pre-backup id so the promote gate can be enforced *server-side*
across separate API calls — a client can never talk the platform into promoting
without a green checklist and a pre-backup.

Phases (``phase`` column):
  draft → cloning → cloned → updating → updated → verifying → verified
        → promoting → promoted            (happy path)
  any phase → *_failed / rolled_back      (failure / rollback)

The per-step jobs (clone/update/verify/promote) update this row as they run, so
the row is the single source of truth the API and UI read.
"""

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

# Lifecycle phases. `*_failed` are terminal error states; `rolled_back` means a
# failed promote restored the pre-update backup.
PIPELINE_PHASES = (
    "draft",
    "cloning",
    "cloned",
    "clone_failed",
    "updating",
    "updated",
    "update_failed",
    "verifying",
    "verified",
    "verify_failed",
    "promoting",
    "promoted",
    "promote_failed",
    "rolled_back",
)


class UpdatePipeline(Base):
    """One safe-update run: clone → staging → verify → promote (+ rollback)."""

    __tablename__ = "update_pipelines"

    id: Mapped[int] = mapped_column(primary_key=True)

    # The production site being updated, and its bench.
    source_site_id: Mapped[int] = mapped_column(
        ForeignKey("sites.id", ondelete="CASCADE"), index=True
    )
    source_bench_id: Mapped[int] = mapped_column(
        ForeignKey("benches.id", ondelete="CASCADE"), index=True
    )
    # The bench the clone is created on (may equal the source bench) and the
    # staging site name created there; staging_site_id is filled once cloned.
    staging_bench_id: Mapped[int] = mapped_column(
        ForeignKey("benches.id", ondelete="CASCADE"), index=True
    )
    staging_site_name: Mapped[str] = mapped_column(String(200))
    staging_site_id: Mapped[int | None] = mapped_column(
        ForeignKey("sites.id", ondelete="SET NULL")
    )

    # Current phase (see PIPELINE_PHASES).
    phase: Mapped[str] = mapped_column(String(30), default="draft", index=True)

    # Optional data-scrub hook for prod→dev copies (uiux §8): a dotted bench
    # method run on the staging clone to mask PII. NULL = no scrub.
    scrub_method: Mapped[str | None] = mapped_column(String(140))

    # The verification checklist result (JSON), written by the verify job:
    # {"all_ok": bool, "checks": [{"key","label","ok","detail"}, ...]}.
    checklist: Mapped[dict | None] = mapped_column(JSON)
    # Denormalised all-green flag the promote gate reads without parsing JSON.
    checklist_ok: Mapped[bool] = mapped_column(Boolean, default=False)

    # The MANDATORY pre-update backup of prod, taken FIRST by the promote job.
    # The promote job refuses to touch prod until this row exists + succeeded;
    # rollback restores exactly this backup.
    pre_backup_id: Mapped[int | None] = mapped_column(
        ForeignKey("backups.id", ondelete="SET NULL")
    )

    # The four jobs, linked for the UI timeline (SET NULL so a purged job row
    # doesn't cascade-delete the pipeline history).
    clone_job_id: Mapped[int | None] = mapped_column(
        ForeignKey("command_jobs.id", ondelete="SET NULL")
    )
    update_job_id: Mapped[int | None] = mapped_column(
        ForeignKey("command_jobs.id", ondelete="SET NULL")
    )
    verify_job_id: Mapped[int | None] = mapped_column(
        ForeignKey("command_jobs.id", ondelete="SET NULL")
    )
    promote_job_id: Mapped[int | None] = mapped_column(
        ForeignKey("command_jobs.id", ondelete="SET NULL")
    )
    rollback_job_id: Mapped[int | None] = mapped_column(
        ForeignKey("command_jobs.id", ondelete="SET NULL")
    )

    # Free-text note surfaced in the UI (e.g. why a phase failed, or the
    # per-task prod sign-off reference the operator supplied).
    note: Mapped[str | None] = mapped_column(String(500))

    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    source_site: Mapped["Site"] = relationship(foreign_keys=[source_site_id])  # noqa: F821
    staging_site: Mapped["Site | None"] = relationship(  # noqa: F821
        foreign_keys=[staging_site_id]
    )
