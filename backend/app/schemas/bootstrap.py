"""Pydantic schemas for the bootstrap / first-run wizard API (session 6.4)."""

from pydantic import BaseModel, EmailStr, Field, field_validator


class BootstrapStatusOut(BaseModel):
    needs_setup: bool


class PreflightCheck(BaseModel):
    name: str
    ok: bool
    detail: str
    hint: str | None = None


class PreflightOut(BaseModel):
    checks: list[PreflightCheck]
    all_ok: bool


class AdminIn(BaseModel):
    email: EmailStr
    full_name: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=12, max_length=128)

    @field_validator("password")
    @classmethod
    def validate_password_complexity(cls, v: str) -> str:
        # A.5.17 privileged-credential policy: enforce complexity server-side.
        # The wizard's 4-bar strength meter is advisory only; this is the gate.
        if len(v) < 12:
            raise ValueError("password must be at least 12 characters")
        if not any(c.isupper() for c in v):
            raise ValueError("password must contain an uppercase letter")
        if not any(c.islower() for c in v):
            raise ValueError("password must contain a lowercase letter")
        if not any(c.isdigit() for c in v):
            raise ValueError("password must contain a digit")
        if not any(not c.isalnum() for c in v):
            raise ValueError("password must contain a special character")
        return v


class BrandingIn(BaseModel):
    product_name: str = Field(default="FDM Platform", min_length=1, max_length=80)
    default_tz: str = Field(default="Asia/Dubai", max_length=64)
    accent_hex: str | None = Field(default=None, max_length=7)

    @field_validator("accent_hex")
    @classmethod
    def validate_hex(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if not v.startswith("#") or len(v) not in (4, 7):
            raise ValueError("accent_hex must be #RGB or #RRGGBB")
        return v


class CompleteIn(BaseModel):
    admin: AdminIn
    branding: BrandingIn = Field(default_factory=BrandingIn)


class CompleteOut(BaseModel):
    setup_complete: bool
    user_id: int
    email: str
