"""Reports suite API (session 6.2, uiux-spec B4.16).

  GET  /api/reports                     the catalogue this role may run
  POST /api/reports/{report_id}/run     generate one report (202 + job, or a
                                        synchronous small-CSV export)
  GET  /api/report-runs                 recent generated runs (metadata)
  GET  /api/report-runs/{id}/download   stream a generated artifact

RBAC is per report, read from the catalogue (golden rule 7): the two ISO-facing
exports — `backup_evidence` and `user_activity` — declare `REPORT_SENSITIVE`,
which only Admin holds, so a Read-only user gets 403 on them and sees them in
neither the catalogue nor the run list. Every non-sensitive report is gated on
plain READ.

Long work never runs in the request (golden rule 3): a run is enqueued as a
`report.generate` CommandJob and the endpoint returns 202 with the job id. The
one exception is a small, bounded CSV export the caller explicitly asks to run
inline (`?sync=true`) — cheap enough to render in-process and hand straight
back as a download reference. PDF and any emailed run always go through the job.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser
from app.api.routes.jobs import get_job_runner
from app.core.commands import RenderError
from app.core.jobs import JobRunner, LockConflict
from app.core.permissions import role_allows
from app.core.reports import (
    ReportError,
    artifact_path,
    filename_for,
    generate,
    get_report,
    visible_reports,
)
from app.core.reports.registry import ReportDef
from app.db import get_db
from app.models import ReportRun

router = APIRouter(prefix="/api", tags=["reports"])

DbSession = Annotated[Session, Depends(get_db)]
Runner = Annotated[JobRunner, Depends(get_job_runner)]

_MEDIA_TYPES = {"csv": "text/csv", "pdf": "application/pdf"}


# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #


class ReportParamOut(BaseModel):
    name: str
    kind: str
    label: str
    required: bool
    default: Any = None
    enum: list[str] | None = None
    min: int | None = None
    max: int | None = None


class ReportOut(BaseModel):
    id: str
    title: str
    description: str
    required_permission: str
    evidence: bool
    params: list[ReportParamOut]


class ReportRunOut(BaseModel):
    id: int
    report_id: str
    format: str
    status: str
    params: dict[str, Any]
    row_count: int | None
    artifact_bytes: int | None
    sha256: str | None
    error: str | None
    requested_by: int | None
    job_id: int | None
    created_at: str | None
    completed_at: str | None
    download_url: str | None

    @classmethod
    def from_model(cls, run: ReportRun) -> ReportRunOut:
        downloadable = run.status == "success" and run.artifact_path is not None
        return cls(
            id=run.id,
            report_id=run.report_id,
            format=run.format,
            status=run.status,
            params=dict(run.params or {}),
            row_count=run.row_count,
            artifact_bytes=run.artifact_bytes,
            sha256=run.sha256,
            error=run.error,
            requested_by=run.requested_by,
            job_id=run.job_id,
            created_at=run.created_at.isoformat() if run.created_at else None,
            completed_at=run.completed_at.isoformat() if run.completed_at else None,
            download_url=(
                f"/api/report-runs/{run.id}/download" if downloadable else None
            ),
        )


class RunRequest(BaseModel):
    format: str = "csv"
    params: dict[str, Any] = {}
    # Comma-separated recipient list; when present the run always goes through
    # the job so the email is sent by the worker, never in the request.
    recipients: str | None = None


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _report_or_404(report_id: str) -> ReportDef:
    try:
        return get_report(report_id)
    except ReportError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _authorize(report: ReportDef, user: CurrentUser) -> None:
    """403 unless the caller's role holds the report's declared permission."""
    perms = list(user.role.permissions or [])
    if not role_allows(perms, report.required_permission):
        raise HTTPException(
            status_code=403,
            detail=(
                f"Role {user.role.name!r} may not run the {report.id!r} report."
            ),
        )


def _param_out(report: ReportDef) -> list[ReportParamOut]:
    return [
        ReportParamOut(
            name=spec.name,
            kind=spec.kind,
            label=spec.label,
            required=spec.required,
            default=spec.default,
            enum=list(spec.enum) if spec.enum else None,
            min=spec.min,
            max=spec.max,
        )
        for spec in report.params
    ]


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #


@router.get("/reports", response_model=list[ReportOut])
def list_reports(db: DbSession, user: CurrentUser) -> list[ReportOut]:
    """The catalogue entries this role may run (sensitive reports are hidden
    from roles that cannot run them)."""
    perms = list(user.role.permissions or [])
    return [
        ReportOut(
            id=r.id,
            title=r.title,
            description=r.description,
            required_permission=r.required_permission,
            evidence=r.evidence,
            params=_param_out(r),
        )
        for r in visible_reports(perms)
    ]


@router.post("/reports/{report_id}/run")
def run_report(
    report_id: str,
    body: RunRequest,
    db: DbSession,
    user: CurrentUser,
    runner: Runner,
    sync: bool = Query(default=False),
):
    """Generate a report.

    Default: enqueue a `report.generate` job and return 202 with its id. When
    `sync=true` for a CSV run with no recipients, render in-process and return
    200 with the completed `ReportRun` (small, bounded exports only)."""
    report = _report_or_404(report_id)
    _authorize(report, user)

    fmt = (body.format or "csv").lower()
    if fmt not in ("csv", "pdf"):
        raise HTTPException(status_code=422, detail=f"unsupported format {fmt!r}")

    # Validate the report parameters up front so a bad value fails the request
    # rather than the job.
    try:
        resolved = report.coerce_params(body.params)
    except ReportError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    recipients = (body.recipients or "").strip()

    # Synchronous path: a small bounded CSV the caller explicitly opted into,
    # with nothing to email. Everything else is a job (rule 3).
    if sync and fmt == "csv" and not recipients:
        try:
            run = generate(
                db,
                report_id=report.id,
                params=resolved,
                fmt="csv",
                requested_by=user.id,
            )
        except ReportError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return JSONResponse(status_code=200, content=ReportRunOut.from_model(run).model_dump())

    # Async path: hand the render to the job engine. `report.generate` is a
    # platform-local template — no server, target_type "report".
    job_params: dict[str, Any] = {
        "report_id": report.id,
        "format": fmt,
        "requested_by": str(user.id),
    }
    if recipients:
        job_params["recipients"] = recipients
    for key in ("range_days", "within_days", "status"):
        if resolved.get(key) not in (None, ""):
            job_params[key] = str(resolved[key])

    try:
        job = runner.create(
            db,
            action_name="report.generate",
            server_id=None,
            target_type="report",
            target_id=report.id,
            params=job_params,
            priority="default",
            created_by=user.id,
        )
    except RenderError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except LockConflict as exc:  # pragma: no cover - report.generate takes no lock
        return JSONResponse(
            status_code=409,
            content={"error": {"code": "conflict", "blocking_job_id": exc.blocking_job_id}},
        )
    return JSONResponse(status_code=202, content={"job_id": job.id, "report_id": report.id})


@router.get("/report-runs", response_model=list[ReportRunOut])
def list_report_runs(
    db: DbSession,
    user: CurrentUser,
    report_id: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[ReportRunOut]:
    """Recent runs the caller is allowed to see. A run for a report the role
    cannot run is filtered out, so a Read-only user never learns that a
    `user_activity` export was generated."""
    perms = list(user.role.permissions or [])
    stmt = select(ReportRun).order_by(ReportRun.id.desc())
    if report_id:
        stmt = stmt.where(ReportRun.report_id == report_id)
    stmt = stmt.limit(limit)

    out: list[ReportRunOut] = []
    for run in db.scalars(stmt):
        try:
            report = get_report(run.report_id)
        except ReportError:
            continue  # a run for a report that has since been retired
        if not role_allows(perms, report.required_permission):
            continue
        out.append(ReportRunOut.from_model(run))
    return out


@router.get("/report-runs/{run_id}/download")
def download_report_run(run_id: int, db: DbSession, user: CurrentUser):
    """Stream a generated artifact, gated on the run's report permission."""
    run = db.get(ReportRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Report run not found.")
    report = _report_or_404(run.report_id)
    _authorize(report, user)
    if run.status != "success":
        raise HTTPException(status_code=409, detail=f"run is {run.status}, not downloadable")

    try:
        path = artifact_path(run)
    except ReportError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    return FileResponse(
        path,
        media_type=_MEDIA_TYPES.get(run.format, "application/octet-stream"),
        filename=filename_for(run),
    )
