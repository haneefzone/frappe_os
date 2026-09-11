"""Auth API: login, refresh, logout, TOTP 2FA + recovery codes, and
per-session enumeration/revocation (session 6.5).

Login is two-step once a user has confirmed TOTP: `POST /login` validates the
password and, if 2FA is active, issues a short-lived `mfa_pending` cookie
instead of a session — no RBAC dependency ever accepts that token type, so a
correct password alone cannot reach a mutating route. `POST /2fa/verify`
exchanges a TOTP code (or a single-use recovery code) for the real session.

Every login/refresh chain shares one `UserSession` row (its `jti` becomes the
`sid` claim on both the access and refresh JWT); revoking that row — or its
policy idle/absolute timeout expiring — is checked by `get_current_user` on
*every* request, so "revoke this session" stops it on the very next request,
not just at its next login (see `app.api.deps._load_session`).
"""

import ipaddress
import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session, joinedload

from app.api.deps import (
    ACCESS_COOKIE,
    CSRF_COOKIE,
    CSRF_HEADER,
    MFA_PENDING_COOKIE,
    REFRESH_COOKIE,
    CurrentUser,
    require,
)
from app.audit import Audit, record_audit
from app.config import Settings, get_settings
from app.core.mfa import (
    decrypt_totp_secret,
    encrypt_totp_secret,
    generate_recovery_codes,
    generate_totp_secret,
    hash_recovery_code,
    looks_like_recovery_code,
    provisioning_uri,
    verify_totp_code,
)
from app.core.permissions import SETTINGS_MANAGE
from app.core.ratelimit import LoginThrottle, get_login_throttle, get_mfa_throttle
from app.core.security import (
    bump_token_version,
    create_session_token,
    decode_session_token,
    new_csrf_token,
    verify_password,
)
from app.db import get_db
from app.models import LoginAttempt as LoginAttemptModel
from app.models import RecoveryCode, SecurityPolicy, User, UserSession, UserTOTP
from app.schemas.auth import (
    LoginAttemptOut,
    LoginOut,
    LoginRequest,
    MFACodeIn,
    SessionOut,
    TOTPConfirmOut,
    TOTPSetupOut,
    UserOut,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])

DbSession = Annotated[Session, Depends(get_db)]
Throttle = Annotated[LoginThrottle, Depends(get_login_throttle)]
MfaThrottle = Annotated[LoginThrottle, Depends(get_mfa_throttle)]


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _user_agent(request: Request) -> str | None:
    return request.headers.get("user-agent")


def _aware(dt: datetime | None) -> datetime | None:
    """Treat a tz-naive value (SQLite test rows) as UTC so age math is correct."""
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _ip_allowed(ip: str, allowlist: list[str]) -> bool:
    """Empty allowlist = unrestricted (default). Entries may be a bare IP or a
    CIDR block; a malformed request IP or entry never grants access."""
    if not allowlist:
        return True
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    for entry in allowlist:
        entry = (entry or "").strip()
        if not entry:
            continue
        try:
            if "/" in entry:
                if addr in ipaddress.ip_network(entry, strict=False):
                    return True
            elif addr == ipaddress.ip_address(entry):
                return True
        except ValueError:
            continue
    return False


def _has_confirmed_totp(db: Session, user_id: int) -> bool:
    row = db.scalars(select(UserTOTP).where(UserTOTP.user_id == user_id)).first()
    return row is not None and row.confirmed_at is not None


def _log_attempt(
    db: Session,
    *,
    email: str,
    user_id: int | None,
    ip: str,
    request: Request,
    success: bool,
    reason: str,
) -> None:
    """Persists one row to `login_attempts` (session 1.1's LoginThrottle is
    in-memory-only and never wrote these). Best-effort: never blocks login."""
    try:
        db.add(
            LoginAttemptModel(
                email=email,
                user_id=user_id,
                ip=ip,
                user_agent=_user_agent(request),
                success=success,
                reason=reason,
            )
        )
        db.commit()
    except Exception:  # noqa: BLE001 — logging an attempt must not break login.
        db.rollback()


