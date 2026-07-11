"""Domains & SSL API (session 2.4).

- GET    /api/sites/{id}/domains                     list a site's domains
- POST   /api/sites/{id}/domains                     add a domain (config row)
- DELETE /api/sites/{id}/domains/{did}               remove a domain (config row)
- POST   /api/sites/{id}/domains/{did}/test-dns      -> domain.dns_check job
- POST   /api/sites/{id}/domains/{did}/render-vhost  -> nginx.render_vhost job
- POST   /api/sites/{id}/domains/{did}/issue-cert    -> ssl.certbot_issue job

Adding/removing a domain is platform configuration (like the uptime toggle), so
it updates a row directly and writes an audit record (rule 2) — no remote
command. Generating the nginx vhost, checking DNS and issuing a certificate are
remote operations, so each POST enqueues a job and returns it (rule 3). Every
mutation needs `ssl:manage`; Read-only can never mutate (rule 7).
"""

import re
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, require
from app.api.routes.jobs import get_job_runner
from app.audit import Audit
from app.core.commands import RenderError, get_template
from app.core.commands.registry import DOMAIN_NAME
from app.core.jobs import JobRunner, LockConflict
from app.core.permissions import READ, SSL_MANAGE, role_allows
from app.db import get_db
from app.models.bench import Bench
from app.models.domain import Domain
from app.models.site import Site
from app.schemas.domain import (
    CreateDomainRequest,
    DomainActionRequest,
    DomainOut,
    IssueCertRequest,
)
from app.schemas.job import JobDetail

router = APIRouter(prefix="/api", tags=["domains"])

DbSession = Annotated[Session, Depends(get_db)]
Runner = Annotated[JobRunner, Depends(get_job_runner)]

DNS_CHECK_ACTION = "domain.dns_check"
RENDER_VHOST_ACTION = "nginx.render_vhost"
ISSUE_CERT_ACTION = "ssl.certbot_issue"

_DOMAIN_RE = re.compile(DOMAIN_NAME)


def _require_ssl_manage(user) -> None:
    if not role_allows(list(user.role.permissions or []), SSL_MANAGE):
        raise HTTPException(
            status_code=403,
            detail=f"Role {user.role.name!r} lacks the {SSL_MANAGE!r} permission.",
        )


def _get_site_or_404(db: Session, site_id: int) -> Site:
    site = db.get(Site, site_id)
    if site is None:
        raise HTTPException(status_code=404, detail="Site not found.")
    return site


def _get_domain_or_404(db: Session, site_id: int, domain_id: int) -> Domain:
    d = db.get(Domain, domain_id)
    if d is None or d.site_id != site_id:
        raise HTTPException(status_code=404, detail="Domain not found.")
    return d


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


@router.get("/sites/{site_id}/domains", response_model=list[DomainOut])
def list_domains(
    site_id: int, db: DbSession, _: Annotated[object, Depends(require(READ))]
) -> list[DomainOut]:
    _get_site_or_404(db, site_id)
    rows = db.scalars(
        select(Domain).where(Domain.site_id == site_id).order_by(
            Domain.is_primary.desc(), Domain.domain
        )
    ).all()
    return [DomainOut.from_model(d) for d in rows]


@router.post("/sites/{site_id}/domains", status_code=201, response_model=DomainOut)
def add_domain(
    site_id: int,
    body: CreateDomainRequest,
    db: DbSession,
    user: CurrentUser,
    audit: Audit,
) -> DomainOut:
    _require_ssl_manage(user)
    site = _get_site_or_404(db, site_id)

    domain = body.domain.strip().lower()
    if _DOMAIN_RE.fullmatch(domain) is None:
        raise HTTPException(
            status_code=422,
            detail="domain must be a valid lowercase hostname, e.g. erp.example.com.",
        )
    existing = db.scalar(select(Domain).where(Domain.domain == domain))
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail=f"Domain {domain!r} is already registered.",
        )

    if body.is_primary:
        for other in db.scalars(
            select(Domain).where(Domain.site_id == site_id, Domain.is_primary.is_(True))
        ).all():
            other.is_primary = False

    row = Domain(site_id=site.id, domain=domain, is_primary=body.is_primary)
    db.add(row)
    db.commit()
    db.refresh(row)
    audit.record(
        action="domain.add",
        summary=f"Added domain {domain} to site {site.name}",
        entity_type="domain",
        entity_id=row.id,
        params={"domain": domain, "is_primary": body.is_primary},
    )
    return DomainOut.from_model(row)


