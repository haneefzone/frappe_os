"""restic config-tier DR backups API (session 4.1 — Full-system DR).

- GET  /api/restic-repos                          fleet evidence list (READ)
- GET  /api/servers/{id}/restic-repo              one server's repo (READ)
- PUT  /api/servers/{id}/restic-repo              configure (server:manage; pw enc)
- POST /api/servers/{id}/restic-repo/install      ensure restic on target -> job
- POST /api/servers/{id}/restic-repo/init         `restic init` -> job
- POST /api/servers/{id}/restic-repo/backup       config-tier snapshot -> job
- POST /api/servers/{id}/restic-repo/snapshots    evidence listing -> job
- POST /api/servers/{id}/restic-repo/forget       retention prune -> job (4.2)
- POST /api/servers/{id}/restic-repo/check        integrity check -> job (4.2)

Reading is READ-visible so the §6 backup-evidence view can render the config
kind/storage chip for everyone. Every mutation (configure + the three job
launches) needs `server:manage` — Admin/Developer in the default matrix, so
Operator/Read-only cannot mutate (golden rule 7). Each job launch funnels
through `runner.create`, which writes the CommandJob + AuditLog and enqueues
after commit (golden rules 2 & 3); the config PUT writes its own audit row with
the password masked. The repo password + S3 keys never appear in any params,
log, or response (rule 6): they reach restic only via a staged env file.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, require
from app.api.routes.jobs import get_job_runner
from app.audit import Audit
from app.core import restic as rst
from app.core.commands import RenderError, get_template
from app.core.jobs import JobRunner, LockConflict
from app.core.permissions import READ, SERVER_MANAGE, role_allows
from app.core.security import SecretsService, get_secrets_service
from app.db import get_db
from app.models.restic import ResticRepo
from app.models.server import Server
from app.models.storage import StorageTarget
from app.schemas.job import JobDetail
from app.schemas.restic import ResticRepoConfigure, ResticRepoOut

router = APIRouter(prefix="/api", tags=["restic"])

DbSession = Annotated[Session, Depends(get_db)]
Runner = Annotated[JobRunner, Depends(get_job_runner)]
Secrets = Annotated[SecretsService, Depends(get_secrets_service)]
ReadAny = Annotated[object, Depends(require(READ))]

INSTALL_ACTION = "restic.install"
INIT_ACTION = "restic.init"
BACKUP_ACTION = "restic.backup"
SNAPSHOTS_ACTION = "restic.snapshots"
FORGET_ACTION = "restic.forget"
CHECK_ACTION = "restic.check"


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


def _server_or_404(db: Session, server_id: int) -> Server:
    server = db.get(Server, server_id)
    if server is None:
        raise HTTPException(status_code=404, detail="Server not found.")
    return server


def _repo_or_404(db: Session, server_id: int) -> ResticRepo:
    repo = db.scalars(
        select(ResticRepo).where(ResticRepo.server_id == server_id)
    ).first()
    if repo is None:
        raise HTTPException(
            status_code=404, detail="This server has no restic repo configured."
        )
    return repo


def _out(db: Session, repo: ResticRepo) -> ResticRepoOut:
    name = None
    if repo.storage_target_id:
        target = db.get(StorageTarget, repo.storage_target_id)
        name = target.name if target else None
    return ResticRepoOut.from_model(repo, storage_target_name=name)


# --------------------------------------------------------------------------- #
# Read
# --------------------------------------------------------------------------- #


@router.get("/restic-repos", response_model=list[ResticRepoOut])
def list_repos(db: DbSession, _: ReadAny) -> list[ResticRepoOut]:
    """The fleet's config-tier repos, newest first — the backup-evidence view."""
    rows = db.scalars(select(ResticRepo).order_by(ResticRepo.created_at.desc())).all()
    targets = {t.id: t.name for t in db.scalars(select(StorageTarget)).all()}
    return [
        ResticRepoOut.from_model(
            r, storage_target_name=targets.get(r.storage_target_id)
        )
        for r in rows
    ]


@router.get("/servers/{server_id}/restic-repo", response_model=ResticRepoOut)
def get_repo(server_id: int, db: DbSession, _: ReadAny) -> ResticRepoOut:
    _server_or_404(db, server_id)
    return _out(db, _repo_or_404(db, server_id))


# --------------------------------------------------------------------------- #
# Configure (server:manage)
# --------------------------------------------------------------------------- #


