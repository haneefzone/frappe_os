"""Tests for the notification feed + preference API (session 2.8)."""

from sqlalchemy.orm import Session

from app.core.notifications import (
    create_notification,
    dispatch_event,
    dispatch_job_event,
    dispatch_uptime_event,
)
from app.models.notification import NOTIFICATION_EVENTS, Notification
from tests.conftest import csrf_headers, login

# --------------------------------------------------------------------------- #
# Unit: notification model helpers                                             #
# --------------------------------------------------------------------------- #

def test_create_notification(db_session: Session, seeded_users):
    user = seeded_users["admin"]
    n = create_notification(
        db_session,
        user_id=user.id,
        event_type="job.failure",
        title="Backup failed",
        body="Job #1 failed.",
        entity_type="job",
        entity_id=1,
    )
    assert n.id is not None
    assert n.read is False
    assert n.entity_type == "job"


def test_prune_keeps_newest(db_session: Session, seeded_users):
    from app.core.notifications import _MAX_PER_USER

    user = seeded_users["admin"]
    for i in range(_MAX_PER_USER + 5):
        create_notification(
            db_session,
            user_id=user.id,
            event_type="job.success",
            title=f"Job {i} succeeded",
        )
    count = db_session.query(Notification).filter_by(user_id=user.id).count()
    assert count <= _MAX_PER_USER


def test_dispatch_event_creates_for_all_users(db_session: Session, seeded_users):
    before = db_session.query(Notification).count()
    dispatch_event(
        db_session,
        event_type="job.failure",
        title="A job failed",
        body="details",
        entity_type="job",
        entity_id=42,
    )
    after = db_session.query(Notification).count()
    # admin, developer, readonly are all active — 3 notifications
    assert after - before == 3


def test_dispatch_job_event_success(db_session: Session, seeded_users):
    dispatch_job_event(db_session, job_id=99, action_name="site.backup", status="success")
    notif = db_session.query(Notification).filter_by(event_type="job.success").first()
    assert notif is not None
    assert "site.backup" in notif.title


def test_dispatch_job_event_failure(db_session: Session, seeded_users):
    dispatch_job_event(db_session, job_id=100, action_name="site.migrate", status="failure")
    notif = db_session.query(Notification).filter_by(event_type="job.failure").first()
    assert notif is not None


def test_dispatch_job_event_skips_other_statuses(db_session: Session, seeded_users):
    before = db_session.query(Notification).count()
    dispatch_job_event(db_session, job_id=1, action_name="site.backup", status="running")
    assert db_session.query(Notification).count() == before


def test_dispatch_uptime_down(db_session: Session, seeded_users):
    dispatch_uptime_event(
        db_session, site_id=1, site_name="test1.localhost", up=False, was_up=True
    )
    notif = db_session.query(Notification).filter_by(event_type="uptime.down").first()
    assert notif is not None
    assert "test1.localhost" in notif.title


def test_dispatch_uptime_restored(db_session: Session, seeded_users):
    dispatch_uptime_event(
        db_session, site_id=2, site_name="test2.localhost", up=True, was_up=False
    )
    notif = db_session.query(Notification).filter_by(event_type="uptime.restored").first()
    assert notif is not None


def test_dispatch_uptime_no_flip_no_event(db_session: Session, seeded_users):
    before = db_session.query(Notification).count()
    dispatch_uptime_event(
        db_session, site_id=3, site_name="site", up=True, was_up=True
    )
    assert db_session.query(Notification).count() == before


def test_dispatch_uptime_unknown_prev(db_session: Session, seeded_users):
    before = db_session.query(Notification).count()
    dispatch_uptime_event(
        db_session, site_id=4, site_name="site", up=False, was_up=None
    )
    assert db_session.query(Notification).count() == before


# --------------------------------------------------------------------------- #
# Integration: notifications API                                               #
# --------------------------------------------------------------------------- #

def test_list_notifications_empty(client, seeded_users):
    login(client, "admin@example.com")
    resp = client.get("/api/notifications")
    assert resp.status_code == 200
    data = resp.json()
    assert "items" in data
    assert data["unread_count"] == 0


