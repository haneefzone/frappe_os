"""Panel copilot API (session 5.2 — Phase 5 AI).

- POST /api/jobs/{id}/analyze   enqueue an AI root-cause analysis of a FAILED job
                                (job:manage; 409 if AI unconfigured; AuditLog).
- GET  /api/jobs/{id}/analyze   latest analysis for the job, for polling (read).
- POST /api/palette/resolve     map a natural-language request to an existing
                                job template + validated params, or refuse (read).

Golden rules: the AI call never runs in the request (rule 3 — it is enqueued to
RQ after the row is committed); RBAC is server-side (rule 7); every request is
audited (rule 2); the NL resolver maps only to registered templates, never raw
shell (rule 1).
"""

from collections.abc import Callable
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, require
from app.audit import Audit
from app.core.ai import AnthropicClient
from app.core.copilot import reap_if_stale, resolve_nl_command
from app.core.permissions import JOB_MANAGE, READ
from app.core.security import SecretsService, get_secrets_service
from app.db import get_db
from app.models import CommandJob, JobAnalysis
from app.models.ai_settings import AISettings
from app.schemas.copilot import (
    JobAnalysisOut,
    NLProposalOut,
    NLResolveRequest,
    NLResolveResponse,
)

router = APIRouter(prefix="/api", tags=["copilot"])

DbSession = Annotated[Session, Depends(get_db)]
Secrets = Annotated[SecretsService, Depends(get_secrets_service)]


def dispatch_analysis(analysis_id: int) -> None:
    """Enqueue the AI analysis on the low-priority RQ queue (rule 3). Overridden
    in tests to run the analysis synchronously against the test session."""
    from redis import Redis  # noqa: PLC0415
    from rq import Callback, Queue  # noqa: PLC0415

    from app.config import get_settings  # noqa: PLC0415
    from app.core.copilot import ANALYSIS_JOB_TIMEOUT  # noqa: PLC0415

    conn = Redis.from_url(get_settings().redis_url)
    Queue("low", connection=conn).enqueue(
        "app.core.copilot.run_job_analysis",
        analysis_id,
        job_timeout=ANALYSIS_JOB_TIMEOUT,
        result_ttl=86400,
        failure_ttl=604800,
        # A job_timeout / worker exception leaves the row 'running' forever; flip
        # it to 'failure' so the panel stops polling (rule 3 stays: still no work
        # in the request). Hard crashes are caught by reap_if_stale on GET.
        on_failure=Callback("app.core.copilot.mark_analysis_failed"),
    )


def get_analysis_dispatcher() -> Callable[[int], None]:
    return dispatch_analysis


Dispatcher = Annotated[Callable[[int], None], Depends(get_analysis_dispatcher)]


def _load_job(db: Session, job_id: int) -> CommandJob:
    job = db.get(CommandJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    return job


@router.post("/jobs/{job_id}/analyze", status_code=202, response_model=JobAnalysisOut)
def analyze_failed_job(
    job_id: int,
    db: DbSession,
    secrets: Secrets,
    audit: Audit,
    dispatch: Dispatcher,
    user: CurrentUser,
    response: Response,
    _: Annotated[object, Depends(require(JOB_MANAGE))],
) -> JobAnalysisOut:
    """Kick off a background AI analysis of a failed job.

    Only failed jobs can be analyzed (422 otherwise). If the AI integration is
    disabled/unkeyed, refuse cleanly (409) — no half-created analysis. The row is
    committed and audited *before* the AI call is enqueued (never blocks, rule 3).

    Idempotent while one is in flight: if the latest analysis is still
    `pending`/`running`, return it (200) instead of stacking a fresh row + AI
    spend — the panel is already polling it.
    """
    job = _load_job(db, job_id)
    if job.status != "failure":
        raise HTTPException(
            status_code=422, detail="Only failed jobs can be analyzed."
        )

    existing = (
        db.execute(
            select(JobAnalysis)
            .where(JobAnalysis.job_id == job.id)
            .order_by(JobAnalysis.created_at.desc(), JobAnalysis.id.desc())
            .limit(1)
        )
        .scalars()
        .first()
    )
    if existing is not None and existing.status in ("pending", "running"):
        response.status_code = 200
        return JobAnalysisOut.from_model(existing)

    row = AISettings.get_or_create(db)
    client = AnthropicClient(row, secrets, db=db)
    if not client.ready:
        detail = (
            "AI integration is disabled."
            if not row.enabled
            else "No Anthropic API key is configured."
        )
        raise HTTPException(status_code=409, detail=detail)

    analysis = JobAnalysis(job_id=job.id, status="pending", requested_by=user.id)
    db.add(analysis)
    db.commit()
    db.refresh(analysis)

    audit.record(
        action="job.analyze.request",
        summary=f"Requested AI analysis of failed job #{job.id}",
        entity_type="job_analysis",
        entity_id=analysis.id,
        params={"job_id": job.id, "action_name": job.action_name},
    )

    # Enqueue only after the row is durably committed (rule 3): the worker will
    # find it. A dispatch failure must not lose the row — it stays 'pending' and
    # can be retried; surface a 502 so the caller knows the worker wasn't reached.
    try:
        dispatch(analysis.id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=502, detail="Could not enqueue the analysis job."
        ) from exc

    return JobAnalysisOut.from_model(analysis)


@router.get("/jobs/{job_id}/analyze", response_model=JobAnalysisOut)
def get_job_analysis(
    job_id: int,
    db: DbSession,
    _: Annotated[object, Depends(require(READ))],
) -> JobAnalysisOut:
    """The most recent analysis for a job (for the panel to poll). 404 if none."""
    _load_job(db, job_id)
    analysis = (
        db.execute(
            select(JobAnalysis)
            .where(JobAnalysis.job_id == job_id)
            .order_by(JobAnalysis.created_at.desc(), JobAnalysis.id.desc())
            .limit(1)
        )
        .scalars()
        .first()
    )
    if analysis is None:
        raise HTTPException(status_code=404, detail="No analysis for this job yet.")
    # Self-heal a row a crashed worker left 'running' so the panel stops polling.
    analysis = reap_if_stale(db, analysis)
    return JobAnalysisOut.from_model(analysis)


@router.post("/palette/resolve", response_model=NLResolveResponse)
def resolve_palette_command(
    body: NLResolveRequest,
    db: DbSession,
    user: CurrentUser,
    _: Annotated[object, Depends(require(READ))],
) -> NLResolveResponse:
    """Map a natural-language palette request to an existing template + validated
    params, or refuse. Read-only can call it (proposing is harmless); each
    proposal carries an `allowed` flag and the actual run rides the action's own
    RBAC-gated endpoint on confirm (rule 1 + rule 7)."""
    perms = list(user.role.permissions or [])
    res = resolve_nl_command(db, body.q, perms)
    return NLResolveResponse(
        resolved=res.resolved,
        intent=res.intent,
        reason=res.reason,
        proposals=[
            NLProposalOut(
                title=p.title,
                action_name=p.action_name,
                summary=p.summary,
                site_id=p.site_id,
                site_name=p.site_name,
                params=p.params,
                allowed=p.allowed,
                confirm=p.confirm,
                run=p.run,
            )
            for p in res.proposals
        ],
    )
