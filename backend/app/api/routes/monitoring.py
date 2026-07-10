"""Monitoring API (session 1.12): per-server samples + service restart.

- GET  /api/servers/{id}/monitoring          a time window of samples + latest.
- POST /api/servers/{id}/services/{name}/restart  restart a managed service ->
       an audited `server.restart_service` job (sudoers allowlist).

Samples are read-only telemetry (written by the background poller). The restart
is a mutating command: it goes through the JobRunner (rule 2/3 — job + audit +
lock), returns `202 {job_id}` and streams like any other job.
"""

from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, require
from app.api.routes.jobs import get_job_runner
from app.core.commands import RenderError
from app.core.commands.registry import SERVICE_NAMES
from app.core.jobs import JobRunner, LockConflict
from app.core.permissions import READ, SERVER_MANAGE, role_allows
from app.db import get_db
from app.models import MonitoringSample, Server
from app.schemas.job import JobDetail
from app.schemas.monitoring import MonitoringSampleOut, MonitoringSeries

router = APIRouter(prefix="/api/servers", tags=["monitoring"])

DbSession = Annotated[Session, Depends(get_db)]
Runner = Annotated[JobRunner, Depends(get_job_runner)]

RESTART_ACTION = "server.restart_service"


def _get_server(db: Session, server_id: int) -> Server:
    server = db.get(Server, server_id)
    if server is None:
        raise HTTPException(status_code=404, detail="Server not found.")
    return server


@router.get("/{server_id}/monitoring", response_model=MonitoringSeries)
def server_monitoring(
    server_id: int,
    db: DbSession,
    _: Annotated[object, Depends(require(READ))],
    hours: int = Query(24, ge=1, le=720),
) -> MonitoringSeries:
    """Samples for the last `hours` (charts) plus the most recent point (gauges)."""
    _get_server(db, server_id)
    since = datetime.now(UTC) - timedelta(hours=hours)
    rows = list(
        db.scalars(
            select(MonitoringSample)
            .where(
                MonitoringSample.server_id == server_id,
                MonitoringSample.ts >= since,
            )
            .order_by(MonitoringSample.ts.asc())
        ).all()
    )
    latest = rows[-1] if rows else db.scalars(
        select(MonitoringSample)
        .where(MonitoringSample.server_id == server_id)
        .order_by(MonitoringSample.ts.desc())
        .limit(1)
    ).first()
    return MonitoringSeries(
        server_id=server_id,
        latest=MonitoringSampleOut.from_model(latest) if latest else None,
        samples=[MonitoringSampleOut.from_model(r) for r in rows],
    )


@router.post(
    "/{server_id}/services/{service}/restart",
    status_code=201,
    response_model=JobDetail,
)
def restart_service(
    server_id: int,
    service: str,
    db: DbSession,
    runner: Runner,
    user: CurrentUser,
) -> JobDetail:
    """Restart one managed service on the server (`server:manage`)."""
    if not role_allows(list(user.role.permissions or []), SERVER_MANAGE):
        raise HTTPException(
            status_code=403,
            detail=f"Role '{user.role.name}' lacks the '{SERVER_MANAGE}' permission.",
        )
    if service not in SERVICE_NAMES:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown service {service!r}; expected one of {list(SERVICE_NAMES)}.",
        )
    server = _get_server(db, server_id)
    try:
        job = runner.create(
            db,
            action_name=RESTART_ACTION,
            server_id=server.id,
            target_type="server",
            target_id=f"{server.id}::{service}",
            params={"service": service},
            priority="high",
            created_by=user.id,
        )
    except RenderError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except LockConflict as exc:
        return JSONResponse(
            status_code=409,
            content={
                "detail": f"A restart of {service} is already running on this server.",
                "blocking_job_id": exc.blocking_job_id,
            },
        )
    db.refresh(job)
    return JobDetail.from_model(job)
