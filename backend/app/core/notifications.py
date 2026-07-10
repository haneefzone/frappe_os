"""Notification dispatch service (session 2.8).

Three responsibilities:
1. Create `Notification` rows for a user (in-app channel).
2. Dispatch an event to *all* users who have that event type enabled — selecting
   their per-user channel preferences (in-app / email / webhook) and sending
   through the relevant sender.
3. Minimal channel senders: SMTP email and signed (HMAC-SHA256) webhook POST.
   The full AlertRule engine is Phase 3.1.

Design constraints:
- Secrets never appear in notification bodies (golden rule 6).
- Dispatch runs inside the RQ worker / uptime-checker async loop after the DB
  commit that triggered it — never blocking the request path (rule 3).
- A channel failure never silences the others: errors are logged and swallowed.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import smtplib
import ssl
from datetime import UTC, datetime
from email.message import EmailMessage

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.auth import User
from app.models.notification import Notification, NotificationPreference

logger = logging.getLogger("app.notifications")

# How many in-app notifications to keep per user before the oldest are pruned.
# Large enough for a week of busy events without growing unbounded.
_MAX_PER_USER = 200


def create_notification(
    db: Session,
    *,
    user_id: int,
    event_type: str,
    title: str,
    body: str = "",
    entity_type: str | None = None,
    entity_id: int | None = None,
) -> Notification:
    """Persist one in-app notification, pruning the oldest if over the limit."""
    notif = Notification(
        user_id=user_id,
        event_type=event_type,
        title=title,
        body=body,
        entity_type=entity_type,
        entity_id=entity_id,
    )
    db.add(notif)
    db.flush()
    _prune_old(db, user_id)
    db.commit()
    db.refresh(notif)
    return notif


def _prune_old(db: Session, user_id: int) -> None:
    from sqlalchemy import delete

    subq = (
        select(Notification.id)
        .where(Notification.user_id == user_id)
        .order_by(Notification.created_at.desc())
        .limit(_MAX_PER_USER)
        .subquery()
    )
    db.execute(
        delete(Notification).where(
            Notification.user_id == user_id,
            Notification.id.notin_(select(subq.c.id)),
        ),
        execution_options={"synchronize_session": False},
    )


def _get_pref(db: Session, user_id: int, event_type: str) -> NotificationPreference:
    """Return the user's preference for this event type, using defaults if absent."""
    pref = db.execute(
        select(NotificationPreference).where(
            NotificationPreference.user_id == user_id,
            NotificationPreference.event_type == event_type,
        )
    ).scalar_one_or_none()
    if pref is None:
        pref = NotificationPreference(
            user_id=user_id,
            event_type=event_type,
        )
        db.add(pref)
        db.commit()
        db.refresh(pref)
    return pref


def dispatch_event(
    db: Session,
    *,
    event_type: str,
    title: str,
    body: str = "",
    entity_type: str | None = None,
    entity_id: int | None = None,
) -> None:
    """Fan out an event to all active users according to their per-event prefs."""
    users = db.execute(select(User).where(User.is_active.is_(True))).scalars().all()
    for user in users:
        try:
            _dispatch_to_user(
                db,
                user=user,
                event_type=event_type,
                title=title,
                body=body,
                entity_type=entity_type,
                entity_id=entity_id,
            )
        except Exception:
            logger.exception("notification dispatch failed for user %s", user.id)


def _dispatch_to_user(
    db: Session,
    *,
    user: User,
    event_type: str,
    title: str,
    body: str,
    entity_type: str | None,
    entity_id: int | None,
) -> None:
    pref = _get_pref(db, user.id, event_type)

    if pref.channel_in_app:
        create_notification(
            db,
            user_id=user.id,
            event_type=event_type,
            title=title,
            body=body,
            entity_type=entity_type,
            entity_id=entity_id,
        )

    if pref.channel_email and user.email:
        try:
            _send_email(user.email, title, body)
        except Exception:
            logger.exception("email notification failed for user %s", user.id)

    if pref.channel_webhook:
        webhook_url = pref.webhook_url or _platform_webhook_url()
        if webhook_url:
            try:
                _send_webhook(webhook_url, event_type, title, body, entity_type, entity_id)
            except Exception:
                logger.exception("webhook notification failed for user %s", user.id)


# --------------------------------------------------------------------------- #
# Event helpers — called from jobs.py / uptime.py                             #
# --------------------------------------------------------------------------- #

def dispatch_job_event(db: Session, *, job_id: int, action_name: str, status: str) -> None:
    """Emit job.success or job.failure when a CommandJob reaches a terminal state."""
    if status not in ("success", "failure"):
        return
    event_type = f"job.{status}"
    verb = "completed" if status == "success" else "failed"
    dispatch_event(
        db,
        event_type=event_type,
        title=f"Job {verb}: {action_name}",
        body=f"Job #{job_id} ({action_name}) {verb}.",
        entity_type="job",
        entity_id=job_id,
    )


def dispatch_uptime_event(
    db: Session, *, site_id: int, site_name: str, up: bool, was_up: bool | None
) -> None:
    """Emit uptime.down or uptime.restored on a state flip."""
    if was_up is None:
        return
    if up and not was_up:
        dispatch_event(
            db,
            event_type="uptime.restored",
            title=f"Site restored: {site_name}",
            body=f"{site_name} is back up.",
            entity_type="site",
            entity_id=site_id,
        )
    elif not up and was_up:
        dispatch_event(
            db,
            event_type="uptime.down",
            title=f"Site down: {site_name}",
            body=f"{site_name} is unreachable.",
            entity_type="site",
            entity_id=site_id,
        )


# --------------------------------------------------------------------------- #
# Channel senders                                                              #
# --------------------------------------------------------------------------- #

def _platform_webhook_url() -> str | None:
    try:
        from app.config import get_settings
        return get_settings().notification_webhook_url or None
    except Exception:
        return None


def _send_email(to_address: str, subject: str, body: str) -> None:
    from app.config import get_settings
    cfg = get_settings()
    if not cfg.smtp_host:
        return
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg.smtp_from or "noreply@fdm.local"
    msg["To"] = to_address
    msg.set_content(body)

    context = ssl.create_default_context()
    with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port or 587) as smtp:
        if cfg.smtp_tls:
            smtp.starttls(context=context)
        if cfg.smtp_user and cfg.smtp_password:
            smtp.login(cfg.smtp_user, cfg.smtp_password)
        smtp.send_message(msg)
    logger.debug("email notification sent to %s: %s", to_address, subject)


def _send_webhook(
    url: str,
    event_type: str,
    title: str,
    body: str,
    entity_type: str | None,
    entity_id: int | None,
) -> None:
    from app.config import get_settings
    cfg = get_settings()
    secret = cfg.notification_webhook_secret or ""

    payload = json.dumps(
        {
            "event_type": event_type,
            "title": title,
            "body": body,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "ts": datetime.now(UTC).isoformat(),
        },
        separators=(",", ":"),
    ).encode()

    sig = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    headers = {
        "Content-Type": "application/json",
        "X-FDM-Signature": f"sha256={sig}",
    }
    resp = httpx.post(url, content=payload, headers=headers, timeout=10)
    resp.raise_for_status()
    logger.debug("webhook notification sent to %s: %s", url, event_type)
