"""Sites API (session 1.8).

- GET  /api/sites                 list all discovered/created sites (+?bench=)
- GET  /api/sites/{id}            one site (detail page)
- POST /api/sites                 create a site -> `site.create` job
- POST /api/sites/{id}/scheduler  enable/disable the scheduler -> job (fast queue)
- POST /api/sites/{id}/maintenance turn maintenance on/off -> job (fast queue)

Creating a site and flipping its controls are state-changing remote operations,
so each POST enqueues a job and returns it (rule 3); the worker runs `bench
new-site` (wrapped in the dev-bench Redis dance) or the toggle command. Listing
is read-only (RBAC `read`); the mutations need `site:operate`, declared by the
templates and enforced here the same way POST /api/jobs is.
"""

from datetime import UTC, datetime, timedelta
from typing import Annotated
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, require
from app.api.routes.jobs import get_job_runner
from app.audit import Audit
from app.core.commands import RenderError, get_template
from app.core.jobs import JobRunner, LockConflict
from app.core.permissions import READ, SITE_OPERATE, role_allows
from app.core.secrets_resolve import SecretResolutionError
from app.core.uptime import HOURS_30D, rolling_uptime
from app.db import get_db
from app.models import Server, UptimeSample
from app.models.bench import Bench
from app.models.site import Site
from app.schemas.job import JobDetail
from app.schemas.site import (
    CreateSiteRequest,
    SiteActionRequest,
    SiteOut,
    SiteToggleRequest,
)
from app.schemas.uptime import (
    UptimeConfigRequest,
    UptimeSampleOut,
    UptimeSeries,
    UptimeSummary,
)

router = APIRouter(prefix="/api", tags=["sites"])

DbSession = Annotated[Session, Depends(get_db)]
Runner = Annotated[JobRunner, Depends(get_job_runner)]

CREATE_ACTION = "site.create"
SCHEDULER_ACTION = "site.set_scheduler"
MAINTENANCE_ACTION = "site.set_maintenance"
MIGRATE_ACTION = "site.migrate"
CLEAR_CACHE_ACTION = "site.clear_cache"
CLEAR_WEBSITE_CACHE_ACTION = "site.clear_website_cache"


def _require_action_permission(user, action_name: str) -> None:
    """Enforce the RBAC action-class the template declares (golden rule 7), the
    same gate POST /api/jobs applies."""
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


def _site_out(db: Session, site: Site) -> SiteOut:
    """Build a SiteOut, loading the site's bench + server for the enriched
    fields (env badge, open-site URL). Both must exist (the FK guarantees the
    bench; discovery/creation guarantees the server)."""
    bench = db.get(Bench, site.bench_id)
    server = db.get(Server, bench.server_id) if bench else None
    if bench is None or server is None:  # pragma: no cover - FK-guaranteed
        raise HTTPException(status_code=404, detail="Site's bench or server is missing.")
    return SiteOut.from_model(site, bench, server)


@router.get("/sites", response_model=list[SiteOut])
def list_sites(
    db: DbSession,
    _: Annotated[object, Depends(require(READ))],
    bench: int | None = Query(default=None),
) -> list[SiteOut]:
    """All sites, grouped in the UI by bench, optionally scoped to one bench.
    Benches and servers are prefetched into dicts to avoid an N+1 per site."""
    stmt = select(Site).order_by(Site.bench_id, Site.name)
    if bench is not None:
        stmt = stmt.where(Site.bench_id == bench)
    sites = db.scalars(stmt).all()

    benches = {b.id: b for b in db.scalars(select(Bench)).all()}
    servers = {s.id: s for s in db.scalars(select(Server)).all()}
    out: list[SiteOut] = []
    for site in sites:
        b = benches.get(site.bench_id)
        s = servers.get(b.server_id) if b else None
        if b is not None and s is not None:
            out.append(SiteOut.from_model(site, b, s))
    return out


@router.get("/sites/{site_id}", response_model=SiteOut)
def get_site(
    site_id: int, db: DbSession, _: Annotated[object, Depends(require(READ))]
) -> SiteOut:
    site = db.get(Site, site_id)
    if site is None:
        raise HTTPException(status_code=404, detail="Site not found.")
    return _site_out(db, site)


