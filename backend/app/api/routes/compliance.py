"""Backup-compliance API (session 2.3).

Per-site policy CRUD + a fleet compliance summary:

- GET    /api/sites/{id}/policy      one site's backup policy (Read-only+)
- PUT    /api/sites/{id}/policy      create/replace the policy (schedule:manage)
- DELETE /api/sites/{id}/policy      remove the policy + its status (schedule:manage)
- GET    /api/compliance             fleet compliance summary (Read-only+)
- POST   /api/compliance/evaluate    run the evaluator now (schedule:manage)

Managing a policy is a scheduling-adjacent config decision, so writes need
`schedule:manage` (Admin/Developer) — the same permission that governs the
recurring backups a policy measures. Viewing needs only `read`. The evaluator is
read-only over backup metadata; POST /evaluate just runs it on demand (e.g. right
after a backup, so a screen reflects reality without waiting for the next tick).
Every write records an audit row (golden rule 2).
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import require
from app.audit import Audit
from app.core import compliance as comp
from app.core.permissions import READ, SCHEDULE_MANAGE
from app.db import get_db
from app.models import Site
from app.models.compliance import BackupPolicy, ComplianceStatus
from app.schemas.compliance import (
    ComplianceStatusOut,
    ComplianceSummaryOut,
    PolicyOut,
    UpsertPolicyRequest,
)

router = APIRouter(tags=["compliance"])

DbSession = Annotated[Session, Depends(get_db)]
ReadAccess = Annotated[object, Depends(require(READ))]
ManagePolicy = Annotated[object, Depends(require(SCHEDULE_MANAGE))]


def _get_site_or_404(db: Session, site_id: int) -> Site:
    site = db.get(Site, site_id)
    if site is None:
        raise HTTPException(status_code=404, detail="Site not found.")
    return site


# --------------------------------------------------------------------------- #
# Per-site policy
# --------------------------------------------------------------------------- #


@router.get("/api/sites/{site_id}/policy", response_model=PolicyOut)
def get_policy(site_id: int, db: DbSession, _: ReadAccess) -> PolicyOut:
    _get_site_or_404(db, site_id)
    policy = db.scalar(select(BackupPolicy).where(BackupPolicy.site_id == site_id))
    if policy is None:
        raise HTTPException(status_code=404, detail="No backup policy for this site.")
    return PolicyOut.from_model(policy)


@router.put("/api/sites/{site_id}/policy", response_model=PolicyOut)
def upsert_policy(
    site_id: int,
    body: UpsertPolicyRequest,
    db: DbSession,
    audit: Audit,
    _: ManagePolicy,
) -> PolicyOut:
    site = _get_site_or_404(db, site_id)
    policy = db.scalar(select(BackupPolicy).where(BackupPolicy.site_id == site_id))
    created = policy is None
    if policy is None:
        policy = BackupPolicy(site_id=site_id, created_by=audit.user.id)
        db.add(policy)

    policy.rpo_hours = body.rpo_hours
    policy.retention_days = body.retention_days
    policy.require_offsite = body.require_offsite
    policy.require_restore_test = body.require_restore_test
    policy.enabled = body.enabled
    db.commit()
    db.refresh(policy)

    audit.record(
        action="backup.policy_set",
        summary=f"{'Created' if created else 'Updated'} backup policy for {site.name}",
        entity_type="site",
        entity_id=site_id,
        params={
            "rpo_hours": body.rpo_hours,
            "retention_days": body.retention_days,
            "require_offsite": body.require_offsite,
            "enabled": body.enabled,
        },
    )
    return PolicyOut.from_model(policy)


@router.delete("/api/sites/{site_id}/policy", status_code=204)
def delete_policy(
    site_id: int, db: DbSession, audit: Audit, _: ManagePolicy
) -> None:
    site = _get_site_or_404(db, site_id)
    policy = db.scalar(select(BackupPolicy).where(BackupPolicy.site_id == site_id))
    if policy is None:
        raise HTTPException(status_code=404, detail="No backup policy for this site.")
    db.delete(policy)
    # Drop the stale status too so the site leaves the compliance metric cleanly.
    status = db.scalar(
        select(ComplianceStatus).where(ComplianceStatus.site_id == site_id)
    )
    if status is not None:
        db.delete(status)
    db.commit()
    audit.record(
        action="backup.policy_removed",
        summary=f"Removed backup policy for {site.name}",
        entity_type="site",
        entity_id=site_id,
    )


# --------------------------------------------------------------------------- #
# Fleet summary + on-demand evaluation
# --------------------------------------------------------------------------- #


def _summary(db: Session) -> ComplianceSummaryOut:
    # Enabled-policy statuses, with the site name for the UI.
    rows = db.execute(
        select(ComplianceStatus, Site.name)
        .join(BackupPolicy, BackupPolicy.site_id == ComplianceStatus.site_id)
        .join(Site, Site.id == ComplianceStatus.site_id)
        .where(BackupPolicy.enabled.is_(True))
        .order_by(Site.name)
    ).all()
    statuses = [
        ComplianceStatusOut.from_model(status, site_name=name)
        for status, name in rows
    ]
    compliant, policied = comp.compliance_counts(db)
    pct = 100 if policied == 0 else round(compliant / policied * 100)
    breached = policied - compliant
    return ComplianceSummaryOut(
        policied=policied,
        compliant=compliant,
        breached=breached,
        compliance_pct=pct,
        statuses=statuses,
    )


@router.get("/api/compliance", response_model=ComplianceSummaryOut)
def get_compliance(db: DbSession, _: ReadAccess) -> ComplianceSummaryOut:
    return _summary(db)


@router.post("/api/compliance/evaluate", response_model=ComplianceSummaryOut)
def evaluate_now(
    db: DbSession, audit: Audit, _: ManagePolicy
) -> ComplianceSummaryOut:
    result = comp.evaluate_all(db)
    audit.record(
        action="compliance.evaluated",
        summary=(
            f"Evaluated {result['policied']} policied site(s): "
            f"{result['compliant']} compliant, {result['breached']} breached"
        ),
        params=result,
    )
    return _summary(db)
