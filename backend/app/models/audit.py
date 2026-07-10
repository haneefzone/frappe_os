"""The immutable audit log (CLAUDE.md golden rule 2, formalized in session 1.12).

Every state-changing operation writes exactly one `AuditLog` row alongside its
`CommandJob` (or, for the handful of non-job mutations — server/app-source/settings
CRUD, backup download, login/logout — on its own). The row captures *who* did
*what* to *which entity*, the request's `source_ip`, the parameters with every
secret already masked (`params_masked` — never a plaintext password or key), and
the `result`. Job-backed rows also carry `job_id` so the Audit page can deep-link
to the job timeline.

The table is append-only by contract: the platform never exposes an update or
delete path for audit rows (rule 2 — "no silent mutations"). `user_id` is a
SET NULL FK so deleting a user never erases their history; `job_id` is SET NULL
so purging old jobs never erases the audit trail.
"""

from datetime import datetime

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Index,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

# JSONB on Postgres, plain JSON on the SQLite test fallback (mirrors job.py).
ParamsJSON = JSON().with_variant(JSONB(), "postgresql")

# Common results, but the column is free-form so any handler can be specific.
AUDIT_RESULTS = ("ok", "enqueued", "denied", "error")


class AuditLog(Base):
    """One immutable record of a state-changing action."""

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(primary_key=True)

    # Actor. SET NULL so a deleted user's history survives (nullable also covers
    # pre-auth events like a failed login where no user is resolved).
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )

    # The action name — a command action_name ("site.backup") for job-backed
    # mutations, or a dotted verb ("server.register", "settings.update",
    # "auth.login") for the rest.
    action: Mapped[str] = mapped_column(String(80), index=True)

    # What the action touched: entity_type is a coarse noun ("site", "server",
    # "backup", "settings", "session"); entity_id its identifier as text (ids are
    # ints today but site/host names read better and stay valid across types).
    entity_type: Mapped[str | None] = mapped_column(String(40), index=True)
    entity_id: Mapped[str | None] = mapped_column(String(120))

    # One human-readable line for the Audit table ("Backed up site dev.localhost").
    summary: Mapped[str] = mapped_column(String(300))

    # The validated parameters with every secret masked as ``••••`` (rule 6).
    # Populated from a job's `params_sanitized`, so secrets are never in the clear.
    params_masked: Mapped[dict] = mapped_column(ParamsJSON, default=dict)

    # ok | enqueued | denied | error (free-form; see AUDIT_RESULTS).
    result: Mapped[str] = mapped_column(String(20), default="ok", index=True)

    # The request source IP (from request.client, honouring trusted proxies only
    # — SEC-M1). NULL for events raised outside a request (e.g. a poller).
    source_ip: Mapped[str | None] = mapped_column(String(64))

    # The job this action enqueued, when it is job-backed. SET NULL so a job
    # purge never erases the audit trail.
    job_id: Mapped[int | None] = mapped_column(
        ForeignKey("command_jobs.id", ondelete="SET NULL"), index=True
    )

    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )

    __table_args__ = (
        # The Audit page's default view is "newest first", often filtered by
        # entity; a composite index serves both.
        Index("ix_audit_logs_entity_ts", "entity_type", "entity_id", "ts"),
    )