@router.put("/servers/{server_id}/restic-repo", response_model=ResticRepoOut)
def configure_repo(
    server_id: int,
    body: ResticRepoConfigure,
    db: DbSession,
    secrets: Secrets,
    audit: Audit,
    user: Annotated[object, Depends(require(SERVER_MANAGE))],
) -> ResticRepoOut:
    """Create or update a server's restic config-tier repo. The password is
    write-only (Fernet-encrypted immediately, never echoed). The S3 credentials
    are reused from the chosen 2.2 StorageTarget — not re-entered here."""
    _server_or_404(db, server_id)

    target = db.get(StorageTarget, body.storage_target_id)
    if target is None:
        raise HTTPException(status_code=404, detail="Storage target not found.")

    try:
        prefix = rst.normalize_prefix(body.prefix)
    except rst.ResticError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    repo = db.scalars(
        select(ResticRepo).where(ResticRepo.server_id == server_id)
    ).first()
    created = repo is None
    if repo is None:
        repo = ResticRepo(server_id=server_id)
        db.add(repo)

    repo.storage_target_id = target.id
    repo.prefix = prefix
    password_action = "unchanged"
    if body.password:
        repo.password_enc = secrets.encrypt(body.password)
        password_action = "set"
    # Retention policy is a full replace from the body (PUT semantics): the client
    # sends the desired keep-* set; None on a dimension clears it. All-None leaves
    # the repo with no policy and the forget action will refuse to prune.
    repo.retention_keep_last = body.retention_keep_last
    repo.retention_keep_daily = body.retention_keep_daily
    repo.retention_keep_weekly = body.retention_keep_weekly
    repo.retention_keep_monthly = body.retention_keep_monthly
    db.commit()
    db.refresh(repo)

    audit.record(
        action="restic.repo.configure",
        summary=(
            f"{'Configured' if created else 'Updated'} restic config-tier repo "
            f"for server #{server_id} → target {target.name!r}"
        ),
        entity_type="server",
        entity_id=str(server_id),
        params={
            "storage_target_id": target.id,
            "prefix": prefix,
            # A non-secret action word ("set"/"unchanged"); keyed so the audit's
            # defensive key-name masking (any key containing "password"/"key"/
            # "secret") never blanks it — the value carries no credential.
            "credential_state": password_action,
            "retention": repo.retention_summary or "none",
        },
    )
    return _out(db, repo)


# --------------------------------------------------------------------------- #
# Job launches (server:manage) — install / init / backup / snapshots
# --------------------------------------------------------------------------- #


def _repo_uri_or_422(db: Session, repo: ResticRepo) -> str:
    """Resolve the repo's S3 URI, failing 422 if it isn't ready to run (no target
    attached, no bucket, no password)."""
    if not repo.password_enc:
        raise HTTPException(
            status_code=422,
            detail="This restic repo has no password configured — set one first.",
        )
    target = db.get(StorageTarget, repo.storage_target_id) if repo.storage_target_id else None
    if target is None:
        raise HTTPException(
            status_code=422,
            detail="This restic repo has no storage target attached.",
        )
    if not (target.access_key_enc and target.secret_key_enc):
        raise HTTPException(
            status_code=422,
            detail=f"Storage target {target.name!r} has no S3 credentials configured.",
        )
    try:
        return rst.repository_uri(target, repo.prefix)
    except rst.ResticError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _launch(
    db: Session,
    runner: JobRunner,
    user,
    *,
    server_id: int,
    action_name: str,
    params: dict,
    conflict_msg: str,
) -> JobDetail:
    _require_action_permission(user, action_name)
    try:
        job = runner.create(
            db,
            action_name=action_name,
            server_id=server_id,
            target_type="server",
            target_id=str(server_id),
            params=params,
            priority="default",
            created_by=user.id,
        )
    except RenderError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except LockConflict as exc:
        return _conflict(exc, conflict_msg)  # type: ignore[return-value]
    return JobDetail.from_model(job)


@router.post(
    "/servers/{server_id}/restic-repo/install",
    status_code=201,
    response_model=JobDetail,
)
def install_restic(
    server_id: int, db: DbSession, runner: Runner, user: CurrentUser
):
    """Ensure a pinned restic is available on the server (detect or install)."""
    _server_or_404(db, server_id)
    return _launch(
        db, runner, user,
        server_id=server_id,
        action_name=INSTALL_ACTION,
        params={},
        conflict_msg="A restic job is already running on this server.",
    )


