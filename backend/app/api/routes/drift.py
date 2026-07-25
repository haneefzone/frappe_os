"""Config-drift API (session 6.7, uiux-spec A2.16).

- GET  /api/drift                      list baselines (filter by server/status)
- GET  /api/drift/summary              fleet drift counters (dashboard)
- GET  /api/drift/{id}                 one baseline + sanitised unified diff
- POST /api/drift/{id}/accept          adopt current state as baseline (Admin)
- POST /api/servers/{id}/drift_check   run a drift check now (→ 202 job)

Viewing needs only `read`. Accepting a drift as the new baseline and launching a
check are `server:manage` (Admin/Developer) — the same permission that governs
the servers whose config is being tracked. Every accept records an audit row
with the operator-supplied reason (golden rule 2). No endpoint ever writes to a
managed server; drift detection is strictly read-only.
"""

from __future__ import annotations

import difflib
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, require
from app.api.routes.jobs import get_job_runner
from app.audit import Audit
from app.core.jobs import JobRunner, LockConflict
from app.core.permissions import READ, SERVER_MANAGE
from app.db import get_db
from app.models import ConfigBaseline, Server
from app.schemas.drift import (
    AcceptBaselineRequest,
    DriftBaselineOut,
    DriftDiffOut,
    DriftSummaryOut,
)

router = APIRouter(tags=["drift"])

DbSession = Annotated[Session, Depends(get_db)]
ReadAccess = Annotated[object, Depends(require(READ))]
ManageDrift = Annotated[object, Depends(require(SERVER_MANAGE))]
Runner = Annotated[JobRunner, Depends(get_job_runner)]


def _get_or_404(db: Session, baseline_id: int) -> ConfigBaseline:
    row = db.get(ConfigBaseline, baseline_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Config baseline not found.")
    return row


@router.get("/api/drift", response_model=list[DriftBaselineOut])
def list_baselines(
    db: DbSession,
    _: ReadAccess,
    server_id: int | None = Query(default=None),
    status: str | None = Query(default=None),
    drifted_only: bool = Query(default=False),
) -> list[ConfigBaseline]:
    stmt = select(ConfigBaseline)
    if server_id is not None:
        stmt = stmt.where(ConfigBaseline.server_id == server_id)
    if status is not None:
        stmt = stmt.where(ConfigBaseline.status == status)
    if drifted_only:
        stmt = stmt.where(ConfigBaseline.status == "drifted")
    stmt = stmt.order_by(
        ConfigBaseline.status.desc(),  # "drifted" sorts before "baseline"/"accepted"
        ConfigBaseline.server_id,
        ConfigBaseline.artifact_key,
    )
    return list(db.scalars(stmt).all())


@router.get("/api/drift/summary", response_model=DriftSummaryOut)
def drift_summary(db: DbSession, _: ReadAccess) -> DriftSummaryOut:
    """Fleet drift counters for the Dashboard "Needs attention" row (B4.1)."""
    drifted = list(
        db.scalars(
            select(ConfigBaseline).where(ConfigBaseline.status == "drifted")
        ).all()
    )
    return DriftSummaryOut(
        drifted_count=len(drifted),
        server_ids=sorted({r.server_id for r in drifted}),
    )


@router.get("/api/drift/{baseline_id}", response_model=DriftDiffOut)
def drift_detail(baseline_id: int, db: DbSession, _: ReadAccess) -> DriftDiffOut:
    row = _get_or_404(db, baseline_id)
    # Hash-only when either side's content was withheld (unparseable / secret-
    # bearing file, or the absence sentinel). Otherwise diff the sanitised text —
    # secrets are already masked as ••••, so no secret can appear here (rule 6).
    baseline_text = row.sanitized_content
    current_text = row.current_content if row.status == "drifted" else row.sanitized_content
    hash_only = baseline_text is None or (row.status == "drifted" and current_text is None)

    unified = ""
    if not hash_only:
        diff = difflib.unified_diff(
            (baseline_text or "").splitlines(keepends=True),
            (current_text or "").splitlines(keepends=True),
            fromfile=f"{row.artifact_key} (baseline)",
            tofile=f"{row.artifact_key} (current)",
        )
        unified = "".join(diff)
    return DriftDiffOut(
        baseline=DriftBaselineOut.model_validate(row),
        hash_only=hash_only,
        unified_diff=unified,
    )


@router.post("/api/drift/{baseline_id}/accept", response_model=DriftBaselineOut)
def accept_baseline(
    baseline_id: int,
    body: AcceptBaselineRequest,
    db: DbSession,
    audit: Audit,
    user: CurrentUser,
    _: ManageDrift,
) -> ConfigBaseline:
    """Adopt the current (drifted) state as the new baseline. Audited with the
    operator's reason. Does NOT touch the managed server — it only re-points the
    baseline hash at what is already on disk (no auto-remediation)."""
    row = _get_or_404(db, baseline_id)
    if row.status != "drifted":
        raise HTTPException(
            status_code=409, detail="Baseline is not drifted; nothing to accept."
        )
    now = datetime.now(UTC)
    # Adopt current hash/content as the accepted baseline; clear drift snapshot.
    row.sha256 = row.current_sha256
    row.sanitized_content = row.current_content
    row.status = "accepted"
    row.current_sha256 = None
    row.current_content = None
    row.drift_detected_at = None
    row.captured_at = now
    row.captured_by_job_id = None  # human-accepted, not job-captured
    db.commit()
    db.refresh(row)

    audit.record(
        action="drift.accept_baseline",
        summary=f"Accepted config drift as new baseline for {row.artifact_key}",
        entity_type="config_baseline",
        entity_id=row.id,
        params={"artifact_key": row.artifact_key, "path": row.path, "reason": body.reason},
    )
    return row


@router.post("/api/servers/{server_id}/drift_check", status_code=202)
def run_drift_check_now(
    server_id: int,
    db: DbSession,
    runner: Runner,
    user: CurrentUser,
    _: ManageDrift,
):
    """Launch a `server.drift_check` job now (low queue). Returns 202 {job_id}
    immediately (rule 3); the job re-hashes every tracked artefact and diffs it
    against baseline."""
    if db.get(Server, server_id) is None:
        raise HTTPException(status_code=404, detail="Server not found.")
    try:
        job = runner.create(
            db,
            action_name="server.drift_check",
            server_id=server_id,
            target_type="server",
            target_id=None,
            params={},
            priority="low",
            created_by=user.id,
        )
    except LockConflict as exc:
        return JSONResponse(
            status_code=409,
            content={
                "error": {
                    "code": "conflict",
                    "message": f"A drift check is already running on this server "
                    f"(blocking job {exc.blocking_job_id}).",
                    "blocking_job_id": exc.blocking_job_id,
                }
            },
        )
    return {"job_id": job.id}
