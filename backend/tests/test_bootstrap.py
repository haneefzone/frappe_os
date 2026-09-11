"""Tests for the first-run installation wizard bootstrap API (session 6.4).

Key acceptance criteria verified:
- /api/bootstrap/status returns needs_setup=true on a fresh DB.
- /api/bootstrap/complete creates the admin, auto-logs in, returns 200.
- After completion, /api/bootstrap/complete and /api/bootstrap/preflight
  return 410 Gone — persisted across restarts.
- Bootstrap routes return 410 when users already exist (even if DB flag not set).
"""

import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("COOKIE_SECURE", "false")
os.environ.setdefault("DEBUG", "true")
os.environ.setdefault("MONITORING_ENABLED", "false")
os.environ.setdefault("UPTIME_ENABLED", "false")

from cryptography.fernet import Fernet  # noqa: E402

os.environ.setdefault("FDM_SECRET_KEY", Fernet.generate_key().decode())

from app.config import get_settings  # noqa: E402

get_settings.cache_clear()

from app.api.routes.bootstrap import _get_rate_limiter  # noqa: E402
from app.db import Base, get_db  # noqa: E402
from app.main import create_app  # noqa: E402
from app.models import Role, User  # noqa: E402
from app.models.audit import AuditLog  # noqa: E402
from app.models.bootstrap import BootstrapState  # noqa: E402


