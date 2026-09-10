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
        # id.desc() breaks created_at ties deterministically: on SQLite
        # server_default now() has 1-second resolution, so a burst of inserts
        # shares a timestamp — without the id tie-break the "newest 200" set is
        # arbitrary and prune can delete a row it just inserted.
        .where(Notification.user_id == user_id)
        .order_by(Notification.created_at.desc(), Notification.id.desc())
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
            _send_email(user.email, title, body, db=db)
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


def dispatch_restore_test_orphan(
    db: Session, *, scratch_site: str, bench_path: str, job_id: int | None = None
) -> None:
    """Emit backup.restore_test_orphan when a restore-test scratch site failed to
    drop (session 3.4, A.8.10). The scratch may still hold a copy of production
    data, so an operator must be actively told to reap it — a job-log WARNING
    alone is not enough. Names only the scratch site + bench path (no secrets)."""
    dispatch_event(
        db,
        event_type="backup.restore_test_orphan",
        title=f"Restore-test scratch site may be orphaned: {scratch_site}",
        body=(
            f"bench drop-site failed for restore-test scratch site {scratch_site} "
            f"on {bench_path}. It may still hold a copy of the source data — verify "
            f"and reap it with `bench drop-site {scratch_site} --force --no-backup`."
        ),
        entity_type="job",
        entity_id=job_id,
    )


def dispatch_config_drift(
    db: Session, *, server_id: int, server_name: str, artifact_keys: list[str]
) -> None:
    """Emit config.drift when a `server.drift_check` finds out-of-band edits.

    Names only the artefact *keys* that drifted (never their content), so no
    secret can reach a notification channel (golden rule 6)."""
    if not artifact_keys:
        return
    keys = ", ".join(sorted(set(artifact_keys)))
    dispatch_event(
        db,
        event_type="config.drift",
        title=f"Config drift on {server_name}",
        body=f"Out-of-band config changes detected on {server_name}: {keys}.",
        entity_type="server",
        entity_id=server_id,
    )


def dispatch_restic_check_failed(
    db: Session, *, server_id: int, server_name: str, message: str
) -> None:
    """Emit backup.check_failed when a scheduled `restic check` (session 4.2)
    finds repo damage or an error.

    `message` is the short, secret-free summary `parse_check_result` produced —
    never raw restic output, which could in principle echo a path fragment."""
    dispatch_event(
        db,
        event_type="backup.check_failed",
        title=f"Backup integrity check failed on {server_name}",
        body=f"restic check found a problem on {server_name}: {message}",
        entity_type="server",
        entity_id=server_id,
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


def _get_product_name(db: Session) -> str:
    """Return the configured product name (falls back to default if not set)."""
    try:
        from app.models.settings import PlatformSettings
        row = PlatformSettings.get_or_create(db)
        return row.product_name or "FDM Platform"
    except Exception:
        return "FDM Platform"


def _email_html(product_name: str, subject: str, body: str) -> str:
    """Minimal branded HTML email template (no external resources)."""
    import html as _html

    n = _html.escape(product_name)
    s = _html.escape(subject)
    b = _html.escape(body).replace("\n", "<br>")
    # fmt: off — HTML template; line lengths intentional.
    bg = "background:#0a0a0b"
    card_style = "background:#111113;border:1px solid #232326;border-radius:8px"  # noqa: E501
    body_font = (
        "margin:0;padding:0;"
        f"{bg};"
        "color:#f4f4f5;"
        "font-family:ui-sans-serif,system-ui,sans-serif"
    )
    return (
        "<!DOCTYPE html>"
        '<html lang="en">'
        f"<head><meta charset=\"UTF-8\"><title>{s}</title></head>"
        f'<body style="{body_font}">'
        f'<table width="100%" cellpadding="0" cellspacing="0" style="{bg}">'
        '<tr><td align="center" style="padding:32px 16px">'
        f'<table width="480" cellpadding="0" cellspacing="0" style="{card_style}">'
        '<tr><td style="padding:20px 24px;border-bottom:1px solid #232326">'
        f'<span style="font-size:14px;font-weight:600;color:#f4f4f5">{n}</span>'
        "</td></tr>"
        '<tr><td style="padding:24px">'
        f'<p style="margin:0 0 8px;font-size:15px;font-weight:600;color:#f4f4f5">{s}</p>'
        f'<p style="margin:0;font-size:14px;color:#a1a1aa;line-height:1.5">{b}</p>'
        "</td></tr>"
        '<tr><td style="padding:16px 24px;border-top:1px solid #232326">'
        f'<span style="font-size:12px;color:#6b6b74">Sent by {n}</span>'
        "</td></tr>"
        "</table>"
        "</td></tr>"
        "</table>"
        "</body>"
        "</html>"
    )


def _send_email(
    to_address: str,
    subject: str,
    body: str,
    *,
    db: Session | None = None,
    attachments: list[tuple[str, bytes, str]] | None = None,
) -> None:
    """Send one email — branded HTML alternative + optional attachments.

    `db`, when provided, resolves the white-label product name for the branded
    HTML alternative (session 6.6). `attachments` is a list of
    (filename, payload, mime_subtype) — used by the 6.2 report delivery to
    attach the generated artifact. Kept on this one sender so every outbound
    email goes through the same SMTP configuration and the same "no SMTP host
    means silently no-op" contract.
    """
    from app.config import get_settings
    cfg = get_settings()
    if not cfg.smtp_host:
        return
    product_name = _get_product_name(db) if db is not None else "FDM Platform"
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg.smtp_from or "noreply@fdm.local"
    msg["To"] = to_address
    # Plain-text fallback first, then attach HTML alternative.
    msg.set_content(body)
    msg.add_alternative(_email_html(product_name, subject, body), subtype="html")
    for filename, payload, subtype in attachments or []:
        maintype, _, sub = subtype.partition("/")
        if not sub:
            maintype, sub = "application", subtype
        msg.add_attachment(payload, maintype=maintype, subtype=sub, filename=filename)

    context = ssl.create_default_context()
    with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port or 587) as smtp:
        if cfg.smtp_tls:
            smtp.starttls(context=context)
        if cfg.smtp_user and cfg.smtp_password:
            smtp.login(cfg.smtp_user, cfg.smtp_password)
        smtp.send_message(msg)
    logger.debug("email notification sent to %s: %s", to_address, subject)


def send_email_with_attachment(
    to_address: str,
    subject: str,
    body: str,
    *,
    attachments: list[tuple[str, bytes, str]] | None = None,
) -> None:
    """Public entry point for a direct (non-notification) email with files —
    the 6.2 scheduled report delivery. Notification-driven mail still goes
    through `dispatch_event`, which honours per-user channel preferences; a
    report schedule addresses an explicit recipient list instead."""
    _send_email(to_address, subject, body, attachments=attachments)


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

    # Fail closed: without a shared secret the HMAC is keyed on b"" and the
    # X-FDM-Signature is trivially forgeable by anyone who can reach the
    # receiver — an unauthenticated request wearing a signature header, which is
    # worse than sending none because a receiver validating in good faith gains
    # a false authenticity guarantee. Skip the channel and surface the
    # misconfiguration rather than emit an empty-key signature (DOO-1031).
    if not secret:
        logger.warning(
            "webhook channel enabled but notification_webhook_secret is unset — "
            "skipping unsigned delivery to %s (event %s)",
            url,
            event_type,
        )
        return

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
