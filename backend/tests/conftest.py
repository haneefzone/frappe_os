import os

import pytest
from cryptography.fernet import Fernet
from fastapi import APIRouter, Depends
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import get_settings

# TestClient talks plain http://testserver, and httpx's cookie jar drops
# Secure cookies over http — so tests run with the dev override. Must be set
# before create_app() reads settings. DEBUG=true opts out of the fail-closed
# placeholder-secret check (SEC-H1, DOO-66); tests covering that check pass
# debug=False explicitly.
os.environ["COOKIE_SECURE"] = "false"
os.environ["DEBUG"] = "true"
# A real Fernet key so SecretsService (server-registry secrets) round-trips in
# tests instead of failing on the placeholder key.
os.environ.setdefault("FDM_SECRET_KEY", Fernet.generate_key().decode())
get_settings.cache_clear()

from app.api.deps import require  # noqa: E402
from app.core.ratelimit import LoginThrottle, get_login_throttle  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.db import Base, get_db  # noqa: E402
from app.main import create_app  # noqa: E402
from app.models import User  # noqa: E402
from app.seed import seed_roles  # noqa: E402

PASSWORD = "correct-horse-battery"


@pytest.fixture
def fake_clock():
    """Mutable monotonic clock so lockout expiry is testable without sleeping."""
    state = {"now": 1000.0}

    def clock() -> float:
        return state["now"]

    clock.advance = lambda seconds: state.__setitem__("now", state["now"] + seconds)
    return clock


def make_throttle(clock):
    settings = get_settings()
    return LoginThrottle(
        threshold=settings.login_lockout_threshold,
        lockout_seconds=settings.login_lockout_seconds,
        email_failure_limit=settings.login_email_failure_limit,
        email_failure_window_seconds=settings.login_email_failure_window_seconds,
        clock=clock,
    )


@pytest.fixture
def throttle(fake_clock):
    return make_throttle(fake_clock)


@pytest.fixture
def db_session():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with session_factory() as session:
        yield session
    engine.dispose()


@pytest.fixture
def seeded_users(db_session):
    roles = seed_roles(db_session)
    users = {}
    for key, role in (("admin", "Admin"), ("developer", "Developer"), ("readonly", "Read-only")):
        users[key] = User(
            email=f"{key}@example.com",
            password_hash=hash_password(PASSWORD),
            full_name=key.title(),
            is_active=True,
            role_id=roles[role].id,
        )
        db_session.add(users[key])
    users["inactive"] = User(
        email="inactive@example.com",
        password_hash=hash_password(PASSWORD),
        full_name="Inactive",
        is_active=False,
        role_id=roles["Read-only"].id,
    )
    db_session.add(users["inactive"])
    db_session.commit()
    return users


@pytest.fixture
def client(db_session, seeded_users, throttle):
    app = create_app()

    # Mutating probe route so RBAC denial is testable before real routers exist.
    probe = APIRouter()

    @probe.post("/api/test/mutate")
    def mutate(user: User = Depends(require("site:operate"))) -> dict:  # noqa: B008
        return {"ok": True, "as": user.email}

    app.include_router(probe)

    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[get_login_throttle] = lambda: throttle
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def proxy_client_factory(db_session, seeded_users, fake_clock, monkeypatch):
    """Builds a TestClient with TRUSTED_PROXY_IPS set (SEC-M1). The TestClient
    socket peer is the literal host 'testclient' — pass that to trust the
    peer, anything else to exercise the untrusted-peer path."""
    clients = []

    def factory(trusted_proxy_ips: str) -> TestClient:
        monkeypatch.setenv("TRUSTED_PROXY_IPS", trusted_proxy_ips)
        get_settings.cache_clear()
        app = create_app()
        shared_throttle = make_throttle(fake_clock)
        app.dependency_overrides[get_db] = lambda: db_session
        app.dependency_overrides[get_login_throttle] = lambda: shared_throttle
        test_client = TestClient(app)
        test_client.__enter__()
        clients.append(test_client)
        return test_client

    yield factory
    for test_client in clients:
        test_client.__exit__(None, None, None)
    get_settings.cache_clear()


def login(client: TestClient, email: str, password: str = PASSWORD):
    return client.post("/api/auth/login", json={"email": email, "password": password})


def csrf_headers(client: TestClient) -> dict:
    return {"X-CSRF-Token": client.cookies.get("fdm_csrf_token", "")}
