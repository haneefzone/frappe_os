"""First-run installation wizard backend (session 6.4).

Security contract (the only unauthenticated mutating surface in the product):
- GET /api/bootstrap/status   — public, safe; returns {needs_setup: bool}.
- POST /api/bootstrap/preflight — public, rate-limited; runs health checks.
- POST /api/bootstrap/complete  — public, rate-limited; creates the first admin
  and marks setup done.  After completion all three routes return 410 Gone
  (persisted in the DB, not in process memory), so a restart cannot re-open them.

Bootstrap routes are:
  (a) Served ONLY while zero users exist (checked per-request from the DB).
  (b) 410 Gone permanently once setup_complete is True in bootstrap_state.
  (c) Rate-limited (in-memory, same mechanism as the login throttle).
  (d) AuditLog rows written for admin creation and completion.
  (e) Return {needs_setup: false} to un-authed callers once done — no
      enumeration of instance state beyond the boolean.
"""

import logging
import secrets
import time
from datetime import UTC, datetime
from functools import lru_cache
from threading import Lock
from typing import Annotated
from urllib.parse import urlparse

import redis as redis_lib
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import (
    ACCESS_COOKIE,
    CSRF_COOKIE,
    REFRESH_COOKIE,
)
from app.api.routes.auth import _client_ip, _user_agent
from app.config import FDM_SECRET_KEY_PLACEHOLDER, Settings, get_settings
from app.core.permissions import DEFAULT_ROLES
from app.core.security import (
    create_session_token,
    hash_password,
    new_csrf_token,
)
from app.db import get_db
from app.models import Role, User, UserSession
from app.models.audit import AuditLog
from app.models.bootstrap import BootstrapState
from app.models.settings import PlatformSettings
from app.schemas.bootstrap import (
    BootstrapStatusOut,
    CompleteIn,
    CompleteOut,
    PreflightCheck,
    PreflightOut,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/bootstrap", tags=["bootstrap"])

DbSession = Annotated[Session, Depends(get_db)]


# ---------------------------------------------------------------------------
# Simple in-memory rate limiter for the bootstrap surface.
# Keyed by client IP; threshold 10 requests / 60 seconds.
# ---------------------------------------------------------------------------

class _BootstrapRateLimiter:
    def __init__(self, threshold: int = 10, window: int = 60) -> None:
        self._threshold = threshold
        self._window = window
        self._buckets: dict[str, list[float]] = {}
        self._lock = Lock()

    def check(self, ip: str) -> None:
        now = time.monotonic()
        with self._lock:
            times = self._buckets.setdefault(ip, [])
            cutoff = now - self._window
            # Evict expired timestamps.
            while times and times[0] < cutoff:
                times.pop(0)
            if len(times) >= self._threshold:
                raise HTTPException(status_code=429, detail="Too many requests")
            times.append(now)


@lru_cache
def _get_rate_limiter() -> _BootstrapRateLimiter:
    return _BootstrapRateLimiter()


# ---------------------------------------------------------------------------
# Shared guards
# ---------------------------------------------------------------------------

def _require_setup_open(db: Session) -> BootstrapState:
    """Raise 410 if setup is already complete."""
    state = BootstrapState.get_or_create(db)
    if state.setup_complete:
        raise HTTPException(status_code=410, detail="Setup already complete")
    return state


def _require_no_users(db: Session) -> None:
    """Raise 410 if any user already exists (belt-and-suspenders)."""
    count = db.scalar(select(func.count()).select_from(User))
    if count and count > 0:
        raise HTTPException(status_code=410, detail="Setup already complete")


# ---------------------------------------------------------------------------
# Preflight checks
# ---------------------------------------------------------------------------

def _redis_host_display(url: str) -> str:
    """Return only host:port from a Redis URL — never credentials."""
    parsed = urlparse(url)
    host = parsed.hostname or "localhost"
    port = parsed.port or 6379
    return f"{host}:{port}"


def _check_db(db: Session) -> PreflightCheck:
    try:
        db.execute(select(func.now()))
        return PreflightCheck(name="Database", ok=True, detail="PostgreSQL reachable")
    except Exception:  # noqa: BLE001
        logger.exception("Bootstrap preflight: database check failed")
        return PreflightCheck(
            name="Database",
            ok=False,
            detail="Cannot reach database",
            hint="Ensure PostgreSQL is running and DATABASE_URL is correct.",
        )


def _check_redis(settings: Settings) -> PreflightCheck:
    try:
        r = redis_lib.from_url(settings.redis_url, socket_connect_timeout=3)
        r.ping()
        return PreflightCheck(name="Redis", ok=True, detail="Redis reachable")
    except Exception:  # noqa: BLE001
        logger.exception("Bootstrap preflight: Redis check failed")
        return PreflightCheck(
            name="Redis",
            ok=False,
            detail="Cannot reach Redis",
            hint=f"Ensure Redis is running at {_redis_host_display(settings.redis_url)}.",
        )


def _check_worker_heartbeat(settings: Settings) -> PreflightCheck:
    try:
        r = redis_lib.from_url(settings.redis_url, socket_connect_timeout=3)
        # RQ workers register heartbeat keys: "rq:worker:<id>" with TTL ≤ 420s.
        workers = r.keys("rq:worker:*")
        if workers:
            return PreflightCheck(
                name="Worker", ok=True, detail=f"{len(workers)} RQ worker(s) detected"
            )
        return PreflightCheck(
            name="Worker",
            ok=False,
            detail="No RQ workers detected",
            hint="Run `make worker` (or `rq worker high default low`) to start a worker.",
        )
    except Exception:  # noqa: BLE001
        return PreflightCheck(
            name="Worker",
            ok=False,
            detail="Cannot reach Redis to check for workers",
            hint="Fix the Redis connection first.",
        )


def _check_secret_key(settings: Settings) -> PreflightCheck:
    key = settings.fdm_secret_key
    if key == FDM_SECRET_KEY_PLACEHOLDER or len(key) < 32:
        return PreflightCheck(
            name="Secret key",
            ok=False,
            detail="FDM_SECRET_KEY is missing or is the placeholder value",
            hint=(
                "Generate a key with `python -c \"from cryptography.fernet import Fernet;"
                " print(Fernet.generate_key().decode())\"` and set FDM_SECRET_KEY= in your .env."
            ),
        )
    # Fernet keys are 32-byte URL-safe base64, always 44 chars when encoded.
    try:
        from cryptography.fernet import Fernet, InvalidToken  # noqa: F401
        Fernet(key.encode())
        return PreflightCheck(name="Secret key", ok=True, detail="FDM_SECRET_KEY is well-formed")
    except Exception:  # noqa: BLE001
        return PreflightCheck(
            name="Secret key",
            ok=False,
            detail="FDM_SECRET_KEY is set but is not a valid Fernet key",
            hint=(
                "Generate a valid key with `python -c \"from cryptography.fernet import Fernet;"
                " print(Fernet.generate_key().decode())\"`"
            ),
        )


# ---------------------------------------------------------------------------
# Session helper (mirrors auth._start_session without import cycle)
# ---------------------------------------------------------------------------

def _issue_session(db: Session, response: Response, user: User, settings: Settings,
                   ip: str, user_agent: str | None) -> None:
    jti = secrets.token_urlsafe(24)
    now = datetime.now(UTC)
    db.add(
        UserSession(
            user_id=user.id,
            jti=jti,
            ip=ip,
            user_agent=user_agent,
            created_at=now,
            last_seen_at=now,
        )
    )
    db.flush()  # caller commits

    csrf = new_csrf_token()
    common = {"secure": settings.cookie_secure, "samesite": "lax", "path": "/"}
    access = create_session_token(
        user_id=user.id,
        token_type="access",
        csrf=csrf,
        ttl_seconds=settings.access_token_ttl_seconds,
        secret=settings.jwt_secret,
        token_version=user.token_version,
        sid=jti,
    )
    refresh = create_session_token(
        user_id=user.id,
        token_type="refresh",
        csrf=csrf,
        ttl_seconds=settings.refresh_token_ttl_seconds,
        secret=settings.jwt_secret,
        token_version=user.token_version,
        sid=jti,
    )
    response.set_cookie(
        ACCESS_COOKIE, access, max_age=settings.access_token_ttl_seconds,
        httponly=True, **common,
    )
    response.set_cookie(
        REFRESH_COOKIE, refresh, max_age=settings.refresh_token_ttl_seconds,
        httponly=True, **common,
    )
    response.set_cookie(
        CSRF_COOKIE, csrf, max_age=settings.refresh_token_ttl_seconds,
        httponly=False, **common,
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/status", response_model=BootstrapStatusOut)
def bootstrap_status(db: DbSession) -> BootstrapStatusOut:
    """Public: return whether the platform needs first-run setup."""
    state = BootstrapState.get_or_create(db)
    # Double-check: if any users exist we treat setup as complete even if the flag
    # somehow was not set (e.g. manual seeding).
    if state.setup_complete:
        return BootstrapStatusOut(needs_setup=False)
    user_count = db.scalar(select(func.count()).select_from(User))
    needs = (user_count or 0) == 0
    return BootstrapStatusOut(needs_setup=needs)


@router.post("/preflight", response_model=PreflightOut)
def bootstrap_preflight(
    request: Request,
    db: DbSession,
    settings: Annotated[Settings, Depends(get_settings)],
) -> PreflightOut:
    """Public, rate-limited: run health checks for the preflight wizard step."""
    _get_rate_limiter().check(_client_ip(request))
    _require_setup_open(db)

    checks = [
        _check_db(db),
        _check_redis(settings),
        _check_worker_heartbeat(settings),
        _check_secret_key(settings),
    ]
    return PreflightOut(checks=checks, all_ok=all(c.ok for c in checks))


@router.post("/complete", response_model=CompleteOut)
def bootstrap_complete(
    payload: CompleteIn,
    request: Request,
    response: Response,
    db: DbSession,
    settings: Annotated[Settings, Depends(get_settings)],
) -> CompleteOut:
    """Public, rate-limited: create the first admin and mark setup complete.

    Atomicity: all writes happen in a single transaction.  A failed commit
    leaves the DB unchanged so the operator can retry without a half-created
    admin user.
    """
    _get_rate_limiter().check(_client_ip(request))
    _require_setup_open(db)
    _require_no_users(db)

    ip = _client_ip(request)

    # 1. Seed roles (idempotent — in case seed was never run).
    roles: dict[str, Role] = {}
    for name, permissions in DEFAULT_ROLES.items():
        role = db.scalars(select(Role).where(Role.name == name)).first()
        if role is None:
            role = Role(name=name, permissions=permissions)
            db.add(role)
        roles[name] = role
    db.flush()

    # 2. Create the admin user.
    admin_role = roles["Admin"]
    user = User(
        email=payload.admin.email.strip().lower(),
        password_hash=hash_password(payload.admin.password),
        full_name=payload.admin.full_name.strip(),
        is_active=True,
        role_id=admin_role.id,
    )
    db.add(user)
    db.flush()  # populate user.id

    # 3. Write branding / locale settings (reuse the 6.6 Settings singleton).
    platform_settings = PlatformSettings.get_or_create(db)
    platform_settings.product_name = payload.branding.product_name
    platform_settings.default_tz = payload.branding.default_tz
    if payload.branding.accent_hex is not None:
        platform_settings.accent_hex = payload.branding.accent_hex
    db.add(platform_settings)

    # 4. Mark setup complete.
    state = db.get(BootstrapState, 1)
    if state is None:
        state = BootstrapState(id=1)
        db.add(state)
    state.setup_complete = True
    state.completed_at = datetime.now(UTC)
    state.completed_by_user_id = user.id
    state.completed_from_ip = ip
    db.add(state)

    # 5. AuditLog rows (golden rule 2).
    db.add(
        AuditLog(
            user_id=user.id,
            action="bootstrap.admin_created",
            entity_type="user",
            entity_id=str(user.id),
            summary=f"First-run wizard: admin account created for {user.email}",
            params_masked={"email": user.email, "full_name": user.full_name},
            source_ip=ip,
            result="ok",
        )
    )
    db.add(
        AuditLog(
            user_id=user.id,
            action="bootstrap.completed",
            entity_type="bootstrap_state",
            entity_id="1",
            summary="First-run wizard: setup completed",
            params_masked={"product_name": payload.branding.product_name},
            source_ip=ip,
            result="ok",
        )
    )

    db.commit()

    # 6. Auto-login: issue session cookies so the wizard can redirect to the dashboard.
    db.refresh(user)
    _issue_session(db, response, user, settings, ip, _user_agent(request))
    db.commit()

    return CompleteOut(setup_complete=True, user_id=user.id, email=user.email)
