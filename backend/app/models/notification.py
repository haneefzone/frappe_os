"""In-app notification feed + per-user channel preferences (session 2.8).

Every significant event (job success/failure, site going down, SSL expiry, …)
can generate a `Notification` row for each user who has that event type enabled
in their `NotificationPreference`. The in-app feed is the primary channel; email
and webhook are the two groundwork channels (full AlertRule engine lands in 3.1).

`NOTIFICATION_EVENTS` is the closed list of event types. Preferences default to
in-app ON, email/webhook OFF — operators opt into external channels per event.
"""

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import expression

from app.db import Base

# Closed list of event types the notification system recognises. Adding one here
# does not require a migration — prefs default to (in_app=True, email/webhook=False)
# the first time a new event type fires for a user.
NOTIFICATION_EVENTS = (
    "job.success",              # a CommandJob completed successfully
    "job.failure",              # a CommandJob failed
    "uptime.down",              # a site went down (was up on last check)
    "uptime.restored",          # a site came back up (was down on last check)
    "ssl.expiry",               # SSL cert expiring ≤30 days (3.1 placeholder)
    "backup.compliance_breach", # RPO breached (3.1 placeholder)
    "backup.check_failed",      # scheduled `restic check` found damage (4.2)
)


class Notification(Base):
    """One in-app notification for a specific user."""

    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(primary_key=True)

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)

    event_type: Mapped[str] = mapped_column(String(50))

    # Short one-liner shown in the bell feed (e.g. "Backup job failed on site1").
    title: Mapped[str] = mapped_column(String(300))
    # Optional longer explanation or context (may be empty).
    body: Mapped[str] = mapped_column(String(1000), default="")

    # Entity this notification links to. Null-ok so structure survives entity
    # deletion (the notification stays as history even when the site is dropped).
    entity_type: Mapped[str | None] = mapped_column(String(30))  # "job"|"site"|"server"|"bench"
    entity_id: Mapped[int | None] = mapped_column(Integer)

    read: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=expression.false(), default=False
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )

    __table_args__ = (
        Index("ix_notifications_user_read", "user_id", "read"),
    )


class NotificationPreference(Base):
    """Per-user, per-event-type channel opt-in (one row per pair).

    Missing rows are treated as the default: in_app=True, email=False,
    webhook=False. The dispatcher upserts on first fire so repeated events
    don't accumulate default rows upfront.
    """

    __tablename__ = "notification_preferences"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    event_type: Mapped[str] = mapped_column(String(50))

    channel_in_app: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=expression.true(), default=True
    )
    channel_email: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=expression.false(), default=False
    )
    channel_webhook: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=expression.false(), default=False
    )
    # Per-preference webhook override (falls back to platform-level if None).
    webhook_url: Mapped[str | None] = mapped_column(String(500))

    __table_args__ = (
        UniqueConstraint("user_id", "event_type", name="uq_notification_pref_user_event"),
    )
