"""Scheduler process entrypoint (session 2.1) — rq-scheduler integration.

This is thin glue over `app.core.scheduler`, which owns all the dispatch logic
and is unit-tested without Redis. Here we only wire the *cadence of the sweep*:

    rq-scheduler  ──enqueues──▶  `dispatch_due_schedules`  ──▶  core.scheduler.tick
       (this process)              (runs on an RQ worker)          (fires due jobs)

`rq_scheduler.Scheduler.run()` is the daemon (the classic `rqscheduler` pattern):
it polls Redis and moves due jobs onto the queue for a normal `make worker` to
execute — so the scheduler adds NO new execution path, just a trigger. We register
exactly one recurring job, `dispatch_due_schedules`, at `SCHEDULER_TICK_SECONDS`;
each time a worker runs it, it sweeps the DB for due schedules and enqueues their
real CommandJobs through the JobRunner (golden rules 2/3).

Run it with:
    make scheduler         # -> python -m app.workers.scheduler

Graceful shutdown: `Scheduler.run()` installs its own SIGINT/SIGTERM handlers
that register the scheduler's death, release its singleton lock, and exit cleanly
— so a deploy never leaves a stale lock behind or double-fires. Only one scheduler
dispatches at a time: the Scheduler acquires a Redis lock each poll (like the
monitoring poller's lease), so running two processes is safe — the second skips.

Missed-run / catch-up: handled in `core.scheduler.dispatch_schedule` (fire once,
recompute forward) — a scheduler outage yields a single catch-up run, never a
backlog stampede.
"""

from __future__ import annotations

import logging

from redis import Redis

from app.config import get_settings

logger = logging.getLogger("app.scheduler")

# The fully-qualified path rq-scheduler enqueues; a worker imports and runs it.
TICK_FUNC = "app.workers.scheduler.dispatch_due_schedules"
# Session 2.3: the same scheduler process also registers a recurring
# backup-compliance sweep (read-only over backup metadata — no CommandJob).
COMPLIANCE_FUNC = "app.workers.scheduler.evaluate_compliance"
# Session 3.1: and a recurring AlertRule sweep — evaluate enabled rules against
# the latest monitoring samples and enqueue any breach dispatch off the sweep.
ALERTS_FUNC = "app.workers.scheduler.evaluate_alerts_tick"


def make_connection() -> Redis:
    return Redis.from_url(get_settings().redis_url)


def dispatch_due_schedules() -> list[int]:
    """The recurring job body (runs on an RQ worker): sweep due schedules and
    enqueue their CommandJobs. Returns the ids of schedules that fired, so the
    RQ result carries a record of the sweep."""
    from datetime import UTC, datetime

    from app.core.jobs import build_runner
    from app.core.scheduler import tick
    from app.db import SessionLocal

    fired = tick(SessionLocal, build_runner(), now=datetime.now(UTC))
    if fired:
        logger.info("scheduler tick fired %d schedule(s): %s", len(fired), fired)
    return fired


def evaluate_compliance() -> dict:
    """Recurring job body (runs on an RQ worker): re-evaluate every enabled
    BackupPolicy and persist each site's ComplianceStatus. Read-only over backup
    metadata (never deletes); returns the sweep summary for the RQ result."""
    from app.core.compliance import evaluate_all
    from app.db import SessionLocal

    with SessionLocal() as db:
        summary = evaluate_all(db)
    if summary["events_emitted"]:
        logger.info(
            "compliance sweep: %d site(s) entered breach", summary["events_emitted"]
        )
    return summary


def evaluate_alerts_tick() -> dict:
    """Recurring job body (runs on an RQ worker): evaluate every enabled AlertRule
    against the latest monitoring samples, fire breaches whose cooldown elapsed,
    and enqueue each delivery off the sweep. Returns the sweep summary for the RQ
    result."""
    from datetime import UTC, datetime

    from app.core.alerts import evaluate_alerts
    from app.db import SessionLocal

    with SessionLocal() as db:
        summary = evaluate_alerts(db, now=datetime.now(UTC))
    if summary["fired"] or summary["resolved"]:
        logger.info(
            "alert sweep fired %d, resolved %d", summary["fired"], summary["resolved"]
        )
    return summary


def _register_recurring(scheduler, *, func, func_name: str, interval: int) -> None:
    """Idempotently register one recurring job, cancelling any existing entry for
    the same func first so a restart with a changed interval never leaves two."""
    for job in scheduler.get_jobs():
        if job.func_name == func_name:
            scheduler.cancel(job)
    scheduler.schedule(
        scheduled_time=_utcnow(),
        func=func,
        interval=interval,
        repeat=None,  # forever
        result_ttl=int(interval) * 4,
    )


def ensure_tick_registered(scheduler, *, interval: int) -> None:
    """Idempotently register the single recurring schedule-dispatch tick job."""
    _register_recurring(
        scheduler, func=dispatch_due_schedules, func_name=TICK_FUNC, interval=interval
    )
    logger.info("registered scheduler tick every %ds", interval)


def ensure_compliance_registered(scheduler, *, interval: int) -> None:
    """Idempotently register the recurring backup-compliance sweep (session 2.3)."""
    _register_recurring(
        scheduler, func=evaluate_compliance, func_name=COMPLIANCE_FUNC, interval=interval
    )
    logger.info("registered compliance sweep every %ds", interval)


def ensure_alerts_registered(scheduler, *, interval: int) -> None:
    """Idempotently register the recurring AlertRule sweep (session 3.1)."""
    _register_recurring(
        scheduler, func=evaluate_alerts_tick, func_name=ALERTS_FUNC, interval=interval
    )
    logger.info("registered alert sweep every %ds", interval)


def _utcnow():
    from datetime import UTC, datetime

    return datetime.now(UTC)


def build_scheduler(connection: Redis | None = None):
    from rq_scheduler import Scheduler

    settings = get_settings()
    conn = connection if connection is not None else make_connection()
    return Scheduler(
        queue_name=settings.scheduler_queue,
        connection=conn,
        interval=settings.scheduler_tick_seconds,
    )


def main() -> None:  # pragma: no cover - process entrypoint (needs live Redis)
    settings = get_settings()
    scheduler = build_scheduler()
    ensure_tick_registered(scheduler, interval=settings.scheduler_tick_seconds)
    ensure_compliance_registered(
        scheduler, interval=settings.compliance_tick_seconds
    )
    ensure_alerts_registered(scheduler, interval=settings.alerts_tick_seconds)
    logger.info(
        "scheduler starting (queue=%s, tick=%ds, compliance=%ds, alerts=%ds)",
        settings.scheduler_queue,
        settings.scheduler_tick_seconds,
        settings.compliance_tick_seconds,
        settings.alerts_tick_seconds,
    )
    # run() installs its own graceful SIGINT/SIGTERM handlers and releases the
    # singleton lock on exit (see module docstring).
    scheduler.run()


if __name__ == "__main__":  # pragma: no cover
    main()
