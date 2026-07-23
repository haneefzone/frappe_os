"""Auth + RBAC dependencies. Every protected router uses `require(<permission>)`."""

import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.permissions import role_allows
from app.core.security import decode_session_token
from app.db import get_db
from app.models import SecurityPolicy, User, UserSession, UserTOTP

ACCESS_COOKIE = "fdm_access_token"
REFRESH_COOKIE = "fdm_refresh_token"
CSRF_COOKIE = "fdm_csrf_token"
CSRF_HEADER = "X-CSRF-Token"
# Separate cookie/type for the password-only intermediate step (session 6.5) —
# never read by get_current_user, so it cannot satisfy any RBAC dependency.
MFA_PENDING_COOKIE = "fdm_mfa_pending_token"

_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

# Mutating routes an Admin locked out by policy-enforced 2FA must still be able
# to reach: enrolling, and getting out of the account.
_MFA_ENFORCEMENT_EXEMPT_PATHS = {
    "/api/auth/2fa/setup",
    "/api/auth/2fa/confirm",
    "/api/auth/logout",
    "/api/auth/logout-all",
}


def _unauthorized(message: str = "Not authenticated.") -> HTTPException:
    return HTTPException(status_code=401, detail=message)


def _aware(dt: datetime | None) -> datetime | None:
    """Treat a tz-naive value (SQLite test rows) as UTC so age math is correct."""
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _load_session(db: Session, sid: str | None) -> UserSession | None:
    """Resolves + validates the `UserSession` a token's `sid` claim names.

    Enforces revocation and the policy's idle/absolute timeouts on *every*
    request (session 6.5) — a session dies the moment it is revoked or expires,
    not just at its next login/refresh. Touches `last_seen_at` at most once a
    minute so this does not turn into a write-per-request storm.
    """
    if not sid:
        return None
    row = db.scalars(select(UserSession).where(UserSession.jti == sid)).first()
    if row is None or row.revoked_at is not None:
        return None

    now = datetime.now(UTC)
    policy = SecurityPolicy.get_or_create(db)
    if now - _aware(row.created_at) > timedelta(minutes=policy.session_absolute_timeout_minutes):
        row.revoked_at = now
        db.commit()
        return None
    if now - _aware(row.last_seen_at) > timedelta(minutes=policy.session_idle_timeout_minutes):
        row.revoked_at = now
        db.commit()
        return None

    if now - _aware(row.last_seen_at) > timedelta(seconds=60):
        row.last_seen_at = now
        db.commit()
    return row


def _enforce_2fa_policy(request: Request, user: User, db: Session) -> None:
    """Blocks a mutating request from a policy-enforced role until 2FA is
    confirmed (session 6.5 acceptance: "Enforce-2FA-for-Admin blocks a
    non-enrolled admin from mutating and routes them to enrolment"). Read
    (safe-method) requests and the enrolment/logout paths are always allowed
    so the frontend can render the enrolment screen and the user can escape."""
    if request.method in _SAFE_METHODS or request.url.path in _MFA_ENFORCEMENT_EXEMPT_PATHS:
        return
    policy = SecurityPolicy.get_or_create(db)
    if user.role.name not in (policy.enforce_2fa_roles or []):
        return
    totp = db.scalars(select(UserTOTP).where(UserTOTP.user_id == user.id)).first()
    if totp is not None and totp.confirmed_at is not None:
        return
    # `detail` stays a plain string (the shared error handler does str(exc.detail),
    # which would otherwise serialise a dict as its Python repr, not JSON) — the
    # machine-readable signal for the frontend travels in a header instead, the
    # same pattern already used for Retry-After on 429s.
    raise HTTPException(
        status_code=403,
        detail="Two-factor authentication is required by policy for your role. "
        "Enrol in Settings > Security to continue.",
        headers={"X-Error-Code": "mfa_enrollment_required"},
    )


def get_current_user(request: Request, db: Annotated[Session, Depends(get_db)]) -> User:
    """Resolves the session user from the access-token cookie.

    For mutating methods it also enforces the CSRF double-submit check: the
    X-CSRF-Token header must match the csrf claim baked into the (httpOnly)
    access token at login.
    """
    token = request.cookies.get(ACCESS_COOKIE)
    if not token:
        raise _unauthorized()

    claims = decode_session_token(
        token, expected_type="access", secret=get_settings().jwt_secret
    )
    if claims is None:
        raise _unauthorized("Session expired or invalid.")

    if request.method not in _SAFE_METHODS:
        header = request.headers.get(CSRF_HEADER)
        if not header or not secrets.compare_digest(header, claims.get("csrf") or ""):
            raise HTTPException(status_code=403, detail="CSRF token missing or invalid.")

    user = db.get(User, int(claims["sub"]))
    if user is None or not user.is_active:
        raise _unauthorized("Account is disabled.")
    # SEC-M2: reject tokens issued before the user's session was revoked
    # (password/role change, deactivate, "log out everywhere"). Same per-request
    # DB read that already re-checks is_active — no extra query.
    if claims.get("tv") != user.token_version:
        raise _unauthorized("Session expired or invalid.")

    # Session 6.5: per-session revocation/timeout, checked on every request —
    # not just at the next login. A token minted before this claim existed (or
    # whose session row is gone/revoked/expired) is rejected here.
    if _load_session(db, claims.get("sid")) is None:
        raise _unauthorized("Session expired or invalid.")

    _enforce_2fa_policy(request, user, db)
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require(permission: str):
    """RBAC dependency factory: `Depends(require("site:operate"))`.

    Grants when the user's role holds the action-class (or the "*" wildcard);
    otherwise 403.
    """

    def dependency(user: CurrentUser) -> User:
        if not role_allows(list(user.role.permissions or []), permission):
            raise HTTPException(
                status_code=403,
                detail=f"Role '{user.role.name}' lacks the '{permission}' permission.",
            )
        return user

    return dependency


def require_admin(user: CurrentUser) -> User:
    """Admin-only gate. Some surfaces (the platform self-backup + master-key
    escrow, session 6.3) are restricted to the Admin role specifically, not just
    any role that happens to hold a broad permission — losing the master key is
    an organisation-ending event, so the routes that back it up and the escrow
    acknowledgement are Admin-only. Checks the role name, so a custom role cannot
    be handed this by adding a permission string."""
    if (user.role.name if user.role else None) != "Admin":
        raise HTTPException(
            status_code=403,
            detail="This action is restricted to the Admin role.",
        )
    return user
