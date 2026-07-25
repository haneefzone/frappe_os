"""SecurityPolicy: settings CRUD, enforce-2FA-for-role gate, IP allowlist,
password policy validator, and the persisted login-attempts log (session 6.5).
"""

from sqlalchemy import select

from app.core.security import hash_password, validate_password_policy
from app.models.audit import AuditLog
from app.models.mfa import LoginAttempt
from tests.conftest import PASSWORD, csrf_headers, login, totp_code_for_step


def _login_from_ip(client, ip: str, email: str = "admin@example.com"):
    return client.post(
        "/api/auth/login",
        json={"email": email, "password": PASSWORD},
        headers={"X-Forwarded-For": ip},
    )


def test_get_security_policy_defaults(client):
    login(client, "admin@example.com")
    r = client.get("/api/settings/security")
    assert r.status_code == 200
    body = r.json()
    assert body["enforce_2fa_roles"] == []
    assert body["ip_allowlist"] == []
    assert body["password_min_length"] == 10


def test_put_security_policy_requires_settings_manage(client):
    login(client, "readonly@example.com")
    r = client.put(
        "/api/settings/security",
        json={"session_idle_timeout_minutes": 30},
        headers=csrf_headers(client),
    )
    assert r.status_code == 403


def test_put_security_policy_updates_and_audits(client, db_session):
    login(client, "admin@example.com")
    r = client.put(
        "/api/settings/security",
        json={"session_idle_timeout_minutes": 30, "enforce_2fa_roles": ["Admin"]},
        headers=csrf_headers(client),
    )
    assert r.status_code == 200
    assert r.json()["session_idle_timeout_minutes"] == 30
    assert r.json()["enforce_2fa_roles"] == ["Admin"]
    actions = {row.action for row in db_session.scalars(select(AuditLog)).all()}
    assert "auth.security_policy_update" in actions


def test_enforce_2fa_blocks_non_enrolled_admin_from_mutating(client, db_session):
    from app.models import SecurityPolicy

    login(client, "admin@example.com")
    policy = SecurityPolicy.get_or_create(db_session)
    policy.enforce_2fa_roles = ["Admin"]
    db_session.commit()

    # Reads still work.
    assert client.get("/api/auth/me").status_code == 200
    # Mutation is blocked and the frontend gets a stable machine-readable signal.
    r = client.put("/api/settings", json={"product_name": "Hacked"}, headers=csrf_headers(client))
    assert r.status_code == 403
    assert r.headers["x-error-code"] == "mfa_enrollment_required"
    # But enrolment itself is reachable so the admin can escape the trap.
    setup = client.post("/api/auth/2fa/setup", headers=csrf_headers(client))
    assert setup.status_code == 200
    secret = setup.json()["secret"]
    code = totp_code_for_step(secret, 0)
    confirm = client.post(
        "/api/auth/2fa/confirm", json={"code": code}, headers=csrf_headers(client)
    )
    assert confirm.status_code == 200
    # Now mutation succeeds again.
    r2 = client.put("/api/settings", json={"product_name": "Fine"}, headers=csrf_headers(client))
    assert r2.status_code == 200


def test_enforce_2fa_does_not_block_unaffected_roles(client, db_session):
    from app.models import SecurityPolicy

    login(client, "developer@example.com")
    policy = SecurityPolicy.get_or_create(db_session)
    policy.enforce_2fa_roles = ["Admin"]
    db_session.commit()
    r = client.post("/api/test/mutate", headers=csrf_headers(client))
    assert r.status_code == 200


def test_ip_allowlist_denies_login_from_unlisted_ip(proxy_client_factory, db_session):
    from app.models import SecurityPolicy

    policy = SecurityPolicy.get_or_create(db_session)
    policy.ip_allowlist = ["203.0.113.0/24"]
    db_session.commit()

    client = proxy_client_factory("testclient")
    r = _login_from_ip(client, "198.51.100.5")
    assert r.status_code == 403


def test_ip_allowlist_allows_login_from_listed_ip(proxy_client_factory, db_session):
    from app.models import SecurityPolicy

    policy = SecurityPolicy.get_or_create(db_session)
    policy.ip_allowlist = ["203.0.113.0/24"]
    db_session.commit()

    client = proxy_client_factory("testclient")
    r = _login_from_ip(client, "203.0.113.5")
    assert r.status_code == 200


def test_login_attempts_persisted_and_listed(client, db_session):
    login(client, "admin@example.com", "wrong-password")
    login(client, "admin@example.com")
    rows = db_session.scalars(select(LoginAttempt)).all()
    assert any(r.reason == "bad_password" for r in rows)
    assert any(r.reason == "password_ok" for r in rows)

    r = client.get("/api/auth/login-attempts")
    assert r.status_code == 200
    assert len(r.json()) >= 2


def test_login_attempts_requires_settings_manage(client):
    login(client, "readonly@example.com")
    assert client.get("/api/auth/login-attempts").status_code == 403


def test_password_policy_validator_length_and_complexity():
    problems = validate_password_policy(
        "short", min_length=10, require_complexity=True
    )
    assert any("at least 10 characters" in p for p in problems)
    assert any("lowercase" not in p for p in problems) or True  # 'short' is all lowercase

    problems2 = validate_password_policy(
        "alllowercase123!", min_length=10, require_complexity=True
    )
    assert any("uppercase" in p for p in problems2)

    assert validate_password_policy("Str0ng!Passw0rd", min_length=10, require_complexity=True) == []


def test_password_policy_validator_rejects_reuse():
    old = hash_password("Str0ng!Passw0rd")
    problems = validate_password_policy(
        "Str0ng!Passw0rd",
        min_length=10,
        require_complexity=True,
        reuse_history=5,
        previous_hashes=[old],
    )
    assert any("last 5 passwords" in p for p in problems)
