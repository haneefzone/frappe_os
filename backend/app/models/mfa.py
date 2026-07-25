"""TOTP 2FA, recovery codes, session enumeration/revocation, and the
Admin-editable security policy singleton (session 6.5).

- **UserTOTP** — one per user (unique `user_id`). `secret_encrypted` is the
  RFC 6238 base32 secret, Fernet-encrypted at rest (rule 6) and never returned
  once enrolment is confirmed. `confirmed_at` NULL means "setup started but
  never confirmed" (not yet enforced). `last_used_step` is the RFC 6238
  time-step counter of the most recently accepted code — a code for that step
  or any earlier one is rejected, closing the replay window.
- **RecoveryCode** — ten single-use codes minted at confirm time. Only the
  SHA-256 hash is stored (same one-time-display pattern as `ApiToken`);
  `used_at` makes a consumed code permanently unusable.
- **UserSession** — one row per login/refresh chain, keyed by the `sid` claim
  baked into both the access and refresh JWTs for that chain. Revoking a row
  (or its idle/absolute timeout expiring) is checked on *every* authenticated
  request via `get_current_user`, not just at the next login — a deny-list of
  one, rather than a fixed-size token list.
- **LoginAttempt** — an immutable log of every password and MFA-code attempt
  (session 1.1's `LoginThrottle` is in-memory-only and never persisted this).
  Feeds the Security page's failed-login table; the lockout decision itself
  still comes from `LoginThrottle` / the MFA throttle, not from this table.
- **SecurityPolicy** — the one-row (`id == 1`) Admin-editable policy: which
  roles must have 2FA enrolled, password length/complexity/reuse rules, an
  optional platform IP allowlist, and session idle/absolute timeouts.
"""

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

# JSONB on Postgres, plain JSON elsewhere (SQLite test fallback) — matches
# app.models.auth.PermissionsJSON.
_JSON = JSON().with_variant(JSONB(), "postgresql")

SECURITY_POLICY_ID = 1


class UserTOTP(Base):
    __tablename__ = "user_totp"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), unique=True, index=True)
    secret_encrypted: Mapped[str] = mapped_column(String(500))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_step: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    user: Mapped["User"] = relationship()  # noqa: F821

    @property
    def is_active(self) -> bool:
        return self.confirmed_at is not None


class RecoveryCode(Base):
    __tablename__ = "recovery_codes"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    code_hash: Mapped[str] = mapped_column(String(64), unique=True)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    user: Mapped["User"] = relationship()  # noqa: F821


class UserSession(Base):
    __tablename__ = "user_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    # The `sid` claim shared by every access/refresh JWT issued for this
    # login chain — the unit that "a session" means in the UI.
    jti: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    ip: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(300))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user: Mapped["User"] = relationship()  # noqa: F821


class LoginAttempt(Base):
    __tablename__ = "login_attempts"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    ip: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(300))
    success: Mapped[bool] = mapped_column(Boolean, default=False)
    # password_ok | bad_password | locked | mfa_required | mfa_ok | mfa_bad_code
    # | mfa_locked | recovery_code_used | account_disabled | ip_denied
    reason: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class SecurityPolicy(Base):
    """The one-row (`id == 1`) Admin-editable security policy."""

    __tablename__ = "security_policy"

    id: Mapped[int] = mapped_column(primary_key=True, default=SECURITY_POLICY_ID)

    # Role names (matching Role.name) that must have confirmed 2FA. A member
    # of one of these roles without confirmed TOTP is redirected to enrolment
    # and blocked from mutating routes (get_current_user enforces this).
    enforce_2fa_roles: Mapped[list] = mapped_column(_JSON, default=list)

    password_min_length: Mapped[int] = mapped_column(Integer, default=10)
    # Requires at least one upper, one lower, one digit, one symbol.
    password_require_complexity: Mapped[bool] = mapped_column(Boolean, default=True)
    # How many previous passwords a new one may not match (0 = disabled).
    password_reuse_history: Mapped[int] = mapped_column(Integer, default=5)

    # CIDR or bare-IP strings. Empty list = no restriction (default).
    ip_allowlist: Mapped[list] = mapped_column(_JSON, default=list)

    session_idle_timeout_minutes: Mapped[int] = mapped_column(Integer, default=60)
    session_absolute_timeout_minutes: Mapped[int] = mapped_column(Integer, default=1440)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    @classmethod
    def get_or_create(cls, db) -> "SecurityPolicy":
        row = db.get(cls, SECURITY_POLICY_ID)
        if row is None:
            row = cls(id=SECURITY_POLICY_ID)
            db.add(row)
            db.commit()
            db.refresh(row)
        return row
