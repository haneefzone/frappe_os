"""Job engine tables (CLAUDE.md "Job engine pattern" + golden rules 1-4).

Every state-changing remote operation is a `CommandJob`. Its work is broken
into ordered `CommandStep` rows; the streamed command output is persisted as
`LogEntry` rows (batched by the worker) so a job's history survives a worker
restart and can be replayed to the log viewer.

Secrets never land here: `params_sanitized` already has every secret masked
(rule 6), and `LogEntry.content` is redacted by the worker before it is written.
"""

from datetime import datetime

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

# JSONB on Postgres, plain JSON elsewhere (SQLite test fallback).
ParamsJSON = JSON().with_variant(JSONB(), "postgresql")

# Lifecycle states shared by the runner, API and UI.
JOB_STATUSES = ("pending", "running", "success", "failure", "cancelled")
STEP_STATUSES = ("pending", "running", "success", "failure", "skipped")


class CommandJob(Base):
    """One queued/executed remote action against a managed target."""

    __tablename__ = "command_jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    server_id: Mapped[int] = mapped_column(
        ForeignKey("servers.id", ondelete="CASCADE"), index=True
    )
    # What the action operates on: server | bench | site (+ the id/name of it).
    target_type: Mapped[str] = mapped_column(String(20), default="server")
    target_id: Mapped[str | None] = mapped_column(String(255))

    action_name: Mapped[str] = mapped_column(String(120), index=True)
    # Queue/priority: high | default | low (maps 1:1 to an RQ queue).
    priority: Mapped[str] = mapped_column(String(10), default="default")

    # pending | running | success | failure | cancelled
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    rq_job_id: Mapped[str | None] = mapped_column(String(64))

    # Rendered params with every secret masked (rule 6) — safe to store/show.
    params_sanitized: Mapped[dict] = mapped_column(ParamsJSON, default=dict)
    # Redis lock this job holds while running; NULL for lock-free actions.
    lock_key: Mapped[str | None] = mapped_column(String(255))

    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    exit_code: Mapped[int | None] = mapped_column(Integer)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    steps: Mapped[list["CommandStep"]] = relationship(
        back_populates="job",
        cascade="all, delete-orphan",
        order_by="CommandStep.order",
    )


class CommandStep(Base):
    """One ordered unit of work inside a job (`with ctx.step("..."): ...`)."""

    __tablename__ = "command_steps"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(
        ForeignKey("command_jobs.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(200))
    # "order" is a SQL keyword; keep the ORM attribute readable, store as step_order.
    order: Mapped[int] = mapped_column("step_order", Integer)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_traceback: Mapped[str | None] = mapped_column(Text)

    job: Mapped[CommandJob] = relationship(back_populates="steps")


class LogEntry(Base):
    """A single line of a job's streamed output. `seq` is monotonic per job so a
    tailing client can resume from `?after_seq=N`."""

    __tablename__ = "log_entries"
    __table_args__ = (UniqueConstraint("job_id", "seq", name="uq_log_entries_job_seq"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(
        ForeignKey("command_jobs.id", ondelete="CASCADE"), index=True
    )
    seq: Mapped[int] = mapped_column(Integer)
    # stdout | stderr | system (runner-emitted status lines)
    stream: Mapped[str] = mapped_column(String(10), default="stdout")
    content: Mapped[str] = mapped_column(Text)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