def _set_session_cookies(
    response: Response, user: User, settings: Settings, *, sid: str
) -> None:
    """Issues access + refresh JWTs (httpOnly) and the CSRF cookie (JS-readable,
    for the double-submit header). All rotate together and share `sid` — the
    `UserSession.jti` this login/refresh chain is tracked under."""
    csrf = new_csrf_token()
    common = {"secure": settings.cookie_secure, "samesite": "lax", "path": "/"}
    access = create_session_token(
        user_id=user.id,
        token_type="access",
        csrf=csrf,
        ttl_seconds=settings.access_token_ttl_seconds,
        secret=settings.jwt_secret,
        token_version=user.token_version,
        sid=sid,
    )
    refresh = create_session_token(
        user_id=user.id,
        token_type="refresh",
        csrf=csrf,
        ttl_seconds=settings.refresh_token_ttl_seconds,
        secret=settings.jwt_secret,
        token_version=user.token_version,
        sid=sid,
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
    for name in (ACCESS_COOKIE, REFRESH_COOKIE, CSRF_COOKIE, MFA_PENDING_COOKIE):
        response.delete_cookie(name, path="/")


def _set_mfa_pending_cookie(response: Response, user: User, settings: Settings) -> None:
    token = create_session_token(
        user_id=user.id,
        token_type="mfa_pending",
        csrf="",
        ttl_seconds=settings.mfa_pending_ttl_seconds,
        secret=settings.jwt_secret,
        token_version=user.token_version,
    )
    response.set_cookie(
        MFA_PENDING_COOKIE,
        token,
        max_age=settings.mfa_pending_ttl_seconds,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )


def _start_session(
    db: Session, response: Response, user: User, settings: Settings, request: Request
) -> None:
    """Creates the `UserSession` row for a brand-new login chain (never on
    /refresh — that reuses the existing row) and issues its cookies."""
    jti = secrets.token_urlsafe(24)
    now = datetime.now(UTC)
    db.add(
        UserSession(
            user_id=user.id,
            jti=jti,
            ip=_client_ip(request),
            user_agent=_user_agent(request),
            created_at=now,
            last_seen_at=now,
        )
    )
    db.commit()
    _set_session_cookies(response, user, settings, sid=jti)


def _current_sid(request: Request, settings: Settings) -> str | None:
    token = request.cookies.get(ACCESS_COOKIE)
    if not token:
        return None
    claims = decode_session_token(token, expected_type="access", secret=settings.jwt_secret)
    return claims.get("sid") if claims else None


@router.post("/login")
def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    db: DbSession,
    throttle: Throttle,
) -> LoginOut:
    email = body.email.lower()
    ip = _client_ip(request)
    settings = get_settings()

    policy = SecurityPolicy.get_or_create(db)
    if not _ip_allowed(ip, policy.ip_allowlist):
        _log_attempt(
            db, email=email, user_id=None, ip=ip, request=request, success=False, reason="ip_denied"
        )
        record_audit(
            db,
            action="auth.login",
            summary=f"Login denied for {email}: source IP not allowlisted",
            entity_type="session",
            entity_id=email,
            result="denied",
            source_ip=ip,
        )
        raise HTTPException(
            status_code=403, detail="Your network is not permitted to access this platform."
        )

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
        _log_attempt(
            db,
            email=email,
            user_id=user.id if user else None,
            ip=ip,
            request=request,
            success=False,
            reason="bad_password",
        )
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
        _log_attempt(
            db, email=email, user_id=user.id, ip=ip, request=request,
            success=False, reason="account_disabled",
        )
        raise HTTPException(status_code=401, detail="Account is disabled.")

    throttle.register_success(email, ip)
    user.last_login = datetime.now(UTC)
    db.commit()

    if _has_confirmed_totp(db, user.id):
        _log_attempt(
            db, email=email, user_id=user.id, ip=ip, request=request,
            success=True, reason="mfa_required",
        )
        record_audit(
            db,
            action="auth.login",
            summary=f"Password OK, awaiting 2FA code for {email}",
            user_id=user.id,
            entity_type="session",
            entity_id=email,
            result="ok",
            source_ip=ip,
        )
        _set_mfa_pending_cookie(response, user, settings)
        return LoginOut(mfa_required=True)

    _log_attempt(
        db, email=email, user_id=user.id, ip=ip, request=request, success=True, reason="password_ok"
    )
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
    _start_session(db, response, user, settings, request)
    return LoginOut(mfa_required=False, user=UserOut.from_user(user))


