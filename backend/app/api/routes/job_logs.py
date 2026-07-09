"""Live job-log streaming + the "show exact command" helper (session 1.4).

- GET /api/jobs/{id}/logs/stream  Server-Sent Events: replay from ?after_seq,
  then live-tail the Redis pub/sub channel; 15s heartbeat; closes on terminal
  state or client disconnect.
- GET /api/jobs/{id}/command      the sanitized shell command the job runs, for
  the detail page's "Show exact command" expander (secrets already masked).

Kept in its own router (included by main.py alongside the jobs router) so the
streaming concern stays isolated from the CRUD job routes.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import require
from app.core.commands import RenderError, UnknownAction, get_template, render
from app.core.permissions import READ
from app.core.streaming import (
    RedisPubSubReader,
    channel_for,
    job_log_stream,
)
from app.db import SessionLocal, get_db
from app.models import CommandJob

router = APIRouter(prefix="/api/jobs", tags=["jobs"])

DbSession = Annotated[Session, Depends(get_db)]

# SSE + reverse-proxy headers: never cache, and X-Accel-Buffering:no tells nginx
# not to buffer the response so lines reach the browser as they are written.
SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


def _require_job(db: Session, job_id: int) -> CommandJob:
    job = db.scalar(select(CommandJob).where(CommandJob.id == job_id))
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    return job


@router.get("/{job_id}/logs/stream")
async def stream_job_logs(
    job_id: int,
    request: Request,
    _: Annotated[object, Depends(require(READ))],
    after_seq: int = Query(default=0, ge=0),
) -> StreamingResponse:
    """Server-Sent Events of a job's log lines (see core/streaming.py)."""
    # 404 pre-check on a short-lived session, released immediately. We deliberately
    # do NOT take Depends(get_db) here: a request-scoped session closes only after
    # the response body is consumed, which for an SSE stream is its entire (hours-
    # long) life — that would pin a pooled DB connection idle per open stream and
    # starve the pool. The generator opens its own per-check sessions (streaming.py).
    with SessionLocal() as db:
        _require_job(db, job_id)

    import redis.asyncio as aioredis

    from app.config import get_settings

    async def event_source():
        conn = aioredis.Redis.from_url(get_settings().redis_url)
        pubsub = conn.pubsub()
        await pubsub.subscribe(channel_for(job_id))
        try:
            reader = RedisPubSubReader(pubsub)
            async for frame in job_log_stream(
                job_id, after_seq, SessionLocal, reader, request.is_disconnected
            ):
                yield frame
        finally:
            # Best-effort teardown; the client may already be gone.
            try:
                await pubsub.unsubscribe(channel_for(job_id))
                await pubsub.aclose()
            finally:
                await conn.aclose()

    return StreamingResponse(
        event_source(), media_type="text/event-stream", headers=SSE_HEADERS
    )


@router.get("/{job_id}/command")
def job_command(
    job_id: int, db: DbSession, _: Annotated[object, Depends(require(READ))]
) -> dict:
    """The exact shell command this job runs, secrets masked (rule 6). Rendered
    from the persisted `params_sanitized`; a secret-bearing template that cannot
    be re-rendered falls back to a readable action + masked-params summary."""
    job = _require_job(db, job_id)
    try:
        template = get_template(job.action_name)
        rendered = render(
            template, dict(job.params_sanitized or {}), from_sanitized=True
        )
        return {"command": rendered.display, "argv": rendered.argv}
    except (RenderError, UnknownAction):
        params = job.params_sanitized or {}
        summary = " ".join(f"{k}={v}" for k, v in params.items())
        return {"command": f"{job.action_name} {summary}".strip(), "argv": []}
