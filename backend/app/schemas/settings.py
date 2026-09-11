"""Request/response models for the Settings API (session 1.12 + 6.6)."""

import re
from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from app.models.settings import PlatformSettings

# Logo / favicon upload: allowed image types and size caps (base64-decoded bytes).
ALLOWED_LOGO_TYPES = ("image/png", "image/jpeg", "image/svg+xml", "image/webp")
ALLOWED_FAVICON_TYPES = ("image/png", "image/x-icon", "image/vnd.microsoft.icon")
MAX_LOGO_BYTES = 512 * 1024       # 512 KB is plenty for a sidebar wordmark.
MAX_FAVICON_BYTES = 64 * 1024     # 64 KB — favicons are tiny.

# Hex colour validation: #RRGGBB or #RGB (case-insensitive).
_HEX_RE = re.compile(r"^#([0-9a-fA-F]{6}|[0-9a-fA-F]{3})$")


class PlatformSettingsOut(BaseModel):
    """The operator-editable settings (General + Defaults tabs)."""

    product_name: str
    logo_path: str | None
    logo_dark_path: str | None
    favicon_path: str | None
    accent_hex: str | None
    support_link: str | None
    footer_line: str | None
    default_tz: str
    bench_base_path: str
    port_range_start: int
    port_range_end: int
    updated_at: datetime | None

    @classmethod
    def from_model(cls, row: PlatformSettings) -> "PlatformSettingsOut":
        return cls(
            product_name=row.product_name,
            logo_path=row.logo_path,
            logo_dark_path=row.logo_dark_path,
            favicon_path=row.favicon_path,
            accent_hex=row.accent_hex,
            support_link=row.support_link,
            footer_line=row.footer_line,
            default_tz=row.default_tz,
            bench_base_path=row.bench_base_path,
            port_range_start=row.port_range_start,
            port_range_end=row.port_range_end,
            updated_at=row.updated_at,
        )


class PlatformSettingsUpdate(BaseModel):
    """A partial update — only the provided fields change."""

    product_name: str | None = Field(default=None, min_length=1, max_length=80)
    accent_hex: str | None = Field(default=None)
    support_link: str | None = Field(default=None, max_length=500)
    footer_line: str | None = Field(default=None, max_length=200)
    default_tz: str | None = Field(default=None, min_length=1, max_length=64)
    bench_base_path: str | None = Field(default=None, min_length=1, max_length=300)
    port_range_start: int | None = Field(default=None, ge=1, le=65535)
    port_range_end: int | None = Field(default=None, ge=1, le=65535)

    @field_validator("bench_base_path")
    @classmethod
    def _abs_path(cls, v: str | None) -> str | None:
        if v is not None and not v.startswith("/"):
            raise ValueError("bench_base_path must be an absolute path")
        return v

    @field_validator("accent_hex")
    @classmethod
    def _valid_hex(cls, v: str | None) -> str | None:
        if v is not None and not _HEX_RE.match(v):
            raise ValueError("accent_hex must be a CSS hex colour (#RRGGBB or #RGB)")
        return v

    @field_validator("support_link")
    @classmethod
    def _valid_url(cls, v: str | None) -> str | None:
        if v is not None and v:
            # Allow empty string to clear the link; otherwise require http(s).
            if not (v.startswith("http://") or v.startswith("https://")):
                raise ValueError("support_link must start with http:// or https://")
        return v


class LogoUpload(BaseModel):
    """A white-label logo as base64 (avoids a multipart dependency)."""

    content_type: str
    content_base64: str


class FaviconUpload(BaseModel):
    """A favicon as base64 (PNG or ICO only)."""

    content_type: str
    content_base64: str


class BrandingOut(BaseModel):
    """Public brand bundle — safe to expose without authentication.

    This model intentionally exposes ONLY the fields the login page and setup
    wizard need before auth. No sensitive settings (SMTP, ports, paths) leak here.
    """

    product_name: str
    accent_hex: str | None
    logo_url: str | None
    logo_dark_url: str | None
    favicon_url: str | None
    support_link: str | None
    footer_line: str | None

    @classmethod
    def from_model(cls, row: PlatformSettings) -> "BrandingOut":
        return cls(
            product_name=row.product_name,
            accent_hex=row.accent_hex,
            logo_url=row.logo_path,
            logo_dark_url=row.logo_dark_path,
            favicon_url=row.favicon_path,
            support_link=row.support_link,
            footer_line=row.footer_line,
        )


class EnvironmentInfo(BaseModel):
    """Read-only environment facts for the Settings "Environment" panel."""

    app_version: str
    python_version: str
    platform: str
    database_backend: str
    redis_configured: bool
    debug: bool
    default_tz: str
    monitoring_enabled: bool
    monitoring_interval_seconds: int