@router.post(
    "/servers/{server_id}/restic-repo/init", status_code=201, response_model=JobDetail
)
def init_repo(server_id: int, db: DbSession, runner: Runner, user: CurrentUser):
    """Create the restic repository in its S3 bucket (`restic init`)."""
    _server_or_404(db, server_id)
    repo = _repo_or_404(db, server_id)
    repo_uri = _repo_uri_or_422(db, repo)
    return _launch(
        db, runner, user,
        server_id=server_id,
        action_name=INIT_ACTION,
        params={"repo": repo_uri},
        conflict_msg="A restic job is already running on this server.",
    )


@router.post(
    "/servers/{server_id}/restic-repo/backup",
    status_code=201,
    response_model=JobDetail,
)
def backup_config(server_id: int, db: DbSession, runner: Runner, user: CurrentUser):
    """Snapshot the server's OS/config tier (nginx/supervisor/redis/mariadb +
    dpkg selections) to its restic repo → returns the enqueued job."""
    server = _server_or_404(db, server_id)
    repo = _repo_or_404(db, server_id)
    repo_uri = _repo_uri_or_422(db, repo)
    if not repo.initialized:
        raise HTTPException(
            status_code=422,
            detail="This restic repo is not initialised — run init before a backup.",
        )
    host = server.hostname or server.name
    return _launch(
        db, runner, user,
        server_id=server_id,
        action_name=BACKUP_ACTION,
        params={"repo": repo_uri, "host": host},
        conflict_msg="A restic job is already running on this server.",
    )


@router.post(
    "/servers/{server_id}/restic-repo/snapshots",
    status_code=201,
    response_model=JobDetail,
)
def list_snapshots(server_id: int, db: DbSession, runner: Runner, user: CurrentUser):
    """List the config-tier snapshots in the repo (evidence) → returns a job that
    streams `restic snapshots`."""
    _server_or_404(db, server_id)
    repo = _repo_or_404(db, server_id)
    repo_uri = _repo_uri_or_422(db, repo)
    return _launch(
        db, runner, user,
        server_id=server_id,
        action_name=SNAPSHOTS_ACTION,
        params={"repo": repo_uri},
        conflict_msg="A restic job is already running on this server.",
    )


@router.post(
    "/servers/{server_id}/restic-repo/forget",
    status_code=201,
    response_model=JobDetail,
)
def forget_config(server_id: int, db: DbSession, runner: Runner, user: CurrentUser):
    """Apply the repo's retention policy and prune (`restic forget --prune`) →
    returns the enqueued job. DESTRUCTIVE (deletes snapshots): the action never
    auto-retries and refuses to run when no retention policy is set. 422 here if
    the repo is not initialised or has no policy configured."""
    _server_or_404(db, server_id)
    repo = _repo_or_404(db, server_id)
    repo_uri = _repo_uri_or_422(db, repo)
    if not repo.initialized:
        raise HTTPException(
            status_code=422,
            detail="This restic repo is not initialised — nothing to prune.",
        )
    if not repo.retention_configured:
        raise HTTPException(
            status_code=422,
            detail="No retention policy is configured — set at least one "
            "keep-last/daily/weekly/monthly value before pruning.",
        )
    return _launch(
        db, runner, user,
        server_id=server_id,
        action_name=FORGET_ACTION,
        params={"repo": repo_uri},
        conflict_msg="A restic job is already running on this server.",
    )


@router.post(
    "/servers/{server_id}/restic-repo/check",
    status_code=201,
    response_model=JobDetail,
)
def check_repo(server_id: int, db: DbSession, runner: Runner, user: CurrentUser):
    """Verify repository integrity (`restic check --read-data-subset`) → returns
    the enqueued job. The action records the result on the repo and raises a
    breach alert on a genuine failure. 422 if the repo is not initialised."""
    _server_or_404(db, server_id)
    repo = _repo_or_404(db, server_id)
    repo_uri = _repo_uri_or_422(db, repo)
    if not repo.initialized:
        raise HTTPException(
            status_code=422,
            detail="This restic repo is not initialised — nothing to check.",
        )
    return _launch(
        db, runner, user,
        server_id=server_id,
        action_name=CHECK_ACTION,
        params={"repo": repo_uri, "subset": rst.DEFAULT_CHECK_SUBSET},
        conflict_msg="A restic job is already running on this server.",
    )
