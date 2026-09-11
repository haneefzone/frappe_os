"""Per-server aggregation rollup (session 2.6).

One read-only query bundle that answers "how is THIS server doing?" for the
Server detail Overview (uiux-spec B4.2): its capacity + service health from the
latest monitoring sample, how many benches/sites it carries, the state of its
sites (up/down from the newest uptime probe each), its job outcomes over the
last 24h, and its backup footprint. Everything is derived — no new columns — so
the rollup always reflects live inventory.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.backup import Backup
from app.models.bench import Bench
from app.models.job import CommandJob
from app.models.monitoring import MonitoringSample
from app.models.server import Server
from app.models.site import Site
from app.models.uptime import UptimeSample


@dataclass
class CapacityRollup:
    ok: bool
    cpu_pct: float | None
    mem_pct: float | None
    disk_pct: float | None
    mem_used_mb: int | None
    mem_total_mb: int | None
    disk_used_gb: float | None
    disk_total_gb: float | None
    load1: float | None
    services: dict[str, str]
    sampled_at: datetime | None
    error: str | None


@dataclass
class SitesRollup:
    total: int
    up: int
    down: int
    unknown: int


@dataclass
class JobsRollup:
    total: int
    success: int
    failure: int
    running: int


@dataclass
class BackupsRollup:
    count: int
    total_size_bytes: int
    last_backup_at: datetime | None


@dataclass
class ServerDashboard:
    server_id: int
    name: str
    hostname: str
    env_tag: str
    status: str
    last_seen: datetime | None
    benches: int
    capacity: CapacityRollup | None
    sites: SitesRollup
    jobs_24h: JobsRollup
    backups: BackupsRollup
    generated_at: datetime = field(default_factory=lambda: datetime.now(UTC))


def _bench_ids(db: Session, server_id: int) -> list[int]:
    return list(db.scalars(select(Bench.id).where(Bench.server_id == server_id)).all())


def _capacity(db: Session, server_id: int) -> CapacityRollup | None:
    sample = db.scalars(
        select(MonitoringSample)
        .where(MonitoringSample.server_id == server_id)
        .order_by(MonitoringSample.ts.desc())
        .limit(1)
    ).first()
    if sample is None:
        return None
    return CapacityRollup(
        ok=sample.ok,
        cpu_pct=sample.cpu_pct,
        mem_pct=sample.mem_pct,
        disk_pct=sample.disk_pct,
        mem_used_mb=sample.mem_used_mb,
        mem_total_mb=sample.mem_total_mb,
        disk_used_gb=sample.disk_used_gb,
        disk_total_gb=sample.disk_total_gb,
        load1=sample.load1,
        services=dict(sample.services or {}),
        sampled_at=sample.ts,
        error=sample.error,
    )


def _sites(db: Session, bench_ids: list[int]) -> SitesRollup:
    if not bench_ids:
        return SitesRollup(0, 0, 0, 0)
    site_ids = list(
        db.scalars(select(Site.id).where(Site.bench_id.in_(bench_ids))).all()
    )
    total = len(site_ids)
    up = down = 0
    for sid in site_ids:
        latest = db.scalars(
            select(UptimeSample)
            .where(UptimeSample.site_id == sid)
            .order_by(UptimeSample.ts.desc())
            .limit(1)
        ).first()
        if latest is None:
            continue
        if latest.up:
            up += 1
        else:
            down += 1
    return SitesRollup(total=total, up=up, down=down, unknown=total - up - down)


def _jobs_24h(db: Session, server_id: int, *, now: datetime) -> JobsRollup:
    since = now - timedelta(hours=24)
    rows = db.execute(
        select(CommandJob.status, func.count())
        .where(CommandJob.server_id == server_id, CommandJob.created_at >= since)
        .group_by(CommandJob.status)
    ).all()
    counts = {status: n for status, n in rows}
    return JobsRollup(
        total=sum(counts.values()),
        success=counts.get("success", 0),
        failure=counts.get("failure", 0),
        running=counts.get("running", 0) + counts.get("pending", 0),
    )


def _backups(db: Session, bench_ids: list[int]) -> BackupsRollup:
    if not bench_ids:
        return BackupsRollup(0, 0, None)
    total_size, count, last_at = db.execute(
        select(
            func.coalesce(func.sum(Backup.size_bytes), 0),
            func.count(),
            func.max(Backup.created_at),
        ).where(Backup.bench_id.in_(bench_ids), Backup.status == "success")
    ).one()
    return BackupsRollup(
        count=int(count or 0),
        total_size_bytes=int(total_size or 0),
        last_backup_at=last_at,
    )


def server_dashboard(
    db: Session, server: Server, *, now: datetime | None = None
) -> ServerDashboard:
    """Assemble the per-server rollup for the Server Overview (B4.2)."""
    now = now or datetime.now(UTC)
    bench_ids = _bench_ids(db, server.id)
    return ServerDashboard(
        server_id=server.id,
        name=server.name,
        hostname=server.hostname,
        env_tag=server.env_tag,
        status=server.status,
        last_seen=server.last_seen,
        benches=len(bench_ids),
        capacity=_capacity(db, server.id),
        sites=_sites(db, bench_ids),
        jobs_24h=_jobs_24h(db, server.id, now=now),
        backups=_backups(db, bench_ids),
    )
