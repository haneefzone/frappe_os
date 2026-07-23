"""Platform self-backup + master-key escrow API (session 6.3).

- GET  /api/platform/backups                 list self-backups (newest first)
- POST /api/platform/backups                 run a self-backup now -> job
- POST /api/platform/backups/{id}/verify     verify a self-backup -> job
- GET  /api/platform/backups/{id}/download   short-lived presigned GET of the
                                             encrypted archive
- GET  /api/platform/escrow                  master-key escrow ack state
- POST /api/platform/escrow/confirm          acknowledge escrow (audited)

**All routes are Admin-only** (`require_admin`): the platform self-backup and the
master-key escrow acknowledgement protect against an organisation-ending event
(loss of `FDM_SECRET_KEY`), so they are restricted to the Admin role specifically
rather than any role holding a broad permission. Every mutation is a job (rule 3)
or writes an audit row (rule 2). No secret material (master key, backup
passphrase, S3 keys) is ever returned.
"""

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, require_admin
from app.api.routes.jobs import get_job_runner
from app.audit import Audit
from app.core import storage as st
from app.core.commands import RenderError
from app.core.jobs import JobRunner, LockConflict
from app.core.security import SecretsService, get_secrets_service
from app.db import get_db
from app.models import User
from app.models.platform_backup import PlatformBackup
from app.models.settings import PlatformSettings
from app.models.storage import StorageTarget
from app.schemas.job import JobDetail
from app.schemas.platform_backup import (
    EscrowConfirmRequest,
    EscrowStatusOut,
    PlatformBackupOut,
    RunSelfBackupRequest,
)

router = APIRouter(prefix="/api/platform", tags=["platform-backup"])

DbSession = Annotated[Session, Depends(get_db)]
Runner = Annotated[JobRunner, Depends(get_job_runner)]
Secrets = Annotated[SecretsService, Depends(get_secrets_service)]
AdminUser = Annotated[User, Depends(require_admin)]

SELF_BACKUP_ACTION = "platform.self_backup"
VERIFY_ACTION = "platform.self_backup_verify"
# A `local` action needs no managed Server; the JobRunner gives it a
# LocalRemoteExecutor. Sentinel server_id + target keep the lock/audit uniform.
PLATFORM_SERVER_ID = 0
PLATFORM_TARGET = "platform"


def _target_name(db: Session, target_id: int | None) -> str | None:
    if target_id is None:
        return None
    target = db.get(StorageTarget, target_id)
    return target.name if target else None


def _resolve_target(db: Session, requested: int | None) -> StorageTarget:
    """The S3 target a self-backup uploads to (offsite is mandatory here). A
    requested id must exist + be enabled; omitted auto-selects the single enabled
    target, refusing (422) if zero or several exist rather than guessing."""
    if requested is not None:
        target = db.get(StorageTarget, requested)
        if target is None:
            raise HTTPException(status_code=404, detail="Storage target not found.")
        if not target.enabled:
            raise HTTPException(
                status_code=422,
                detail=f"Storage target {target.name!r} is disabled — enable it first.",
            )
        return target
    enabled = db.scalars(
        select(StorageTarget).where(StorageTarget.enabled.is_(True)).order_by(StorageTarget.id)
    ).all()
    if not enabled:
        raise HTTPException(
            status_code=422,
            detail="No enabled storage target — configure one in Settings → Storage "
            "before running a platform self-backup (the archive is stored offsite).",
        )
    if len(enabled) > 1:
        raise HTTPException(
            status_code=422,
            detail="Several storage targets are enabled — pass storage_target_id to "
            "choose which one the self-backup uploads to.",
        )
    return enabled[0]


def _conflict(exc: LockConflict) -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content={
            "error": {
                "code": "conflict",
                "message": "A platform self-backup is already running.",
                "blocking_job_id": exc.blocking_job_id,
            }
        },
    )


# --------------------------------------------------------------------------- #
# Self-backups
# --------------------------------------------------------------------------- #


@router.get("/backups", response_model=list[PlatformBackupOut])
def list_platform_backups(db: DbSession, _: AdminUser) -> list[PlatformBackupOut]:
    rows = db.scalars(
        select(PlatformBackup).order_by(PlatformBackup.created_at.desc())
    ).all()
    names = {t.id: t.name for t in db.scalars(select(StorageTarget)).all()}
    return [
        PlatformBackupOut.from_model(r, storage_target_name=names.get(r.storage_target_id))
        for r in rows
    ]


