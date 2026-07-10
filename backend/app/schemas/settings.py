"""Request/response models for the Settings API (session 1.12, B4.17)."""

from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from app.models.settings import PlatformSettings

# Logo upload: allowed image types and a size cap (base64-decoded bytes).
ALLOWED_LOGO_TYPES = ("image/png", "image/jpeg", "image/svg+xml", "image/webp")
MAX_LOGO_BYTES = 512 * 1024  # 512 KB is plenty for a sidebar wordmark.


class PlatformSettingsOut(BaseModel):
    """The operator-editable settings (General + Defaults tabs)."""

    product_name: str
    logo_path: str | None
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
            default_tz=row.default_tz,
            bench_base_path=row.bench_base_path,
            port_range_start=row.port_range_start,
            port_range_end=row.port_range_end,
            updated_at=row.updated_at,
        )


class PlatformSettingsUpdate(BaseModel):
    """A partial update — only the provided fields change."""

    product_name: str | None = Field(default=None, min_length=1, max_length=80)
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


class LogoUpload(BaseModel):
    """A white-label logo as base64 (avoids a multipart dependency)."""

    content_type: str
    content_base64: str


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
