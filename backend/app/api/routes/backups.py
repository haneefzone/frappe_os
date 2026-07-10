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
from app.core import backups as bk
from app.core.commands import RenderError, get_template
from app.core.jobs import JobRunner, LockConflict
from app.core.permissions import BACKUP_RESTORE, DANGER, READ, role_allows
from app.core.secrets_resolve import SecretResolutionError
from app.core.ssh import SSHService, get_ssh_service
from app.db import get_db
from app.models import Server
from app.models.backup import Backup
from app.models.bench import Bench
from app.models.site import Site
from app.schemas.backup import (
    BackupOut,
    CompatibilityOut,
    CreateBackupRequest,
    RestoreRequest,
)
from app.schemas.job import JobDetail

logger = logging.getLogger("app.audit")

router = APIRouter(prefix="/api", tags=["backups"])

DbSession = Annotated[Session, Depends(get_db)]
Runner = Annotated[JobRunner, Depends(get_job_runner)]
Ssh = Annotated[SSHService, Depends(get_ssh_service)]

BACKUP_ACTION = "site.backup"
VALIDATE_ACTION = "backup.validate"
RESTORE_ACTION = "site.restore"

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

    row = bk.create_pending_backup(
        db,
        site_id=site.id,
        bench_id=bench.id,
        backup_type="with-files" if body.with_files else "db",
        taken_by_job_id=None,
    )
    try:
        job = runner.create(
            db,
            action_name=BACKUP_ACTION,
            server_id=bench.server_id,
            target_type="site",
            target_id=f"{bench.path}::{site.name}",
            params={
                "site": site.name,
                "bench_path": bench.path,
                "with_files": "1" if body.with_files else "0",
                "backup_id": str(row.id),
            },
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
    user: Annotated[object, Depends(require(BACKUP_RESTORE))],
    artifact: str = Query(...),
) -> StreamingResponse:
    """Stream one backup artifact to the browser. Developer+ (`backup:restore`);
    each download is recorded to the audit logger (first-class audit trail lands
    in session 1.12)."""
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
    logger.info(
        "backup.download user=%s backup=%s artifact=%s path=%s",
        getattr(user, "id", "?"),
        backup_id,
        artifact,
        path,
    )
    return StreamingResponse(
        ssh.stream_file(server, server.credential, path),
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


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
