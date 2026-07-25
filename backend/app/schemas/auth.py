from datetime import datetime

from pydantic import BaseModel, EmailStr, Field

from app.models import User
from app.models.mfa import SecurityPolicy, UserSession


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class UserOut(BaseModel):
    id: int
    email: str
    full_name: str
    role: str
    permissions: list[str]
    last_login: datetime | None
    mfa_enabled: bool = False

    @classmethod
    def from_user(cls, user: User, *, mfa_enabled: bool = False) -> "UserOut":
        return cls(
            id=user.id,
            email=user.email,
            full_name=user.full_name,
            role=user.role.name,
            permissions=list(user.role.permissions or []),
            last_login=user.last_login,
            mfa_enabled=mfa_enabled,
        )


class LoginOut(BaseModel):
    """Login is two-step once 2FA is active: `mfa_required=true` means the
    password was correct but no session exists yet — `user` is omitted so the
    frontend never mistakes this for a signed-in state."""

    mfa_required: bool
    user: UserOut | None = None


class MFACodeIn(BaseModel):
    code: str = Field(min_length=4, max_length=20)


class TOTPSetupOut(BaseModel):
    """Returned only from /2fa/setup, before confirmation. The raw secret is
    shown once for manual entry (same value the QR-code URI encodes) — neither
    is ever returned again once /2fa/confirm succeeds."""

    secret: str
    provisioning_uri: str


class TOTPConfirmOut(BaseModel):
    """The ten recovery codes, shown exactly once (same pattern as ApiToken)."""

    recovery_codes: list[str]


class SessionOut(BaseModel):
    id: int
    created_at: datetime
    last_seen_at: datetime
    ip: str | None
    user_agent: str | None
    is_current: bool

    @classmethod
    def from_model(cls, row: UserSession, *, current_jti: str | None) -> "SessionOut":
        return cls(
            id=row.id,
            created_at=row.created_at,
            last_seen_at=row.last_seen_at,
            ip=row.ip,
            user_agent=row.user_agent,
            is_current=row.jti == current_jti,
        )


class LoginAttemptOut(BaseModel):
    id: int
    email: str
    ip: str | None
    success: bool
    reason: str
    created_at: datetime


class SecurityPolicyOut(BaseModel):
    enforce_2fa_roles: list[str]
    password_min_length: int
    password_require_complexity: bool
    password_reuse_history: int
    ip_allowlist: list[str]
    session_idle_timeout_minutes: int
    session_absolute_timeout_minutes: int
    updated_at: datetime

    @classmethod
    def from_model(cls, row: SecurityPolicy) -> "SecurityPolicyOut":
        return cls(
            enforce_2fa_roles=list(row.enforce_2fa_roles or []),
            password_min_length=row.password_min_length,
            password_require_complexity=row.password_require_complexity,
            password_reuse_history=row.password_reuse_history,
            ip_allowlist=list(row.ip_allowlist or []),
            session_idle_timeout_minutes=row.session_idle_timeout_minutes,
            session_absolute_timeout_minutes=row.session_absolute_timeout_minutes,
            updated_at=row.updated_at,
        )


class SecurityPolicyUpdate(BaseModel):
    enforce_2fa_roles: list[str] | None = None
    password_min_length: int | None = Field(default=None, ge=6, le=128)
    password_require_complexity: bool | None = None
    password_reuse_history: int | None = Field(default=None, ge=0, le=24)
    ip_allowlist: list[str] | None = None
    session_idle_timeout_minutes: int | None = Field(default=None, ge=1, le=10080)
    session_absolute_timeout_minutes: int | None = Field(default=None, ge=1, le=43200)
