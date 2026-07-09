"""Bench inventory API (session 1.6).

- GET  /api/benches               list all discovered benches (+?server= filter)
- GET  /api/benches/{id}          one bench (detail page)
- POST /api/servers/{id}/discover-benches   launch a discovery job

Discovery is long-ish remote work, so the POST enqueues a `bench.discover` job
and returns it immediately (rule 3); the worker does the SSH walk and upserts
the rows. Listing is read-only (RBAC `read`); launching needs `server:manage`,
declared by the template and checked here the same way POST /api/jobs does.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, require
from app.api.routes.jobs import get_job_runner
from app.core.commands import RenderError, get_template
from app.core.discovery import DiscoveryError, validate_base_paths
from app.core.jobs import JobRunner, LockConflict
from app.core.permissions import READ, role_allows
from app.db import get_db
from app.models import Server
from app.models.bench import Bench
from app.schemas.bench import BenchOut, DiscoverRequest
from app.schemas.job import JobDetail

router = APIRouter(prefix="/api", tags=["benches"])

DbSession = Annotated[Session, Depends(get_db)]
Runner = Annotated[JobRunner, Depends(get_job_runner)]

DISCOVER_ACTION = "bench.discover"


@router.get("/benches", response_model=list[BenchOut])
def list_benches(
    db: DbSession,
    _: Annotated[object, Depends(require(READ))],
    server: int | None = Query(default=None),
) -> list[BenchOut]:
    """All benches, newest-discovered first, optionally scoped to one server.
    The frontend groups the flat list by server_id."""
    stmt = select(Bench).order_by(Bench.server_id, Bench.name)
    if server is not None:
        stmt = stmt.where(Bench.server_id == server)
    return [BenchOut.from_model(b) for b in db.scalars(stmt).all()]


@router.get("/benches/{bench_id}", response_model=BenchOut)
def get_bench(
    bench_id: int, db: DbSession, _: Annotated[object, Depends(require(READ))]
) -> BenchOut:
    bench = db.get(Bench, bench_id)
    if bench is None:
        raise HTTPException(status_code=404, detail="Bench not found.")
    return BenchOut.from_model(bench)


@router.post(
    "/servers/{server_id}/discover-benches", status_code=201, response_model=JobDetail
)
def discover_benches(
    server_id: int,
    body: DiscoverRequest,
    db: DbSession,
    runner: Runner,
    user: CurrentUser,
):
    """Launch a bench-discovery job for one server."""
    if db.get(Server, server_id) is None:
        raise HTTPException(status_code=404, detail="Server not found.")

    template = get_template(DISCOVER_ACTION)
    if not role_allows(list(user.role.permissions or []), template.required_permission):
        raise HTTPException(
            status_code=403,
            detail=f"Role {user.role.name!r} lacks the "
            f"{template.required_permission!r} permission to discover benches.",
        )

    try:
        paths = validate_base_paths(body.base_paths or None)
    except DiscoveryError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    # Only pass base_paths when the caller overrode the defaults, so the action
    # applies the server defaults (incl. $HOME) when none were given.
    params = {"base_paths": ",".join(paths)} if body.base_paths else {}

    try:
        job = runner.create(
            db,
            action_name=DISCOVER_ACTION,
            server_id=server_id,
            target_type="server",
            target_id=None,
            params=params,
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
                    "message": f"A discovery job is already running on this "
                    f"server (blocking job {exc.blocking_job_id}).",
                    "blocking_job_id": exc.blocking_job_id,
                }
            },
        )

    db.refresh(job)
    return JobDetail.from_model(job)
