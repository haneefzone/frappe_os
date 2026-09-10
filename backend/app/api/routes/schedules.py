"""Recurring-schedule API (session 2.1).

- GET    /api/schedules            list schedules (Read-only+)
- POST   /api/schedules            create a schedule (Admin/Developer)
- GET    /api/schedules/{id}       one schedule (Read-only+)
- PATCH  /api/schedules/{id}       edit a schedule (Admin/Developer)
- DELETE /api/schedules/{id}       delete a schedule (Admin/Developer)
- POST   /api/schedules/{id}/enabled   enable/disable (Admin/Developer)
- POST   /api/schedules/{id}/run-now   fire now -> a CommandJob (Operator+)

Managing schedules (create/edit/enable/delete) needs `schedule:manage`
(Admin/Developer). *Running* one now needs the underlying action's own
permission — e.g. `backup:create` — so an Operator may trigger an already-defined
schedule but not create or reshape one. Read-only can only view.

A schedule never runs work in the request: run-now (like the scheduler tick) goes
through `JobRunner.create`, so it is locked, audited and enqueued exactly like any
other job (golden rules 2/3). CRUD writes an audit row of their own (rule 2).
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, require
from app.api.routes.jobs import get_job_runner
from app.audit import Audit
from app.core.commands import get_template
from app.core.jobs import JobRunner, LockConflict
from app.core.permissions import READ, SCHEDULE_MANAGE, role_allows
from app.core.scheduler import (
    ScheduleError,
    compute_next_run,
    fire_schedule,
    validate_cadence,
)
from app.db import get_db
from app.models import CommandJob
from app.models.bench import Bench
from app.models.schedule import SCHEDULE_ACTIONS, SCHEDULE_TARGET_TYPES, Schedule
from app.models.site import Site
from app.schemas.job import JobDetail
from app.schemas.schedule import (
    CreateScheduleRequest,
    ScheduleOut,
    SetEnabledRequest,
    UpdateScheduleRequest,
)

router = APIRouter(prefix="/api/schedules", tags=["schedules"])

DbSession = Annotated[Session, Depends(get_db)]
Runner = Annotated[JobRunner, Depends(get_job_runner)]

VALID_PRIORITIES = ("high", "default", "low")


def _utcnow():
    # Isolated so tests can monkeypatch the module clock deterministically.
    from datetime import UTC, datetime

    return datetime.now(UTC)


def _target_label(db: Session, schedule: Schedule) -> str | None:
    """Human label for the schedule's target (the site name) for the list."""
    if schedule.target_type == "site":
        site = db.get(Site, schedule.target_id)
        return site.name if site else None
    return None


def _last_status(db: Session, schedule: Schedule) -> str | None:
    if schedule.last_run_job_id is None:
        return None
    job = db.get(CommandJob, schedule.last_run_job_id)
    return job.status if job else None


def _out(db: Session, schedule: Schedule) -> ScheduleOut:
    return ScheduleOut.from_model(
        schedule,
        target_label=_target_label(db, schedule),
        last_run_status=_last_status(db, schedule),
    )


def _conflict(exc: LockConflict, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content={
            "error": {
                "code": "conflict",
                "message": message,
                "blocking_job_id": exc.blocking_job_id,
            }
        },
    )


def _validate_action_params(
    *,
    action_name: str,
    with_files: bool,
    keep_last: int | None,
    keep_days: int | None,
) -> None:
    """Reject action/param combinations that would never do useful work."""
    if action_name == "backup.retention_sweep" and keep_last is None and keep_days is None:
        raise HTTPException(
            status_code=422,
            detail="A retention sweep needs at least one of "
            "retention_keep_last or retention_keep_days.",
        )


def _resolve_target(db: Session, target_type: str, target_id: int | None) -> None:
    """Ensure the schedule points at a real, supported target (validated at
    create/edit so a schedule can never be saved pointing at nothing).

    Branches on `target_type`: a `site` target resolves a Site (+ its bench); a
    `server` target (6.7 drift, 4.2 restic DR) resolves a Server; a `report`
    target (6.2) has no row at all — its config lives in `params`."""
    if target_type not in SCHEDULE_TARGET_TYPES:
        raise HTTPException(
            status_code=422, detail=f"Unsupported target_type {target_type!r}."
        )
    if target_type == "report":
        return  # no row target — report config lives in params
    if target_type == "server":
        from app.models.server import Server

        if target_id is None or db.get(Server, target_id) is None:
            raise HTTPException(status_code=404, detail="Target server not found.")
        return
    site = db.get(Site, target_id) if target_id is not None else None
    if site is None:
        raise HTTPException(status_code=404, detail="Target site not found.")
    if db.get(Bench, site.bench_id) is None:  # pragma: no cover - FK-guaranteed
        raise HTTPException(status_code=404, detail="Target site's bench is missing.")


