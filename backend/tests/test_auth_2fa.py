"""TOTP 2FA enrolment, two-step login, recovery codes, disable (session 6.5)."""

from sqlalchemy import select

from app.models import RecoveryCode, User, UserTOTP
from app.models.audit import AuditLog
from tests.conftest import csrf_headers, login, totp_code_for_step


def _enrol(client, email="admin@example.com"):
    login(client, email)
    setup = client.post("/api/auth/2fa/setup", headers=csrf_headers(client))
    assert setup.status_code == 200
    secret = setup.json()["secret"]
    code = totp_code_for_step(secret, 0)
    confirm = client.post(
        "/api/auth/2fa/confirm", json={"code": code}, headers=csrf_headers(client)
    )
    assert confirm.status_code == 200
    return secret, confirm.json()["recovery_codes"]


def test_setup_returns_provisioning_uri_and_secret(client):
    login(client, "admin@example.com")
    r = client.post("/api/auth/2fa/setup", headers=csrf_headers(client))
    assert r.status_code == 200
    body = r.json()
    assert body["secret"]
    assert body["provisioning_uri"].startswith("otpauth://totp/")


def test_confirm_wrong_code_rejected(client):
    login(client, "admin@example.com")
    client.post("/api/auth/2fa/setup", headers=csrf_headers(client))
    r = client.post(
        "/api/auth/2fa/confirm", json={"code": "000000"}, headers=csrf_headers(client)
    )
    assert r.status_code == 401


def test_confirm_issues_ten_unique_recovery_codes(client):
    _, codes = _enrol(client)
    assert len(codes) == 10
    assert len(set(codes)) == 10


def test_login_after_enrolment_requires_mfa_and_omits_user(client):
    _enrol(client)
    client.post("/api/auth/logout")
    r = login(client, "admin@example.com")
    assert r.status_code == 200
    body = r.json()
    assert body["mfa_required"] is True
    assert body["user"] is None


def test_mfa_pending_token_does_not_satisfy_current_user(client):
    _enrol(client)
    client.post("/api/auth/logout")
    login(client, "admin@example.com")
    assert client.get("/api/auth/me").status_code == 401


def test_mfa_pending_token_cannot_reach_mutating_route(client):
    _enrol(client)
    client.post("/api/auth/logout")
    login(client, "admin@example.com")
    r = client.post("/api/test/mutate", headers=csrf_headers(client))
    assert r.status_code == 401


def test_verify_with_totp_code_succeeds_and_starts_session(client):
    secret, _ = _enrol(client)
    client.post("/api/auth/logout")
    login(client, "admin@example.com")
    code = totp_code_for_step(secret, 1)
    r = client.post("/api/auth/2fa/verify", json={"code": code})
    assert r.status_code == 200
    assert r.json()["mfa_enabled"] is True
    assert client.get("/api/auth/me").status_code == 200


def test_verify_rejects_replay_of_the_same_code(client):
    secret, _ = _enrol(client)
    client.post("/api/auth/logout")
    login(client, "admin@example.com")
    code = totp_code_for_step(secret, 1)
    assert client.post("/api/auth/2fa/verify", json={"code": code}).status_code == 200

    client.post("/api/auth/logout")
    login(client, "admin@example.com")
    r = client.post("/api/auth/2fa/verify", json={"code": code})
    assert r.status_code == 401


def test_recovery_code_works_exactly_once(client):
    _, codes = _enrol(client)
    client.post("/api/auth/logout")
    login(client, "admin@example.com")
    r = client.post("/api/auth/2fa/verify", json={"code": codes[0]})
    assert r.status_code == 200

    client.post("/api/auth/logout")
    login(client, "admin@example.com")
    r2 = client.post("/api/auth/2fa/verify", json={"code": codes[0]})
    assert r2.status_code == 401


def test_six_wrong_mfa_codes_lock_out(client):
    secret, _ = _enrol(client)
    client.post("/api/auth/logout")
    login(client, "admin@example.com")
    for _ in range(6):
        assert client.post("/api/auth/2fa/verify", json={"code": "000000"}).status_code == 401
    r = client.post("/api/auth/2fa/verify", json={"code": "000000"})
    assert r.status_code == 429
    assert int(r.headers["Retry-After"]) > 0
    # Even the correct code is rejected while locked.
    good = totp_code_for_step(secret, 1)
    assert client.post("/api/auth/2fa/verify", json={"code": good}).status_code == 429


def test_disable_removes_totp_and_recovery_codes(client, db_session):
    _enrol(client)
    r = client.post("/api/auth/2fa/disable", headers=csrf_headers(client))
    assert r.status_code == 204

    user = db_session.scalars(select(User).where(User.email == "admin@example.com")).first()
    assert db_session.scalars(select(UserTOTP).where(UserTOTP.user_id == user.id)).first() is None
    assert (
        db_session.scalars(select(RecoveryCode).where(RecoveryCode.user_id == user.id)).first()
        is None
    )

    client.post("/api/auth/logout")
    r2 = login(client, "admin@example.com")
    assert r2.json()["mfa_required"] is False


def test_audit_rows_written_for_enrol_and_disable(client, db_session):
    _enrol(client)
    client.post("/api/auth/2fa/disable", headers=csrf_headers(client))
    actions = {row.action for row in db_session.scalars(select(AuditLog)).all()}
    assert "auth.mfa_enrol" in actions
    assert "auth.mfa_disable" in actions


def test_setup_refuses_when_already_confirmed(client):
    _enrol(client)
    r = client.post("/api/auth/2fa/setup", headers=csrf_headers(client))
    assert r.status_code == 400