def test_notifications_lifecycle(client, seeded_users, db_session: Session):
    login(client, "admin@example.com")
    user = seeded_users["admin"]
    create_notification(
        db_session,
        user_id=user.id,
        event_type="job.failure",
        title="Backup failed",
        body="details",
        entity_type="job",
        entity_id=7,
    )
    resp = client.get("/api/notifications")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["items"]) == 1
    assert data["unread_count"] == 1
    notif_id = data["items"][0]["id"]

    # Mark read
    resp = client.post(f"/api/notifications/{notif_id}/read", headers=csrf_headers(client))
    assert resp.status_code == 200
    assert resp.json()["read"] is True

    # Unread count drops
    resp = client.get("/api/notifications")
    assert resp.json()["unread_count"] == 0


def test_mark_all_read(client, seeded_users, db_session: Session):
    login(client, "admin@example.com")
    user = seeded_users["admin"]
    for i in range(3):
        create_notification(
            db_session, user_id=user.id, event_type="job.success", title=f"Job {i}"
        )
    resp = client.post("/api/notifications/read-all", headers=csrf_headers(client))
    assert resp.status_code == 200
    resp = client.get("/api/notifications")
    assert resp.json()["unread_count"] == 0


def test_dismiss_notification(client, seeded_users, db_session: Session):
    login(client, "admin@example.com")
    user = seeded_users["admin"]
    n = create_notification(
        db_session, user_id=user.id, event_type="job.failure", title="fail"
    )
    resp = client.delete(f"/api/notifications/{n.id}", headers=csrf_headers(client))
    assert resp.status_code == 200
    resp = client.get("/api/notifications")
    assert len(resp.json()["items"]) == 0


def test_cannot_read_other_users_notification(client, seeded_users, db_session: Session):
    login(client, "admin@example.com")
    other = seeded_users["developer"]
    n = create_notification(
        db_session, user_id=other.id, event_type="job.failure", title="other"
    )
    resp = client.post(f"/api/notifications/{n.id}/read", headers=csrf_headers(client))
    assert resp.status_code == 404


def test_preferences_defaults(client, seeded_users):
    login(client, "admin@example.com")
    resp = client.get("/api/notifications/preferences")
    assert resp.status_code == 200
    prefs = resp.json()
    assert len(prefs) == len(NOTIFICATION_EVENTS)
    for p in prefs:
        assert p["channel_in_app"] is True
        assert p["channel_email"] is False
        assert p["channel_webhook"] is False


def test_set_preference(client, seeded_users):
    login(client, "admin@example.com")
    resp = client.put(
        "/api/notifications/preferences/job.failure",
        json={"channel_in_app": True, "channel_email": True, "channel_webhook": False},
        headers=csrf_headers(client),
    )
    assert resp.status_code == 200
    assert resp.json()["channel_email"] is True

    resp = client.get("/api/notifications/preferences")
    pref = next(p for p in resp.json() if p["event_type"] == "job.failure")
    assert pref["channel_email"] is True


def test_set_preference_invalid_event(client, seeded_users):
    login(client, "admin@example.com")
    resp = client.put(
        "/api/notifications/preferences/unknown.event",
        json={"channel_in_app": True},
        headers=csrf_headers(client),
    )
    assert resp.status_code == 422


# --------------------------------------------------------------------------- #
# Integration: search API                                                     #
# --------------------------------------------------------------------------- #

def test_search_empty_query_returns_nav(client, seeded_users):
    login(client, "admin@example.com")
    resp = client.get("/api/search?q=")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["results"]) > 0
    kinds = {r["kind"] for r in data["results"]}
    assert "nav" in kinds


def test_search_nav_match(client, seeded_users):
    login(client, "admin@example.com")
    resp = client.get("/api/search?q=dashboard")
    assert resp.status_code == 200
    results = resp.json()["results"]
    nav_items = [r for r in results if r["kind"] == "nav"]
    assert any("Dashboard" in r["title"] for r in nav_items)


def test_search_unauthenticated(client):
    resp = client.get("/api/search?q=test")
    assert resp.status_code == 401


def test_search_readonly_no_actions(client, seeded_users, db_session):
    login(client, "readonly@example.com")
    resp = client.get("/api/search?q=backup")
    assert resp.status_code == 200
    results = resp.json()["results"]
    action_results = [r for r in results if r["kind"] == "action"]
    assert len(action_results) == 0
