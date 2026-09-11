"""Scheduled restore-test selection + result recording (session 3.4).

Proof-of-restorability: a backup is only trustworthy once it has actually been
restored somewhere and verified. This module owns the *policy* half of that —
which sites are due for a restore-test, and how a completed test's verdict is
stamped back onto the backup's badge. The *doing* half (restore into a scratch
site → verify → destroy) lives in ``RestoreTestAction``; keeping the selection
pure (no Redis/SSH) makes the sweep unit-testable, mirroring 2.3's compliance
evaluator and 3.1's alert evaluator.

The sweep tests the site's **newest successful backup**: proving the latest
backup restores is what an operator actually cares about. A backup is due when
it has never been restore-tested, or its last test is older than the policy's
``restore_test_interval_days``. NULL interval = never auto-run (on-demand only).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import NamedTuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.backup import Backup
from app.models.bench import Bench
from app.models.compliance import BackupPolicy
from app.models.site import Site


class DueRestoreTest(NamedTuple):
    """A site whose newest backup should be restore-tested now."""

    site: Site
    bench: Bench
    backup: Backup


def _latest_success_backup(db: Session, site_id: int) -> Backup | None:
    """The newest successful backup for a site that carries a usable database
    artifact — the only kind a restore can rehearse from."""
    return db.scalars(
        select(Backup)
        .where(
            Backup.site_id == site_id,
            Backup.status == "success",
            Backup.db_path.is_not(None),
        )
        .order_by(Backup.created_at.desc())
    ).first()


def _is_due(backup: Backup, interval_days: int, now: datetime) -> bool:
    """A backup is due when never tested, or its last test is older than the
    per-site interval."""
    last = backup.restore_tested_at
    if last is None:
        return True
    # SQLite (tests) yields tz-naive UTC; Postgres yields tz-aware. Compare in a
    # single naive-UTC space so a mixed environment never crashes the sweep
    # (mirrors the tz handling in core/dashboard.py's backup grid).
    if last.tzinfo is not None:
        last = last.replace(tzinfo=None)
    ref = now.replace(tzinfo=None) if now.tzinfo is not None else now
    return (ref - last) >= timedelta(days=interval_days)


def select_due(db: Session, *, now: datetime) -> list[DueRestoreTest]:
    """Every site whose newest backup is due a restore-test under an enabled
    policy. Read-only: never mutates a row (the sweep enqueues the jobs)."""
    due: list[DueRestoreTest] = []
    policies = db.scalars(
        select(BackupPolicy).where(
            BackupPolicy.enabled.is_(True),
            BackupPolicy.require_restore_test.is_(True),
            BackupPolicy.restore_test_interval_days.is_not(None),
        )
    ).all()
    for policy in policies:
        site = db.get(Site, policy.site_id)
        if site is None:
            continue
        bench = db.get(Bench, site.bench_id)
        if bench is None:
            continue
        backup = _latest_success_backup(db, site.id)
        if backup is None:
            continue  # nothing to prove yet — no successful backup exists.
        if _is_due(backup, int(policy.restore_test_interval_days), now):
            due.append(DueRestoreTest(site=site, bench=bench, backup=backup))
    return due


def record_result(
    db: Session,
    backup: Backup,
    *,
    passed: bool,
    detail: str,
    now: datetime,
    job_id: int | None = None,
) -> None:
    """Stamp a restore-test verdict onto the backup's badge. Called from the
    restore-test job after the scratch site has been verified (or a probe/
    restore failed). ``restore_tested`` (the legacy 1.11 bool) is kept in sync
    so it stays true only while the newest proof passed."""
    backup.restore_test_status = "passed" if passed else "failed"
    backup.restore_tested = bool(passed)
    backup.restore_tested_at = now
    backup.restore_test_detail = (detail or "")[:500]
    if job_id is not None:
        backup.restore_test_job_id = job_id
    db.commit()


def failed_backups(db: Session) -> list[Backup]:
    """Backups whose most recent restore-test failed — the dashboard "Needs
    attention → failed restore tests" feed (uiux §1, dashboard row 4)."""
    return list(
        db.scalars(
            select(Backup)
            .where(Backup.restore_test_status == "failed")
            .order_by(Backup.restore_tested_at.desc())
        ).all()
    )