@router.post("/2fa/verify")
def totp_verify(
    body: MFACodeIn,
    request: Request,
    response: Response,
    db: DbSession,
    mfa_throttle: MfaThrottle,
) -> UserOut:
    """Exchanges the `mfa_pending` cookie + a TOTP code (or a recovery code)
    for a real session. `mfa_pending` is a distinct cookie/token type that no
    RBAC dependency accepts, so a bare password never reaches a mutating
    route while 2FA is outstanding."""
    settings = get_settings()
    token = request.cookies.get(MFA_PENDING_COOKIE)
    claims = (
        decode_session_token(token, expected_type="mfa_pending", secret=settings.jwt_secret)
        if token
        else None
    )
    if claims is None:
        raise HTTPException(status_code=401, detail="MFA session expired. Please log in again.")

    user = db.scalars(
        select(User).options(joinedload(User.role)).where(User.id == int(claims["sub"]))
    ).first()
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="Account is disabled.")
    if claims.get("tv") != user.token_version:
        raise HTTPException(status_code=401, detail="MFA session expired. Please log in again.")

    ip = _client_ip(request)
    retry_after = mfa_throttle.retry_after(str(user.id), ip)
    if retry_after:
        raise HTTPException(
            status_code=429,
            detail=f"Too many failed codes. Try again in {retry_after} seconds.",
            headers={"Retry-After": str(retry_after)},
        )

    code = body.code
    if looks_like_recovery_code(code):
        target_hash = hash_recovery_code(code)
        candidates = db.scalars(
            select(RecoveryCode).where(
                RecoveryCode.user_id == user.id, RecoveryCode.used_at.is_(None)
            )
        ).all()
        matched = next(
            (row for row in candidates if secrets.compare_digest(row.code_hash, target_hash)),
            None,
        )
        if matched is None:
            mfa_throttle.register_failure(str(user.id), ip)
            _log_attempt(
                db, email=user.email, user_id=user.id, ip=ip, request=request,
                success=False, reason="mfa_bad_code",
            )
            raise HTTPException(status_code=401, detail="Invalid or already-used recovery code.")
        matched.used_at = datetime.now(UTC)
        db.commit()
        mfa_throttle.register_success(str(user.id), ip)
        _log_attempt(
            db, email=user.email, user_id=user.id, ip=ip, request=request,
            success=True, reason="recovery_code_used",
        )
        record_audit(
            db,
            action="auth.mfa_recovery_used",
            summary=f"{user.email} signed in with a recovery code",
            user_id=user.id,
            entity_type="session",
            entity_id=user.email,
            result="ok",
            source_ip=ip,
        )
    else:
        totp_row = db.scalars(select(UserTOTP).where(UserTOTP.user_id == user.id)).first()
        if totp_row is None or totp_row.confirmed_at is None:
            raise HTTPException(status_code=400, detail="2FA is not enrolled for this account.")
        secret = decrypt_totp_secret(totp_row.secret_encrypted)
        step = verify_totp_code(secret, code, last_used_step=totp_row.last_used_step)
        if step is None:
            mfa_throttle.register_failure(str(user.id), ip)
            _log_attempt(
                db, email=user.email, user_id=user.id, ip=ip, request=request,
                success=False, reason="mfa_bad_code",
            )
            raise HTTPException(status_code=401, detail="Invalid or expired code.")
        totp_row.last_used_step = step
        db.commit()
        mfa_throttle.register_success(str(user.id), ip)
        _log_attempt(
            db, email=user.email, user_id=user.id, ip=ip, request=request,
            success=True, reason="mfa_ok",
        )
        record_audit(
            db,
            action="auth.mfa_verify",
            summary=f"Signed in as {user.email} (2FA)",
            user_id=user.id,
            entity_type="session",
            entity_id=user.email,
            result="ok",
            source_ip=ip,
        )

    response.delete_cookie(MFA_PENDING_COOKIE, path="/")
    _start_session(db, response, user, settings, request)
    return UserOut.from_user(user, mfa_enabled=True)