@router.post("/sites", status_code=201, response_model=JobDetail)
def create_site(
    body: CreateSiteRequest, db: DbSession, runner: Runner, user: CurrentUser
):
    """Create a site inside a known bench: one `site.create` job runs the dev/
    prod detection, the dev-bench Redis dance, `bench new-site`, then registers
    the site. The admin password is carried to the worker encrypted; the MariaDB
    root password is pulled from the server settings server-side."""
    _require_action_permission(user, CREATE_ACTION)

    bench = db.get(Bench, body.bench_id)
    if bench is None:
        raise HTTPException(status_code=404, detail="Bench not found.")

    try:
        job = runner.create(
            db,
            action_name=CREATE_ACTION,
            server_id=bench.server_id,
            # Lock on the bench so two site creates on the same dev bench can't
            # fight over its shared Redis (rule 4 + gotcha #3).
            target_type="bench",
            target_id=bench.path,
            params={"site": body.name, "bench_path": bench.path},
            user_secrets={"admin_pw": body.admin_password},
            priority=body.priority,
            created_by=user.id,
        )
    except SecretResolutionError as exc:
        # e.g. the server has no MariaDB root password set yet.
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RenderError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except LockConflict as exc:
        return _conflict(
            exc, f"A create/site job is already running on bench {bench.path!r}."
        )

    db.refresh(job)
    return JobDetail.from_model(job)


def _launch_toggle(
    db: Session,
    runner: JobRunner,
    user,
    *,
    site: Site,
    action_name: str,
    state: str,
):
    """Shared launcher for the scheduler/maintenance toggles: resolve the site's
    bench, enqueue the single-command job locked on the site, return the job."""
    bench = db.get(Bench, site.bench_id)
    if bench is None:  # pragma: no cover - FK-guaranteed
        raise HTTPException(status_code=404, detail="Site's bench is missing.")
    _require_action_permission(user, action_name)
    try:
        job = runner.create(
            db,
            action_name=action_name,
            server_id=bench.server_id,
            target_type="site",
            target_id=f"{bench.path}::{site.name}",
            params={"site": site.name, "bench_path": bench.path, "state": state},
            priority="high",
            created_by=user.id,
        )
    except RenderError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except LockConflict as exc:
        return _conflict(exc, f"A job is already running on site {site.name!r}.")
    db.refresh(job)
    return JobDetail.from_model(job)


@router.post("/sites/{site_id}/scheduler", status_code=201, response_model=JobDetail)
def set_scheduler(
    site_id: int,
    body: SiteToggleRequest,
    db: DbSession,
    runner: Runner,
    user: CurrentUser,
):
    """Enable or disable the site's scheduler (fast queue)."""
    site = db.get(Site, site_id)
    if site is None:
        raise HTTPException(status_code=404, detail="Site not found.")
    return _launch_toggle(
        db,
        runner,
        user,
        site=site,
        action_name=SCHEDULER_ACTION,
        state="enable" if body.enabled else "disable",
    )


@router.post("/sites/{site_id}/maintenance", status_code=201, response_model=JobDetail)
def set_maintenance(
    site_id: int,
    body: SiteToggleRequest,
    db: DbSession,
    runner: Runner,
    user: CurrentUser,
):
    """Turn maintenance mode on or off for the site (fast queue)."""
    site = db.get(Site, site_id)
    if site is None:
        raise HTTPException(status_code=404, detail="Site not found.")
    return _launch_toggle(
        db,
        runner,
        user,
        site=site,
        action_name=MAINTENANCE_ACTION,
        state="on" if body.enabled else "off",
    )


def _launch_site_maintenance(
    db: Session,
    runner: JobRunner,
    user,
    *,
    site: Site,
    action_name: str,
    priority: str,
):
    """Shared launcher for the single-command site maintenance ops (migrate /
    clear-cache / clear-website-cache): resolve the site's bench, enqueue the job
    locked on the site, return the job. Each op runs `bench --site X <verb>`
    wrapped in the dev-bench Redis dance by SiteMaintenanceAction."""
    bench = db.get(Bench, site.bench_id)
    if bench is None:  # pragma: no cover - FK-guaranteed
        raise HTTPException(status_code=404, detail="Site's bench is missing.")
    _require_action_permission(user, action_name)
    try:
        job = runner.create(
            db,
            action_name=action_name,
            server_id=bench.server_id,
            target_type="site",
            target_id=f"{bench.path}::{site.name}",
            params={"site": site.name, "bench_path": bench.path},
            priority=priority,
            created_by=user.id,
        )
    except RenderError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except LockConflict as exc:
        return _conflict(exc, f"A job is already running on site {site.name!r}.")
    db.refresh(job)
    return JobDetail.from_model(job)


def _get_site_or_404(db: Session, site_id: int) -> Site:
    site = db.get(Site, site_id)
    if site is None:
        raise HTTPException(status_code=404, detail="Site not found.")
    return site


