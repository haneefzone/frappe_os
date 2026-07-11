"""Update advisor API (session 3.2) — read-only release detection.

- GET  /api/updates                         behind-by verdicts (chips + banner)
- GET  /api/updates/summary                 fleet rollup (dashboard needs-attention)
- GET  /api/updates/{installed_app_id}/changelog   the release-range preview
- POST /api/updates/refresh                 enqueue a poll now (202)

No endpoint here performs an update (that is the 3.3 safe-update pipeline). The
poll's outbound git traffic runs on a worker, never in the request (rule 3): the
GET routes read the DB/cache, and refresh enqueues. Reads need `read`; refresh
needs `app:manage`.
"""

from __future__ import annotations

import json
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, require
from app.audit import Audit
from app.core.permissions import APP_MANAGE, READ, role_allows
from app.core.updates import changelog_preview, resolve_repo, updates_summary
from app.db import get_db
from app.models.app import AppSource, InstalledApp
from app.models.bench import Bench
from app.models.site import Site
from app.models.updates import AppVersionStatus, UpstreamTagCache
from app.schemas.updates import (
    ChangelogPreviewOut,
    UpdatesSummaryOut,
    UpdateStatusOut,
)

router = APIRouter(prefix="/api", tags=["updates"])

DbSession = Annotated[Session, Depends(get_db)]


@router.get("/updates", response_model=list[UpdateStatusOut])
def list_updates(
    db: DbSession,
    _: Annotated[object, Depends(require(READ))],
    bench: int | None = Query(default=None),
    site: int | None = Query(default=None),
    behind_only: bool = Query(default=False),
) -> list[UpdateStatusOut]:
    """The advisor verdict per installed app. `behind_only=true` returns just the
    apps with an update available (what the banner/chips highlight)."""
    stmt = select(AppVersionStatus)
    if site is not None:
        stmt = stmt.where(AppVersionStatus.site_id == site)
    if behind_only:
        stmt = stmt.where(AppVersionStatus.behind_by > 0)
    rows = db.scalars(stmt).all()

    sites = {s.id: s for s in db.scalars(select(Site)).all()}
    benches = {b.id: b for b in db.scalars(select(Bench)).all()}
    # installed_app_id -> bench_id (the matrix cell owns the bench link).
    ia_bench = {
        ia.id: ia.bench_id for ia in db.scalars(select(InstalledApp)).all()
    }

    out: list[UpdateStatusOut] = []
    for r in sorted(rows, key=lambda x: (x.site_id, x.app_name)):
        s = sites.get(r.site_id)
        bench_id = ia_bench.get(r.installed_app_id)
        b = benches.get(bench_id) if bench_id is not None else None
        if s is None or b is None:
            continue
        if bench is not None and b.id != bench:
            continue
        out.append(
            UpdateStatusOut.from_row(
                r, bench_id=b.id, site_name=s.name, bench_name=b.name
            )
        )
    return out


@router.get("/updates/summary", response_model=UpdatesSummaryOut)
def get_updates_summary(
    db: DbSession, _: Annotated[object, Depends(require(READ))]
) -> UpdatesSummaryOut:
    return UpdatesSummaryOut(**updates_summary(db))


@router.get(
    "/updates/{installed_app_id}/changelog", response_model=ChangelogPreviewOut
)
def get_changelog(
    installed_app_id: int,
    db: DbSession,
    _: Annotated[object, Depends(require(READ))],
) -> ChangelogPreviewOut:
    """The release-range between the installed ref and the latest, from cached
    tags (populated by the poll). Read-only; no outbound call in the request."""
    ia = db.get(InstalledApp, installed_app_id)
    if ia is None:
        raise HTTPException(status_code=404, detail="Installed app not found.")

    source = db.get(AppSource, ia.app_source_id) if ia.app_source_id else None
    resolved = resolve_repo(ia.app_name, source)
    repo_key = resolved[0] if resolved else None

    tags: list[str] = []
    if repo_key is not None:
        cache = db.scalars(
            select(UpstreamTagCache).where(UpstreamTagCache.repo_key == repo_key)
        ).first()
        if cache and cache.tags_json:
            tags = json.loads(cache.tags_json)

    preview = changelog_preview(
        repo_key=repo_key,
        installed_ref=ia.version,
        branch=ia.branch,
        tags=tags,
    )
    return ChangelogPreviewOut(
        installed_app_id=ia.id, app_name=ia.app_name, **preview
    )


@router.post("/updates/refresh", status_code=202)
def refresh_updates(user: CurrentUser, audit: Audit) -> JSONResponse:
    """Enqueue an advisor poll now (force-refresh the tag cache). Returns 202 with
    the RQ job id; the sweep runs on the low queue. Requires `app:manage`."""
    if not role_allows(list(user.role.permissions or []), APP_MANAGE):
        raise HTTPException(
            status_code=403,
            detail=f"Role {user.role.name!r} lacks the {APP_MANAGE!r} permission.",
        )
    from app.workers.updates import enqueue_poll

    rq_job_id = enqueue_poll(force=True)
    audit.record(
        action="updates.refresh",
        summary="Enqueued an update-advisor poll (force refresh)",
        entity_type="updates",
    )
    return JSONResponse(status_code=202, content={"rq_job_id": rq_job_id})
