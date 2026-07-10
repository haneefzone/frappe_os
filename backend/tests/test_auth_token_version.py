"""SEC-M2 (DOO-68): server-side token revocation via per-user token_version.

Covers the required cases: a stale-version access token is rejected, a stale
refresh token is rejected, the normal flow is unaffected, and "log out
everywhere" invalidates an already-exfiltrated refresh cookie.
"""

import jwt

from app.config import get_settings
from tests.conftest import csrf_headers, login


def _bump_db(db_session, seeded_users, key="admin"):
    """Simulate an out-of-band revocation (password/role change, deactivate,
    or a logout-all from another device) by advancing token_version in the DB."""
    user = seeded_users[key]
    user.token_version += 1
    db_session.commit()


def test_issued_tokens_carry_token_version_claim(client):
    login(client, "admin@example.com")
    secret = get_settings().jwt_secret
    for cookie in ("fdm_access_token", "fdm_refresh_token"):
        claims = jwt.decode(client.cookies.get(cookie), secret, algorithms=["HS256"])
        assert claims["tv"] == 0


def test_normal_flow_unaffected(client):
    login(client, "admin@example.com")
    assert client.get("/api/auth/me").status_code == 200
    # A refresh re-mints tokens at the current version and keeps working.
    assert client.post("/api/auth/refresh", headers=csrf_headers(client)).status_code == 200
    assert client.get("/api/auth/me").status_code == 200


def test_stale_access_token_rejected(client, db_session, seeded_users):
    login(client, "admin@example.com")
    assert client.get("/api/auth/me").status_code == 200
    _bump_db(db_session, seeded_users)
    # The access cookie still decodes and is unexpired, but its tv is now stale.
    assert client.get("/api/auth/me").status_code == 401


def test_stale_refresh_token_rejected(client, db_session, seeded_users):
    login(client, "admin@example.com")
    _bump_db(db_session, seeded_users)
    resp = client.post("/api/auth/refresh", headers=csrf_headers(client))
    assert resp.status_code == 401


def test_logout_all_revokes_all_sessions(client, db_session, seeded_users):
    login(client, "admin@example.com")
    # Capture the pre-logout cookies to model an exfiltrated refresh token.
    stolen_refresh = client.cookies.get("fdm_refresh_token")
    csrf = client.cookies.get("fdm_csrf_token")

    resp = client.post("/api/auth/logout-all", headers={"X-CSRF-Token": csrf})
    assert resp.status_code == 204
    # token_version advanced in the DB.
    db_session.refresh(seeded_users["admin"])
    assert seeded_users["admin"].token_version == 1

    # The stolen refresh cookie no longer refreshes — it was invalidated, not
    # merely cleared from this browser. Replay it against a clean jar.
    client.cookies.set("fdm_refresh_token", stolen_refresh)
    client.cookies.set("fdm_csrf_token", csrf)
    replay = client.post("/api/auth/refresh", headers={"X-CSRF-Token": csrf})
    assert replay.status_code == 401


def test_logout_all_requires_auth(client):
    assert client.post("/api/auth/logout-all").status_code == 401


def test_logout_all_requires_csrf(client):
    login(client, "admin@example.com")
    # Authenticated but no CSRF header → double-submit check fails.
    assert client.post("/api/auth/logout-all").status_code == 403