@router.post("/sites/{site_id}/migrate", status_code=201, response_model=JobDetail)
def migrate_site(
    site_id: int,
    body: SiteActionRequest,
    db: DbSession,
    runner: Runner,
    user: CurrentUser,
):
    """Run `bench --site X migrate` on the site (pending schema patches)."""
    site = _get_site_or_404(db, site_id)
    return _launch_site_maintenance(
        db, runner, user, site=site, action_name=MIGRATE_ACTION, priority=body.priority
    )


@router.post("/sites/{site_id}/clear-cache", status_code=201, response_model=JobDetail)
def clear_site_cache(
    site_id: int,
    body: SiteActionRequest,
    db: DbSession,
    runner: Runner,
    user: CurrentUser,
):
    """Run `bench --site X clear-cache` on the site."""
    site = _get_site_or_404(db, site_id)
    return _launch_site_maintenance(
        db,
        runner,
        user,
        site=site,
        action_name=CLEAR_CACHE_ACTION,
        priority=body.priority,
    )


@router.post(
    "/sites/{site_id}/clear-website-cache", status_code=201, response_model=JobDetail
)
def clear_site_website_cache(
    site_id: int,
    body: SiteActionRequest,
    db: DbSession,
    runner: Runner,
    user: CurrentUser,
):
    """Run `bench --site X clear-website-cache` on the site."""
    site = _get_site_or_404(db, site_id)
    return _launch_site_maintenance(
        db,
        runner,
        user,
        site=site,
        action_name=CLEAR_WEBSITE_CACHE_ACTION,
        priority=body.priority,
    )


# --------------------------------------------------------------------------- #
# Uptime (session 2.7): external HTTP checks — series + summary + config
# --------------------------------------------------------------------------- #


@router.get("/sites/{site_id}/uptime", response_model=UptimeSeries)
def site_uptime(
    site_id: int,
    db: DbSession,
    _: Annotated[object, Depends(require(READ))],
    hours: int = Query(24, ge=1, le=HOURS_30D),
) -> UptimeSeries:
    """Uptime samples for the last `hours` (response-time sparkline) plus the
    rolling 24h/30d summary the Overview health card renders. Read-only."""
    site = _get_site_or_404(db, site_id)
    since = datetime.now(UTC) - timedelta(hours=hours)
    rows = list(
        db.scalars(
            select(UptimeSample)
            .where(UptimeSample.site_id == site_id, UptimeSample.ts >= since)
            .order_by(UptimeSample.ts.asc())
        ).all()
    )
    return UptimeSeries(
        site_id=site_id,
        enabled=site.uptime_enabled,
        check_url=site.check_url,
        summary=UptimeSummary(**rolling_uptime(db, site_id)),
        samples=[UptimeSampleOut.from_model(r) for r in rows],
    )


def _validate_check_url(url: str) -> str:
    """A check-URL override must be a plain http(s) URL with a host — no shell,
    no other schemes. The checker only ever GETs it, but keep the surface tight."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise HTTPException(
            status_code=422,
            detail="check_url must be an http(s) URL, e.g. https://erp.example.com/api/method/ping.",
        )
    return url


@router.post("/sites/{site_id}/uptime-config", response_model=SiteOut)
def set_uptime_config(
    site_id: int,
    body: UptimeConfigRequest,
    db: DbSession,
    user: CurrentUser,
    audit: Audit,
) -> SiteOut:
    """Enable/disable uptime checking or set a check-URL override for the site.

    This is platform configuration for a read-only telemetry probe, not a remote
    command, so it updates the row directly (no job) but still writes an audit row
    (rule 2). Requires `site:operate`; Read-only can never mutate."""
    if not role_allows(list(user.role.permissions or []), SITE_OPERATE):
        raise HTTPException(
            status_code=403,
            detail=f"Role {user.role.name!r} lacks the {SITE_OPERATE!r} permission.",
        )
    site = _get_site_or_404(db, site_id)
    changes: dict[str, object] = {}
    if body.enabled is not None:
        site.uptime_enabled = body.enabled
        changes["uptime_enabled"] = body.enabled
    if body.check_url is not None:
        url = body.check_url.strip()
        site.check_url = _validate_check_url(url) if url else None
        changes["check_url"] = site.check_url
    db.commit()
    db.refresh(site)
    audit.record(
        action="site.uptime_config",
        summary=f"Updated uptime config for site {site.name}",
        entity_type="site",
        entity_id=site.id,
        params=changes,
    )
    return _site_out(db, site)
