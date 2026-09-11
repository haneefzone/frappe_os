"""Pydantic schemas for maintenance windows (session 3.5)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.models.maintenance_window import DANGER_CLASSES


class CreateMaintenanceWindowRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    server_id: int
    cron: str = Field(..., description="5-field cron expression (e.g. '0 9 * * 1-5')")
    duration_minutes: int = Field(120, ge=1, le=10080)
    timezone: str = Field("Asia/Dubai", max_length=64)
    blocked_danger_classes: list[str] = Field(default_factory=list)
    enabled: bool = True

    model_config = {"str_strip_whitespace": True}

    def model_post_init(self, __context) -> None:  # noqa: ANN001
        for cls in self.blocked_danger_classes:
            if cls not in DANGER_CLASSES:
                raise ValueError(
                    f"'{cls}' is not a valid danger class; "
                    f"must be one of {list(DANGER_CLASSES)}"
                )


class UpdateMaintenanceWindowRequest(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=200)
    cron: str | None = None
    duration_minutes: int | None = Field(None, ge=1, le=10080)
    timezone: str | None = Field(None, max_length=64)
    blocked_danger_classes: list[str] | None = None
    enabled: bool | None = None

    model_config = {"str_strip_whitespace": True}

    def model_post_init(self, __context) -> None:  # noqa: ANN001
        if self.blocked_danger_classes is not None:
            for cls in self.blocked_danger_classes:
                if cls not in DANGER_CLASSES:
                    raise ValueError(
                        f"'{cls}' is not a valid danger class; "
                        f"must be one of {list(DANGER_CLASSES)}"
                    )


class MaintenanceWindowOut(BaseModel):
    id: int
    name: str
    server_id: int
    cron: str
    duration_minutes: int
    timezone: str
    blocked_danger_classes: list[str]
    enabled: bool
    created_by: int | None
    created_at: datetime
    updated_at: datetime
    is_active_now: bool

    model_config = {"from_attributes": True}

    @classmethod
    def from_model(cls, mw) -> MaintenanceWindowOut:
        return cls(
            id=mw.id,
            name=mw.name,
            server_id=mw.server_id,
            cron=mw.cron,
            duration_minutes=mw.duration_minutes,
            timezone=mw.timezone,
            blocked_danger_classes=mw.blocked_danger_classes or [],
            enabled=mw.enabled,
            created_by=mw.created_by,
            created_at=mw.created_at,
            updated_at=mw.updated_at,
            is_active_now=mw.is_active_at(),
        )