@router.post("/backups", status_code=201, response_model=JobDetail)
def run_platform_backup(
    body: RunSelfBackupRequest, db: DbSession, runner: Runner, user: AdminUser
):
    """Run a platform self-backup now. A `pending` PlatformBackup row is created
    first (so a failed run still leaves a visible record), then one
    `platform.self_backup` job dumps the platform DB + config set, encrypts the
    archive with the operator-held backup passphrase, and uploads it offsite."""
    target = _resolve_target(db, body.storage_target_id)
    row = PlatformBackup(status="pending")
    db.add(row)
    db.commit()
    db.refresh(row)
    try:
        job = runner.create(
            db,
            action_name=SELF_BACKUP_ACTION,
            server_id=PLATFORM_SERVER_ID,
            target_type=PLATFORM_TARGET,
            target_id=PLATFORM_TARGET,
            params={"backup_id": str(row.id), "storage_target_id": str(target.id)},
            priority=body.priority,
            created_by=user.id,
        )
    except RenderError as exc:
        db.delete(row)
        db.commit()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except LockConflict as exc:
        db.delete(row)
        db.commit()
        return _conflict(exc)
    row.taken_by_job_id = job.id
    db.commit()
    db.refresh(job)
    return JobDetail.from_model(job)


@router.post("/backups/{backup_id}/verify", status_code=201, response_model=JobDetail)
def verify_platform_backup(
    backup_id: int, db: DbSession, runner: Runner, user: AdminUser
):
    """Verify a self-backup: download → checksum → decrypt → `pg_restore --list`.
    The only real proof the self-backup is restorable."""
    row = db.get(PlatformBackup, backup_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Platform backup not found.")
    if row.status != "success" or not row.object_key:
        raise HTTPException(
            status_code=422,
            detail="This backup has no uploaded archive to verify.",
        )
    try:
        job = runner.create(
            db,
            action_name=VERIFY_ACTION,
            server_id=PLATFORM_SERVER_ID,
            target_type=PLATFORM_TARGET,
            target_id=PLATFORM_TARGET,
            params={"backup_id": str(row.id)},
            priority="high",
            created_by=user.id,
        )
    except RenderError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    db.refresh(job)
    return JobDetail.from_model(job)


@router.get("/backups/{backup_id}/download")
def download_platform_backup(
    backup_id: int,
    db: DbSession,
    secrets: Secrets,
    audit: Audit,
    _: AdminUser,
) -> dict:
    """Return a short-lived presigned GET URL for the encrypted archive. The URL
    carries only an HMAC signature — never the S3 secret key. The archive is
    encrypted with the operator-held backup passphrase (not downloadable in the
    clear); decrypting it needs the escrowed passphrase (see the runbook)."""
    row = db.get(PlatformBackup, backup_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Platform backup not found.")
    if not row.object_key or row.storage_target_id is None:
        raise HTTPException(
            status_code=409, detail="This backup has no uploaded archive."
        )
    target = db.get(StorageTarget, row.storage_target_id)
    if target is None:
        raise HTTPException(
            status_code=409, detail="The backup's storage target has been deleted."
        )
    import posixpath

    filename = posixpath.basename(row.object_key)
    try:
        url = st.presign_get(
            target, row.object_key, filename=filename, secrets=secrets
        )
    except st.StorageError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    audit.record(
        action="platform.backup.download",
        summary=f"Presigned download of platform backup #{backup_id}",
        entity_type="platform_backup",
        entity_id=backup_id,
        params={"filename": filename, "target": target.name},
    )
    return {"url": url, "expires_in": st.PRESIGN_TTL_SECONDS, "filename": filename}


# --------------------------------------------------------------------------- #
# Master-key escrow acknowledgement
# --------------------------------------------------------------------------- #


@router.get("/escrow", response_model=EscrowStatusOut)
def escrow_status(db: DbSession, _: AdminUser) -> EscrowStatusOut:
    row = PlatformSettings.get_or_create(db)
    email = None
    if row.master_key_escrow_confirmed_by is not None:
        u = db.get(User, row.master_key_escrow_confirmed_by)
        email = u.email if u else None
    return EscrowStatusOut(
        confirmed=row.master_key_escrow_confirmed_at is not None,
        confirmed_at=row.master_key_escrow_confirmed_at,
        confirmed_by=row.master_key_escrow_confirmed_by,
        confirmed_by_email=email,
    )


@router.post("/escrow/confirm", response_model=EscrowStatusOut)
def confirm_escrow(
    body: EscrowConfirmRequest, db: DbSession, audit: Audit, user: AdminUser
) -> EscrowStatusOut:
    """Acknowledge that `FDM_SECRET_KEY` (and the backup passphrase) have been
    escrowed per docs/master-key-escrow.md. Clears the persistent warning banner;
    stored with who + when and written to the audit log."""
    if not body.acknowledge:
        raise HTTPException(
            status_code=422, detail="acknowledge must be true to confirm escrow."
        )
    row = PlatformSettings.get_or_create(db)
    now = datetime.now(UTC)
    row.master_key_escrow_confirmed_at = now
    row.master_key_escrow_confirmed_by = user.id
    db.commit()
    audit.record(
        action="platform.escrow.confirm",
        summary=f"Master-key escrow confirmed by {user.email}",
        entity_type="platform_settings",
        entity_id=row.id,
        params={"confirmed_at": now.isoformat()},
    )
    return EscrowStatusOut(
        confirmed=True,
        confirmed_at=now,
        confirmed_by=user.id,
        confirmed_by_email=user.email,
    )
