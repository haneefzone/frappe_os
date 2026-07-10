"""Bench inventory API (session 1.6).

- GET  /api/benches               list all discovered benches (+?server= filter)
- GET  /api/benches/{id}          one bench (detail page)
- POST /api/servers/{id}/discover-benches   launch a discovery job

Discovery is long-ish remote work, so the POST enqueues a `bench.discover` job
and returns it immediately (rule 3); the worker does the SSH walk and upserts
the rows. Listing is read-only (RBAC `read`); launching needs `server:manage`,
declared by the template and checked here the same way POST /api/jobs does.
"""

import posixpath
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
from app.core.version_matrix import MATRIX
from app.db import get_db
from app.models import Server
from app.models.bench import Bench
from app.schemas.bench import (
    BenchActionRequest,
    BenchOut,
    CreateBenchRequest,
    DiscoverRequest,
    PreflightRequest,
    VersionMatrixEntryOut,
    VersionMatrixOut,
)
from app.schemas.job import JobDetail

router = APIRouter(prefix="/api", tags=["benches"])

DbSession = Annotated[Session, Depends(get_db)]
Runner = Annotated[JobRunner, Depends(get_job_runner)]

DISCOVER_ACTION = "bench.discover"
PREFLIGHT_ACTION = "bench.preflight"
CREATE_ACTION = "bench.create"
BUILD_ACTION = "bench.build"
RESTART_ACTION = "bench.restart"
MIGRATE_ALL_ACTION = "bench.migrate_all"
UPDATE_ACTION = "bench.update"


