"""Maintenance windows (session 3.5).

A `MaintenanceWindow` marks a recurring time slot on a server during which
dangerous action-classes (update / restore / production_setup) are refused.
The guard runs server-side in `JobRunner.create`, so API callers cannot bypass
it by skipping the UI.

Recurrence is expressed as a 5-field cron expression in the window's timezone.
`duration_minutes` determines how long after the cron trigger the window stays
active. Exactly one server is scoped per window; fleet-wide windows are modelled
by creating one window per server (or extending this model later with
target_type="fleet").
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import expression

from app.db import Base

# The coarse action-classes that maintenance windows can block. These match
# the `danger_class` field on CommandTemplate (templates.py).
DANGER_CLASSES = ("update", "restore", "production_setup")


class MaintenanceWindow(Base):
    """One recurring blocked-time slot on a server."""

    __tablename__ = "maintenance_windows"

    id: Mapped[int] = mapped_column(primary_key=True)

    name: Mapped[str] = mapped_column(String(200))

    server_id: Mapped[int] = mapped_column(
        ForeignKey("servers.id", ondelete="CASCADE"), index=True
    )

    # 5-field cron expression (minute hour dom month dow) evaluated in `timezone`.
    # Instants derived from it are always converted to UTC before comparison.
    cron: Mapped[str] = mapped_column(String(100))

    # How long after the cron trigger fires this window stays active.
    duration_minutes: Mapped[int] = mapped_column(Integer, default=120)

    # IANA timezone for the cron expression (stored UTC as usual).
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Dubai")

    # JSON list of danger-class strings that are blocked during this window.
    # Example: ["update", "restore"]. Empty list = window exists but blocks nothing.
    blocked_danger_classes: Mapped[list] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), default=list
    )

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

    def is_active_at(self, at: datetime | None = None) -> bool:
        """Return True if this window is active at `at` (default: now UTC).

        Active means: the most-recent cron trigger AT OR BEFORE `at` started a
        window that hasn't yet expired. We add 1 second to `at` before calling
        get_prev so that an `at` exactly equal to a trigger is treated as within
        the window that trigger opened (not the previous one).
        """
        if not self.enabled:
            return False
        if at is None:
            at = datetime.now(UTC)
        try:
            from datetime import timedelta
            from zoneinfo import ZoneInfo

            from croniter import croniter

            tz = ZoneInfo(self.timezone or "UTC")
            # Add 1 second so that `at` exactly on a trigger returns THAT trigger.
            probe = (at + timedelta(seconds=1)).astimezone(tz)
            itr = croniter(self.cron, probe)
            prev_local: datetime = itr.get_prev(datetime)
            prev_utc = prev_local.astimezone(UTC)
            window_end = prev_utc.timestamp() + self.duration_minutes * 60
            return at.timestamp() < window_end
        except Exception:
            return False

    def occurrences_between(self, start: datetime, end: datetime) -> list[datetime]:
        """Return cron trigger UTC datetimes in [start, end) for calendar rendering."""
        try:
            from zoneinfo import ZoneInfo

            from croniter import croniter

            tz = ZoneInfo(self.timezone or "UTC")
            start_local = start.astimezone(tz)
            itr = croniter(self.cron, start_local)
            results: list[datetime] = []
            while True:
                nxt_local: datetime = itr.get_next(datetime)
                nxt_utc = nxt_local.astimezone(UTC)
                if nxt_utc > end:
                    break
                results.append(nxt_utc)
            return results
        except Exception:
            return []
