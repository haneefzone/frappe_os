"""RQ worker entrypoint.

Consumes the three priority queues (high > default > low) and runs
`app.core.jobs.execute_job`. Redis comes from settings (REDIS_URL), so the
worker and the API always agree on the broker.

Run it with:
    make worker            # -> python -m app.workers.worker
or directly:
    .venv/bin/python -m app.workers.worker

Graceful shutdown: RQ performs a *warm* shutdown on the first SIGINT/SIGTERM —
it lets the in-flight job finish (up to its 4h timeout, rule 3) before exiting,
so a deploy never kills a long `bench init`/`update` midway. A second signal
forces a cold shutdown.
"""

from __future__ import annotations

from redis import Redis
from rq import Queue, Worker

from app.config import get_settings

QUEUES = ["high", "default", "low"]


def make_connection() -> Redis:
    return Redis.from_url(get_settings().redis_url)


def main() -> None:
    connection = make_connection()
    queues = [Queue(name, connection=connection) for name in QUEUES]
    worker = Worker(queues, connection=connection)
    # with_scheduler=False: this session has no scheduled jobs yet.
    worker.work(with_scheduler=False)


if __name__ == "__main__":
    main()
