"""Apps API (session 1.9).

- GET/POST/PATCH/DELETE /api/app-sources        app-source CRUD (deploy key WO)
- POST /api/app-sources/branches                list remote branches -> job
- GET  /api/installed-apps                       the app×site matrix (+?bench=)
- POST /api/sites/{id}/apps                       get (if needed) + install -> job
- DELETE /api/sites/{id}/apps/{app}              uninstall (type-name) -> job

Every remote operation enqueues a job and returns it (rule 3). Repo URLs are
host-allowlist validated before anything is stored or launched (rule 1); deploy
keys are stored Fernet-encrypted and never returned (rule 6). Mutations require
`app:manage`; uninstall requires `danger`; listing requires `read`.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, require
from app.api.routes.jobs import get_job_runner
from app.audit import Audit
from app.core.appsources import RepoSourceError, validate_repo_source
from app.core.commands import RenderError, get_template
from app.core.jobs import JobRunner, LockConflict
from app.core.marketplace import (
    DependencyError,
    PlanStep,
    RegistryCache,
    RegistryError,
    get_registry_cache,
    resolve_catalog,
    resolve_install_plan,
)
from app.core.permissions import READ, role_allows
from app.core.secrets_resolve import SecretResolutionError
from app.core.security import get_secrets_service
from app.db import get_db
from app.models import Server
from app.models.app import AppSource, InstalledApp
from app.models.bench import Bench
from app.models.site import Site
from app.models.updates import AppVersionStatus
from app.schemas.app import (
    AppSourceOut,
    CreateAppSourceRequest,
    InstallAppRequest,
    InstalledAppOut,
    ListBranchesRequest,
    StoreCatalogAppOut,
    UninstallAppRequest,
    UpdateAppSourceRequest,
)
from app.schemas.job import JobDetail

router = APIRouter(prefix="/api", tags=["apps"])

DbSession = Annotated[Session, Depends(get_db)]
Runner = Annotated[JobRunner, Depends(get_job_runner)]
Registry = Annotated[RegistryCache, Depends(get_registry_cache)]

INSTALL_ACTION = "site.install_app"
UNINSTALL_ACTION = "app.uninstall"
BRANCHES_ACTION = "app.list_branches"


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


# --------------------------------------------------------------------------- #
# App sources CRUD
# --------------------------------------------------------------------------- #


def _classify_and_validate(repo_url: str) -> str:
    """Host-allowlist gate + kind classification. Raises HTTP 422 on a bad host
    or shape."""
    try:
        return validate_repo_source(repo_url).kind
    except RepoSourceError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/app-sources", response_model=list[AppSourceOut])
def list_app_sources(
    db: DbSession, _: Annotated[object, Depends(require(READ))]
) -> list[AppSourceOut]:
    sources = db.scalars(select(AppSource).order_by(AppSource.name)).all()
    return [AppSourceOut.from_model(s) for s in sources]


@router.post("/app-sources", status_code=201, response_model=AppSourceOut)
def create_app_source(
    body: CreateAppSourceRequest, db: DbSession, user: CurrentUser, audit: Audit
) -> AppSourceOut:
    _require_action_permission(user, INSTALL_ACTION)  # app:manage
    kind = _classify_and_validate(body.repo_url)
    if db.scalars(select(AppSource).where(AppSource.name == body.name)).first():
        raise HTTPException(
            status_code=409, detail=f"An app source named {body.name!r} already exists."
        )

    deploy_key_enc = None
    if body.deploy_key:
        deploy_key_enc = get_secrets_service().encrypt(body.deploy_key)

    source = AppSource(
        name=body.name,
        repo_url=body.repo_url,
        kind=kind,
        default_branch=body.default_branch,
        is_private=body.is_private,
        deploy_key_enc=deploy_key_enc,
        notes=body.notes,
    )
    db.add(source)
    db.commit()
    db.refresh(source)
    audit.record(
        action="app_source.create",
        summary=f"Added app source {source.name} ({source.kind})",
        entity_type="app_source",
        entity_id=source.id,
        params={"name": source.name, "repo_url": source.repo_url, "kind": source.kind},
    )
    return AppSourceOut.from_model(source)


@router.get("/app-sources/{source_id}", response_model=AppSourceOut)
def get_app_source(
    source_id: int, db: DbSession, _: Annotated[object, Depends(require(READ))]
) -> AppSourceOut:
    source = db.get(AppSource, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="App source not found.")
    return AppSourceOut.from_model(source)


@router.patch("/app-sources/{source_id}", response_model=AppSourceOut)
def update_app_source(
    source_id: int, body: UpdateAppSourceRequest, db: DbSession, user: CurrentUser, audit: Audit
) -> AppSourceOut:
    _require_action_permission(user, INSTALL_ACTION)
    source = db.get(AppSource, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="App source not found.")

    if body.repo_url is not None:
        source.kind = _classify_and_validate(body.repo_url)
        source.repo_url = body.repo_url
    if body.name is not None:
        clash = db.scalars(
            select(AppSource).where(
                AppSource.name == body.name, AppSource.id != source_id
            )
        ).first()
        if clash:
            raise HTTPException(
                status_code=409,
                detail=f"An app source named {body.name!r} already exists.",
            )
        source.name = body.name
    if body.default_branch is not None:
        source.default_branch = body.default_branch or None
    if body.is_private is not None:
        source.is_private = body.is_private
    if body.notes is not None:
        source.notes = body.notes or None
    if body.deploy_key is not None:
        # "" clears the key; a non-empty value replaces it.
        source.deploy_key_enc = (
            get_secrets_service().encrypt(body.deploy_key) if body.deploy_key else None
        )
    db.commit()
    db.refresh(source)
    audit.record(
        action="app_source.update",
        summary=f"Updated app source {source.name}",
        entity_type="app_source",
        entity_id=source.id,
    )
    return AppSourceOut.from_model(source)


@router.delete("/app-sources/{source_id}", status_code=204)
def delete_app_source(source_id: int, db: DbSession, user: CurrentUser, audit: Audit) -> None:
    _require_action_permission(user, INSTALL_ACTION)
    source = db.get(AppSource, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="App source not found.")
    name = source.name
    db.delete(source)
    db.commit()
    audit.record(
        action="app_source.delete",
        summary=f"Deleted app source {name}",
        entity_type="app_source",
        entity_id=source_id,
    )


# --------------------------------------------------------------------------- #
# Branch listing (for the picker)
# --------------------------------------------------------------------------- #


@router.post("/app-sources/branches", status_code=201, response_model=JobDetail)
def list_branches(body: ListBranchesRequest, db: DbSession, runner: Runner, user: CurrentUser):
    """Launch a `git ls-remote --heads` job; the picker tails it for the
    BRANCHES_RESULT line. Give a saved `app_source_id` (its deploy key is used)
    or a raw `repo_url`."""
    _require_action_permission(user, BRANCHES_ACTION)
    if db.get(Server, body.server_id) is None:
        raise HTTPException(status_code=404, detail="Server not found.")

    repo_url = body.repo_url
    user_secrets: dict[str, str] | None = None
    if body.app_source_id is not None:
        source = db.get(AppSource, body.app_source_id)
        if source is None:
            raise HTTPException(status_code=404, detail="App source not found.")
        repo_url = source.repo_url
        if source.is_private and source.deploy_key_enc:
            user_secrets = {"deploy_key": get_secrets_service().decrypt(source.deploy_key_enc)}
    if not repo_url:
        raise HTTPException(status_code=422, detail="repo_url or app_source_id is required.")

    try:
        validate_repo_source(repo_url)  # host allowlist (marketplace names too)
    except RepoSourceError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    try:
        job = runner.create(
            db,
            action_name=BRANCHES_ACTION,
            server_id=body.server_id,
            target_type="server",
            target_id=str(body.server_id),
            params={"url": repo_url},
            user_secrets=user_secrets,
            priority="high",
            created_by=user.id,
        )
    except (RenderError, SecretResolutionError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    db.refresh(job)
    return JobDetail.from_model(job)


# --------------------------------------------------------------------------- #
# Installed-app matrix
# --------------------------------------------------------------------------- #


@router.get("/installed-apps", response_model=list[InstalledAppOut])
def list_installed_apps(
    db: DbSession,
    _: Annotated[object, Depends(require(READ))],
    bench: int | None = Query(default=None),
) -> list[InstalledAppOut]:
    """The app×site matrix rows. Sites/benches are prefetched to avoid N+1."""
    stmt = select(InstalledApp).order_by(InstalledApp.bench_id, InstalledApp.app_name)
    if bench is not None:
        stmt = stmt.where(InstalledApp.bench_id == bench)
    rows = db.scalars(stmt).all()

    sites = {s.id: s for s in db.scalars(select(Site)).all()}
    benches = {b.id: b for b in db.scalars(select(Bench)).all()}
    # Advisor verdicts keyed by installed-app id → "behind by N" chip data (3.2).
    statuses = {
        st.installed_app_id: st
        for st in db.scalars(select(AppVersionStatus)).all()
    }
    out: list[InstalledAppOut] = []
    for ia in rows:
        site = sites.get(ia.site_id)
        b = benches.get(ia.bench_id)
        if site is None or b is None:
            continue
        out.append(
            InstalledAppOut.from_model(
                ia,
                site_name=site.name,
                bench_name=b.name,
                server_id=b.server_id,
                update_status=statuses.get(ia.id),
            )
        )
    return out


# --------------------------------------------------------------------------- #
# App-store catalog (DOO-1192)
# --------------------------------------------------------------------------- #


def _bench_installed_apps(db: Session, bench_id: int) -> set[str]:
    """App names already present on a bench (any site of it), plus the Frappe
    core which every bench carries."""
    names = set(
        db.scalars(
            select(InstalledApp.app_name).where(InstalledApp.bench_id == bench_id)
        ).all()
    )
    names.add("frappe")
    return names


def _site_installed_apps(db: Session, site_id: int) -> set[str]:
    """App names already installed on a specific site, plus the Frappe core."""
    names = set(
        db.scalars(
            select(InstalledApp.app_name).where(InstalledApp.site_id == site_id)
        ).all()
    )
    names.add("frappe")
    return names


@router.get("/store/catalog", response_model=list[StoreCatalogAppOut])
def store_catalog(
    registry: Registry,
    db: DbSession,
    _: Annotated[object, Depends(require(READ))],
    bench: int = Query(..., description="Bench id to resolve compatibility against."),
) -> list[StoreCatalogAppOut]:
    """The app-store catalog resolved against one bench's installed Frappe
    version. Incompatible apps are returned with `is_installable=false` and a
    reason — never hidden (AC2). A never-cloned registry is a hard 503 (AC1)."""
    b = db.get(Bench, bench)
    if b is None:
        raise HTTPException(status_code=404, detail="Bench not found.")
    if not b.frappe_version:
        raise HTTPException(
            status_code=409,
            detail="This bench's Frappe version is unknown — run a discovery on "
            "its server before browsing the store.",
        )
    try:
        registry.ensure_fresh()
        records = registry.records()
    except RegistryError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    installed = _bench_installed_apps(db, b.id)
    resolved = resolve_catalog(records, b.frappe_version, installed)
    # `categories` is a tuple on the dataclass; pydantic coerces it to a list.
    return [StoreCatalogAppOut(**vars(r)) for r in resolved]


# --------------------------------------------------------------------------- #
# Install / uninstall on a site
# --------------------------------------------------------------------------- #


def _bench_for_site(db: Session, site: Site) -> Bench:
    bench = db.get(Bench, site.bench_id)
    if bench is None:  # pragma: no cover - FK-guaranteed
        raise HTTPException(status_code=404, detail="Site's bench is missing.")
    return bench


# Dependencies are encoded onto the single `site.install_app` job as an ordered
# `app~source~branch` list joined by ",". Every character in that grammar is
# shell-safe (letters, digits, and `_ ~ . : / @ - ,` only — no shell
# metacharacter can appear), and the value is NEVER interpolated into a shell
# command: InstallAppOnSiteAction splits it and re-renders each field through the
# whitelisted `app.get`/`app.install` templates as its own argv element. This
# keeps the store's dependency install on the SAME job/lock/progress surface as a
# single-app install rather than adding a second install code path (AC3/AC5).
def _encode_dep_plan(steps: list[PlanStep]) -> str:
    return ",".join(f"{s.app}~{s.source}~{s.branch}" for s in steps)


def _resolve_store_install(
    db: Session,
    registry: RegistryCache,
    bench: Bench,
    site: Site,
    store_app: str,
) -> tuple[str, str, str, str | None]:
    """Resolve a store app name into (source_repo, pinned_branch, app_name,
    dep_plan) for the shared install job. Raises the right HTTP error for each
    failure mode: unknown Frappe version (409), already installed (409),
    never-cloned registry (503), unknown/incompatible app or dependency
    conflict/cycle (422)."""
    if not bench.frappe_version:
        raise HTTPException(
            status_code=409,
            detail="This bench's Frappe version is unknown — run a discovery on "
            "its server before installing store apps.",
        )
    # Already installed on THIS site → clean 409, not a crash (AC6).
    if store_app in _site_installed_apps(db, site.id):
        raise HTTPException(
            status_code=409,
            detail=f"{store_app!r} is already installed on site {site.name!r}.",
        )
    try:
        registry.ensure_fresh()
    except RegistryError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    installed = _site_installed_apps(db, site.id)
    try:
        plan = resolve_install_plan(
            store_app, bench.frappe_version, registry.record, installed
        )
    except RegistryError as exc:  # index unreadable mid-resolve
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except DependencyError as exc:
        # Unknown app, no compatible release, version conflict, or a cycle — a
        # clear error, never a partial install (AC5).
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    if not plan:  # pragma: no cover - the already-installed guard above covers this
        raise HTTPException(
            status_code=409,
            detail=f"{store_app!r} is already installed on site {site.name!r}.",
        )
    target = plan[-1]
    deps = plan[:-1]
    if not target.branch:
        raise HTTPException(
            status_code=422,
            detail=f"The catalog release for {store_app!r} has no branch to pin to.",
        )
    # Host-allowlist gate on every repo we are about to fetch (golden rule 1).
    for step in plan:
        try:
            validate_repo_source(step.source)
        except RepoSourceError as exc:
            raise HTTPException(
                status_code=422,
                detail=f"catalog repo for {step.app!r} is not allowed: {exc}",
            ) from exc
    dep_plan = _encode_dep_plan(deps) if deps else None
    return target.source, target.branch, target.app, dep_plan


@router.post("/sites/{site_id}/apps", status_code=201, response_model=JobDetail)
def install_app(
    site_id: int,
    body: InstallAppRequest,
    db: DbSession,
    runner: Runner,
    registry: Registry,
    user: CurrentUser,
):
    """Get the app onto the bench if needed, then install it on the site — one
    chained job. Install parameters come from ONE of: `store_app` (an app-store
    name resolved against the bench's Frappe version, DOO-1192), a saved
    `app_source_id`, a raw `source`, or neither for an already-fetched
    marketplace app. All four funnel into the same `site.install_app` job."""
    _require_action_permission(user, INSTALL_ACTION)
    site = db.get(Site, site_id)
    if site is None:
        raise HTTPException(status_code=404, detail="Site not found.")
    bench = _bench_for_site(db, site)

    source_arg: str | None = None
    branch: str | None = body.branch
    source_name: str | None = None
    app_name: str | None = body.app
    dep_plan: str | None = None
    user_secrets: dict[str, str] | None = None

    if body.store_app is not None:
        source_arg, branch, app_name, dep_plan = _resolve_store_install(
            db, registry, bench, site, body.store_app
        )
    elif body.app_source_id is not None:
        src = db.get(AppSource, body.app_source_id)
        if src is None:
            raise HTTPException(status_code=404, detail="App source not found.")
        source_arg = src.repo_url
        branch = branch or src.default_branch
        source_name = src.name
        app_name = app_name or src.name
        if src.is_private and src.deploy_key_enc:
            user_secrets = {"deploy_key": get_secrets_service().decrypt(src.deploy_key_enc)}
    elif body.source:
        try:
            resolved = validate_repo_source(body.source)
        except RepoSourceError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        source_arg = body.source
        # A bare marketplace name doubles as the module name.
        if resolved.kind == "marketplace":
            app_name = app_name or body.source

    if not app_name:
        raise HTTPException(
            status_code=422,
            detail="An app (module) name is required — give `app`, an "
            "`app_source_id`, or a marketplace `source`.",
        )
    if source_arg and not branch:
        raise HTTPException(
            status_code=422,
            detail="A branch is required when fetching an app from a repo.",
        )

    params: dict[str, str] = {"site": site.name, "bench_path": bench.path, "app": app_name}
    if source_arg:
        params["source"] = source_arg
        params["branch"] = branch
    if source_name:
        params["source_name"] = source_name
    if dep_plan:
        params["dep_plan"] = dep_plan

    try:
        job = runner.create(
            db,
            action_name=INSTALL_ACTION,
            server_id=bench.server_id,
            target_type="site",
            target_id=f"{bench.path}::{site.name}",
            params=params,
            user_secrets=user_secrets,
            priority=body.priority,
            created_by=user.id,
        )
    except (RenderError, SecretResolutionError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except LockConflict as exc:
        return _conflict(exc, f"A job is already running on site {site.name!r}.")
    db.refresh(job)
    return JobDetail.from_model(job)


@router.delete("/sites/{site_id}/apps/{app_name}", status_code=201, response_model=JobDetail)
def uninstall_app(
    site_id: int,
    app_name: str,
    body: UninstallAppRequest,
    db: DbSession,
    runner: Runner,
    user: CurrentUser,
):
    """Uninstall an app from a site (destructive). `confirm_name` must equal the
    app name (type-the-target-name-to-confirm, rule 5)."""
    _require_action_permission(user, UNINSTALL_ACTION)  # danger
    if body.confirm_name != app_name:
        raise HTTPException(
            status_code=422,
            detail="Type the exact app name to confirm the uninstall.",
        )
    site = db.get(Site, site_id)
    if site is None:
        raise HTTPException(status_code=404, detail="Site not found.")
    bench = _bench_for_site(db, site)

    try:
        job = runner.create(
            db,
            action_name=UNINSTALL_ACTION,
            server_id=bench.server_id,
            target_type="site",
            target_id=f"{bench.path}::{site.name}",
            params={"site": site.name, "app": app_name, "bench_path": bench.path},
            priority=body.priority,
            created_by=user.id,
        )
    except RenderError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except LockConflict as exc:
        return _conflict(exc, f"A job is already running on site {site.name!r}.")
    db.refresh(job)
    return JobDetail.from_model(job)
