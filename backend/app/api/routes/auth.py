import secrets
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.api.deps import (
    ACCESS_COOKIE,
    CSRF_COOKIE,
    CSRF_HEADER,
    REFRESH_COOKIE,
    CurrentUser,
)
from app.audit import record_audit
from app.config import Settings, get_settings
from app.core.ratelimit import LoginThrottle, get_login_throttle
from app.core.security import (
    create_session_token,
    decode_session_token,
    new_csrf_token,
    verify_password,
)
from app.db import get_db
from app.models import User
from app.schemas.auth import LoginRequest, UserOut

router = APIRouter(prefix="/api/auth", tags=["auth"])

DbSession = Annotated[Session, Depends(get_db)]
Throttle = Annotated[LoginThrottle, Depends(get_login_throttle)]


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _set_session_cookies(response: Response, user_id: int, settings: Settings) -> None:
    """Issues access + refresh JWTs (httpOnly) and the CSRF cookie (JS-readable,
    for the double-submit header). All rotate together."""
    csrf = new_csrf_token()
    common = {"secure": settings.cookie_secure, "samesite": "lax", "path": "/"}
    access = create_session_token(
        user_id=user_id,
        token_type="access",
        csrf=csrf,
        ttl_seconds=settings.access_token_ttl_seconds,
        secret=settings.jwt_secret,
    )
    refresh = create_session_token(
        user_id=user_id,
        token_type="refresh",
        csrf=csrf,
        ttl_seconds=settings.refresh_token_ttl_seconds,
        secret=settings.jwt_secret,
    )
    response.set_cookie(
        ACCESS_COOKIE, access, max_age=settings.access_token_ttl_seconds, httponly=True, **common
    )
    response.set_cookie(
        REFRESH_COOKIE, refresh, max_age=settings.refresh_token_ttl_seconds, httponly=True, **common
    )
    response.set_cookie(
        CSRF_COOKIE, csrf, max_age=settings.refresh_token_ttl_seconds, httponly=False, **common
    )


def _clear_session_cookies(response: Response) -> None:
    for name in (ACCESS_COOKIE, REFRESH_COOKIE, CSRF_COOKIE):
        response.delete_cookie(name, path="/")


@router.post("/login")
def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    db: DbSession,
    throttle: Throttle,
) -> UserOut:
    email = body.email.lower()
    ip = _client_ip(request)

    retry_after = throttle.retry_after(email, ip)
    if retry_after:
        raise HTTPException(
            status_code=429,
            detail=f"Too many failed attempts. Try again in {retry_after} seconds.",
            headers={"Retry-After": str(retry_after)},
        )

    user = db.scalars(
        select(User).options(joinedload(User.role)).where(User.email == email)
    ).first()
    # Same failure path whether the email exists or not (no user enumeration).
    if user is None or not verify_password(user.password_hash, body.password):
        throttle.register_failure(email, ip)
        record_audit(
            db,
            action="auth.login",
            summary=f"Failed login for {email}",
            user_id=user.id if user else None,
            entity_type="session",
            entity_id=email,
            result="denied",
            source_ip=ip,
        )
        raise HTTPException(status_code=401, detail="Invalid email or password.")
    if not user.is_active:
        raise HTTPException(status_code=401, detail="Account is disabled.")

    throttle.register_success(email, ip)
    user.last_login = datetime.now(UTC)
    db.commit()

    record_audit(
        db,
        action="auth.login",
        summary=f"Signed in as {email}",
        user_id=user.id,
        entity_type="session",
        entity_id=email,
        result="ok",
        source_ip=ip,
    )
    settings = get_settings()
    _set_session_cookies(response, user.id, settings)
    return UserOut.from_user(user)


@router.post("/refresh")
def refresh(request: Request, response: Response, db: DbSession) -> UserOut:
    """Rotates the whole cookie set from a valid refresh token.

    CSRF double-submit applies here too: the header must match the csrf claim
    inside the (httpOnly) refresh token.
    """
    settings = get_settings()
    token = request.cookies.get(REFRESH_COOKIE)
    claims = (
        decode_session_token(token, expected_type="refresh", secret=settings.jwt_secret)
        if token
        else None
    )
    if claims is None:
        raise HTTPException(status_code=401, detail="Session expired or invalid.")
    header = request.headers.get(CSRF_HEADER)
    if not header or not secrets.compare_digest(header, claims.get("csrf") or ""):
        raise HTTPException(status_code=403, detail="CSRF token missing or invalid.")

    user = db.get(User, int(claims["sub"]))
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="Account is disabled.")

    _set_session_cookies(response, user.id, settings)
    return UserOut.from_user(user)


@router.get("/me")
def me(user: CurrentUser) -> UserOut:
    return UserOut.from_user(user)


@router.post("/logout", status_code=204)
def logout(response: Response) -> None:
    """Clears the session cookies. Deliberately unauthenticated so a client
    with an expired/broken session can always reach a clean state."""
    _clear_session_cookies(response)
