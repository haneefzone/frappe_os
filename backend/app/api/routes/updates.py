"""Safe update pipeline API (session 3.3, uiux §2/§5).

A production update never touches prod directly. Instead:

- POST /api/update-pipelines                     clone source site -> staging site
- POST /api/update-pipelines/{id}/update-staging update the staging clone
- POST /api/update-pipelines/{id}/verify         run the verification checklist
- POST /api/update-pipelines/{id}/promote        pre-backup -> update prod -> post-check
- GET  /api/update-pipelines[/{id}]              inspect runs
- POST /api/sites/{id}/environment              classify a site dev|staging|prod

Every mutating step enqueues a job and returns it (rule 3). Promoting to a `prod`
site is destructive: it requires the `danger` permission, the typed source-site
name, a per-task client sign-off, AND a green verification checklist — enforced
here, server-side. The pre-update backup gate is enforced inside the promote job
itself, so prod is never mutated without a recoverable backup taken first.
"""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, require
from app.api.routes.jobs import get_job_runner
from app.core.commands import RenderError, get_template
from app.core.jobs import JobRunner, LockConflict
from app.core.permissions import DANGER, READ, SITE_OPERATE, role_allows
from app.core.secrets_resolve import SecretResolutionError
from app.db import get_db
from app.models.bench import Bench
from app.models.site import SITE_ENVIRONMENTS, Site
from app.models.update_pipeline import UpdatePipeline
from app.schemas.job import JobDetail
from app.schemas.update import (
    CreatePipelineRequest,
    PipelineOut,
    PromoteRequest,
    SetEnvironmentRequest,
    VerifyRequest,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["updates"])

DbSession = Annotated[Session, Depends(get_db)]
Runner = Annotated[JobRunner, Depends(get_job_runner)]

CLONE_ACTION = "site.clone_to_staging"
UPDATE_ACTION = "bench.update"
VERIFY_ACTION = "site.verify_checklist"
PROMOTE_ACTION = "site.promote_update"


def _require_action_permission(user, action_name: str) -> None:
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
        content={"error": {"code": "conflict", "message": message,
                           "blocking_job_id": exc.blocking_job_id}},
    )


def _load_pipeline(db: Session, pipeline_id: int) -> UpdatePipeline:
    p = db.get(UpdatePipeline, pipeline_id)
    if p is None:
        raise HTTPException(status_code=404, detail="Update pipeline not found.")
    return p


def _bench(db: Session, bench_id: int) -> Bench:
    bench = db.get(Bench, bench_id)
    if bench is None:  # pragma: no cover - FK-guaranteed at call sites
        raise HTTPException(status_code=404, detail="Bench not found.")
    return bench


# --------------------------------------------------------------------------- #
# Site environment classification
# --------------------------------------------------------------------------- #


@router.post("/sites/{site_id}/environment", response_model=dict)
def set_environment(
    site_id: int,
    body: SetEnvironmentRequest,
    db: DbSession,
    user: Annotated[object, Depends(require(SITE_OPERATE))],
) -> dict:
    """Classify a site as dev|staging|prod (drives the EnvironmentBadge + the
    prod-update guardrails)."""
    if body.environment not in SITE_ENVIRONMENTS:
        raise HTTPException(
            status_code=422,
            detail=f"environment must be one of {list(SITE_ENVIRONMENTS)}.",
        )
    site = db.get(Site, site_id)
    if site is None:
        raise HTTPException(status_code=404, detail="Site not found.")
    site.environment = body.environment
    db.commit()
    return {"id": site.id, "environment": site.environment}


# --------------------------------------------------------------------------- #
# Create pipeline (clone prod -> staging)
# --------------------------------------------------------------------------- #


