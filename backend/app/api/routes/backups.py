"""Backups + guided restore API (session 1.11).

- POST /api/sites/{id}/backups        back up a site now -> `site.backup` job
- GET  /api/backups                   the backup inventory (+filters)
- GET  /api/backups/{id}              one backup (restore wizard step 1)
- POST /api/backups/{id}/validate     re-verify checksums -> `backup.validate` job
- GET  /api/backups/{id}/download     stream one artifact (Developer+, audited)
- GET  /api/restores/compatibility    source-vs-target major check (gotcha #7)
- POST /api/restores                  guided restore -> `site.restore` job

Every state-changing remote operation enqueues a job and returns it (rule 3);
listing/detail are read-only. A restore over an existing site is destructive:
it requires `danger` + the typed target-site-name confirm (rule 5) and the
orchestrator takes an automatic pre-restore backup first. Restoring a newer
Frappe major onto older code is blocked (gotcha #7 — never downgrade).
"""

import logging
import posixpath
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.api.deps import CurrentUser, require
from app.api.routes.jobs import get_job_runner
from app.audit import Audit
from app.core import backups as bk
from app.core import storage as st
from app.core.commands import RenderError, get_template
from app.core.jobs import JobRunner, LockConflict
from app.core.permissions import BACKUP_RESTORE, DANGER, READ, role_allows
from app.core.secrets_resolve import SecretResolutionError
from app.core.security import SecretsService, get_secrets_service
from app.core.ssh import SSHService, get_ssh_service
from app.db import get_db
from app.models import Server
from app.models.backup import Backup
from app.models.bench import Bench
from app.models.site import Site
from app.models.storage import StorageTarget
from app.schemas.backup import (
    BackupOut,
    CompatibilityOut,
    CreateBackupRequest,
    MoveBackupRequest,
    RestoreRequest,
)
from app.schemas.job import JobDetail

logger = logging.getLogger("app.audit")

router = APIRouter(prefix="/api", tags=["backups"])

DbSession = Annotated[Session, Depends(get_db)]
Runner = Annotated[JobRunner, Depends(get_job_runner)]
Ssh = Annotated[SSHService, Depends(get_ssh_service)]
Secrets = Annotated[SecretsService, Depends(get_secrets_service)]

BACKUP_ACTION = "site.backup"
VALIDATE_ACTION = "backup.validate"
RESTORE_ACTION = "site.restore"
MOVE_ACTION = "backup.move_across_servers"

RESTORE_MODES = ("same_site", "new_site", "different_bench")

# artifact kind -> (Backup column holding its path, download media type).
_ARTIFACT_COLUMN = {
    "database": ("db_path", "application/gzip"),
    "public_files": ("public_files_path", "application/x-tar"),
    "private_files": ("private_files_path", "application/x-tar"),
    "config": ("config_path", "application/json"),
}


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
        content={
            "error": {
                "code": "conflict",
                "message": message,
                "blocking_job_id": exc.blocking_job_id,
            }
        },
    )


def _resolve_storage_target(db: Session, requested: int | None) -> int | None:
    """Which storage target (if any) a new backup should upload to.

    - requested is a positive id  -> that target (must exist + be enabled).
    - requested is None (omitted)  -> auto-select the single enabled target, if
      exactly one exists; if zero or several exist, upload nothing (a local-only
      backup) rather than guess.
    - requested is 0 / negative    -> force a local-only backup.
    """
    if requested is not None and requested <= 0:
        return None
    if requested is not None:
        target = db.get(StorageTarget, requested)
        if target is None:
            raise HTTPException(status_code=404, detail="Storage target not found.")
        if not target.enabled:
            raise HTTPException(
                status_code=422,
                detail=f"Storage target {target.name!r} is disabled — enable it or "
                "back up locally.",
            )
        return target.id
    enabled = db.scalars(
        select(StorageTarget).where(StorageTarget.enabled.is_(True))
    ).all()
    return enabled[0].id if len(enabled) == 1 else None


def _backup_out(db: Session, backup: Backup) -> BackupOut:
    site = db.get(Site, backup.site_id)
    bench = db.get(Bench, backup.bench_id)
    server = db.get(Server, bench.server_id) if bench else None
    if site is None or bench is None or server is None:  # pragma: no cover - FK-guaranteed
        raise HTTPException(status_code=404, detail="Backup's site/bench/server is missing.")
    return BackupOut.from_model(backup, site, bench, server)


# --------------------------------------------------------------------------- #
# Create a backup
# --------------------------------------------------------------------------- #


