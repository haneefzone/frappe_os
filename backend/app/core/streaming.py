"""SSE log streaming for jobs (CLAUDE.md "Job engine pattern").

`GET /api/jobs/{id}/logs/stream` (see api/routes/job_logs.py) replays persisted
`LogEntry` rows after `?after_seq=N`, then live-tails the Redis pub/sub channel
`job:{id}:logs` that the worker's `LogWriter` publishes to. A heartbeat comment
is emitted every 15s so proxies and the client can tell a quiet-but-alive stream
from a dead one, and the stream closes cleanly once the job reaches a terminal
state or the client disconnects.

The core generator (`job_log_stream`) is deliberately decoupled from Redis and
FastAPI: it takes a `session_factory` (short-lived sessions for replay + status
checks — never one held open for the life of the connection) and a `PubSubReader`
(any object with `async get_message(timeout)`), so it is unit-testable with an
in-memory reader and drives the real Redis pub/sub in production.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CommandJob, LogEntry

HEARTBEAT_SECONDS = 15.0
TERMINAL_STATUSES = ("success", "failure", "cancelled")


def channel_for(job_id: int) -> str:
    """Redis pub/sub channel a job's log lines are published to (matches
    `RedisJobBackend.publish_log` in core/jobs.py)."""
    return f"job:{job_id}:logs"


# --------------------------------------------------------------------------- #
# SSE frame formatting (pure — the wire format lives in one place).
# --------------------------------------------------------------------------- #


def sse_comment(text: str) -> str:
    """A comment line (`: ...`). Used for the initial hello and heartbeats;
    ignored by EventSource but keeps the connection and any proxy buffers warm."""
    return f": {text}\n\n"


def sse_event(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


# --------------------------------------------------------------------------- #
# DB replay.
# --------------------------------------------------------------------------- #


def replay_entries(db: Session, job_id: int, after_seq: int) -> list[dict]:
    """Persisted log lines with `seq > after_seq`, in order. `seq` is monotonic
    per job, so a reconnecting client resumes exactly where it left off."""
    rows = db.scalars(
        select(LogEntry)
        .where(LogEntry.job_id == job_id, LogEntry.seq > after_seq)
        .order_by(LogEntry.seq)
    ).all()
    return [{"seq": r.seq, "stream": r.stream, "content": r.content} for r in rows]


def _job_status(db: Session, job_id: int) -> str | None:
    return db.scalar(select(CommandJob.status).where(CommandJob.id == job_id))


# --------------------------------------------------------------------------- #
# Live tail.
# --------------------------------------------------------------------------- #


class PubSubReader(Protocol):
    async def get_message(self, timeout: float) -> dict | None:
        """Return the next pub/sub message ({'data': <str|bytes>}), or None if
        `timeout` seconds pass with nothing (the heartbeat tick)."""
        ...


def _decode(payload: object) -> dict | None:
    if isinstance(payload, bytes):
        payload = payload.decode()
    if not isinstance(payload, str):
        return None
    try:
        data = json.loads(payload)
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) and "seq" in data else None


async def job_log_stream(
    job_id: int,
    after_seq: int,
    session_factory: Callable[[], Session],
    reader: PubSubReader,
    is_disconnected: Callable[[], Awaitable[bool]],
) -> AsyncIterator[str]:
    """Yield SSE frames for one job's logs: replay, then live-tail until the job
    is terminal or the client goes away.

    Ordering is gap-free and duplicate-free because the caller subscribes to the
    pub/sub channel *before* calling this (so nothing published during replay is
    lost) and every line is keyed by its monotonic `seq` — anything at or below
    the high-water mark we have already sent is dropped.
    """
    yield sse_comment("connected")
    # Tell the browser's EventSource to wait 3s before auto-reconnecting.
    yield "retry: 3000\n\n"

    last_seq = after_seq

    # 1) Replay everything persisted so far.
    with session_factory() as db:
        for entry in replay_entries(db, job_id, last_seq):
            last_seq = entry["seq"]
            yield sse_event("log", entry)
        status = _job_status(db, job_id)

    # 2) A job that already finished before we connected: drain any late-persisted
    #    lines and close — no point holding the connection open to tail nothing.
    if status in TERMINAL_STATUSES:
        async for frame in _drain_and_end(job_id, last_seq, session_factory):
            yield frame
        return

    # 3) Live tail: pub/sub for new lines, heartbeat + terminal-check on each lull.
    while True:
        if await is_disconnected():
            return

        message = await reader.get_message(timeout=HEARTBEAT_SECONDS)

        if message is None:  # heartbeat interval elapsed with no new line
            yield sse_comment("heartbeat")
            with session_factory() as db:
                status = _job_status(db, job_id)
            if status is None or status in TERMINAL_STATUSES:
                async for frame in _drain_and_end(job_id, last_seq, session_factory):
                    yield frame
                return
            continue

        data = _decode(message.get("data"))
        if data is None:
            continue
        if data["seq"] > last_seq:
            last_seq = data["seq"]
            yield sse_event("log", data)


async def _drain_and_end(
    job_id: int, last_seq: int, session_factory: Callable[[], Session]
) -> AsyncIterator[str]:
    """Flush any persisted lines past `last_seq` (a terminal job's final batch may
    land after its last pub/sub message) and emit the closing `end` event."""
    with session_factory() as db:
        for entry in replay_entries(db, job_id, last_seq):
            last_seq = entry["seq"]
            yield sse_event("log", entry)
        status = _job_status(db, job_id) or "unknown"
    yield sse_event("end", {"status": status, "last_seq": last_seq})


# --------------------------------------------------------------------------- #
# Production Redis reader.
# --------------------------------------------------------------------------- #


class RedisPubSubReader:
    """Adapts a `redis.asyncio` PubSub to the `PubSubReader` protocol."""

    def __init__(self, pubsub) -> None:
        self._ps = pubsub

    async def get_message(self, timeout: float) -> dict | None:
        return await self._ps.get_message(
            ignore_subscribe_messages=True, timeout=timeout
        )