# --------------------------------------------------------------------------- #
# List / detail
# --------------------------------------------------------------------------- #


@router.get("", response_model=list[ScheduleOut])
def list_schedules(
    db: DbSession,
    _: Annotated[object, Depends(require(READ))],
) -> list[ScheduleOut]:
    rows = db.scalars(select(Schedule).order_by(Schedule.created_at.desc())).all()
    return [_out(db, s) for s in rows]


@router.get("/{schedule_id}", response_model=ScheduleOut)
def get_schedule(
    schedule_id: int,
    db: DbSession,
    _: Annotated[object, Depends(require(READ))],
) -> ScheduleOut:
    schedule = db.get(Schedule, schedule_id)
    if schedule is None:
        raise HTTPException(status_code=404, detail="Schedule not found.")
    return _out(db, schedule)


# --------------------------------------------------------------------------- #
# Create / update / delete
# --------------------------------------------------------------------------- #


@router.post("", status_code=201, response_model=ScheduleOut)
def create_schedule(
    body: CreateScheduleRequest,
    db: DbSession,
    audit: Audit,
    _: Annotated[object, Depends(require(SCHEDULE_MANAGE))],
) -> ScheduleOut:
    if body.action_name not in SCHEDULE_ACTIONS:
        raise HTTPException(
            status_code=422,
            detail=f"action_name must be one of {list(SCHEDULE_ACTIONS)}.",
        )
    if body.priority not in VALID_PRIORITIES:
        raise HTTPException(status_code=422, detail="priority must be high|default|low.")
    _resolve_target(db, body.target_type, body.target_id)
    _validate_action_params(
        action_name=body.action_name,
        with_files=body.with_files,
        keep_last=body.retention_keep_last,
        keep_days=body.retention_keep_days,
    )
    try:
        validate_cadence(
            cron=body.cron,
            interval_seconds=body.interval_seconds,
            timezone=body.timezone,
        )
    except ScheduleError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    schedule = Schedule(
        name=body.name,
        target_type=body.target_type,
        target_id=body.target_id,
        action_name=body.action_name,
        cron=body.cron,
        interval_seconds=body.interval_seconds,
        timezone=body.timezone,
        priority=body.priority,
        with_files=body.with_files,
        retention_keep_last=body.retention_keep_last,
        retention_keep_days=body.retention_keep_days,
        enabled=True,
        created_by=None,
    )
    schedule.next_run_at = compute_next_run(schedule, after=_utcnow())
    db.add(schedule)
    db.commit()
    db.refresh(schedule)
    audit.record(
        action="schedule.create",
        summary=f"Created schedule {schedule.name!r} ({schedule.action_name})",
        entity_type="schedule",
        entity_id=schedule.id,
        params={
            "action_name": schedule.action_name,
            "target_id": schedule.target_id,
            "cron": schedule.cron,
            "interval_seconds": schedule.interval_seconds,
        },
    )
    return _out(db, schedule)