@router.post("/sites/{site_id}/backups", status_code=201, response_model=JobDetail)
def create_backup(
    site_id: int,
    body: CreateBackupRequest,
    db: DbSession,
    runner: Runner,
    user: CurrentUser,
):
    """Back up a site now. A `pending` Backup row is created first (so a failed
    backup still leaves a visible record), then one `site.backup` job runs the
    dev-bench Redis dance, `bench backup [--with-files]`, and records the
    artifacts (paths + sizes + sha256) onto the row."""
    _require_action_permission(user, BACKUP_ACTION)
    site = db.get(Site, site_id)
    if site is None:
        raise HTTPException(status_code=404, detail="Site not found.")
    bench = db.get(Bench, site.bench_id)
    if bench is None:  # pragma: no cover - FK-guaranteed
        raise HTTPException(status_code=404, detail="Site's bench is missing.")

    target_id = _resolve_storage_target(db, body.storage_target_id)

    row = bk.create_pending_backup(
        db,
        site_id=site.id,
        bench_id=bench.id,
        backup_type="with-files" if body.with_files else "db",
        taken_by_job_id=None,
    )
    params = {
        "site": site.name,
        "bench_path": bench.path,
        "with_files": "1" if body.with_files else "0",
        "backup_id": str(row.id),
    }
    if target_id is not None:
        params["storage_target_id"] = str(target_id)
    try:
        job = runner.create(
            db,
            action_name=BACKUP_ACTION,
            server_id=bench.server_id,
            target_type="site",
            target_id=f"{bench.path}::{site.name}",
            params=params,
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
        return _conflict(exc, f"A job is already running on site {site.name!r}.")

    row.taken_by_job_id = job.id
    db.commit()
    db.refresh(job)
    return JobDetail.from_model(job)


# --------------------------------------------------------------------------- #
# Move a backup across servers (session 2.6)
# --------------------------------------------------------------------------- #


@router.post("/backups/{backup_id}/move", status_code=201, response_model=JobDetail)
def move_backup(
    backup_id: int,
    body: MoveBackupRequest,
    db: DbSession,
    runner: Runner,
    user: CurrentUser,
):
    """Move an offsite backup onto another server (Developer+, `backup:transfer`).

    The artifacts stream from the backup's S3 target down onto the destination
    bench's server, each sha256 re-verified on arrival, then a moved Backup copy
    is registered there. Preconditions (S3 is the cross-server transit medium):
    the source backup must be a successful, fully-offsite backup. The destination
    site must already exist on the target bench."""
    _require_action_permission(user, MOVE_ACTION)

    source = db.get(Backup, backup_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Backup not found.")
    if source.status != "success":
        raise HTTPException(
            status_code=422,
            detail="Only a successful backup can be moved.",
        )
    if source.storage_state != "offsite" or not (source.object_keys and source.storage_target_id):
        raise HTTPException(
            status_code=422,
            detail="This backup is not offsite yet. A cross-server move streams "
            "the artifacts through the backup's S3 target — push it offsite "
            "first (Backups → upload to storage), then move it.",
        )

    source_bench = db.get(Bench, source.bench_id)
    if source_bench is None:  # pragma: no cover - FK-guaranteed
        raise HTTPException(status_code=404, detail="Source backup's bench is missing.")

    dest_bench = db.get(Bench, body.target_bench_id)
    if dest_bench is None:
        raise HTTPException(status_code=404, detail="Destination bench not found.")
    if dest_bench.server_id == source_bench.server_id:
        raise HTTPException(
            status_code=422,
            detail="The backup is already on that server. Pick a bench on a "
            "different server to move it across.",
        )

    source_site = db.get(Site, source.site_id)
    site_name = body.target_site or (source_site.name if source_site else None)
    if not site_name:  # pragma: no cover - source site FK-guaranteed
        raise HTTPException(status_code=422, detail="Could not determine the target site name.")
    dest_site = db.scalars(
        select(Site).where(Site.bench_id == dest_bench.id, Site.name == site_name)
    ).first()
    if dest_site is None:
        raise HTTPException(
            status_code=404,
            detail=f"Site {site_name!r} does not exist on the destination bench "
            f"{dest_bench.name!r}. Create it there first, then move the backup onto it.",
        )

    dest_dir = posixpath.join(dest_bench.path, "sites", dest_site.name, "private", "backups")
    try:
        job = runner.create(
            db,
            action_name=MOVE_ACTION,
            server_id=dest_bench.server_id,
            target_type="site",
            target_id=f"{dest_bench.path}::{dest_site.name}",
            params={
                "backup_id": str(source.id),
                "storage_target_id": str(source.storage_target_id),
                "dest_dir": dest_dir,
                "target_site": dest_site.name,
                "target_bench_id": str(dest_bench.id),
            },
            priority=body.priority,
            created_by=user.id,
        )
    except RenderError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except LockConflict as exc:
        return _conflict(
            exc, f"A job is already running on site {dest_site.name!r} on the destination."
        )

    db.refresh(job)
    return JobDetail.from_model(job)


# --------------------------------------------------------------------------- #
# Backup inventory
# --------------------------------------------------------------------------- #


@router.get("/backups", response_model=list[BackupOut])
def list_backups(
    db: DbSession,
    _: Annotated[object, Depends(require(READ))],
    site: int | None = Query(default=None),
    bench: int | None = Query(default=None),
    type: str | None = Query(default=None),
    restore_tested: bool | None = Query(default=None),
) -> list[BackupOut]:
    """The backup inventory, newest first. Sites/benches/servers are prefetched
    into dicts to avoid an N+1 per backup."""
    stmt = select(Backup).order_by(Backup.created_at.desc())
    if site is not None:
        stmt = stmt.where(Backup.site_id == site)
    if bench is not None:
        stmt = stmt.where(Backup.bench_id == bench)
    if type is not None:
        stmt = stmt.where(Backup.type == type)
    if restore_tested is not None:
        stmt = stmt.where(Backup.restore_tested == restore_tested)
    rows = db.scalars(stmt).all()

    sites = {s.id: s for s in db.scalars(select(Site)).all()}
    benches = {b.id: b for b in db.scalars(select(Bench)).all()}
    servers = {s.id: s for s in db.scalars(select(Server)).all()}
    out: list[BackupOut] = []
    for b in rows:
        s = sites.get(b.site_id)
        bn = benches.get(b.bench_id)
        sv = servers.get(bn.server_id) if bn else None
        if s is not None and bn is not None and sv is not None:
            out.append(BackupOut.from_model(b, s, bn, sv))
    return out


@router.get("/backups/{backup_id}", response_model=BackupOut)
def get_backup(
    backup_id: int, db: DbSession, _: Annotated[object, Depends(require(READ))]
) -> BackupOut:
    backup = db.get(Backup, backup_id)
    if backup is None:
        raise HTTPException(status_code=404, detail="Backup not found.")
    return _backup_out(db, backup)


@router.post("/backups/{backup_id}/validate", status_code=201, response_model=JobDetail)
def validate_backup(
    backup_id: int, db: DbSession, runner: Runner, user: CurrentUser
):
    """Re-verify a backup's artifact checksums against the files on the server
    (restore wizard step 1). Read-only on the server; emits VALIDATE_RESULT."""
    _require_action_permission(user, VALIDATE_ACTION)
    backup = db.get(Backup, backup_id)
    if backup is None:
        raise HTTPException(status_code=404, detail="Backup not found.")
    site = db.get(Site, backup.site_id)
    bench = db.get(Bench, backup.bench_id)
    if site is None or bench is None:  # pragma: no cover - FK-guaranteed
        raise HTTPException(status_code=404, detail="Backup's site/bench is missing.")
    try:
        job = runner.create(
            db,
            action_name=VALIDATE_ACTION,
            server_id=bench.server_id,
            target_type="site",
            target_id=f"{bench.path}::{site.name}",
            params={
                "backup_id": str(backup.id),
                "site": site.name,
                "bench_path": bench.path,
            },
            priority="high",
            created_by=user.id,
        )
    except RenderError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    db.refresh(job)
    return JobDetail.from_model(job)


# --------------------------------------------------------------------------- #
# Download one artifact (streamed over SSH, Developer+, audited)
# --------------------------------------------------------------------------- #


@router.get("/backups/{backup_id}/download")
async def download_artifact(
    backup_id: int,
    db: DbSession,
    ssh: Ssh,
    audit: Audit,
    user: Annotated[object, Depends(require(BACKUP_RESTORE))],
    artifact: str = Query(...),
) -> StreamingResponse:
    """Stream one backup artifact to the browser. Developer+ (`backup:restore`);
    each download writes a first-class audit row (session 1.12)."""
    backup = db.get(Backup, backup_id)
    if backup is None:
        raise HTTPException(status_code=404, detail="Backup not found.")
    spec = _ARTIFACT_COLUMN.get(artifact)
    if spec is None:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown artifact {artifact!r}; expected one of "
            f"{sorted(_ARTIFACT_COLUMN)}.",
        )
    column, media_type = spec
    path = getattr(backup, column, None)
    if not path:
        raise HTTPException(
            status_code=404, detail=f"This backup has no {artifact} artifact."
        )

    bench = db.get(Bench, backup.bench_id)
    server = (
        db.scalars(
            select(Server)
            .options(joinedload(Server.credential))
            .where(Server.id == bench.server_id)
        ).first()
        if bench
        else None
    )
    if server is None or server.credential is None:
        raise HTTPException(
            status_code=409, detail="Backup's server has no SSH credential configured."
        )

    filename = posixpath.basename(path)
    audit.record(
        action="backup.download",
        summary=f"Downloaded {artifact} artifact of backup #{backup_id}",
        entity_type="backup",
        entity_id=backup_id,
        params={"artifact": artifact, "filename": filename},
    )
    return StreamingResponse(
        ssh.stream_file(server, server.credential, path),
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# --------------------------------------------------------------------------- #
# Presigned offsite download (S3-compatible target, short TTL, Developer+, audited)
# --------------------------------------------------------------------------- #


@router.get("/backups/{backup_id}/offsite-download")
def presign_offsite_download(
    backup_id: int,
    db: DbSession,
    secrets: Secrets,
    audit: Audit,
    user: Annotated[object, Depends(require(BACKUP_RESTORE))],
    artifact: str = Query(...),
) -> dict:
    """Return a short-lived presigned GET URL for one offsite artifact. The URL
    carries only an HMAC signature — never the S3 secret key — so it is safe to
    hand to the browser. Developer+ (`backup:restore`); each request is audited."""
    backup = db.get(Backup, backup_id)
    if backup is None:
        raise HTTPException(status_code=404, detail="Backup not found.")
    if backup.storage_state != "offsite" or backup.storage_target_id is None:
        raise HTTPException(
            status_code=409,
            detail="This backup has no offsite copy — it is stored locally only.",
        )
    key = (backup.object_keys or {}).get(artifact)
    if not key:
        raise HTTPException(
            status_code=404,
            detail=f"This backup has no offsite {artifact!r} artifact. "
            f"Available: {sorted((backup.object_keys or {}).keys())}.",
        )
    target = db.get(StorageTarget, backup.storage_target_id)
    if target is None:
        raise HTTPException(
            status_code=409,
            detail="The backup's storage target has been deleted.",
        )
    filename = posixpath.basename(key)
    try:
        url = st.presign_get(
            target,
            key,
            ttl_seconds=st.PRESIGN_TTL_SECONDS,
            filename=filename,
            secrets=secrets,
        )
    except st.StorageError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    audit.record(
        action="backup.offsite_download",
        summary=f"Presigned offsite {artifact} download of backup #{backup_id}",
        entity_type="backup",
        entity_id=backup_id,
        params={"artifact": artifact, "filename": filename, "target": target.name},
    )
    return {"url": url, "expires_in": st.PRESIGN_TTL_SECONDS, "filename": filename}


# --------------------------------------------------------------------------- #
# Guided restore
# --------------------------------------------------------------------------- #


def _resolve_target(
    db: Session, backup: Backup, body: RestoreRequest
) -> tuple[Bench, str, Bench, Site, bool]:
    """Resolve the restore target from the mode. Returns (target_bench,
    target_site_name, source_bench, source_site, destructive). Raises HTTP 4xx on
    an invalid target."""
    source_site = db.get(Site, backup.site_id)
    source_bench = db.get(Bench, backup.bench_id)
    if source_site is None or source_bench is None:  # pragma: no cover - FK-guaranteed
        raise HTTPException(status_code=404, detail="Backup's source site/bench is missing.")

    if body.mode == "same_site":
        return source_bench, source_site.name, source_bench, source_site, True

    # new_site / different_bench both need an explicit target bench + site name.
    if body.target_bench_id is None or not body.target_site_name:
        raise HTTPException(
            status_code=422,
            detail="target_bench_id and target_site_name are required for this mode.",
        )
    target_bench = db.get(Bench, body.target_bench_id)
    if target_bench is None:
        raise HTTPException(status_code=404, detail="Target bench not found.")
    # Artifacts live on the source server; cross-server restore (copying the files
    # to another host) is out of scope for this session.
    if target_bench.server_id != source_bench.server_id:
        raise HTTPException(
            status_code=422,
            detail="The target bench must be on the same server as the backup "
            "(cross-server restore is not supported yet).",
        )
    existing = db.scalars(
        select(Site).where(
            Site.bench_id == target_bench.id, Site.name == body.target_site_name
        )
    ).first()
    if body.mode == "new_site":
        if existing is not None:
            raise HTTPException(
                status_code=409,
                detail=f"Site {body.target_site_name!r} already exists on that bench "
                "— use same_site / different_bench to restore over it.",
            )
        return target_bench, body.target_site_name, source_bench, source_site, False
    # different_bench: restore over an existing site on another bench (destructive).
    if existing is None:
        raise HTTPException(
            status_code=404,
            detail=f"Site {body.target_site_name!r} does not exist on that bench "
            "— use new_site to create and restore into a fresh site.",
        )
    return target_bench, body.target_site_name, source_bench, source_site, True


@router.get("/restores/compatibility", response_model=CompatibilityOut)
def restore_compatibility(
    db: DbSession,
    _: Annotated[object, Depends(require(READ))],
    backup_id: int = Query(...),
    bench_id: int = Query(...),
) -> CompatibilityOut:
    """Would `backup_id` restore onto `bench_id` (gotcha #7 downgrade guard)?"""
    backup = db.get(Backup, backup_id)
    if backup is None:
        raise HTTPException(status_code=404, detail="Backup not found.")
    bench = db.get(Bench, bench_id)
    if bench is None:
        raise HTTPException(status_code=404, detail="Bench not found.")
    result = bk.check_restore_compatibility(backup.frappe_version, bench.frappe_version)
    return CompatibilityOut(
        ok=result.ok,
        source_major=result.source_major,
        target_major=result.target_major,
        reason=result.reason,
    )


@router.post("/restores", status_code=201, response_model=JobDetail)
def create_restore(
    body: RestoreRequest, db: DbSession, runner: Runner, user: CurrentUser
):
    """Launch a guided restore. Validates the mode/target, the downgrade guard
    (gotcha #7), the destructive-restore confirm (rule 5), then enqueues one
    `site.restore` job that (optionally creates the target,) takes an automatic
    pre-restore backup, restores, copies the encryption_key and migrates."""
    if body.mode not in RESTORE_MODES:
        raise HTTPException(
            status_code=422, detail=f"mode must be one of {list(RESTORE_MODES)}."
        )
    _require_action_permission(user, RESTORE_ACTION)  # backup:restore

    backup = db.get(Backup, body.backup_id)
    if backup is None:
        raise HTTPException(status_code=404, detail="Backup not found.")
    if backup.status != "success" or not backup.db_path:
        raise HTTPException(
            status_code=422,
            detail="This backup has no usable database artifact to restore.",
        )

    target_bench, target_site, _source_bench, _source_site, destructive = _resolve_target(
        db, backup, body
    )

    # gotcha #7: block restoring a newer major onto older code (down-grade).
    compat = bk.check_restore_compatibility(backup.frappe_version, target_bench.frappe_version)
    if not compat.ok:
        raise HTTPException(status_code=422, detail=compat.reason)

    # A destructive restore (over an existing site) needs `danger` + typed confirm.
    if destructive:
        if not role_allows(list(user.role.permissions or []), DANGER):
            raise HTTPException(
                status_code=403,
                detail=f"Role {user.role.name!r} lacks the {DANGER!r} permission "
                "required to restore over an existing site.",
            )
        if body.confirm_name != target_site:
            raise HTTPException(
                status_code=422,
                detail="Type the exact target site name to confirm this destructive "
                "restore.",
            )

    with_files = bool(backup.public_files_path and backup.private_files_path)
    params: dict[str, str] = {
        "site": target_site,
        "bench_path": target_bench.path,
        "mode": body.mode,
        "with_files": "1" if with_files else "0",
        "backup_id": str(backup.id),
        "db_path": backup.db_path,
    }
    if with_files:
        params["public_files"] = backup.public_files_path
        params["private_files"] = backup.private_files_path
    if backup.config_path:
        params["config_path"] = backup.config_path

    user_secrets: dict[str, str] | None = None
    if body.mode == "new_site":
        if not body.admin_password:
            raise HTTPException(
                status_code=422,
                detail="admin_password is required to create the new target site.",
            )
        user_secrets = {"admin_pw": body.admin_password}

    try:
        job = runner.create(
            db,
            action_name=RESTORE_ACTION,
            server_id=target_bench.server_id,
            target_type="site",
            target_id=f"{target_bench.path}::{target_site}",
            params=params,
            user_secrets=user_secrets,
            priority=body.priority,
            created_by=user.id,
        )
    except (RenderError, SecretResolutionError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except LockConflict as exc:
        return _conflict(exc, f"A job is already running on site {target_site!r}.")
    db.refresh(job)
    return JobDetail.from_model(job)
