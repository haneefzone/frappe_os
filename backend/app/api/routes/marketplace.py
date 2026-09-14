"""Frappe app store API (DOO-1194).

- GET  /api/marketplace/apps                    browse the catalog (+?bench= for compat)
- GET  /api/marketplace/apps/{name}             app detail: releases + resolved plan
- POST /api/marketplace/refresh                 force a catalog sync (serve-stale safe)
- POST /api/sites/{id}/marketplace-apps         install a store app + its deps -> job

The catalog is the cached `frappe/marketplace` git checkout, read off disk. Every
app's compatibility with a **target bench's installed Frappe version** is computed
and surfaced (`is_installable` + a human-readable `reason`) rather than silently
hidden (AC2). Install resolves the full transitive dependency plan and validates
it BEFORE enqueuing anything, so a conflict never yields a partial install (AC3);
the enqueued job reuses the existing get-app/install-app connector path (AC4).
"""

import base64
import json
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, require
from app.api.routes.jobs import get_job_runner
from app.audit import Audit
from app.core.appsources import RepoSourceError, validate_repo_source
from app.core.jobs import JobRunner, LockConflict
from app.core.marketplace import (
    RegistryCache,
    RegistryReader,
    RegistryUnavailableError,
    ResolutionError,
    build_cache,
    compatible_releases,
    resolve_plan,
)
from app.core.permissions import APP_MANAGE, READ, role_allows
from app.db import get_db
from app.models.bench import Bench
from app.models.site import Site
from app.schemas.job import JobDetail
from app.schemas.marketplace import (
    MarketplaceAppDetailOut,
    MarketplaceAppOut,
    MarketplaceInstallRequest,
    MarketplaceRefreshOut,
    MarketplaceReleaseOut,
    PlanStepOut,
)

router = APIRouter(prefix="/api", tags=["marketplace"])

DbSession = Annotated[Session, Depends(get_db)]
Runner = Annotated[JobRunner, Depends(get_job_runner)]
Cache = Annotated[RegistryCache, Depends(build_cache)]

INSTALL_ACTION = "site.install_marketplace_app"


def _require_app_manage(user) -> None:
    if not role_allows(list(user.role.permissions or []), APP_MANAGE):
        raise HTTPException(
            status_code=403,
            detail=f"Role {user.role.name!r} lacks the {APP_MANAGE!r} permission.",
        )


def _reader_or_503(cache: RegistryCache) -> RegistryReader:
    try:
        return cache.reader()
    except RegistryUnavailableError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"The app catalog is not available yet: {exc}",
        ) from exc


def _bench_frappe_version(db: Session, bench_id: int) -> tuple[Bench, str]:
    """Resolve a bench and its known Frappe version, or raise a legible HTTP
    error (the whole feature turns on the target bench's actual version)."""
    bench = db.get(Bench, bench_id)
    if bench is None:
        raise HTTPException(status_code=404, detail="Bench not found.")
    if not bench.frappe_version:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Bench {bench.name!r} has no known Frappe version — run a "
                "discovery on it first so compatibility can be computed."
            ),
        )
    return bench, bench.frappe_version


def _compat_for(reader: RegistryReader, name: str, frappe_version: str):
    """Compute (is_installable, reason, latest_compatible_version) for one app
    against a target Frappe version. Dependency conflicts count as not-installable
    with their legible reason (AC2/AC3)."""
    releases = reader.releases(name)
    compat = compatible_releases(releases, frappe_version)
    if not compat:
        avail = ", ".join(sorted({r.frappe_core for r in releases})) or "no releases"
        return (
            False,
            f"No release compatible with Frappe {frappe_version} "
            f"(available: {avail}).",
            None,
        )
    try:
        resolve_plan(reader, name, frappe_version)
    except ResolutionError as exc:
        return (False, str(exc), compat[0].version)
    return (True, None, compat[0].version)


# --------------------------------------------------------------------------- #
# Browse
# --------------------------------------------------------------------------- #


@router.get("/marketplace/apps", response_model=list[MarketplaceAppOut])
def list_marketplace_apps(
    cache: Cache,
    db: DbSession,
    _: Annotated[object, Depends(require(READ))],
    bench: int | None = Query(default=None),
    category: str | None = Query(default=None),
    q: str | None = Query(default=None),
) -> list[MarketplaceAppOut]:
    """The catalog. With `?bench=<id>` each app carries its compatibility against
    that bench's installed Frappe version; without it, compatibility is omitted."""
    reader = _reader_or_503(cache)

    frappe_version: str | None = None
    if bench is not None:
        _, frappe_version = _bench_frappe_version(db, bench)

    needle = (q or "").strip().lower()
    out: list[MarketplaceAppOut] = []
    for app in reader.apps():
        if category and category not in app.categories and category != app.category:
            continue
        if needle and needle not in app.title.lower() and needle not in app.description.lower():
            continue
        if frappe_version is not None:
            installable, reason, latest = _compat_for(reader, app.name, frappe_version)
            out.append(
                MarketplaceAppOut.from_app(
                    app,
                    is_installable=installable,
                    reason=reason,
                    latest_compatible_version=latest,
                )
            )
        else:
            out.append(MarketplaceAppOut.from_app(app))
    return out


