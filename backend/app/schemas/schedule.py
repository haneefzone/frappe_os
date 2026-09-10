"""Request/response models for the schedules API (session 2.1)."""

from datetime import datetime

from pydantic import BaseModel, Field

from app.models.schedule import Schedule

# Actions a schedule may drive (mirrors app.models.schedule.SCHEDULE_ACTIONS,
# re-declared as a Literal so the OpenAPI schema and request validation are tight).
ScheduleActionName = str


class ScheduleOut(BaseModel):
    """One schedule for the Schedules list, enriched with a display label for its
    target and the status of its last fired job (drives the "last result" cell)."""

    id: int
    name: str
    target_type: str
    target_id: int | None
    target_label: str | None
    action_name: str
    cron: str | None
    interval_seconds: int | None
    timezone: str
    priority: str
    with_files: bool
    retention_keep_last: int | None
    retention_keep_days: int | None
    enabled: bool
    next_run_at: datetime | None
    last_run_at: datetime | None
    last_run_job_id: int | None
    last_run_status: str | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_model(
        cls,
        schedule: Schedule,
        *,
        target_label: str | None = None,
        last_run_status: str | None = None,
    ) -> "ScheduleOut":
        return cls(
            id=schedule.id,
            name=schedule.name,
            target_type=schedule.target_type,
            target_id=schedule.target_id,
            target_label=target_label,
            action_name=schedule.action_name,
            cron=schedule.cron,
            interval_seconds=schedule.interval_seconds,
            timezone=schedule.timezone,
            priority=schedule.priority,
            with_files=schedule.with_files,
            retention_keep_last=schedule.retention_keep_last,
            retention_keep_days=schedule.retention_keep_days,
            enabled=schedule.enabled,
            next_run_at=schedule.next_run_at,
            last_run_at=schedule.last_run_at,
            last_run_job_id=schedule.last_run_job_id,
            last_run_status=last_run_status,
            created_at=schedule.created_at,
            updated_at=schedule.updated_at,
        )


class CreateScheduleRequest(BaseModel):
    """Create a recurring schedule. Provide exactly one cadence (cron OR
    interval_seconds). `action_name` decides which extra params apply:
    `site.backup` reads `with_files`; `backup.retention_sweep` requires at least
    one of `retention_keep_last` / `retention_keep_days`."""

    name: str = Field(min_length=1, max_length=200)
    target_type: str = "site"
    target_id: int
    action_name: str

    cron: str | None = Field(default=None, max_length=100)
    interval_seconds: int | None = Field(default=None, ge=1)
    timezone: str = "Asia/Dubai"
    priority: str = "low"

    with_files: bool = False
    retention_keep_last: int | None = Field(default=None, ge=1)
    retention_keep_days: int | None = Field(default=None, ge=1)


class UpdateScheduleRequest(BaseModel):
    """Patch a schedule. Only supplied fields change; changing a cadence field
    (cron/interval/timezone) recomputes next_run_at from now."""

    name: str | None = Field(default=None, min_length=1, max_length=200)
    cron: str | None = Field(default=None, max_length=100)
    interval_seconds: int | None = Field(default=None, ge=1)
    timezone: str | None = None
    priority: str | None = None
    with_files: bool | None = None
    retention_keep_last: int | None = Field(default=None, ge=1)
    retention_keep_days: int | None = Field(default=None, ge=1)
    enabled: bool | None = None
    # Sentinel-free clears: set to true to unset the paired retention dimension.
    clear_keep_last: bool = False
    clear_keep_days: bool = False


class SetEnabledRequest(BaseModel):
    enabled: bool
