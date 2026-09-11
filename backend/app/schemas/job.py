"""Request/response models for the job engine API."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.models import CommandJob, CommandStep

Priority = Literal["high", "default", "low"]
TargetType = Literal["server", "bench", "site"]


class JobCreate(BaseModel):
    action_name: str = Field(min_length=1, max_length=120)
    server_id: int
    target_type: TargetType = "server"
    target_id: str | None = Field(default=None, max_length=255)
    priority: Priority = "default"
    params: dict[str, object] = Field(default_factory=dict)


class StepOut(BaseModel):
    id: int
    name: str
    # 1-based auto-retry attempt this step belongs to; `order` is per-attempt, so
    # the timeline groups by `attempt` then renders steps in `order` (DOO-96).
    attempt: int
    order: int
    status: str
    started_at: datetime | None
    ended_at: datetime | None
    error_traceback: str | None

    @classmethod
    def from_model(cls, step: CommandStep) -> "StepOut":
        return cls(
            id=step.id,
            name=step.name,
            attempt=step.attempt,
            order=step.order,
            status=step.status,
            started_at=step.started_at,
            ended_at=step.ended_at,
            error_traceback=step.error_traceback,
        )


class JobOut(BaseModel):
    id: int
    server_id: int | None
    target_type: str
    target_id: str | None
    action_name: str
    priority: str
    status: str
    rq_job_id: str | None
    retry_count: int
    exit_code: int | None
    lock_key: str | None
    params_sanitized: dict
    created_by: int | None
    started_at: datetime | None
    ended_at: datetime | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_model(cls, job: CommandJob) -> "JobOut":
        return cls(
            id=job.id,
            server_id=job.server_id,
            target_type=job.target_type,
            target_id=job.target_id,
            action_name=job.action_name,
            priority=job.priority,
            status=job.status,
            rq_job_id=job.rq_job_id,
            retry_count=job.retry_count,
            exit_code=job.exit_code,
            lock_key=job.lock_key,
            params_sanitized=dict(job.params_sanitized or {}),
            created_by=job.created_by,
            started_at=job.started_at,
            ended_at=job.ended_at,
            created_at=job.created_at,
            updated_at=job.updated_at,
        )


class JobDetail(JobOut):
    steps: list[StepOut] = Field(default_factory=list)

    @classmethod
    def from_model(cls, job: CommandJob) -> "JobDetail":
        base = JobOut.from_model(job)
        return cls(
            **base.model_dump(),
            steps=[StepOut.from_model(s) for s in job.steps],
        )
