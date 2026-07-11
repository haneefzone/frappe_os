"""Dashboard v1 aggregation (session 1.12, B4.1 — "the CEO screen").

`build_dashboard(db)` returns the whole payload the dashboard renders in one
round-trip: KPI cards, a morning-brief sentence, the servers strip (latest
mini-gauges), a 7-day backup grid, the running-jobs feed, and the first-run flag.
All figures come from real rows — no placeholder data — except the three Fleet
Health components the platform cannot measure yet (documented below).

### Fleet Health formula (documented per the session spec)

    fleet_health = uptime(40 × frac) + backup(30 × frac) + alerts(20) + updates(10)

Two components are **real** today:
  - uptime (40)  → 40 × (fleet 30-day uptime fraction from external HTTP checks;
                   1.0 when nothing has been measured yet — a fresh fleet is not
                   penalised). Became real in session 2.7 (was a placeholder).
  - backup (30)  → 30 × Backup Compliance fraction = compliant / policied sites
                   (session 2.3 — the compliance evaluator; replaces the 1.12
                   "backed up in last 24h" placeholder). 1.0 when no site has a
                   policy yet — nothing under policy is vacuously compliant.
The other two are **placeholders awarded in full** until their engines exist:
  - alerts (20)  → real "no critical alerts" lands with the AlertRule engine
  - updates (10) → real "everything up to date" lands with the update advisor
So health is driven by real uptime + backup compliance today; each remaining
placeholder becomes a live signal in a later phase without changing this shape.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.compliance import compliance_counts
from app.core.uptime import fleet_uptime_fraction
from app.models import Backup, CommandJob, Domain, MonitoringSample, Server, Site

# Fleet Health component weights (must sum to 100).
HEALTH_W_UPTIME = 40  # real (session 2.7 — external HTTP checks)
HEALTH_W_BACKUP = 30  # real (session 2.3 — backup compliance)
HEALTH_W_ALERTS = 20  # placeholder (Phase 2)
HEALTH_W_UPDATES = 10  # placeholder (Phase 3)

RUNNING_STATUSES = ("pending", "running")
BACKUP_GRID_DAYS = 7


def _now() -> datetime:
    return datetime.now(UTC)


def _latest_sample_by_server(db: Session) -> dict[int, MonitoringSample]:
    """The most recent monitoring sample per server (one pass)."""
    rows = db.scalars(
        select(MonitoringSample).order_by(
            MonitoringSample.server_id, MonitoringSample.ts.desc()
        )
    ).all()
    latest: dict[int, MonitoringSample] = {}
    for s in rows:
        latest.setdefault(s.server_id, s)  # first per server = newest (ts desc)
    return latest


def _backup_grid(db: Session) -> list[dict]:
    """A GitHub-contribution-style 7-day grid: per day, success/failed counts."""
    today = _now().date()
    start = today - timedelta(days=BACKUP_GRID_DAYS - 1)
    start_dt = datetime(start.year, start.month, start.day, tzinfo=UTC)
    rows = db.execute(
        select(Backup.created_at, Backup.status).where(Backup.created_at >= start_dt)
    ).all()

    buckets: dict[date, dict[str, int]] = {
        start + timedelta(days=i): {"success": 0, "failed": 0}
        for i in range(BACKUP_GRID_DAYS)
    }
    for created_at, status in rows:
        # SQLite (tests) returns tz-naive UTC; Postgres returns tz-aware. Treat a
        # naive value as UTC so the day bucket is correct under any local tz.
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        day = created_at.astimezone(UTC).date()
        if day in buckets:
            key = "success" if status == "success" else "failed"
            buckets[day][key] += 1

    return [
        {
            "date": day.isoformat(),
            "success": counts["success"],
            "failed": counts["failed"],
        }
        for day, counts in sorted(buckets.items())
    ]


def _morning_brief(
    *,
    servers_total: int,
    servers_online: int,
    sites_up: int,
    sites_total: int,
    backups_24h: int,
    failed_jobs_24h: int,
) -> str:
    """One plain-language sentence summarising the fleet from real numbers."""
    if servers_total == 0:
        return "No servers yet — add your first server to start managing benches."
    parts = [
        f"{servers_online}/{servers_total} server{'s' if servers_total != 1 else ''} online",
        f"{sites_up}/{sites_total} site{'s' if sites_total != 1 else ''} active",
        f"{backups_24h} backup{'s' if backups_24h != 1 else ''} in the last 24h",
    ]
    tail = (
        f"{failed_jobs_24h} failed job{'s' if failed_jobs_24h != 1 else ''} needs attention"
        if failed_jobs_24h
        else "no failed jobs"
    )
    return "Good morning — " + ", ".join(parts) + f", and {tail}."


def build_dashboard(db: Session) -> dict:
    now = _now()
    since_24h = now - timedelta(hours=24)

    servers = list(db.scalars(select(Server).order_by(Server.name)).all())
    latest = _latest_sample_by_server(db)
    servers_online = sum(1 for s in servers if s.status == "online")

    sites_total = db.scalar(select(func.count(Site.id)).where(Site.status == "active")) or 0
    sites_up = (
        db.scalar(
            select(func.count(Site.id)).where(
                Site.status == "active", Site.health == "ok"
            )
        )
        or 0
    )

    backups_24h = (
        db.scalar(
            select(func.count(Backup.id)).where(
                Backup.status == "success", Backup.created_at >= since_24h
            )
        )
        or 0
    )
    failed_jobs_24h = (
        db.scalar(
            select(func.count(CommandJob.id)).where(
                CommandJob.status == "failure", CommandJob.updated_at >= since_24h
            )
        )
        or 0
    )

    # SSL expiring ≤30d (session 2.4, B4.1 KPI): SSL-enabled domains whose
    # certificate expires within 30 days (and hasn't already lapsed) — the
    # renewal watch-list the dashboard surfaces.
    ssl_expiring_30d = (
        db.scalar(
            select(func.count(Domain.id)).where(
                Domain.ssl_enabled.is_(True),
                Domain.cert_expires_at.is_not(None),
                Domain.cert_expires_at <= now + timedelta(days=30),
            )
        )
        or 0
    )

    # Fleet Health (see module docstring for the documented formula).
    # Backup component is real Backup Compliance % now (session 2.3): the fraction
    # of policied sites the evaluator last found compliant. No policies yet → 1.0.
    compliant_sites, policied_sites = compliance_counts(db)
    backup_fraction = 1.0 if policied_sites == 0 else compliant_sites / policied_sites
    uptime_fraction, _ = fleet_uptime_fraction(db)  # real 30-day uptime (2.7)
    fleet_health = round(
        HEALTH_W_UPTIME * uptime_fraction
        + HEALTH_W_BACKUP * backup_fraction
        + HEALTH_W_ALERTS
        + HEALTH_W_UPDATES
    )

    running = list(
        db.scalars(
            select(CommandJob)
            .where(CommandJob.status.in_(RUNNING_STATUSES))
            .order_by(CommandJob.created_at.desc())
            .limit(25)
        ).all()
    )

    servers_strip = []
    for s in servers:
        sample = latest.get(s.id)
        servers_strip.append(
            {
                "id": s.id,
                "name": s.name,
                "env_tag": s.env_tag,
                "status": s.status,
                "cpu_pct": sample.cpu_pct if sample else None,
                "mem_pct": sample.mem_pct if sample else None,
                "disk_pct": sample.disk_pct if sample else None,
                "sample_ts": sample.ts.isoformat() if sample else None,
            }
        )

    return {
        "generated_at": now.isoformat(),
        "onboarding": {"has_servers": bool(servers)},
        "kpis": {
            "fleet_health_pct": fleet_health,
            "sites_up": int(sites_up),
            "sites_total": int(sites_total),
            "backups_24h": int(backups_24h),
            "failed_jobs_24h": int(failed_jobs_24h),
            # Real Backup Compliance % (session 2.3): compliant / policied sites.
            "backup_compliance_pct": round(backup_fraction * 100),
            "sites_compliant": int(compliant_sites),
            "sites_policied": int(policied_sites),
            # Real 30-day fleet uptime (session 2.7); pairs with Sites Up (n/n).
            "uptime_30d_pct": round(uptime_fraction * 100),
            # SSL certificates expiring within 30 days (session 2.4).
            "ssl_expiring_30d": int(ssl_expiring_30d),
        },
        "morning_brief": _morning_brief(
            servers_total=len(servers),
            servers_online=servers_online,
            sites_up=int(sites_up),
            sites_total=int(sites_total),
            backups_24h=int(backups_24h),
            failed_jobs_24h=int(failed_jobs_24h),
        ),
        "servers": servers_strip,
        "backup_grid": _backup_grid(db),
        "running_jobs": [
            {
                "id": j.id,
                "action_name": j.action_name,
                "status": j.status,
                "target_id": j.target_id,
                "server_id": j.server_id,
                "created_at": j.created_at.isoformat(),
            }
            for j in running
        ],
    }
