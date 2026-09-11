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
    password: str = Field(min_length=8, max_length=128)


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


class NotificationsIn(BaseModel):
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_from: str | None = None
    smtp_username: str | None = None
    smtp_password: str | None = None


class CompleteIn(BaseModel):
    admin: AdminIn
    branding: BrandingIn = Field(default_factory=BrandingIn)
    notifications: NotificationsIn | None = None


class CompleteOut(BaseModel):
    setup_complete: bool
    user_id: int
    email: str
