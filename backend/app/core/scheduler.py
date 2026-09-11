"""Schedule dispatch logic (session 2.1) — the testable core of the scheduler.

This module owns *when* a schedule is due and *what* firing it does; the process
that drives it (``app.workers.scheduler``, built on rq-scheduler) is thin glue.
Everything here runs against a plain DB session + a `JobRunner`, so the whole
dispatch path is unit-testable with an in-memory backend and an injected clock —
no Redis, RQ, SSH or wall-clock needed (matching the session's "fake-clock tests
proceed without a live target" note).

Firing a schedule goes through the *same* `JobRunner.create` every other
mutation uses, so a scheduled run is locked (rule 4), audited (rule 2) and
enqueued off the request path (rule 3) exactly like a hand-launched job.

Missed-run / catch-up policy: `dispatch_schedule` fires a due schedule once and
recomputes `next_run_at` **forward from now**, never iterating the missed slots.
A scheduler outage therefore yields a single catch-up run, not a backlog stampede
(see app/models/schedule.py for the full rationale).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import CroniterBadCronError, croniter
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.jobs import JobRunner, LockConflict
from app.models import CommandJob
from app.models.schedule import Schedule

logger = logging.getLogger("app.scheduler")


class ScheduleError(RuntimeError):
    """A schedule cannot be dispatched (target gone, bad cadence, etc.)."""


# --------------------------------------------------------------------------- #
# Cadence -> next fire instant (UTC)
# --------------------------------------------------------------------------- #


def _resolve_tz(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ScheduleError(f"unknown timezone {name!r}") from exc


def compute_next_run(schedule: Schedule, *, after: datetime) -> datetime:
    """Next fire instant strictly after ``after`` (an aware UTC datetime), as a
    timezone-aware UTC datetime.

    - interval: ``after + interval_seconds`` (tz-independent).
    - cron: the next match of the 5-field expression, evaluated in the schedule's
      timezone so "0 2 * * *" means 02:00 *local* even across DST, then converted
      back to UTC for storage (golden rule 8).
    """
    if after.tzinfo is None:
        raise ScheduleError("`after` must be timezone-aware (UTC)")
    after = after.astimezone(UTC)

    if schedule.interval_seconds is not None:
        if schedule.interval_seconds <= 0:
            raise ScheduleError("interval_seconds must be positive")
        return after + timedelta(seconds=schedule.interval_seconds)

    if schedule.cron:
        tz = _resolve_tz(schedule.timezone or "UTC")
        base_local = after.astimezone(tz)
        try:
            itr = croniter(schedule.cron, base_local)
        except (CroniterBadCronError, ValueError) as exc:
            raise ScheduleError(f"invalid cron expression {schedule.cron!r}") from exc
        nxt_local: datetime = itr.get_next(datetime)
        return nxt_local.astimezone(UTC)

    raise ScheduleError("schedule has neither cron nor interval_seconds")


def validate_cadence(*, cron: str | None, interval_seconds: int | None, timezone: str) -> None:
    """Raise ScheduleError unless exactly one cadence is given and it parses.

    Called by the API before persisting so a bad cron/interval is rejected at the
    boundary (422) rather than silently never firing.
    """
    if (cron is None) == (interval_seconds is None):
        raise ScheduleError("provide exactly one of `cron` or `interval_seconds`")
    if interval_seconds is not None:
        if interval_seconds <= 0:
            raise ScheduleError("interval_seconds must be positive")
        return
    _resolve_tz(timezone or "UTC")
    try:
        croniter(cron)
    except (CroniterBadCronError, ValueError) as exc:
        raise ScheduleError(f"invalid cron expression {cron!r}") from exc


# --------------------------------------------------------------------------- #
# Target resolution -> action params
# --------------------------------------------------------------------------- #


def _resolve_site_target(db: Session, schedule: Schedule):
    """Load the (site, bench, server_id) a site-targeted schedule fires against.

    Raises ScheduleError if any link has vanished, so a stale schedule fails its
    fire cleanly (logged/skipped) instead of exploding inside the worker.
    """
    from app.models.bench import Bench
    from app.models.site import Site

    if schedule.target_type != "site":
        raise ScheduleError(f"unsupported target_type {schedule.target_type!r}")
    site = db.get(Site, schedule.target_id)
    if site is None:
        raise ScheduleError(f"site {schedule.target_id} no longer exists")
    bench = db.get(Bench, site.bench_id)
    if bench is None:  # pragma: no cover - FK-guaranteed in practice
        raise ScheduleError(f"site {site.name!r} has no bench")
    return site, bench, bench.server_id


def _build_report_fire(schedule: Schedule) -> tuple[None, str, dict, None]:
    """Params for a `report.generate` fire (session 6.2).

    A report schedule has no row target — its configuration lives entirely in
    `Schedule.params` (report id, format, recipients, and the report's own
    parameters). `server_id` is None: this is a platform-local job.
    """
    config = dict(schedule.params or {})
    report_id = config.get("report_id")
    if not report_id:
        raise ScheduleError("report schedule has no report_id")

    params = {
        "report_id": str(report_id),
        "format": str(config.get("format") or "csv"),
    }
    recipients = config.get("recipients") or []
    if isinstance(recipients, list):
        recipients = ",".join(str(address) for address in recipients)
    if recipients:
        params["recipients"] = str(recipients)
    # Only forward the report parameters the template declares; anything else
    # would fail render() as an unknown parameter.
    for key in ("range_days", "within_days", "status"):
        if config.get(key) not in (None, ""):
            params[key] = str(config[key])
    if schedule.created_by is not None:
        params["requested_by"] = str(schedule.created_by)
    return None, str(report_id), params, None


def _build_fire(
    db: Session, schedule: Schedule
) -> tuple[int | None, str, dict, object | None]:
    """Return (server_id, target_id_str, params, side_row) for the job the
    schedule fires, pre-creating any side rows the action needs.

    For `site.backup` a `pending` Backup row is created up front (mirroring the
    manual backup endpoint) so a failed scheduled backup still leaves a visible
    failed record, and its id is threaded into the job params.

    `server_id` is None for a platform-local action (6.2 report delivery).
    """
    # server.drift_check is server-targeted: no site/bench resolution, no params.
    if schedule.action_name == "server.drift_check":
        if schedule.target_type != "server":
            raise ScheduleError("server.drift_check requires target_type 'server'")
        from app.models.server import Server

        server = db.get(Server, schedule.target_id)
        if server is None:
            raise ScheduleError(f"server {schedule.target_id} no longer exists")
        return server.id, None, {}, None

    if schedule.action_name == "report.generate":
        return _build_report_fire(schedule)

    site, bench, server_id = _resolve_site_target(db, schedule)
    target_id = f"{bench.path}::{site.name}"

    if schedule.action_name == "site.backup":
        from app.core import backups as bk

        row = bk.create_pending_backup(
            db,
            site_id=site.id,
            bench_id=bench.id,
            backup_type="with-files" if schedule.with_files else "db",
            taken_by_job_id=None,
        )
        params = {
            "site": site.name,
            "bench_path": bench.path,
            "with_files": "1" if schedule.with_files else "0",
            "backup_id": str(row.id),
        }
        return server_id, target_id, params, row

    if schedule.action_name == "backup.retention_sweep":
        params = {"site": site.name, "bench_path": bench.path}
        if schedule.retention_keep_last is not None:
            params["keep_last"] = str(schedule.retention_keep_last)
        if schedule.retention_keep_days is not None:
            params["keep_days"] = str(schedule.retention_keep_days)
        return server_id, target_id, params, None

    if schedule.action_name in ("ssl.certbot_renew", "ssl.expiry_scan"):
        # Both operate on the site's domains; the action loads the Domain rows.
        params = {"site": site.name, "bench_path": bench.path}
        return server_id, target_id, params, None

    raise ScheduleError(f"unschedulable action {schedule.action_name!r}")


# --------------------------------------------------------------------------- #
# Firing
# --------------------------------------------------------------------------- #


def fire_schedule(
    db: Session,
    runner: JobRunner,
    schedule: Schedule,
    *,
    now: datetime,
    created_by: int | None = None,
) -> CommandJob:
    """Enqueue the schedule's action as a real CommandJob and record the fire on
    the schedule (last_run_at + last_run_job_id). Does **not** touch
    `next_run_at` — that is the caller's choice (advanced by the scheduled tick,
    left alone by a manual run-now).

    ``created_by`` attributes the job's audit row (the operator who pressed
    run-now); defaults to the schedule's owner for an automated fire.

    Propagates LockConflict (the target is busy) so the caller decides policy;
    any side row created for the fire (a pending Backup) is rolled back first so
    a conflict doesn't strand a phantom pending backup.
    """
    server_id, target_id, params, side_row = _build_fire(db, schedule)
    try:
        job = runner.create(
            db,
            action_name=schedule.action_name,
            server_id=server_id,
            target_type=schedule.target_type,
            target_id=target_id,
            params=params,
            priority=schedule.priority,
            created_by=created_by if created_by is not None else schedule.created_by,
        )
    except LockConflict:
        if side_row is not None:
            db.delete(side_row)
            db.commit()
        raise
    except Exception:
        if side_row is not None:
            db.delete(side_row)
            db.commit()
        raise

    if side_row is not None:
        side_row.taken_by_job_id = job.id
    schedule.last_run_at = now
    schedule.last_run_job_id = job.id
    db.commit()
    return job


def dispatch_schedule(
    db: Session, runner: JobRunner, schedule: Schedule, *, now: datetime
) -> CommandJob | None:
    """Fire a due schedule from the tick loop and advance `next_run_at` forward
    from ``now`` (collapsing any missed occurrences — the catch-up policy).

    Returns the enqueued job, or None if the fire was skipped:
    - LockConflict (target busy): the occurrence is skipped and rescheduled to the
      next slot so the scheduler never queues behind a running job.
    - ScheduleError (stale target / bad cadence): logged; next_run cleared so it
      stops re-attempting every tick until an operator fixes it.
    """
    try:
        job = fire_schedule(db, runner, schedule, now=now)
    except LockConflict as exc:
        logger.info(
            "schedule %s skipped: target locked by job %s; rescheduling",
            schedule.id,
            exc.blocking_job_id,
        )
        schedule.next_run_at = compute_next_run(schedule, after=now)
        db.commit()
        return None
    except ScheduleError as exc:
        logger.warning("schedule %s cannot fire: %s; pausing", schedule.id, exc)
        schedule.next_run_at = None
        db.commit()
        return None

    schedule.next_run_at = compute_next_run(schedule, after=now)
    db.commit()
    return job


def due_schedules(db: Session, *, now: datetime) -> list[Schedule]:
    """Enabled schedules whose next_run_at has arrived, oldest-due first."""
    return list(
        db.scalars(
            select(Schedule)
            .where(
                Schedule.enabled.is_(True),
                Schedule.next_run_at.is_not(None),
                Schedule.next_run_at <= now,
            )
            .order_by(Schedule.next_run_at)
        ).all()
    )


def tick(
    session_factory, runner: JobRunner, *, now: datetime
) -> list[int]:
    """One scheduler pass: dispatch every due schedule, each isolated so one bad
    schedule can't sink the batch. Returns the ids of schedules that fired a job.

    A fresh session per schedule keeps a failure's rollback from poisoning the
    others' work (failure isolation, the same posture as the bulk migrate fan-out).
    """
    fired: list[int] = []
    with session_factory() as scan:
        due_ids = [s.id for s in due_schedules(scan, now=now)]
    for sid in due_ids:
        with session_factory() as db:
            schedule = db.get(Schedule, sid)
            if schedule is None:  # deleted mid-tick
                continue
            try:
                job = dispatch_schedule(db, runner, schedule, now=now)
            except Exception:  # noqa: BLE001 — isolate; never let one kill the tick.
                db.rollback()
                logger.exception("schedule %s dispatch crashed", sid)
                continue
            if job is not None:
                fired.append(sid)
    return fired