@router.get("/marketplace/apps/{name}", response_model=MarketplaceAppDetailOut)
def get_marketplace_app(
    name: str,
    cache: Cache,
    db: DbSession,
    _: Annotated[object, Depends(require(READ))],
    bench: int | None = Query(default=None),
) -> MarketplaceAppDetailOut:
    """One app: its metadata, releases, and (with `?bench=`) the resolved install
    plan or a legible explanation of why it cannot be installed."""
    reader = _reader_or_503(cache)
    app = reader.get_app(name)
    if app is None:
        raise HTTPException(status_code=404, detail=f"App {name!r} is not in the catalog.")

    releases = reader.releases(name)
    frappe_version: str | None = None
    if bench is not None:
        _, frappe_version = _bench_frappe_version(db, bench)

    compat_versions: set[str] = set()
    if frappe_version is not None:
        compat_versions = {r.version for r in compatible_releases(releases, frappe_version)}

    detail = MarketplaceAppDetailOut.from_app(app)
    detail.releases = [
        MarketplaceReleaseOut.from_release(
            r,
            is_compatible=(r.version in compat_versions) if frappe_version else None,
        )
        for r in releases
    ]

    if frappe_version is not None:
        try:
            plan = resolve_plan(reader, name, frappe_version)
            detail.plan = [PlanStepOut.from_step(s) for s in plan]
            detail.is_installable = True
        except ResolutionError as exc:
            detail.plan_error = str(exc)
            detail.is_installable = False
            detail.reason = str(exc)
    return detail


@router.post("/marketplace/refresh", response_model=MarketplaceRefreshOut)
def refresh_marketplace(
    cache: Cache, user: CurrentUser, audit: Audit
) -> MarketplaceRefreshOut:
    """Force a catalog sync. A refresh that cannot reach the remote serves the
    existing cache rather than failing (AC1); only a first sync with no network
    and no cache errors."""
    _require_app_manage(user)
    try:
        refreshed = cache.refresh()
    except RegistryUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    reader = cache.reader()
    app_count = len(reader.apps())
    audit.record(
        action="marketplace.refresh",
        summary=f"Refreshed app catalog ({app_count} apps, "
        f"{'updated' if refreshed else 'served stale'})",
        entity_type="marketplace",
        entity_id=None,
    )
    return MarketplaceRefreshOut(
        refreshed=refreshed, served_stale=not refreshed, app_count=app_count
    )


# --------------------------------------------------------------------------- #
# Install (reuses the get-app/install-app connector path)
# --------------------------------------------------------------------------- #


@router.post("/sites/{site_id}/marketplace-apps", status_code=201, response_model=JobDetail)
def install_marketplace_app(
    site_id: int,
    body: MarketplaceInstallRequest,
    cache: Cache,
    db: DbSession,
    runner: Runner,
    user: CurrentUser,
):
    """Install a store app (and its resolved dependencies) on a site. The full
    transitive plan is resolved and validated against the site's bench's Frappe
    version BEFORE anything is enqueued — a cycle/conflict/incompatibility 422s
    with a legible message and nothing is installed (never partial, AC3). On
    success one job runs the whole plan through the existing connector (AC4)."""
    _require_app_manage(user)
    site = db.get(Site, site_id)
    if site is None:
        raise HTTPException(status_code=404, detail="Site not found.")
    bench = db.get(Bench, site.bench_id)
    if bench is None:  # pragma: no cover - FK-guaranteed
        raise HTTPException(status_code=404, detail="Site's bench is missing.")
    if not bench.frappe_version:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Bench {bench.name!r} has no known Frappe version — run a "
                "discovery on it first."
            ),
        )

    reader = _reader_or_503(cache)
    try:
        plan = resolve_plan(reader, body.app, bench.frappe_version)
    except ResolutionError as exc:
        # Cycle / version conflict / incompatibility / unknown app: legible 422,
        # nothing enqueued.
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    # Each step's repo host must clear the allowlist (golden rule 1) before we
    # launch a fetch. A step off an unapproved host is rejected up front, so the
    # plan is never partially installed.
    steps: list[dict[str, str]] = []
    for s in plan:
        source = s.repo or s.app
        try:
            validate_repo_source(source)
        except RepoSourceError as exc:
            raise HTTPException(
                status_code=422,
                detail=f"Cannot install {s.app!r}: {exc}",
            ) from exc
        steps.append({"app": s.app, "source": source, "branch": s.branch})

    plan_b64 = base64.b64encode(json.dumps(steps).encode()).decode()

    try:
        job = runner.create(
            db,
            action_name=INSTALL_ACTION,
            server_id=bench.server_id,
            target_type="site",
            target_id=f"{bench.path}::{site.name}",
            params={"site": site.name, "bench_path": bench.path, "plan_b64": plan_b64},
            priority=body.priority,
            created_by=user.id,
        )
    except LockConflict as exc:
        from fastapi.responses import JSONResponse

        return JSONResponse(
            status_code=409,
            content={
                "error": {
                    "code": "conflict",
                    "message": f"A job is already running on site {site.name!r}.",
                    "blocking_job_id": exc.blocking_job_id,
                }
            },
        )
    db.refresh(job)
    return JobDetail.from_model(job)
