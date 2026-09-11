"""Job engine API (session 1.3).

- POST   /api/jobs            launch an action (RBAC gated per template)
- GET    /api/jobs            list with filters (status/server/target/user/mine)
- GET    /api/jobs/{id}       one job with its steps + sanitized params
- POST   /api/jobs/{id}/cancel   cancel (job:manage)
- POST   /api/jobs/{id}/retry    re-run a terminal job (job:manage)

Long work never runs in the request (rule 3): POST returns the pending job while
the RQ worker executes it. Read-only users can never launch/cancel/retry (rule 7).
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.api.deps import CurrentUser, require
from app.core.commands import RenderError, UnknownAction, get_template
from app.core.jobs import JobRunner, LockConflict, MaintenanceWindowBlocked, build_runner
from app.core.permissions import JOB_MANAGE, READ, role_allows
from app.db import get_db
from app.models import CommandJob, Server
from app.schemas.job import JobCreate, JobDetail, JobOut

router = APIRouter(prefix="/api/jobs", tags=["jobs"])

DbSession = Annotated[Session, Depends(get_db)]


def get_job_runner() -> JobRunner:
    """FastAPI dependency: the production runner (real Redis + RQ + SSH).
    Overridden in tests with an in-memory backend and a no-op enqueue."""
    return build_runner()


Runner = Annotated[JobRunner, Depends(get_job_runner)]


def _load_job(db: Session, job_id: int, *, with_steps: bool = False) -> CommandJob:
    stmt = select(CommandJob).where(CommandJob.id == job_id)
    if with_steps:
        stmt = stmt.options(joinedload(CommandJob.steps))
    job = db.scalars(stmt).unique().first()
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    return job


@router.post("", status_code=201, response_model=JobDetail)
def create_job(body: JobCreate, db: DbSession, runner: Runner, user: CurrentUser):
    try:
        template = get_template(body.action_name)
    except UnknownAction as exc:
        raise HTTPException(
            status_code=404, detail=f"Unknown action {body.action_name!r}."
        ) from exc

    # RBAC is per-template (rule 7): the action declares the permission it needs.
    if not role_allows(list(user.role.permissions or []), template.required_permission):
        raise HTTPException(
            status_code=403,
            detail=f"Role {user.role.name!r} lacks the "
            f"{template.required_permission!r} permission for this action.",
        )

    if db.get(Server, body.server_id) is None:
        raise HTTPException(status_code=404, detail="Server not found.")

    try:
        job = runner.create(
            db,
            action_name=body.action_name,
            server_id=body.server_id,
            target_type=body.target_type,
            target_id=body.target_id,
            params=body.params,
            priority=body.priority,
            created_by=user.id,
        )
    except RenderError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except LockConflict as exc:
        return JSONResponse(
            status_code=409,
            content={
                "error": {
                    "code": "conflict",
                    "message": f"A job is already running on this target "
                    f"(blocking job {exc.blocking_job_id}).",
                    "blocking_job_id": exc.blocking_job_id,
                }
            },
        )
    except MaintenanceWindowBlocked as exc:
        return JSONResponse(
            status_code=409,
            content={
                "error": {
                    "code": "maintenance_window_blocked",
                    "message": str(exc),
                    "window_id": exc.window_id,
                    "window_name": exc.window_name,
                    "danger_class": exc.danger_class,
                }
            },
        )
    return JobDetail.from_model(_load_job(db, job.id, with_steps=True))


@router.get("", response_model=list[JobOut])
def list_jobs(
    db: DbSession,
    user: CurrentUser,
    _: Annotated[object, Depends(require(READ))],
    status: str | None = None,
    server: int | None = Query(default=None),
    target: str | None = None,
    created_by: int | None = Query(default=None, alias="user"),
    mine: bool = False,
    limit: int = Query(default=100, ge=1, le=500),
) -> list[JobOut]:
    stmt = select(CommandJob).order_by(CommandJob.created_at.desc()).limit(limit)
    if status is not None:
        stmt = stmt.where(CommandJob.status == status)
    if server is not None:
        stmt = stmt.where(CommandJob.server_id == server)
    if target is not None:
        stmt = stmt.where(CommandJob.target_id == target)
    if mine:
        stmt = stmt.where(CommandJob.created_by == user.id)
    elif created_by is not None:
        stmt = stmt.where(CommandJob.created_by == created_by)
    return [JobOut.from_model(j) for j in db.scalars(stmt).all()]


@router.get("/{job_id}", response_model=JobDetail)
def get_job(
    job_id: int, db: DbSession, _: Annotated[object, Depends(require(READ))]
) -> JobDetail:
    return JobDetail.from_model(_load_job(db, job_id, with_steps=True))


@router.post("/{job_id}/cancel", response_model=JobDetail)
def cancel_job(
    job_id: int,
    db: DbSession,
    runner: Runner,
    _: Annotated[object, Depends(require(JOB_MANAGE))],
) -> JobDetail:
    job = _load_job(db, job_id)
    runner.cancel(db, job)
    return JobDetail.from_model(_load_job(db, job_id, with_steps=True))


@router.post("/{job_id}/retry", status_code=201, response_model=JobDetail)
def retry_job(
    job_id: int,
    db: DbSession,
    runner: Runner,
    user: CurrentUser,
    _: Annotated[object, Depends(require(JOB_MANAGE))],
):
    job = _load_job(db, job_id)
    try:
        new_job = runner.retry(db, job, created_by=user.id)
    except RenderError as exc:
        # A secret-bearing template can't be re-rendered from masked params
        # (SecretParamUnresolved) — a validation problem, not a state conflict.
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except LockConflict as exc:
        return JSONResponse(
            status_code=409,
            content={
                "error": {
                    "code": "conflict",
                    "message": f"A job is already running on this target "
                    f"(blocking job {exc.blocking_job_id}).",
                    "blocking_job_id": exc.blocking_job_id,
                }
            },
        )
    except MaintenanceWindowBlocked as exc:
        return JSONResponse(
            status_code=409,
            content={
                "error": {
                    "code": "maintenance_window_blocked",
                    "message": str(exc),
                    "window_id": exc.window_id,
                    "window_name": exc.window_name,
                    "danger_class": exc.danger_class,
                }
            },
        )
    return JobDetail.from_model(_load_job(db, new_job.id, with_steps=True))