@router.post("/refresh")
def refresh(request: Request, response: Response, db: DbSession) -> UserOut:
    """Rotates the whole cookie set from a valid refresh token, reusing the
    same `UserSession` row (its `sid`) across the whole login chain.

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
    # SEC-M2: a refresh token issued before the session was revoked is dead —
    # this stops an exfiltrated refresh cookie from surviving logout-everywhere.
    if claims.get("tv") != user.token_version:
        raise HTTPException(status_code=401, detail="Session expired or invalid.")

    sid = claims.get("sid")
    session_row = (
        db.scalars(select(UserSession).where(UserSession.jti == sid)).first() if sid else None
    )
    if session_row is None or session_row.revoked_at is not None:
        raise HTTPException(status_code=401, detail="Session expired or invalid.")

    policy = SecurityPolicy.get_or_create(db)
    now = datetime.now(UTC)
    absolute_limit = timedelta(minutes=policy.session_absolute_timeout_minutes)
    idle_limit = timedelta(minutes=policy.session_idle_timeout_minutes)
    if (
        now - _aware(session_row.created_at) > absolute_limit
        or now - _aware(session_row.last_seen_at) > idle_limit
    ):
        session_row.revoked_at = now
        db.commit()
        raise HTTPException(status_code=401, detail="Session expired or invalid.")

    session_row.last_seen_at = now
    session_row.ip = _client_ip(request)
    session_row.user_agent = _user_agent(request)
    db.commit()

    _set_session_cookies(response, user, settings, sid=sid)
    return UserOut.from_user(user, mfa_enabled=_has_confirmed_totp(db, user.id))


@router.get("/me")
def me(user: CurrentUser, db: DbSession) -> UserOut:
    return UserOut.from_user(user, mfa_enabled=_has_confirmed_totp(db, user.id))


@router.post("/logout", status_code=204)
def logout(request: Request, response: Response, db: DbSession) -> None:
    """Clears the session cookies and revokes the underlying `UserSession` row
    (best-effort — from whichever cookie still decodes). Deliberately
    unauthenticated so a client with an expired/broken session can always
    reach a clean state.

    A.8.15 (Monitoring, DOO-417): whenever a valid token is present we also
    write an `auth.logout` audit row (mirroring `auth.logout_all`), attributed
    to the actor decoded from the token. A broken/absent/expired token still
    reaches a clean state — there is just no actor to attribute, so no row is
    written. `record_audit` never raises, so best-effort logging can never turn
    logout into a 500."""
    settings = get_settings()
    actor: dict | None = None
    for cookie_name, expected_type in ((ACCESS_COOKIE, "access"), (REFRESH_COOKIE, "refresh")):
        token = request.cookies.get(cookie_name)
        if not token:
            continue
        claims = decode_session_token(
            token, expected_type=expected_type, secret=settings.jwt_secret
        )
        if claims and actor is None:
            actor = claims
        sid = claims.get("sid") if claims else None
        if sid:
            db.execute(
                update(UserSession)
                .where(UserSession.jti == sid, UserSession.revoked_at.is_(None))
                .values(revoked_at=datetime.now(UTC))
            )
            db.commit()
            break
    if actor is not None:
        user_id = int(actor["sub"])
        user = db.get(User, user_id)
        entity_id = actor.get("sid") or (user.email if user else str(user_id))
        record_audit(
            db,
            action="auth.logout",
            summary=f"Signed out {user.email if user else user_id}",
            user_id=user_id,
            entity_type="session",
            entity_id=entity_id,
            result="ok",
            source_ip=_client_ip(request),
        )
    _clear_session_cookies(response)


@router.post("/logout-all", status_code=204)
def logout_all(
    request: Request,
    response: Response,
    db: DbSession,
    user: CurrentUser,
) -> None:
    """Log out everywhere (SEC-M2 + session 6.5): revokes every UserSession
    row for this account and bumps token_version as defence-in-depth, so any
    outstanding token — on any device, plus an exfiltrated refresh cookie —
    is rejected on its next use."""
    bump_token_version(user)
    db.execute(
        update(UserSession)
        .where(UserSession.user_id == user.id, UserSession.revoked_at.is_(None))
        .values(revoked_at=datetime.now(UTC))
    )
    db.commit()
    record_audit(
        db,
        action="auth.logout_all",
        summary=f"Revoked all sessions for {user.email}",
        user_id=user.id,
        entity_type="session",
        entity_id=user.email,
        result="ok",
        source_ip=_client_ip(request),
    )
    _clear_session_cookies(response)


# --------------------------------------------------------------------------- #
# TOTP enrolment (session 6.5): setup -> confirm -> disable. Self-service —
# any authenticated user manages their own 2FA, no extra permission gate.
# --------------------------------------------------------------------------- #


@router.post("/2fa/setup")
def totp_setup(user: CurrentUser, db: DbSession) -> TOTPSetupOut:
    existing = db.scalars(select(UserTOTP).where(UserTOTP.user_id == user.id)).first()
    if existing is not None and existing.confirmed_at is not None:
        raise HTTPException(
            status_code=400, detail="2FA is already enrolled. Disable it before re-enrolling."
        )
    secret = generate_totp_secret()
    if existing is None:
        db.add(UserTOTP(user_id=user.id, secret_encrypted=encrypt_totp_secret(secret)))
    else:
        existing.secret_encrypted = encrypt_totp_secret(secret)
        existing.last_used_step = None
    db.commit()
    return TOTPSetupOut(secret=secret, provisioning_uri=provisioning_uri(secret, user.email))


@router.post("/2fa/confirm")
def totp_confirm(
    body: MFACodeIn, user: CurrentUser, db: DbSession, audit: Audit
) -> TOTPConfirmOut:
    row = db.scalars(select(UserTOTP).where(UserTOTP.user_id == user.id)).first()
    if row is None or row.confirmed_at is not None:
        raise HTTPException(status_code=400, detail="No pending 2FA setup. Call /2fa/setup first.")
    secret = decrypt_totp_secret(row.secret_encrypted)
    step = verify_totp_code(secret, body.code, last_used_step=row.last_used_step)
    if step is None:
        raise HTTPException(status_code=401, detail="Invalid or expired code.")

    row.confirmed_at = datetime.now(UTC)
    row.last_used_step = step
    # Defence-in-depth: clear any codes from an earlier enrol/disable/re-enrol
    # cycle before minting the new set.
    db.execute(delete(RecoveryCode).where(RecoveryCode.user_id == user.id))
    plaintext_codes = generate_recovery_codes()
    for code in plaintext_codes:
        db.add(RecoveryCode(user_id=user.id, code_hash=hash_recovery_code(code)))
    db.commit()

    audit.record(
        action="auth.mfa_enrol",
        summary=f"Enabled 2FA for {user.email}",
        entity_type="user",
        entity_id=user.id,
    )
    return TOTPConfirmOut(recovery_codes=plaintext_codes)


@router.post("/2fa/disable", status_code=204)
def totp_disable(user: CurrentUser, db: DbSession, audit: Audit) -> None:
    db.execute(delete(RecoveryCode).where(RecoveryCode.user_id == user.id))
    db.execute(delete(UserTOTP).where(UserTOTP.user_id == user.id))
    db.commit()
    audit.record(
        action="auth.mfa_disable",
        summary=f"Disabled 2FA for {user.email}",
        entity_type="user",
        entity_id=user.id,
    )


# --------------------------------------------------------------------------- #
# Session enumeration + revocation (session 6.5).
# --------------------------------------------------------------------------- #


@router.get("/sessions")
def list_sessions(request: Request, user: CurrentUser, db: DbSession) -> list[SessionOut]:
    current_jti = _current_sid(request, get_settings())
    rows = db.scalars(
        select(UserSession)
        .where(UserSession.user_id == user.id, UserSession.revoked_at.is_(None))
        .order_by(UserSession.last_seen_at.desc())
    ).all()
    return [SessionOut.from_model(row, current_jti=current_jti) for row in rows]


@router.post("/sessions/{session_id}/revoke", status_code=204)
def revoke_session(session_id: int, user: CurrentUser, db: DbSession, audit: Audit) -> None:
    row = db.get(UserSession, session_id)
    if row is None or row.user_id != user.id:
        raise HTTPException(status_code=404, detail="Session not found.")
    if row.revoked_at is None:
        row.revoked_at = datetime.now(UTC)
        db.commit()
        audit.record(
            action="auth.session_revoke",
            summary=f"Revoked a session for {user.email}",
            entity_type="session",
            entity_id=row.id,
        )


@router.post("/sessions/revoke-others", status_code=204)
def revoke_other_sessions(
    request: Request, user: CurrentUser, db: DbSession, audit: Audit
) -> None:
    current_jti = _current_sid(request, get_settings())
    rows = db.scalars(
        select(UserSession).where(UserSession.user_id == user.id, UserSession.revoked_at.is_(None))
    ).all()
    now = datetime.now(UTC)
    revoked = 0
    for row in rows:
        if row.jti != current_jti:
            row.revoked_at = now
            revoked += 1
    db.commit()
    audit.record(
        action="auth.session_revoke_others",
        summary=f"Revoked {revoked} other session(s) for {user.email}",
        entity_type="session",
        entity_id=user.email,
    )


@router.get("/login-attempts")
def list_login_attempts(
    db: DbSession,
    _: Annotated[object, Depends(require(SETTINGS_MANAGE))],
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> list[LoginAttemptOut]:
    rows = db.scalars(
        select(LoginAttemptModel)
        .order_by(LoginAttemptModel.created_at.desc(), LoginAttemptModel.id.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    return [
        LoginAttemptOut(
            id=r.id, email=r.email, ip=r.ip, success=r.success, reason=r.reason,
            created_at=r.created_at,
        )
        for r in rows
    ]