@router.delete("/sites/{site_id}/domains/{domain_id}", status_code=200)
def remove_domain(
    site_id: int,
    domain_id: int,
    db: DbSession,
    user: CurrentUser,
    audit: Audit,
) -> dict:
    _require_ssl_manage(user)
    site = _get_site_or_404(db, site_id)
    row = _get_domain_or_404(db, site_id, domain_id)
    domain = row.domain
    db.delete(row)
    db.commit()
    audit.record(
        action="domain.remove",
        summary=f"Removed domain {domain} from site {site.name}",
        entity_type="domain",
        entity_id=domain_id,
        params={"domain": domain},
    )
    # The generated vhost file / certificate are left on the server; an operator
    # removes them out of band (documented) — the platform never deletes remote
    # nginx/letsencrypt state on a config-row delete.
    return {"ok": True, "domain": domain}


def _launch_domain_job(
    db: Session,
    runner: JobRunner,
    user,
    *,
    site: Site,
    domain: Domain,
    action_name: str,
    extra_params: dict | None = None,
    priority: str = "default",
):
    """Enqueue a domain/SSL job. nginx-mutating actions (render_vhost / certbot)
    lock per-server; the read-only dns_check does not (its template says so)."""
    bench = db.get(Bench, site.bench_id)
    if bench is None:  # pragma: no cover - FK-guaranteed
        raise HTTPException(status_code=404, detail="Site's bench is missing.")
    _require_ssl_manage(user)
    template = get_template(action_name)
    params = {
        "domain": domain.domain,
        "domain_id": str(domain.id),
        "site": site.name,
        "bench_path": bench.path,
    }
    if extra_params:
        params.update(extra_params)
    # nginx-mutating actions serialize per server; dns_check is lock-free.
    target_type = "server" if template.requires_lock else "site"
    target_id = None if template.requires_lock else f"{bench.path}::{site.name}"
    try:
        job = runner.create(
            db,
            action_name=action_name,
            server_id=bench.server_id,
            target_type=target_type,
            target_id=target_id,
            params=params,
            priority=priority,
            created_by=user.id,
        )
    except RenderError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except LockConflict as exc:
        return _conflict(
            exc, f"An nginx/SSL job is already running on server {bench.server_id}."
        )
    db.refresh(job)
    return JobDetail.from_model(job)


@router.post(
    "/sites/{site_id}/domains/{domain_id}/test-dns",
    status_code=201,
    response_model=JobDetail,
)
def test_dns(
    site_id: int,
    domain_id: int,
    body: DomainActionRequest,
    db: DbSession,
    runner: Runner,
    user: CurrentUser,
):
    """Resolve the domain's A/AAAA records and compare to the server IP."""
    site = _get_site_or_404(db, site_id)
    domain = _get_domain_or_404(db, site_id, domain_id)
    return _launch_domain_job(
        db, runner, user, site=site, domain=domain,
        action_name=DNS_CHECK_ACTION, priority=body.priority,
    )


@router.post(
    "/sites/{site_id}/domains/{domain_id}/render-vhost",
    status_code=201,
    response_model=JobDetail,
)
def render_vhost(
    site_id: int,
    domain_id: int,
    body: DomainActionRequest,
    db: DbSession,
    runner: Runner,
    user: CurrentUser,
):
    """(Re)generate the domain's nginx vhost, validate with `nginx -t`, reload."""
    site = _get_site_or_404(db, site_id)
    domain = _get_domain_or_404(db, site_id, domain_id)
    return _launch_domain_job(
        db, runner, user, site=site, domain=domain,
        action_name=RENDER_VHOST_ACTION,
        extra_params={"ssl": "on" if domain.ssl_enabled else "off"},
        priority=body.priority,
    )


@router.post(
    "/sites/{site_id}/domains/{domain_id}/issue-cert",
    status_code=201,
    response_model=JobDetail,
)
def issue_cert(
    site_id: int,
    domain_id: int,
    body: IssueCertRequest,
    db: DbSession,
    runner: Runner,
    user: CurrentUser,
):
    """Issue a Let's Encrypt certificate for the domain (certbot --webroot),
    then re-render the vhost with TLS on and reload."""
    site = _get_site_or_404(db, site_id)
    domain = _get_domain_or_404(db, site_id, domain_id)
    return _launch_domain_job(
        db, runner, user, site=site, domain=domain,
        action_name=ISSUE_CERT_ACTION,
        extra_params={"email": body.email.strip()},
        priority=body.priority,
    )