def _require_action_permission(user, action_name: str) -> None:
    """Enforce the RBAC action-class the template declares (golden rule 7), the
    same gate POST /api/jobs applies."""
    template = get_template(action_name)
    if not role_allows(list(user.role.permissions or []), template.required_permission):
        raise HTTPException(
            status_code=403,
            detail=f"Role {user.role.name!r} lacks the "
            f"{template.required_permission!r} permission for this action.",
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


@router.get("/benches/version-matrix", response_model=VersionMatrixOut)
def version_matrix(_: Annotated[object, Depends(require(READ))]) -> VersionMatrixOut:
    """The Frappe version matrix (single source of truth). The Create-Bench
    wizard renders its radio cards from this so the UI and the server-side
    pre-flight can never disagree. Declared before `/benches/{bench_id}` so the
    literal path wins over the int route."""
    return VersionMatrixOut(
        entries=[
            VersionMatrixEntryOut(
                major=e.major,
                branch=e.branch,
                python=e.python_display,
                node=e.node_display,
                mariadb=e.mariadb_display,
                tooling=e.tooling,
                line=e.line,
            )
            for e in MATRIX
        ]
    )


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


@router.post("/benches/preflight", status_code=201, response_model=JobDetail)
def preflight_bench(
    body: PreflightRequest, db: DbSession, runner: Runner, user: CurrentUser
):
    """Launch the wizard's live, re-runnable pre-flight for a candidate bench.
    Read-only probes; the job always finishes, and its `PREFLIGHT_RESULT` log
    line carries the verdict the wizard renders."""
    if db.get(Server, body.server_id) is None:
        raise HTTPException(status_code=404, detail="Server not found.")
    _require_action_permission(user, PREFLIGHT_ACTION)

    try:
        job = runner.create(
            db,
            action_name=PREFLIGHT_ACTION,
            server_id=body.server_id,
            target_type="server",
            target_id=None,
            params={"frappe_version": body.frappe_version, "path": body.path},
            priority=body.priority,
            created_by=user.id,
        )
    except RenderError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except LockConflict as exc:  # lock-free template, but stay defensive
        return _conflict(exc, "A pre-flight is already running for this target.")

    db.refresh(job)
    return JobDetail.from_model(job)


@router.post("/benches", status_code=201, response_model=JobDetail)
def create_bench(
    body: CreateBenchRequest, db: DbSession, runner: Runner, user: CurrentUser
):
    """Create a bench end to end (session 1.7): one `bench.create` job runs the
    pre-flight, then `bench init`, then registers the new bench. A blocking
    pre-flight failure fails the job before init (never a half-install). The
    frontend navigates to the returned job's detail to watch it stream."""
    if db.get(Server, body.server_id) is None:
        raise HTTPException(status_code=404, detail="Server not found.")
    _require_action_permission(user, CREATE_ACTION)

    # Lock keyed on the concrete new bench path so two creates of the same bench
    # conflict, while different benches on the server run independently (rule 4).
    target_id = posixpath.join(body.path, body.name)

    try:
        job = runner.create(
            db,
            action_name=CREATE_ACTION,
            server_id=body.server_id,
            target_type="bench",
            target_id=target_id,
            params={
                "frappe_version": body.frappe_version,
                "name": body.name,
                "path": body.path,
            },
            priority=body.priority,
            created_by=user.id,
        )
    except RenderError as exc:
        # Rejects an invalid version / bad name / bad path (injection guard) at
        # the boundary — nothing runs on the server.
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except LockConflict as exc:
        return _conflict(
            exc, f"A create job is already running for bench {target_id!r}."
        )

    db.refresh(job)
    return JobDetail.from_model(job)


# --- Maintenance actions (session 1.10) --------------------------------- #


def _launch_bench_action(
    db: Session,
    runner: JobRunner,
    user,
    *,
    bench: Bench,
    action_name: str,
    priority: str,
):
    """Shared launcher for the parameter-free bench maintenance ops (build /
    restart / migrate-all / update): enqueue the orchestrator job locked on the
    bench path and return it."""
    _require_action_permission(user, action_name)
    try:
        job = runner.create(
            db,
            action_name=action_name,
            server_id=bench.server_id,
            target_type="bench",
            target_id=bench.path,
            params={"bench_path": bench.path},
            priority=priority,
            created_by=user.id,
        )
    except RenderError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except LockConflict as exc:
        return _conflict(exc, f"A job is already running on bench {bench.path!r}.")
    db.refresh(job)
    return JobDetail.from_model(job)


def _get_bench_or_404(db: Session, bench_id: int) -> Bench:
    bench = db.get(Bench, bench_id)
    if bench is None:
        raise HTTPException(status_code=404, detail="Bench not found.")
    return bench


@router.post("/benches/{bench_id}/build", status_code=201, response_model=JobDetail)
def build_bench(
    bench_id: int,
    body: BenchActionRequest,
    db: DbSession,
    runner: Runner,
    user: CurrentUser,
):
    """Run `bench build` to (re)compile the bench's assets."""
    bench = _get_bench_or_404(db, bench_id)
    return _launch_bench_action(
        db,
        runner,
        user,
        bench=bench,
        action_name=BUILD_ACTION,
        priority=body.priority or "default",
    )


@router.post("/benches/{bench_id}/restart", status_code=201, response_model=JobDetail)
def restart_bench(
    bench_id: int,
    body: BenchActionRequest,
    db: DbSession,
    runner: Runner,
    user: CurrentUser,
):
    """Restart the bench's services. Production benches restart via
    `sudo supervisorctl restart`; dev benches fail with an informative message
    (they run via a manual `bench start`)."""
    bench = _get_bench_or_404(db, bench_id)
    return _launch_bench_action(
        db,
        runner,
        user,
        bench=bench,
        action_name=RESTART_ACTION,
        priority=body.priority or "high",
    )


@router.post(
    "/benches/{bench_id}/migrate-all", status_code=201, response_model=JobDetail
)
def migrate_all_sites(
    bench_id: int,
    body: BenchActionRequest,
    db: DbSession,
    runner: Runner,
    user: CurrentUser,
):
    """Migrate every known active site on the bench, each as its own step."""
    bench = _get_bench_or_404(db, bench_id)
    return _launch_bench_action(
        db,
        runner,
        user,
        bench=bench,
        action_name=MIGRATE_ALL_ACTION,
        priority=body.priority or "default",
    )


@router.post("/benches/{bench_id}/update", status_code=201, response_model=JobDetail)
def update_bench(
    bench_id: int,
    body: BenchActionRequest,
    db: DbSession,
    runner: Runner,
    user: CurrentUser,
):
    """Update the whole bench (long-running, high queue). An automatic db-only
    safety backup of every site runs first."""
    bench = _get_bench_or_404(db, bench_id)
    return _launch_bench_action(
        db,
        runner,
        user,
        bench=bench,
        action_name=UPDATE_ACTION,
        priority=body.priority or "high",
    )
