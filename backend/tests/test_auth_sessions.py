"""Session enumeration + revocation (session 6.5): revoking a session must stop
the very next request, not just the next login."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.models import SecurityPolicy, UserSession
from app.models.audit import AuditLog
from tests.conftest import csrf_headers, login


def test_login_creates_one_session_marked_current(client):
    login(client, "admin@example.com")
    r = client.get("/api/auth/sessions")
    assert r.status_code == 200
    sessions = r.json()
    assert len(sessions) == 1
    assert sessions[0]["is_current"] is True


def test_revoking_current_session_rejects_the_next_request(client):
    login(client, "admin@example.com")
    session_id = client.get("/api/auth/sessions").json()[0]["id"]
    r = client.post(f"/api/auth/sessions/{session_id}/revoke", headers=csrf_headers(client))
    assert r.status_code == 204
    # Not just the next login — the very next request on the same cookies.
    assert client.get("/api/auth/me").status_code == 401


def test_revoking_someone_elses_session_is_not_found(client, db_session):
    login(client, "admin@example.com")
    other = UserSession(user_id=99999, jti="not-mine", created_at=datetime.now(UTC),
                         last_seen_at=datetime.now(UTC))
    db_session.add(other)
    db_session.commit()
    r = client.post(f"/api/auth/sessions/{other.id}/revoke", headers=csrf_headers(client))
    assert r.status_code == 404


def test_revoke_others_leaves_current_session_intact(client, proxy_client_factory):
    # Two independent logins (each its own cookie jar = its own UserSession row)
    # sharing the same underlying app/db, mirroring two devices.
    login(client, "admin@example.com")
    other_device = proxy_client_factory("testclient")
    login(other_device, "admin@example.com")

    assert len(client.get("/api/auth/sessions").json()) == 2

    r = client.post("/api/auth/sessions/revoke-others", headers=csrf_headers(client))
    assert r.status_code == 204

    assert client.get("/api/auth/me").status_code == 200
    assert other_device.get("/api/auth/me").status_code == 401


def test_logout_revokes_the_session_row(client, db_session):
    login(client, "admin@example.com")
    session_id = client.get("/api/auth/sessions").json()[0]["id"]
    r = client.post("/api/auth/logout")
    assert r.status_code == 204
    row = db_session.get(UserSession, session_id)
    assert row.revoked_at is not None


def test_logout_all_revokes_every_session(client, proxy_client_factory):
    login(client, "admin@example.com")
    other_device = proxy_client_factory("testclient")
    login(other_device, "admin@example.com")

    r = client.post("/api/auth/logout-all", headers=csrf_headers(client))
    assert r.status_code == 204
    assert other_device.get("/api/auth/me").status_code == 401


def test_idle_timeout_revokes_session_on_next_request(client, db_session):
    login(client, "admin@example.com")
    policy = SecurityPolicy.get_or_create(db_session)
    policy.session_idle_timeout_minutes = 1
    db_session.commit()

    session_id = client.get("/api/auth/sessions").json()[0]["id"]
    row = db_session.get(UserSession, session_id)
    row.last_seen_at = datetime.now(UTC) - timedelta(minutes=5)
    db_session.commit()

    assert client.get("/api/auth/me").status_code == 401
    db_session.refresh(row)
    assert row.revoked_at is not None


def test_absolute_timeout_revokes_session_on_next_request(client, db_session):
    login(client, "admin@example.com")
    policy = SecurityPolicy.get_or_create(db_session)
    policy.session_absolute_timeout_minutes = 1
    db_session.commit()

    session_id = client.get("/api/auth/sessions").json()[0]["id"]
    row = db_session.get(UserSession, session_id)
    row.created_at = datetime.now(UTC) - timedelta(minutes=5)
    db_session.commit()

    assert client.get("/api/auth/me").status_code == 401


def test_session_revoke_writes_audit_row(client, db_session):
    login(client, "admin@example.com")
    session_id = client.get("/api/auth/sessions").json()[0]["id"]
    client.post(f"/api/auth/sessions/{session_id}/revoke", headers=csrf_headers(client))
    actions = {row.action for row in db_session.scalars(select(AuditLog)).all()}
    assert "auth.session_revoke" in actions
