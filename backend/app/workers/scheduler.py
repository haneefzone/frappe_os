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


def ensure_tick_registered(scheduler, *, interval: int) -> None:
    """Idempotently register the single recurring tick job. Cancels any existing
    tick entries first so a restart with a changed interval doesn't leave two."""
    for job in scheduler.get_jobs():
        if job.func_name == TICK_FUNC:
            scheduler.cancel(job)
    scheduler.schedule(
        scheduled_time=_utcnow(),
        func=dispatch_due_schedules,
        interval=interval,
        repeat=None,  # forever
        result_ttl=int(interval) * 4,
    )
    logger.info("registered scheduler tick every %ds", interval)


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
    logger.info(
        "scheduler starting (queue=%s, tick=%ds)",
        settings.scheduler_queue,
        settings.scheduler_tick_seconds,
    )
    # run() installs its own graceful SIGINT/SIGTERM handlers and releases the
    # singleton lock on exit (see module docstring).
    scheduler.run()


if __name__ == "__main__":  # pragma: no cover
    main()
