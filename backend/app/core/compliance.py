"""Backup compliance evaluator (session 2.3) — the testable core.

`evaluate_all(db, now=…)` sweeps every **enabled** `BackupPolicy`, computes each
site's compliance from backup metadata alone (read-only — retention *deletion*
lives in session 2.1), upserts its `ComplianceStatus`, and — on the edge a site
transitions *into* breach — emits one `ComplianceBreachEvent` for session 3.1 to
consume. It returns a summary the API and the dashboard read.

Everything here runs against a plain DB session + an injected `now`, so the whole
path is unit-testable with seeded backups and a fake clock — no Redis, no worker,
no wall-clock (matching the session's "evaluator unit-testable with seeded
backups" note). The scheduler process (``app.workers.scheduler``) drives it on a
cadence; that glue is thin.

### Compliance rule (documented — drives the dashboard %)

A policied+enabled site is **compliant** when ALL required dimensions hold:

  1. **RPO**       — a *successful* backup exists whose age ≤ `rpo_hours`.
  2. **retention** — if `retention_days` is set AND the site is older than that
     window, history must reach back: the oldest successful backup is at least
     `retention_days` old (allowing a 1-day grace). A brand-new site (younger
     than the window) cannot yet breach retention — there is nothing to keep yet.
  3. **offsite**   — if `require_offsite`, the newest successful backup's
     `storage_state` is ``offsite`` (session 2.2).

`require_restore_test` is groundwork: the policy carries it, but it is **not**
scored here (a later session turns it on).

Anything else is a **breach**, with each failed dimension recorded in `breaches`
as ``{"code": <rpo|retention|offsite>, "detail": <human string>}``.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Site
from app.models.backup import Backup
from app.models.compliance import (
    BREACH_OFFSITE,
    BREACH_RETENTION,
    BREACH_RPO,
    BackupPolicy,
    ComplianceBreachEvent,
    ComplianceStatus,
)

logger = logging.getLogger("app.compliance")

# Grace applied to the retention-history check so a backup taken a few hours
# short of the exact window boundary is not flagged as a breach.
_RETENTION_GRACE = timedelta(days=1)


def _aware(dt: datetime | None) -> datetime | None:
    """Treat a tz-naive value (SQLite test rows) as UTC so age math is correct."""
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _successful_backups(db: Session, site_id: int) -> list[Backup]:
    """All successful backups for a site, newest first."""
    return list(
        db.scalars(
            select(Backup)
            .where(Backup.site_id == site_id, Backup.status == "success")
            .order_by(Backup.created_at.desc())
        ).all()
    )


def evaluate_policy(
    db: Session, policy: BackupPolicy, *, now: datetime, site: Site | None = None
) -> dict:
    """Compute one site's compliance without persisting. Returns
    ``{"state", "last_backup_at", "breaches"}``.

    Pure over the backup rows + policy + clock, so a caller can unit-test it with
    seeded backups. Persistence + event emission happen in `evaluate_all`.
    """
    if site is None:
        site = db.get(Site, policy.site_id)
    now = now.astimezone(UTC)
    backups = _successful_backups(db, policy.site_id)
    newest = backups[0] if backups else None
    oldest = backups[-1] if backups else None
    last_backup_at = _aware(newest.created_at) if newest else None

    breaches: list[dict] = []

    # 1. RPO — newest successful backup must be younger than rpo_hours.
    rpo_cutoff = now - timedelta(hours=policy.rpo_hours)
    if last_backup_at is None:
        breaches.append({"code": BREACH_RPO, "detail": "no successful backup yet"})
    elif last_backup_at < rpo_cutoff:
        age_h = (now - last_backup_at).total_seconds() / 3600
        breaches.append(
            {
                "code": BREACH_RPO,
                "detail": f"newest backup is {age_h:.0f}h old (RPO {policy.rpo_hours}h)",
            }
        )

    # 2. Retention — history must reach back retention_days, once the site is old
    #    enough for that to be observable.
    if policy.retention_days is not None and oldest is not None:
        window = timedelta(days=policy.retention_days)
        site_created = _aware(getattr(site, "created_at", None)) if site else None
        site_old_enough = site_created is None or (now - site_created) >= window
        oldest_at = _aware(oldest.created_at)
        history_reaches = (now - oldest_at) >= (window - _RETENTION_GRACE)
        if site_old_enough and not history_reaches:
            span_d = (now - oldest_at).total_seconds() / 86400
            breaches.append(
                {
                    "code": BREACH_RETENTION,
                    "detail": (
                        f"history spans {span_d:.0f}d "
                        f"(retention {policy.retention_days}d)"
                    ),
                }
            )

    # 3. Offsite — newest successful backup must be in an offsite target.
    if policy.require_offsite:
        if newest is None:
            # RPO already flagged the missing backup; add offsite so the UI shows
            # both requirements are unmet.
            breaches.append(
                {"code": BREACH_OFFSITE, "detail": "no backup to store offsite"}
            )
        elif newest.storage_state != "offsite":
            breaches.append(
                {
                    "code": BREACH_OFFSITE,
                    "detail": f"newest backup is {newest.storage_state}, not offsite",
                }
            )

    state = "breached" if breaches else "compliant"
    return {"state": state, "last_backup_at": last_backup_at, "breaches": breaches}


def _upsert_status(
    db: Session, policy: BackupPolicy, result: dict, *, now: datetime
) -> tuple[ComplianceStatus, str | None]:
    """Persist the result onto the site's ComplianceStatus. Returns
    ``(status, prev_state)`` where prev_state is the state before this write
    (None if the row is newly created), used to detect a transition into breach.
    """
    status = db.scalar(
        select(ComplianceStatus).where(ComplianceStatus.site_id == policy.site_id)
    )
    prev_state: str | None
    if status is None:
        prev_state = None
        status = ComplianceStatus(site_id=policy.site_id, state="unknown")
        db.add(status)
    else:
        prev_state = status.state

    status.state = result["state"]
    status.last_backup_at = result["last_backup_at"]
    status.breaches = result["breaches"]
    status.evaluated_at = now
    return status, prev_state


def evaluate_all(db: Session, *, now: datetime | None = None) -> dict:
    """Sweep every enabled policy: evaluate, persist status, emit a breach event
    on transition into breach. Returns a summary
    ``{"policied", "compliant", "breached", "events_emitted"}``.

    Read-only over backups — never deletes anything. One commit at the end keeps
    the sweep atomic; a crash mid-sweep leaves the prior statuses intact.
    """
    now = (now or datetime.now(UTC)).astimezone(UTC)
    policies = list(
        db.scalars(select(BackupPolicy).where(BackupPolicy.enabled.is_(True))).all()
    )

    compliant = breached = events = 0
    for policy in policies:
        site = db.get(Site, policy.site_id)
        if site is None:  # policy for a vanished site — skip (FK cascade normally clears it)
            continue
        result = evaluate_policy(db, policy, now=now, site=site)
        status, prev_state = _upsert_status(db, policy, result, now=now)

        if result["state"] == "breached":
            breached += 1
            # Edge-triggered: emit an event only when the site *enters* breach, so
            # a persistently-breached site is not re-alerted every tick.
            if prev_state != "breached":
                db.add(
                    ComplianceBreachEvent(
                        site_id=site.id,
                        site_name=site.name,
                        breaches=result["breaches"],
                        last_backup_at=result["last_backup_at"],
                        rpo_hours=policy.rpo_hours,
                    )
                )
                events += 1
                logger.info(
                    "site %s entered backup-compliance breach: %s",
                    site.name,
                    [b["code"] for b in result["breaches"]],
                )
        else:
            compliant += 1

    db.commit()
    return {
        "policied": len(policies),
        "compliant": compliant,
        "breached": breached,
        "events_emitted": events,
    }


def compliance_counts(db: Session) -> tuple[int, int]:
    """Return ``(compliant_policied_sites, policied_sites)`` from the persisted
    ComplianceStatus of enabled policies — the dashboard Backup Compliance %
    numerator/denominator (formula = compliant / policied).

    Counts only sites with an *enabled* policy; a disabled policy's stale status
    row is ignored so pausing a policy removes the site from the metric.
    """
    rows = db.execute(
        select(ComplianceStatus.state)
        .join(BackupPolicy, BackupPolicy.site_id == ComplianceStatus.site_id)
        .where(BackupPolicy.enabled.is_(True))
    ).all()
    policied = len(rows)
    compliant = sum(1 for (state,) in rows if state == "compliant")
    return compliant, policied
