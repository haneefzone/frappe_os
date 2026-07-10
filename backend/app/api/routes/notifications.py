"""Notifications API (session 2.8).

- GET    /api/notifications                 list the current user's notifications
- POST   /api/notifications/{id}/read       mark one notification read
- POST   /api/notifications/read-all        mark all read
- DELETE /api/notifications/{id}            dismiss a notification
- GET    /api/notifications/preferences     get per-event channel prefs
- PUT    /api/notifications/preferences/{event_type}   set prefs for one event
"""
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser
from app.db import get_db
from app.models.notification import NOTIFICATION_EVENTS, Notification, NotificationPreference

router = APIRouter(prefix="/api/notifications", tags=["notifications"])


# --------------------------------------------------------------------------- #
# Schemas                                                                      #
# --------------------------------------------------------------------------- #

class NotificationOut(BaseModel):
    id: int
    event_type: str
    title: str
    body: str
    entity_type: str | None
    entity_id: int | None
    read: bool
    created_at: str

    model_config = {"from_attributes": True}


class NotificationListOut(BaseModel):
    items: list[NotificationOut]
    unread_count: int


class NotificationPrefOut(BaseModel):
    event_type: str
    channel_in_app: bool
    channel_email: bool
    channel_webhook: bool
    webhook_url: str | None

    model_config = {"from_attributes": True}


class SetPrefRequest(BaseModel):
    channel_in_app: bool = True
    channel_email: bool = False
    channel_webhook: bool = False
    webhook_url: str | None = None

    @field_validator("webhook_url")
    @classmethod
    def validate_webhook_url(cls, v: str | None) -> str | None:
        if v is not None and not v.startswith(("http://", "https://")):
            raise ValueError("webhook_url must be http(s)")
        return v or None


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #

def _serialize(n: Notification) -> NotificationOut:
    return NotificationOut(
        id=n.id,
        event_type=n.event_type,
        title=n.title,
        body=n.body,
        entity_type=n.entity_type,
        entity_id=n.entity_id,
        read=n.read,
        created_at=n.created_at.isoformat(),
    )


# --------------------------------------------------------------------------- #
# Routes                                                                       #
# --------------------------------------------------------------------------- #

@router.get("", response_model=NotificationListOut)
def list_notifications(
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
    limit: int = 50,
    unread_only: bool = False,
) -> NotificationListOut:
    q = select(Notification).where(Notification.user_id == user.id)
    if unread_only:
        q = q.where(Notification.read.is_(False))
    items = db.execute(q.order_by(Notification.created_at.desc()).limit(limit)).scalars().all()

    unread_count = db.execute(
        select(Notification).where(
            Notification.user_id == user.id,
            Notification.read.is_(False),
        )
    ).scalars().all()

    return NotificationListOut(
        items=[_serialize(n) for n in items],
        unread_count=len(unread_count),
    )


@router.post("/{notification_id}/read", response_model=NotificationOut)
def mark_read(
    notification_id: int,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
) -> NotificationOut:
    notif = db.get(Notification, notification_id)
    if notif is None or notif.user_id != user.id:
        raise HTTPException(status_code=404, detail="Notification not found.")
    notif.read = True
    db.commit()
    db.refresh(notif)
    return _serialize(notif)


@router.post("/read-all")
def mark_all_read(
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
) -> dict:
    from sqlalchemy import update

    db.execute(
        update(Notification)
        .where(Notification.user_id == user.id, Notification.read.is_(False))
        .values(read=True),
        execution_options={"synchronize_session": False},
    )
    db.commit()
    return {"ok": True}


@router.delete("/{notification_id}")
def dismiss_notification(
    notification_id: int,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
) -> dict:
    notif = db.get(Notification, notification_id)
    if notif is None or notif.user_id != user.id:
        raise HTTPException(status_code=404, detail="Notification not found.")
    db.delete(notif)
    db.commit()
    return {"ok": True}


@router.get("/preferences", response_model=list[NotificationPrefOut])
def get_preferences(
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
) -> list[NotificationPrefOut]:
    rows = db.execute(
        select(NotificationPreference).where(NotificationPreference.user_id == user.id)
    ).scalars().all()
    prefs_by_event = {p.event_type: p for p in rows}

    result = []
    for event_type in NOTIFICATION_EVENTS:
        pref = prefs_by_event.get(event_type)
        if pref:
            result.append(
                NotificationPrefOut(
                    event_type=pref.event_type,
                    channel_in_app=pref.channel_in_app,
                    channel_email=pref.channel_email,
                    channel_webhook=pref.channel_webhook,
                    webhook_url=pref.webhook_url,
                )
            )
        else:
            result.append(
                NotificationPrefOut(
                    event_type=event_type,
                    channel_in_app=True,
                    channel_email=False,
                    channel_webhook=False,
                    webhook_url=None,
                )
            )
    return result


@router.put("/preferences/{event_type}", response_model=NotificationPrefOut)
def set_preference(
    event_type: str,
    body: SetPrefRequest,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
) -> NotificationPrefOut:
    if event_type not in NOTIFICATION_EVENTS:
        raise HTTPException(status_code=422, detail=f"Unknown event type: {event_type!r}")

    pref = db.execute(
        select(NotificationPreference).where(
            NotificationPreference.user_id == user.id,
            NotificationPreference.event_type == event_type,
        )
    ).scalar_one_or_none()

    if pref is None:
        pref = NotificationPreference(user_id=user.id, event_type=event_type)
        db.add(pref)

    pref.channel_in_app = body.channel_in_app
    pref.channel_email = body.channel_email
    pref.channel_webhook = body.channel_webhook
    pref.webhook_url = body.webhook_url
    db.commit()
    db.refresh(pref)

    return NotificationPrefOut(
        event_type=pref.event_type,
        channel_in_app=pref.channel_in_app,
        channel_email=pref.channel_email,
        channel_webhook=pref.channel_webhook,
        webhook_url=pref.webhook_url,
    )
