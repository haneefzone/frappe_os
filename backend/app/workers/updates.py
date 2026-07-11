"""Update-advisor poll job (session 3.2).

The advisor's network work (reading upstream git tags) never runs in a request
(golden rule 3) — it runs here, on an RQ worker, in two ways:

- **Scheduled** — the 2.1 scheduler process registers `run_poll` as a recurring
  job (`app.workers.scheduler.ensure_updates_poll_registered`), so the fleet is
  swept on a cadence (`UPDATES_POLL_INTERVAL_SECONDS`) with no operator action.
- **On demand** — `POST /api/updates/refresh` enqueues `run_poll` (force=True) so
  an operator can refresh now; the endpoint returns 202 immediately.

`run_poll` is a thin wrapper over `app.core.updates.poll_updates` (all the logic,
unit-tested without Redis). It is idempotent: re-running only updates rows.
"""

from __future__ import annotations

import logging

from redis import Redis

from app.config import get_settings

logger = logging.getLogger("app.updates")

# The fully-qualified path RQ enqueues; a worker imports and runs it.
POLL_FUNC = "app.workers.updates.run_poll"


def run_poll(force: bool = False) -> dict:
    """RQ job body: sweep the fleet for available updates. Returns the summary so
    the RQ result carries a record of the poll."""
    from datetime import UTC, datetime

    from app.core.updates import poll_updates
    from app.db import SessionLocal

    settings = get_settings()
    with SessionLocal() as db:
        return poll_updates(
            db,
            now=datetime.now(UTC),
            ttl_seconds=settings.updates_tag_cache_ttl_seconds,
            git_timeout=settings.git_remote_timeout_seconds,
            force=force,
        )


def enqueue_poll(*, force: bool = True) -> str:
    """Enqueue a one-off poll on the low queue (run-now). Returns the RQ job id."""
    from rq import Queue

    conn = Redis.from_url(get_settings().redis_url)
    queue = Queue("low", connection=conn)
    rq_job = queue.enqueue(POLL_FUNC, force)
    logger.info("enqueued update-advisor poll %s (force=%s)", rq_job.id, force)
    return rq_job.id