@router.post("/update-pipelines", status_code=201, response_model=PipelineOut)
def create_pipeline(
    body: CreatePipelineRequest, db: DbSession, runner: Runner, user: CurrentUser
):
    """Start a safe-update run: back up the source site and restore it into a new
    staging site (optionally scrubbing PII), then track the run."""
    _require_action_permission(user, CLONE_ACTION)  # backup:restore

    source = db.get(Site, body.source_site_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Source site not found.")

    # A.8.11 Data masking: a clone carries the source's live db + files (incl.
    # PII) into a *non-production* staging site. When the source is classified
    # `prod`, refuse to clone unmasked unless the caller either supplies a
    # `scrub_method` (masks PII on the clone) or explicitly accepts the residual
    # risk (`acknowledge_unmasked` — the codified prod-clone-for-DR exception).
    # The unmasked clone must be deleted after use (A.8.10); we record the
    # accepted-risk decision on the pipeline note for audit/tracking.
    audit_note: str | None = None
    if source.environment == "prod" and not body.scrub_method:
        if not body.acknowledge_unmasked:
            raise HTTPException(
                status_code=422,
                detail=(
                    "Cloning a production site to a non-production staging site "
                    "copies live PII unmasked (ISO 27001 A.8.11 data masking). "
                    "Provide a `scrub_method` to mask PII on the clone, or set "
                    "`acknowledge_unmasked=true` to accept the residual risk for a "
                    "valid prod-clone (e.g. a DR rehearsal). An unmasked clone must "
                    "be deleted after use (A.8.10 information deletion)."
                ),
            )
        audit_note = (
            "A.8.11: UNMASKED prod clone — residual risk accepted at creation "
            f"by user {user.id}; delete staging site after use (A.8.10)."
        )
        logger.warning(
            "update-pipeline: unmasked prod clone accepted for source site %s "
            "(%s) by user %s — no scrub_method supplied (A.8.11 residual risk).",
            source.id, source.name, user.id,
        )

    source_bench = _bench(db, source.bench_id)
    staging_bench = (
        _bench(db, body.staging_bench_id)
        if body.staging_bench_id is not None
        else source_bench
    )
    # Artifacts live on the source server; the clone restore reads them locally.
    if staging_bench.server_id != source_bench.server_id:
        raise HTTPException(
            status_code=422,
            detail="The staging bench must be on the same server as the source "
            "site (cross-server clone is not supported).",
        )
    # The staging site must not already exist on the staging bench.
    if db.scalars(
        select(Site).where(
            Site.bench_id == staging_bench.id, Site.name == body.staging_site_name
        )
    ).first() is not None:
        raise HTTPException(
            status_code=409,
            detail=f"Site {body.staging_site_name!r} already exists on that bench.",
        )

    pipeline = UpdatePipeline(
        source_site_id=source.id,
        source_bench_id=source_bench.id,
        staging_bench_id=staging_bench.id,
        staging_site_name=body.staging_site_name,
        scrub_method=body.scrub_method,
        phase="cloning",
        note=audit_note,
        created_by=user.id,
    )
    db.add(pipeline)
    db.commit()
    db.refresh(pipeline)

    params: dict[str, str] = {
        "source_site": source.name,
        "source_bench_path": source_bench.path,
        "site": body.staging_site_name,
        "bench_path": staging_bench.path,
        "pipeline_id": str(pipeline.id),
    }
    if body.scrub_method:
        params["scrub_method"] = body.scrub_method
    try:
        job = runner.create(
            db,
            action_name=CLONE_ACTION,
            server_id=staging_bench.server_id,
            target_type="site",
            target_id=f"{staging_bench.path}::{body.staging_site_name}",
            params=params,
            user_secrets={"admin_pw": body.admin_password},
            priority=body.priority,
            created_by=user.id,
        )
    except (RenderError, SecretResolutionError) as exc:
        db.delete(pipeline)
        db.commit()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except LockConflict as exc:
        db.delete(pipeline)
        db.commit()
        return _conflict(exc, f"A job is already running on {body.staging_site_name!r}.")

    pipeline.clone_job_id = job.id
    db.commit()
    db.refresh(pipeline)
    return PipelineOut.from_model(pipeline)


# --------------------------------------------------------------------------- #
# Update the staging clone
# --------------------------------------------------------------------------- #


@router.post("/update-pipelines/{pipeline_id}/update-staging",
             status_code=201, response_model=JobDetail)
def update_staging(pipeline_id: int, db: DbSession, runner: Runner, user: CurrentUser):
    """Run `bench update` on the staging bench so the clone advances a version."""
    _require_action_permission(user, UPDATE_ACTION)  # bench:operate
    pipeline = _load_pipeline(db, pipeline_id)
    if pipeline.staging_site_id is None:
        raise HTTPException(
            status_code=409, detail="The staging clone is not ready yet."
        )
    staging_bench = _bench(db, pipeline.staging_bench_id)
    try:
        job = runner.create(
            db,
            action_name=UPDATE_ACTION,
            server_id=staging_bench.server_id,
            target_type="bench",
            target_id=staging_bench.path,
            params={"bench_path": staging_bench.path},
            priority="high",
            created_by=user.id,
        )
    except RenderError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except LockConflict as exc:
        return _conflict(exc, "A job is already running on the staging bench.")
    pipeline.update_job_id = job.id
    pipeline.phase = "updating"
    # Re-updating invalidates any prior green verify: the clone just changed, so
    # the promote gate must not stay green against the old checklist result.
    pipeline.checklist_ok = False
    pipeline.checklist = None
    pipeline.verify_job_id = None
    db.commit()
    db.refresh(job)
    return JobDetail.from_model(job)


# --------------------------------------------------------------------------- #
# Verification checklist
# --------------------------------------------------------------------------- #


@router.post("/update-pipelines/{pipeline_id}/verify",
             status_code=201, response_model=JobDetail)
def verify(pipeline_id: int, body: VerifyRequest, db: DbSession,
           runner: Runner, user: CurrentUser):
    """Run the verification checklist against the staging clone (all-green gate)."""
    _require_action_permission(user, VERIFY_ACTION)  # site:operate
    pipeline = _load_pipeline(db, pipeline_id)
    staging = db.get(Site, pipeline.staging_site_id) if pipeline.staging_site_id else None
    if staging is None:
        raise HTTPException(status_code=409, detail="The staging clone is not ready yet.")
    staging_bench = _bench(db, staging.bench_id)
    source = db.get(Site, pipeline.source_site_id)
    source_bench = _bench(db, pipeline.source_bench_id)
    params = {
        "site": staging.name,
        "bench_path": staging_bench.path,
        "pipeline_id": str(pipeline.id),
    }
    if source is not None:
        params["source_site"] = source.name
        params["source_bench_path"] = source_bench.path
    try:
        job = runner.create(
            db,
            action_name=VERIFY_ACTION,
            server_id=staging_bench.server_id,
            target_type="site",
            target_id=f"{staging_bench.path}::{staging.name}",
            params=params,
            priority=body.priority,
            created_by=user.id,
        )
    except RenderError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except LockConflict as exc:
        return _conflict(exc, f"A job is already running on {staging.name!r}.")
    pipeline.verify_job_id = job.id
    pipeline.phase = "verifying"
    db.commit()
    db.refresh(job)
    return JobDetail.from_model(job)


# --------------------------------------------------------------------------- #
# Promote to production
# --------------------------------------------------------------------------- #


@router.post("/update-pipelines/{pipeline_id}/promote",
             status_code=201, response_model=JobDetail)
def promote(pipeline_id: int, body: PromoteRequest, db: DbSession,
            runner: Runner, user: CurrentUser):
    """Promote the tested update to production. Blocked unless the checklist is
    all-green; for a `prod` source it also requires `danger` + the typed site
    name + a per-task sign-off. The promote job then takes the mandatory
    pre-update backup FIRST and rolls back automatically on failure."""
    _require_action_permission(user, PROMOTE_ACTION)  # backup:restore
    pipeline = _load_pipeline(db, pipeline_id)
    source = db.get(Site, pipeline.source_site_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Source site is missing.")
    source_bench = _bench(db, pipeline.source_bench_id)

    # Gate 1: the verification checklist must be all-green (verified before promote).
    if not pipeline.checklist_ok:
        raise HTTPException(
            status_code=409,
            detail="The verification checklist is not all-green — run/verify the "
            "staging clone before promoting.",
        )

    # Gate 2: promoting a prod site is destructive — danger + typed name + sign-off.
    if source.environment == "prod":
        if not role_allows(list(user.role.permissions or []), DANGER):
            raise HTTPException(
                status_code=403,
                detail=f"Role {user.role.name!r} lacks the {DANGER!r} permission "
                "required to update a production site.",
            )
        if body.confirm_name != source.name:
            raise HTTPException(
                status_code=422,
                detail="Type the exact production site name to confirm this update.",
            )
        if not (body.signoff and body.signoff.strip()):
            raise HTTPException(
                status_code=422,
                detail="A per-task client sign-off reference is required before "
                "writing a production site.",
            )

    try:
        job = runner.create(
            db,
            action_name=PROMOTE_ACTION,
            server_id=source_bench.server_id,
            target_type="site",
            target_id=f"{source_bench.path}::{source.name}",
            params={
                "site": source.name,
                "bench_path": source_bench.path,
                "pipeline_id": str(pipeline.id),
            },
            priority=body.priority,
            created_by=user.id,
        )
    except RenderError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except LockConflict as exc:
        return _conflict(exc, f"A job is already running on {source.name!r}.")
    pipeline.promote_job_id = job.id
    pipeline.phase = "promoting"
    if body.signoff:
        pipeline.note = f"prod sign-off: {body.signoff}"
    db.commit()
    db.refresh(job)
    return JobDetail.from_model(job)


# --------------------------------------------------------------------------- #
# Read
# --------------------------------------------------------------------------- #


@router.get("/update-pipelines", response_model=list[PipelineOut])
def list_pipelines(
    db: DbSession, _: Annotated[object, Depends(require(READ))]
) -> list[PipelineOut]:
    rows = db.scalars(
        select(UpdatePipeline).order_by(UpdatePipeline.created_at.desc())
    ).all()
    return [PipelineOut.from_model(p) for p in rows]


@router.get("/update-pipelines/{pipeline_id}", response_model=PipelineOut)
def get_pipeline(
    pipeline_id: int, db: DbSession, _: Annotated[object, Depends(require(READ))]
) -> PipelineOut:
    return PipelineOut.from_model(_load_pipeline(db, pipeline_id))
