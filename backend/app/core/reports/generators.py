"""The seven prebuilt report generators (session 6.2, uiux-spec B4.16).

Every generator is a pure `(db, params, *, now) -> ReportResult` function over
models that already exist. **No generator defines a metric.** Where a figure
already has an owner it is imported from there:

- fleet health, backup counts, failed-job counts -> `app.core.dashboard`
- backup compliance state / breaches            -> `app.core.compliance`
- uptime percentages                            -> `app.core.uptime`

That is deliberate: a report and the dashboard tile showing the same number
must never be able to disagree, and an ISO evidence export must state the same
compliance verdict the platform acted on.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.audit import mask_params
from app.core.compliance import compliance_counts, evaluate_policy
from app.core.dashboard import build_dashboard
from app.core.permissions import READ, REPORT_SENSITIVE
from app.core.reports.registry import (
    RANGE_DAYS,
    ReportDef,
    ReportParam,
    ReportResult,
    register,
    resolve_range,
)
from app.core.uptime import fleet_uptime_fraction, rolling_uptime
from app.models.app import InstalledApp
from app.models.audit import AuditLog
from app.models.auth import User
from app.models.backup import Backup
from app.models.bench import Bench
from app.models.compliance import BackupPolicy
from app.models.domain import Domain
from app.models.job import CommandJob
from app.models.monitoring import MonitoringSample
from app.models.server import Server
from app.models.site import Site


def _aware(dt: datetime | None) -> datetime | None:
    """Treat a tz-naive timestamp as UTC.

    SQLite (tests) hands back naive datetimes where Postgres returns aware ones;
    the same helper guards `app.core.compliance` for the same reason. Without it
    every date comparison below would raise on one backend and pass on the other.
    """
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _iso(dt: datetime | None) -> str:
    aware = _aware(dt)
    return aware.isoformat(timespec="seconds") if aware else ""


def _pct(value: float | None) -> str:
    return "" if value is None else f"{value:.1f}%"


# --------------------------------------------------------------------------- #
# 1. Fleet summary
# --------------------------------------------------------------------------- #


def fleet_summary(db: Session, params: dict[str, Any], *, now: datetime) -> ReportResult:
    """Fleet-wide posture: one row per server, with the dashboard's own KPIs as
    the summary block. Reuses `build_dashboard` verbatim so the report's
    fleet-health score is the score the dashboard shows, weights and all."""
    dashboard = build_dashboard(db)
    kpis = dashboard.get("kpis", {})

    site_counts = dict(
        db.execute(
            select(Bench.server_id, func.count(Site.id))
            .join(Site, Site.bench_id == Bench.id)
            .group_by(Bench.server_id)
        ).all()
    )
    bench_counts = dict(
        db.execute(select(Bench.server_id, func.count(Bench.id)).group_by(Bench.server_id)).all()
    )

    rows = []
    for server in db.scalars(select(Server).order_by(Server.name)).all():
        rows.append(
            {
                "server": server.name,
                "hostname": server.hostname,
                "env": server.env_tag,
                "status": server.status,
                "benches": bench_counts.get(server.id, 0),
                "sites": site_counts.get(server.id, 0),
                "os_version": server.os_version or "",
                "last_seen": _iso(server.last_seen),
            }
        )

    return ReportResult(
        columns=[
            ("server", "Server"),
            ("hostname", "Hostname"),
            ("env", "Env"),
            ("status", "Status"),
            ("benches", "Benches"),
            ("sites", "Sites"),
            ("os_version", "OS"),
            ("last_seen", "Last seen"),
        ],
        rows=rows,
        summary=[
            ("Fleet health", f"{kpis.get('fleet_health_pct', 0)}%"),
            ("Sites up", f"{kpis.get('sites_up', 0)} / {kpis.get('sites_total', 0)}"),
            ("Backups (24h)", str(kpis.get("backups_24h", 0))),
            ("Failed jobs (24h)", str(kpis.get("failed_jobs_24h", 0))),
            ("Backup compliance", f"{kpis.get('backup_compliance_pct', 0)}%"),
            ("Uptime (30d)", f"{kpis.get('uptime_30d_pct', 0)}%"),
            ("SSL expiring (30d)", str(kpis.get("ssl_expiring_30d", 0))),
            ("Servers", str(len(rows))),
        ],
        note="Fleet health and compliance figures are the live dashboard values.",
    )


# --------------------------------------------------------------------------- #
# 2. Backup evidence (ISO-facing, Admin-only)
# --------------------------------------------------------------------------- #


def backup_evidence(db: Session, params: dict[str, Any], *, now: datetime) -> ReportResult:
    """Per-site backup evidence over an explicit window (uiux-spec A1.6).

    Compliance state and breach reasons come from `evaluate_policy` — the same
    call the compliance sweep makes — so this artifact records the verdict the
    platform actually held, not a re-derivation of it.
    """
    start, end = resolve_range(params, now=now)

    policies = {
        policy.site_id: policy for policy in db.scalars(select(BackupPolicy)).all()
    }
    in_range: dict[int, tuple[int, int]] = {}
    for site_id, status, count in db.execute(
        select(Backup.site_id, Backup.status, func.count(Backup.id))
        .where(Backup.created_at >= start, Backup.created_at <= end)
        .group_by(Backup.site_id, Backup.status)
    ).all():
        ok, failed = in_range.get(site_id, (0, 0))
        if status == "success":
            ok += count
        elif status == "failure":
            failed += count
        in_range[site_id] = (ok, failed)

    rows = []
    for site, bench, server in db.execute(
        select(Site, Bench, Server)
        .join(Bench, Site.bench_id == Bench.id)
        .join(Server, Bench.server_id == Server.id)
        .order_by(Server.name, Site.name)
    ).all():
        policy = policies.get(site.id)
        ok, failed = in_range.get(site.id, (0, 0))
        if policy is not None:
            verdict = evaluate_policy(db, policy, now=now, site=site)
            state = verdict["state"]
            breaches = "; ".join(
                f"{b['code']}: {b['detail']}" for b in verdict.get("breaches", [])
            )
            last_backup = _iso(verdict.get("last_backup_at"))
            rpo = f"{policy.rpo_hours}h"
            retention = f"{policy.retention_days}d" if policy.retention_days else "—"
            offsite = "required" if policy.require_offsite else "—"
        else:
            state, breaches, last_backup = "no policy", "", ""
            rpo = retention = offsite = "—"

        rows.append(
            {
                "server": server.name,
                "site": site.name,
                "bench": bench.name,
                "policy_enabled": "yes" if policy is not None and policy.enabled else "no",
                "rpo": rpo,
                "retention": retention,
                "offsite": offsite,
                "state": state,
                "last_backup_at": last_backup,
                "backups_ok_in_range": ok,
                "backups_failed_in_range": failed,
                "breaches": breaches,
            }
        )

    compliant, policied = compliance_counts(db)
    return ReportResult(
        columns=[
            ("server", "Server"),
            ("site", "Site"),
            ("bench", "Bench"),
            ("policy_enabled", "Policy"),
            ("rpo", "RPO"),
            ("retention", "Retention"),
            ("offsite", "Offsite"),
            ("state", "State"),
            ("last_backup_at", "Last successful backup"),
            ("backups_ok_in_range", "Successful (range)"),
            ("backups_failed_in_range", "Failed (range)"),
            ("breaches", "Breaches"),
        ],
        rows=rows,
        summary=[
            ("Sites with a policy", str(policied)),
            ("Compliant", str(compliant)),
            ("Breached", str(policied - compliant)),
            (
                "Compliance",
                "100%" if policied == 0 else f"{round(compliant / policied * 100)}%",
            ),
            ("Sites covered", str(len(rows))),
        ],
        note=(
            "Compliance state is the live verdict of the backup policy engine "
            "(RPO / retention / offsite). Backup counts cover the stated range only."
        ),
    )


# --------------------------------------------------------------------------- #
# 3. Job history
# --------------------------------------------------------------------------- #

STATUS_PARAM = ReportParam(
    name="status",
    kind="enum",
    label="Status",
    default="all",
    enum=("all", "pending", "running", "success", "failure", "cancelled"),
)


def job_history(db: Session, params: dict[str, Any], *, now: datetime) -> ReportResult:
    """Every job run in the window, with the **same sanitized params the Jobs UI
    renders** — `params_sanitized` is already secret-masked at render time, and
    it goes through `mask_params` again here so a key-shaped secret that entered
    by some other route still cannot reach a report body (golden rule 6)."""
    start, end = resolve_range(params, now=now)
    status = params.get("status") or "all"

    stmt = (
        select(CommandJob, Server)
        .outerjoin(Server, CommandJob.server_id == Server.id)
        .where(CommandJob.created_at >= start, CommandJob.created_at <= end)
        .order_by(CommandJob.created_at.desc())
    )
    if status != "all":
        stmt = stmt.where(CommandJob.status == status)

    users = {user.id: user.email for user in db.scalars(select(User)).all()}

    rows = []
    counts: dict[str, int] = {}
    for job, server in db.execute(stmt).all():
        counts[job.status] = counts.get(job.status, 0) + 1
        started, ended = _aware(job.started_at), _aware(job.ended_at)
        duration = f"{(ended - started).total_seconds():.0f}s" if started and ended else ""
        rows.append(
            {
                "id": job.id,
                "action": job.action_name,
                "server": server.name if server is not None else "platform",
                "target": f"{job.target_type} {job.target_id or ''}".strip(),
                "status": job.status,
                "exit_code": "" if job.exit_code is None else job.exit_code,
                "created_at": _iso(job.created_at),
                "duration": duration,
                "requested_by": users.get(job.created_by, "") if job.created_by else "",
                "params": ", ".join(
                    f"{k}={v}" for k, v in sorted(mask_params(job.params_sanitized).items())
                ),
            }
        )

    total = len(rows)
    failed = counts.get("failure", 0)
    return ReportResult(
        columns=[
            ("id", "Job"),
            ("action", "Action"),
            ("server", "Server"),
            ("target", "Target"),
            ("status", "Status"),
            ("exit_code", "Exit"),
            ("created_at", "Created"),
            ("duration", "Duration"),
            ("requested_by", "Requested by"),
            ("params", "Parameters"),
        ],
        rows=rows,
        summary=[
            ("Jobs", str(total)),
            ("Succeeded", str(counts.get("success", 0))),
            ("Failed", str(failed)),
            ("Cancelled", str(counts.get("cancelled", 0))),
            (
                "Success rate",
                "—"
                if total == 0
                else f"{round((counts.get('success', 0) / total) * 100)}%",
            ),
        ],
        note="Parameters are the masked values shown in the Jobs UI; secrets are never included.",
    )


# --------------------------------------------------------------------------- #
# 4. App versions
# --------------------------------------------------------------------------- #


def app_versions(db: Session, params: dict[str, Any], *, now: datetime) -> ReportResult:
    """What is installed where — the inventory an upgrade plan is built from."""
    rows = []
    spread: dict[str, set[str]] = {}
    for installed, site, bench, server in db.execute(
        select(InstalledApp, Site, Bench, Server)
        .join(Site, InstalledApp.site_id == Site.id)
        .join(Bench, InstalledApp.bench_id == Bench.id)
        .join(Server, Bench.server_id == Server.id)
        .order_by(Server.name, Bench.name, Site.name, InstalledApp.app_name)
    ).all():
        version = installed.version or ""
        spread.setdefault(installed.app_name, set()).add(version)
        rows.append(
            {
                "server": server.name,
                "bench": bench.name,
                "site": site.name,
                "app": installed.app_name,
                "version": version,
                "branch": installed.branch or "",
                "frappe_version": bench.frappe_version or "",
                "installed_at": _iso(installed.installed_at),
            }
        )

    divergent = sorted(name for name, versions in spread.items() if len(versions) > 1)
    return ReportResult(
        columns=[
            ("server", "Server"),
            ("bench", "Bench"),
            ("site", "Site"),
            ("app", "App"),
            ("version", "Version"),
            ("branch", "Branch"),
            ("frappe_version", "Frappe"),
            ("installed_at", "Installed"),
        ],
        rows=rows,
        summary=[
            ("Installations", str(len(rows))),
            ("Distinct apps", str(len(spread))),
            ("Apps on mixed versions", str(len(divergent))),
            ("Mixed", ", ".join(divergent) if divergent else "none"),
        ],
        note="An app on more than one version across the fleet is an upgrade-drift signal.",
    )


# --------------------------------------------------------------------------- #
# 5. SSL expiry
# --------------------------------------------------------------------------- #

WITHIN_DAYS = ReportParam(
    name="within_days",
    kind="int",
    label="Expiring within (days)",
    default=60,
    min=1,
    max=365,
)


def ssl_expiry(db: Session, params: dict[str, Any], *, now: datetime) -> ReportResult:
    """Certificate expiry across every managed domain, soonest first."""
    within = int(params.get("within_days") or WITHIN_DAYS.default)
    reference = now.astimezone(UTC)

    rows = []
    expiring = expired = 0
    for domain, site, _bench, server in db.execute(
        select(Domain, Site, Bench, Server)
        .join(Site, Domain.site_id == Site.id)
        .join(Bench, Site.bench_id == Bench.id)
        .join(Server, Bench.server_id == Server.id)
    ).all():
        expires = _aware(domain.cert_expires_at)
        days_left = (expires - reference).days if expires else None
        if days_left is not None:
            if days_left < 0:
                expired += 1
            elif days_left <= within:
                expiring += 1
        rows.append(
            {
                "domain": domain.domain,
                "site": site.name,
                "server": server.name,
                "primary": "yes" if domain.is_primary else "no",
                "ssl": "on" if domain.ssl_enabled else "off",
                "cert_status": domain.cert_status,
                "expires_at": _iso(domain.cert_expires_at),
                "days_left": "" if days_left is None else days_left,
                "dns_ok": "" if domain.dns_ok is None else ("yes" if domain.dns_ok else "no"),
                "last_checked": _iso(domain.last_checked),
            }
        )

    # Unknown expiry sorts last: an unchecked cert is not evidence of safety,
    # but it must not outrank a cert that genuinely expires tomorrow.
    rows.sort(key=lambda r: (r["days_left"] == "", r["days_left"] if r["days_left"] != "" else 0))

    return ReportResult(
        columns=[
            ("domain", "Domain"),
            ("site", "Site"),
            ("server", "Server"),
            ("primary", "Primary"),
            ("ssl", "SSL"),
            ("cert_status", "Cert status"),
            ("expires_at", "Expires"),
            ("days_left", "Days left"),
            ("dns_ok", "DNS"),
            ("last_checked", "Last checked"),
        ],
        rows=rows,
        summary=[
            ("Domains", str(len(rows))),
            (f"Expiring within {within}d", str(expiring)),
            ("Already expired", str(expired)),
            ("Expiry unknown", str(sum(1 for r in rows if r["days_left"] == ""))),
        ],
        note=f"Expiry measured against {_iso(reference)}.",
    )


# --------------------------------------------------------------------------- #
# 6. User activity (ISO-facing, Admin-only)
# --------------------------------------------------------------------------- #


def user_activity(db: Session, params: dict[str, Any], *, now: datetime) -> ReportResult:
    """Per-user action counts over an explicit window — the access-review
    evidence of uiux-spec A1.6, built from the immutable audit trail.

    `params_masked` is not surfaced: this report answers *who did what, when and
    how often*, and aggregating avoids putting per-action parameter values into
    an export that circulates outside the platform (golden rule 6).
    """
    start, end = resolve_range(params, now=now)

    users = {user.id: user for user in db.scalars(select(User)).all()}
    roles = {user.id: user.role.name if user.role else "" for user in users.values()}

    agg: dict[tuple[int | None, str], dict[str, Any]] = {}
    failures = 0
    for log in db.scalars(
        select(AuditLog).where(AuditLog.ts >= start, AuditLog.ts <= end)
    ).all():
        key = (log.user_id, log.action)
        entry = agg.setdefault(
            key, {"count": 0, "failures": 0, "first": None, "last": None}
        )
        entry["count"] += 1
        if log.result == "failure":
            entry["failures"] += 1
            failures += 1
        ts = _aware(log.ts)
        if entry["first"] is None or (ts and ts < entry["first"]):
            entry["first"] = ts
        if entry["last"] is None or (ts and ts > entry["last"]):
            entry["last"] = ts

    rows = []
    for (user_id, action), entry in agg.items():
        user = users.get(user_id) if user_id else None
        rows.append(
            {
                "user": user.email if user else "(system / deleted user)",
                "full_name": user.full_name if user else "",
                "role": roles.get(user_id, "") if user_id else "",
                "active": ("yes" if user.is_active else "no") if user else "",
                "action": action,
                "count": entry["count"],
                "failures": entry["failures"],
                "first_at": _iso(entry["first"]),
                "last_at": _iso(entry["last"]),
            }
        )
    rows.sort(key=lambda r: (r["user"], -r["count"], r["action"]))

    acting = len({user_id for user_id, _ in agg if user_id})
    return ReportResult(
        columns=[
            ("user", "User"),
            ("full_name", "Name"),
            ("role", "Role"),
            ("active", "Active"),
            ("action", "Action"),
            ("count", "Count"),
            ("failures", "Failures"),
            ("first_at", "First"),
            ("last_at", "Last"),
        ],
        rows=rows,
        summary=[
            ("Audited events", str(sum(e["count"] for e in agg.values()))),
            ("Failed events", str(failures)),
            ("Users with activity", str(acting)),
            ("Users total", str(len(users))),
            ("Inactive accounts", str(sum(1 for u in users.values() if not u.is_active))),
        ],
        note=(
            "Aggregated from the immutable audit trail. Action parameters are "
            "deliberately omitted from this export."
        ),
    )


# --------------------------------------------------------------------------- #
# 7. Server capacity
# --------------------------------------------------------------------------- #


def server_capacity(db: Session, params: dict[str, Any], *, now: datetime) -> ReportResult:
    """Latest resource headroom per server, plus 30-day uptime for context.

    Uptime figures come from `app.core.uptime` (per-site rolling window and the
    fleet fraction), not from a fresh aggregate over `UptimeSample`.
    """
    latest: dict[int, MonitoringSample] = {}
    for sample in db.scalars(select(MonitoringSample).order_by(MonitoringSample.ts)).all():
        latest[sample.server_id] = sample  # ordered ascending -> last wins

    sites_by_server: dict[int, list[Site]] = {}
    for site, bench in db.execute(select(Site, Bench).join(Bench, Site.bench_id == Bench.id)).all():
        sites_by_server.setdefault(bench.server_id, []).append(site)

    rows = []
    low_disk = 0
    for server in db.scalars(select(Server).order_by(Server.name)).all():
        sample = latest.get(server.id)
        site_rows = sites_by_server.get(server.id, [])
        uptimes = [
            rolling_uptime(db, site.id).get("uptime_30d_pct")
            for site in site_rows
            if site.uptime_enabled
        ]
        measured = [value for value in uptimes if value is not None]
        disk_pct = sample.disk_pct if sample else None
        if disk_pct is not None and disk_pct >= 85:
            low_disk += 1
        rows.append(
            {
                "server": server.name,
                "env": server.env_tag,
                "sites": len(site_rows),
                "cpu_pct": _pct(sample.cpu_pct if sample else None),
                "mem_pct": _pct(sample.mem_pct if sample else None),
                "mem_used_mb": (sample.mem_used_mb if sample else "") or "",
                "mem_total_mb": (sample.mem_total_mb if sample else "") or "",
                "disk_pct": _pct(disk_pct),
                "disk_used_gb": (sample.disk_used_gb if sample else "") or "",
                "disk_total_gb": (sample.disk_total_gb if sample else "") or "",
                "load1": (sample.load1 if sample else "") or "",
                "uptime_30d": _pct(sum(measured) / len(measured) if measured else None),
                "sampled_at": _iso(sample.ts) if sample else "",
            }
        )

    fleet_fraction, measured_sites = fleet_uptime_fraction(db)
    return ReportResult(
        columns=[
            ("server", "Server"),
            ("env", "Env"),
            ("sites", "Sites"),
            ("cpu_pct", "CPU"),
            ("mem_pct", "Memory"),
            ("mem_used_mb", "Mem used (MB)"),
            ("mem_total_mb", "Mem total (MB)"),
            ("disk_pct", "Disk"),
            ("disk_used_gb", "Disk used (GB)"),
            ("disk_total_gb", "Disk total (GB)"),
            ("load1", "Load 1m"),
            ("uptime_30d", "Uptime 30d"),
            ("sampled_at", "Sampled"),
        ],
        rows=rows,
        summary=[
            ("Servers", str(len(rows))),
            ("Servers at >=85% disk", str(low_disk)),
            ("Fleet uptime (30d)", f"{fleet_fraction * 100:.1f}%"),
            ("Sites with uptime data", str(measured_sites)),
            ("Servers never sampled", str(sum(1 for r in rows if not r["sampled_at"]))),
        ],
        note="Resource figures are the most recent monitoring sample per server.",
    )


# --------------------------------------------------------------------------- #
# Registrations
# --------------------------------------------------------------------------- #

register(
    ReportDef(
        id="fleet_summary",
        title="Fleet summary",
        description="Servers, benches and sites with the live fleet-health KPIs.",
        required_permission=READ,
        params=(),
        generator=fleet_summary,
    )
)

register(
    ReportDef(
        id="backup_evidence",
        title="Backup evidence",
        description=(
            "Per-site backup policy, compliance verdict and backup history over a "
            "date range. ISO-facing evidence export."
        ),
        required_permission=REPORT_SENSITIVE,
        params=(RANGE_DAYS,),
        generator=backup_evidence,
        evidence=True,
    )
)

register(
    ReportDef(
        id="job_history",
        title="Job history",
        description="Every job run in a date range with its masked parameters.",
        required_permission=READ,
        params=(RANGE_DAYS, STATUS_PARAM),
        generator=job_history,
    )
)

register(
    ReportDef(
        id="app_versions",
        title="App versions",
        description="Installed apps and versions across every site, with drift flags.",
        required_permission=READ,
        params=(),
        generator=app_versions,
    )
)

register(
    ReportDef(
        id="ssl_expiry",
        title="SSL expiry",
        description="Certificate expiry across managed domains, soonest first.",
        required_permission=READ,
        params=(WITHIN_DAYS,),
        generator=ssl_expiry,
    )
)

register(
    ReportDef(
        id="user_activity",
        title="User activity",
        description=(
            "Per-user action counts from the audit trail over a date range. "
            "ISO-facing access-review export."
        ),
        required_permission=REPORT_SENSITIVE,
        params=(RANGE_DAYS,),
        generator=user_activity,
        evidence=True,
    )
)

register(
    ReportDef(
        id="server_capacity",
        title="Server capacity",
        description="Latest CPU/memory/disk headroom per server with 30-day uptime.",
        required_permission=READ,
        params=(),
        generator=server_capacity,
    )
)