@router.patch("/{schedule_id}", response_model=ScheduleOut)
def update_schedule(
    schedule_id: int,
    body: UpdateScheduleRequest,
    db: DbSession,
    audit: Audit,
    _: Annotated[object, Depends(require(SCHEDULE_MANAGE))],
) -> ScheduleOut:
    schedule = db.get(Schedule, schedule_id)
    if schedule is None:
        raise HTTPException(status_code=404, detail="Schedule not found.")
    fields = body.model_dump(exclude_unset=True)

    if body.priority is not None and body.priority not in VALID_PRIORITIES:
        raise HTTPException(status_code=422, detail="priority must be high|default|low.")

    for key in ("name", "priority", "with_files", "enabled"):
        if key in fields and fields[key] is not None:
            setattr(schedule, key, fields[key])
    if body.retention_keep_last is not None:
        schedule.retention_keep_last = body.retention_keep_last
    if body.clear_keep_last:
        schedule.retention_keep_last = None
    if body.retention_keep_days is not None:
        schedule.retention_keep_days = body.retention_keep_days
    if body.clear_keep_days:
        schedule.retention_keep_days = None

    cadence_touched = any(k in fields for k in ("cron", "interval_seconds", "timezone"))
    if cadence_touched:
        new_cron = fields.get("cron", schedule.cron)
        new_interval = fields.get("interval_seconds", schedule.interval_seconds)
        new_tz = fields.get("timezone", schedule.timezone) or schedule.timezone
        try:
            validate_cadence(
                cron=new_cron, interval_seconds=new_interval, timezone=new_tz
            )
        except ScheduleError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        schedule.cron = new_cron
        schedule.interval_seconds = new_interval
        schedule.timezone = new_tz

    _validate_action_params(
        action_name=schedule.action_name,
        with_files=schedule.with_files,
        keep_last=schedule.retention_keep_last,
        keep_days=schedule.retention_keep_days,
    )

    # Recompute the next fire whenever cadence changed or the schedule was
    # (re-)enabled — from now, so a re-enable doesn't fire a stale backlog.
    if schedule.enabled and (cadence_touched or fields.get("enabled") is True):
        schedule.next_run_at = compute_next_run(schedule, after=_utcnow())
    db.commit()
    db.refresh(schedule)
    audit.record(
        action="schedule.update",
        summary=f"Updated schedule {schedule.name!r}",
        entity_type="schedule",
        entity_id=schedule.id,
        params={k: v for k, v in fields.items()},
    )
    return _out(db, schedule)


@router.post("/{schedule_id}/enabled", response_model=ScheduleOut)
def set_enabled(
    schedule_id: int,
    body: SetEnabledRequest,
    db: DbSession,
    audit: Audit,
    _: Annotated[object, Depends(require(SCHEDULE_MANAGE))],
) -> ScheduleOut:
    schedule = db.get(Schedule, schedule_id)
    if schedule is None:
        raise HTTPException(status_code=404, detail="Schedule not found.")
    schedule.enabled = body.enabled
    # Re-enabling recomputes next_run from now (no stale backlog); disabling
    # simply makes it non-due (its next_run is ignored while disabled).
    if body.enabled:
        schedule.next_run_at = compute_next_run(schedule, after=_utcnow())
    db.commit()
    db.refresh(schedule)
    audit.record(
        action="schedule.enabled" if body.enabled else "schedule.disabled",
        summary=f"{'Enabled' if body.enabled else 'Disabled'} schedule {schedule.name!r}",
        entity_type="schedule",
        entity_id=schedule.id,
    )
    return _out(db, schedule)


@router.delete("/{schedule_id}", status_code=204)
def delete_schedule(
    schedule_id: int,
    db: DbSession,
    audit: Audit,
    _: Annotated[object, Depends(require(SCHEDULE_MANAGE))],
) -> None:
    schedule = db.get(Schedule, schedule_id)
    if schedule is None:
        raise HTTPException(status_code=404, detail="Schedule not found.")
    name = schedule.name
    db.delete(schedule)
    db.commit()
    audit.record(
        action="schedule.delete",
        summary=f"Deleted schedule {name!r}",
        entity_type="schedule",
        entity_id=schedule_id,
    )


# --------------------------------------------------------------------------- #
# Run now
# --------------------------------------------------------------------------- #


@router.post("/{schedule_id}/run-now", status_code=201, response_model=JobDetail)
def run_now(
    schedule_id: int,
    db: DbSession,
    runner: Runner,
    user: CurrentUser,
) -> JobDetail:
    """Fire the schedule's action immediately as a CommandJob, without disturbing
    the periodic `next_run_at`. Requires the underlying action's permission so an
    Operator can trigger a backup/sweep they couldn't otherwise reshape."""
    schedule = db.get(Schedule, schedule_id)
    if schedule is None:
        raise HTTPException(status_code=404, detail="Schedule not found.")

    template = get_template(schedule.action_name)
    if not role_allows(list(user.role.permissions or []), template.required_permission):
        raise HTTPException(
            status_code=403,
            detail=f"Role {user.role.name!r} lacks the "
            f"{template.required_permission!r} permission to run this schedule.",
        )
    # A manual run-now is attributed to the operator who pressed it (the automated
    # tick uses the schedule owner instead).
    try:
        job = fire_schedule(db, runner, schedule, now=_utcnow(), created_by=user.id)
    except LockConflict as exc:
        return _conflict(exc, "A job is already running on this schedule's target.")
    except ScheduleError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    db.refresh(job)
    return JobDetail.from_model(job)
