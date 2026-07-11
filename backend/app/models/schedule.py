"""Recurring-job schedules (session 2.1).

A `Schedule` makes a tracked action run automatically on a cadence. Today it
drives two actions against a site:

- `site.backup`            — a recurring backup (reuses the 1.11 engine), and
- `backup.retention_sweep` — prune a site's backups down to a retention policy.

The scheduler process (``app.workers.scheduler``) ticks periodically, finds
schedules whose ``next_run_at`` has arrived, and — through the same `JobRunner`
every other mutation uses — enqueues a real `CommandJob` (golden rules 2/3). So
a scheduled fire is indistinguishable from a hand-launched job: it is locked,
audited, streamed and visible in the Jobs list; only its *trigger* differs.

**Cadence** is either a 5-field cron expression (`cron`) evaluated in the
schedule's `timezone` (default `Asia/Dubai`, golden rule 8 — all instants are
stored UTC), or a fixed `interval_seconds`. Exactly one is set.

**Missed-run / catch-up policy** (documented here because the column semantics
encode it): the scheduler does **not** back-fill. If the process was down and
`next_run_at` is now well in the past, the schedule fires **once** on the next
tick and its new `next_run_at` is computed forward from *now*, not from the
stale value — so a day of downtime yields one catch-up backup, never a stampede
of missed occurrences. `enabled=False` makes it non-due, stopping future runs
without losing its configuration or history.
"""

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import expression

from app.db import Base

# The actions a schedule may drive. Both target a site; future maintenance-window
# actions (session 3.5) will extend this — kept as a plain tuple the API validates
# against rather than a DB enum so adding an action needs no migration.
SCHEDULE_ACTIONS = (
    "site.backup",
    "backup.retention_sweep",
    # Domains & SSL (session 2.4): certbot renewal + cert-expiry refresh, both
    # scoped to a site's domains, wired here so 2.1 fires them on a cadence.
    "ssl.certbot_renew",
    "ssl.expiry_scan",
)

# What a schedule points at. Only "site" today (backups + sweeps operate on a
# site); "bench"/"server" become valid when their scheduled actions land.
SCHEDULE_TARGET_TYPES = ("site",)


class Schedule(Base):
    """One recurring action bound to a target, its cadence, and run bookkeeping."""

    __tablename__ = "schedules"

    id: Mapped[int] = mapped_column(primary_key=True)

    # Human label shown in the Schedules list ("Nightly backup — test1.localhost").
    name: Mapped[str] = mapped_column(String(200))

    # What this schedule acts on. target_type is a coarse kind ("site") and
    # target_id is that row's primary key, resolved to a site/bench/server at
    # dispatch time. Kept generic (not a hard FK) so a target row that is later
    # marked missing doesn't cascade-delete its schedule history; the dispatcher
    # fails the fire cleanly if the target has vanished.
    target_type: Mapped[str] = mapped_column(String(20), default="site")
    target_id: Mapped[int] = mapped_column(Integer, index=True)

    # site.backup | backup.retention_sweep (see SCHEDULE_ACTIONS).
    action_name: Mapped[str] = mapped_column(String(60))

    # Cadence: exactly one of cron / interval_seconds is set. cron is a 5-field
    # expression evaluated in `timezone`; interval_seconds is a fixed period.
    cron: Mapped[str | None] = mapped_column(String(100))
    interval_seconds: Mapped[int | None] = mapped_column(Integer)
    # IANA tz the cron is read in (golden rule 8 default). Instants stay UTC.
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Dubai")

    # RQ priority the enqueued CommandJob runs at. Backups/sweeps default low.
    priority: Mapped[str] = mapped_column(String(10), default="low")

    # site.backup: whether the recurring backup includes files (--with-files).
    with_files: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=expression.false(), default=False
    )

    # Retention policy (backup.retention_sweep). Keep the newest `keep_last`
    # backups AND/OR any backup newer than `keep_days` days; NULL means that
    # dimension is not applied. The sweep never deletes the newest/only backup
    # regardless of policy (safety floor, enforced in the action).
    retention_keep_last: Mapped[int | None] = mapped_column(Integer)
    retention_keep_days: Mapped[int | None] = mapped_column(Integer)

    # Enabled gates due-ness: disabling stops future runs without deleting the row.
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=expression.true(), default=True
    )

    # When this schedule next becomes due (UTC). NULL = never (e.g. cadence not
    # yet computed). Set on create/enable and advanced after every fire.
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    # When it last fired (UTC), and the job that fire produced (SET NULL so the
    # schedule survives a job purge and the list can still show "last result").
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_run_job_id: Mapped[int | None] = mapped_column(
        ForeignKey("command_jobs.id", ondelete="SET NULL"), index=True
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