@pytest.fixture
def empty_db():
    """SQLite in-memory DB with no users (fresh install)."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as session:
        yield session
    engine.dispose()


@pytest.fixture
def fresh_client(empty_db):
    """TestClient on a fresh (no users) DB."""
    app = create_app()
    app.dependency_overrides[get_db] = lambda: empty_db
    # Reset the rate limiter between tests.
    _get_rate_limiter.cache_clear()
    with TestClient(app) as c:
        yield c


COMPLETE_PAYLOAD = {
    "admin": {
        "email": "admin@example.com",
        "full_name": "Admin User",
        "password": "S3cure!pass123",
    },
    "branding": {
        "product_name": "My FDM",
        "default_tz": "Asia/Dubai",
    },
}


# ---------------------------------------------------------------------------
# status endpoint
# ---------------------------------------------------------------------------

def test_status_needs_setup_on_fresh_db(fresh_client):
    r = fresh_client.get("/api/bootstrap/status")
    assert r.status_code == 200
    assert r.json()["needs_setup"] is True


def test_status_does_not_need_setup_when_users_exist(empty_db, fresh_client):
    from app.core.security import hash_password
    from app.seed import seed_roles

    seed_roles(empty_db)
    admin_role = empty_db.scalars(
        select(Role).where(Role.name == "Admin")
    ).one()
    empty_db.add(
        User(
            email="existing@example.com",
            password_hash=hash_password("password"),
            full_name="Existing",
            is_active=True,
            role_id=admin_role.id,
        )
    )
    empty_db.commit()

    r = fresh_client.get("/api/bootstrap/status")
    assert r.status_code == 200
    assert r.json()["needs_setup"] is False


# ---------------------------------------------------------------------------
# complete endpoint
# ---------------------------------------------------------------------------

def test_complete_creates_admin_and_sets_flag(fresh_client, empty_db):
    r = fresh_client.post("/api/bootstrap/complete", json=COMPLETE_PAYLOAD)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["setup_complete"] is True
    assert body["email"] == "admin@example.com"

    # DB flag is set.
    state = empty_db.get(BootstrapState, 1)
    assert state is not None
    assert state.setup_complete is True

    # User exists with the right role.
    user = empty_db.scalars(
        select(User).where(User.email == "admin@example.com")
    ).first()
    assert user is not None
    role = empty_db.get(Role, user.role_id)
    assert role is not None
    assert role.name == "Admin"


def test_complete_issues_session_cookies(fresh_client):
    r = fresh_client.post("/api/bootstrap/complete", json=COMPLETE_PAYLOAD)
    assert r.status_code == 200
    assert "fdm_access_token" in r.cookies or "fdm_refresh_token" in r.cookies


def test_complete_writes_audit_log(fresh_client, empty_db):
    r = fresh_client.post("/api/bootstrap/complete", json=COMPLETE_PAYLOAD)
    assert r.status_code == 200

    logs = empty_db.scalars(
        select(AuditLog).where(AuditLog.action.in_(
            ["bootstrap.admin_created", "bootstrap.completed"]
        ))
    ).all()
    assert len(logs) == 2


def test_complete_applies_branding(fresh_client, empty_db):
    from app.models.settings import PlatformSettings

    payload = {**COMPLETE_PAYLOAD, "branding": {"product_name": "Acme FDM", "default_tz": "UTC"}}
    r = fresh_client.post("/api/bootstrap/complete", json=payload)
    assert r.status_code == 200

    settings = PlatformSettings.get_or_create(empty_db)
    assert settings.product_name == "Acme FDM"
    assert settings.default_tz == "UTC"


# ---------------------------------------------------------------------------
# password complexity (A.5.17 — DOO-1074 F-1)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "weak",
    [
        "aaaaaaaa",            # too short, no complexity
        "aaaaaaaaaaaa",        # 12 chars but no upper/digit/special
        "Abcdefghijkl",        # no digit, no special
        "Abcdefghij12",        # no special character
        "ABCDEFGH123!",        # no lowercase
        "S3cure!pass",         # 11 chars — below 12-char minimum
    ],
)
def test_complete_rejects_weak_password(fresh_client, weak):
    payload = {**COMPLETE_PAYLOAD, "admin": {**COMPLETE_PAYLOAD["admin"], "password": weak}}
    r = fresh_client.post("/api/bootstrap/complete", json=payload)
    assert r.status_code == 422, r.text


def test_complete_accepts_strong_password(fresh_client):
    # The existing COMPLETE_PAYLOAD password must still pass the policy.
    r = fresh_client.post("/api/bootstrap/complete", json=COMPLETE_PAYLOAD)
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# 410 after completion (the security core of this session)
# ---------------------------------------------------------------------------

def test_complete_returns_410_after_first_run(fresh_client):
    r1 = fresh_client.post("/api/bootstrap/complete", json=COMPLETE_PAYLOAD)
    assert r1.status_code == 200

    r2 = fresh_client.post("/api/bootstrap/complete", json=COMPLETE_PAYLOAD)
    assert r2.status_code == 410


def test_complete_returns_410_when_users_exist(empty_db):
    from app.core.security import hash_password
    from app.seed import seed_roles

    seed_roles(empty_db)
    admin_role = empty_db.scalars(
        select(Role).where(Role.name == "Admin")
    ).one()
    empty_db.add(
        User(
            email="existing@example.com",
            password_hash=hash_password("password"),
            full_name="Existing",
            is_active=True,
            role_id=admin_role.id,
        )
    )
    empty_db.commit()

    app = create_app()
    app.dependency_overrides[get_db] = lambda: empty_db
    _get_rate_limiter.cache_clear()
    with TestClient(app) as c:
        r = c.post("/api/bootstrap/complete", json=COMPLETE_PAYLOAD)
        assert r.status_code == 410


def test_status_returns_needs_setup_false_after_completion(fresh_client):
    fresh_client.post("/api/bootstrap/complete", json=COMPLETE_PAYLOAD)
    r = fresh_client.get("/api/bootstrap/status")
    assert r.status_code == 200
    assert r.json()["needs_setup"] is False


def test_410_persists_flag_check(empty_db):
    """Simulate a restart: a new client pointing at the same DB with setup_complete=True
    must still get 410 (flag is in DB, not memory)."""
    # Mark setup complete directly in the DB.
    state = BootstrapState(id=1, setup_complete=True)
    empty_db.add(state)
    empty_db.commit()

    app = create_app()
    app.dependency_overrides[get_db] = lambda: empty_db
    _get_rate_limiter.cache_clear()
    with TestClient(app) as c:
        r = c.post("/api/bootstrap/complete", json=COMPLETE_PAYLOAD)
        assert r.status_code == 410


# ---------------------------------------------------------------------------
# preflight endpoint
# ---------------------------------------------------------------------------

def test_preflight_returns_checks_structure(fresh_client):
    r = fresh_client.post("/api/bootstrap/preflight")
    assert r.status_code == 200
    body = r.json()
    assert "checks" in body
    assert "all_ok" in body
    names = [c["name"] for c in body["checks"]]
    assert "Database" in names
    assert "Redis" in names
    assert "Worker" in names
    assert "Secret key" in names


def test_preflight_db_check_ok_in_tests(fresh_client):
    r = fresh_client.post("/api/bootstrap/preflight")
    assert r.status_code == 200
    db_check = next(c for c in r.json()["checks"] if c["name"] == "Database")
    assert db_check["ok"] is True


def test_preflight_returns_410_after_setup(fresh_client):
    fresh_client.post("/api/bootstrap/complete", json=COMPLETE_PAYLOAD)
    r = fresh_client.post("/api/bootstrap/preflight")
    assert r.status_code == 410
