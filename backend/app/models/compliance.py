"""Backup compliance policy + evaluated status + breach events (session 2.3).

This turns backups from *files* into *policy*. Three rows model the feature:

- **BackupPolicy** — one per site (unique `site_id`): the promise an operator
  makes about a site's backups. `rpo_hours` (Recovery Point Objective) is the
  maximum tolerable age of the newest successful backup; `retention_days` is how
  far back history must reach; `require_offsite` demands the newest backup live
  in an S3 target (session 2.2); `require_restore_test` is **groundwork only**
  (the column exists; the evaluator does not yet score it — a later session turns
  it on). `enabled` gates whether the site is evaluated at all.

- **ComplianceStatus** — one per policied site (unique `site_id`): the latest
  result the evaluator persisted. `state` is compliant / breached / unknown,
  `last_backup_at` is the newest successful backup's time, and `breaches` is the
  list of failed dimensions ([{code, detail}, …]) driving the UI ticks and the
  dashboard Backup Compliance %.

- **ComplianceBreachEvent** — a structured, immutable record emitted **on the
  edge** a site transitions *into* breach (not every tick — that would spam).
  It is deliberately channel-free groundwork: session 3.1's AlertRule engine
  consumes unconsumed rows and turns them into notifications. `consumed_at`
  lets 3.1 mark a row processed without deleting the audit trail.

The evaluator (``app.core.compliance``) is **read-only** over backup metadata —
retention *deletion* lives in session 2.1's retention sweep, never here.
"""

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import expression

from app.db import Base

# JSONB on Postgres, plain JSON on the SQLite test fallback (mirrors backup.py).
_JSON = JSON().with_variant(JSONB(), "postgresql")

# Compliance states persisted on ComplianceStatus.
#  compliant — every required dimension satisfied.
#  breached  — at least one dimension failed (see `breaches`).
#  unknown   — not yet evaluated (transient; a status row starts here).
COMPLIANCE_STATES = ("compliant", "breached", "unknown")

# Breach dimension codes recorded in `breaches` / emitted on events.
BREACH_RPO = "rpo"              # newest successful backup older than rpo_hours (or none)
BREACH_RETENTION = "retention"  # history does not reach back retention_days
BREACH_OFFSITE = "offsite"      # newest backup not in an offsite target


class BackupPolicy(Base):
    """Per-site backup policy (RPO / retention / offsite requirements)."""

    __tablename__ = "backup_policies"
    __table_args__ = (
        UniqueConstraint("site_id", name="uq_backup_policies_site"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(
        ForeignKey("sites.id", ondelete="CASCADE"), index=True
    )

    # Recovery Point Objective: max tolerable age (hours) of the newest
    # successful backup. 24 = "a successful backup at least daily".
    rpo_hours: Mapped[int] = mapped_column(Integer, nullable=False, default=24)

    # How far back backup history must reach (days). NULL = retention not policed.
    retention_days: Mapped[int | None] = mapped_column(Integer)

    # Require the newest backup to be in an offsite (S3) target (session 2.2).
    require_offsite: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=expression.false(), default=False
    )
    # Groundwork only: the column exists so the policy editor can carry it, but
    # the 2.3 evaluator does NOT yet score restore tests (a later session does).
    require_restore_test: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=expression.false(), default=False
    )

    # Disabling stops the site being evaluated without deleting the policy.
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=expression.true(), default=True
    )

    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    site: Mapped["Site"] = relationship()  # noqa: F821


class ComplianceStatus(Base):
    """Latest evaluated compliance result for a policied site (one row per site)."""

    __tablename__ = "compliance_statuses"
    __table_args__ = (
        UniqueConstraint("site_id", name="uq_compliance_statuses_site"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(
        ForeignKey("sites.id", ondelete="CASCADE"), index=True
    )

    # compliant | breached | unknown (see COMPLIANCE_STATES).
    state: Mapped[str] = mapped_column(
        String(20), nullable=False, default="unknown", index=True
    )
    # Newest successful backup's timestamp at evaluation (NULL = never backed up).
    last_backup_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Failed dimensions: [{"code": "rpo", "detail": "…"}, …]. Empty when compliant.
    breaches: Mapped[list] = mapped_column(_JSON, default=list)

    # When the evaluator last ran for this site (UTC).
    evaluated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    site: Mapped["Site"] = relationship()  # noqa: F821


class ComplianceBreachEvent(Base):
    """Structured, channel-free breach record emitted on transition into breach.

    Session 3.1's AlertRule engine reads unconsumed rows (``consumed_at IS NULL``)
    and dispatches notifications; it stamps `consumed_at` to mark a row handled.
    We keep the row after consumption as an audit trail (`site_id` is SET NULL so
    it survives the site being dropped).
    """

    __tablename__ = "compliance_breach_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int | None] = mapped_column(
        ForeignKey("sites.id", ondelete="SET NULL"), index=True
    )
    # Denormalised so a consumer (3.1) can render "site X breached RPO" even after
    # the site row is gone.
    site_name: Mapped[str | None] = mapped_column(String(200))

    # The breach dimensions at emission time (same shape as ComplianceStatus.breaches).
    breaches: Mapped[list] = mapped_column(_JSON, default=list)
    # Snapshot of the newest successful backup age driver at emission.
    last_backup_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rpo_hours: Mapped[int | None] = mapped_column(Integer)

    # NULL until session 3.1 consumes this row into a notification/alert.
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
