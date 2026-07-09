"""SSE job-log streaming (session 1.4).

The core generator is driven with an in-memory pub/sub reader so replay,
gap-free resume, live-tail, heartbeat and terminal close are all deterministic
(no Redis, no timing). The HTTP surface is checked for auth + 404 only — the
long-lived streaming loop is covered by the generator tests.
"""

import asyncio
import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.streaming import (
    job_log_stream,
    replay_entries,
    sse_comment,
    sse_event,
)
from app.db import Base
from app.models import CommandJob, LogEntry, Server
from tests.conftest import csrf_headers, login

# --------------------------------------------------------------------------- #
# Fixtures: an isolated sqlite engine + factory so the generator can open its
# own short-lived sessions (as it does in production against SessionLocal).
# --------------------------------------------------------------------------- #


@pytest.fixture
def factory():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    engine.dispose()


def _make_job(factory, status="running"):
    with factory() as db:
        server = Server(name="vm", hostname="10.0.0.1", ssh_port=22)
        db.add(server)
        db.flush()
        job = CommandJob(
            server_id=server.id, action_name="system.echo_demo", status=status
        )
        db.add(job)
        db.commit()
        return job.id


def _add_logs(factory, job_id, lines, start_seq=1):
    with factory() as db:
        for i, (stream, content) in enumerate(lines):
            db.add(
                LogEntry(
                    job_id=job_id, seq=start_seq + i, stream=stream, content=content
                )
            )
        db.commit()


class FakeReader:
    """PubSubReader that yields queued frames then None (a heartbeat tick).

    `on_exhaust` fires once when the queue first empties — used to flip a job
    terminal so the post-heartbeat check can close the stream deterministically."""

    def __init__(self, messages, on_exhaust=None):
        self._messages = list(messages)
        self._on_exhaust = on_exhaust
        self._exhausted = False

    async def get_message(self, timeout: float):
        if self._messages:
            return self._messages.pop(0)
        if self._on_exhaust is not None and not self._exhausted:
            self._exhausted = True
            self._on_exhaust()
        return None


def _pub(seq, content, stream="stdout"):
    return {"data": json.dumps({"seq": seq, "stream": stream, "content": content})}


async def _collect(agen):
    frames = []
    async for frame in agen:
        frames.append(frame)
    return frames


def _log_payloads(frames):
    out = []
    for f in frames:
        if f.startswith("event: log"):
            data = f.split("data:", 1)[1].strip()
            out.append(json.loads(data))
    return out


# --------------------------------------------------------------------------- #
# Pure helpers.
# --------------------------------------------------------------------------- #


def test_sse_frame_formatting():
    assert sse_comment("heartbeat") == ": heartbeat\n\n"
    frame = sse_event("log", {"seq": 3, "content": "hi"})
    assert frame.startswith("event: log\ndata: ")
    assert frame.endswith("\n\n")
    assert json.loads(frame.split("data:", 1)[1].strip()) == {"seq": 3, "content": "hi"}


def test_replay_entries_after_seq(factory):
    job_id = _make_job(factory)
    _add_logs(factory, job_id, [("stdout", "a"), ("stdout", "b"), ("stderr", "c")])
    with factory() as db:
        assert [e["seq"] for e in replay_entries(db, job_id, 0)] == [1, 2, 3]
        resumed = replay_entries(db, job_id, 1)
        assert [e["seq"] for e in resumed] == [2, 3]
        assert resumed[1] == {"seq": 3, "stream": "stderr", "content": "c"}


# --------------------------------------------------------------------------- #
# Generator: replay, live-tail, resume, heartbeat, terminal close.
# --------------------------------------------------------------------------- #


def test_terminal_job_replays_then_ends(factory):
    job_id = _make_job(factory, status="success")
    _add_logs(factory, job_id, [("stdout", "one"), ("stdout", "two")])

    async def never_disconnected():
        return False

    frames = asyncio.run(
        _collect(
            job_log_stream(job_id, 0, factory, FakeReader([]), never_disconnected)
        )
    )

    assert _log_payloads(frames) == [
        {"seq": 1, "stream": "stdout", "content": "one"},
        {"seq": 2, "stream": "stdout", "content": "two"},
    ]
    end = [f for f in frames if f.startswith("event: end")]
    assert len(end) == 1
    assert json.loads(end[0].split("data:", 1)[1].strip()) == {
        "status": "success",
        "last_seq": 2,
    }


def test_live_tail_merges_pubsub_without_duplicates(factory):
    """Replay covers seq 1-2 while the job runs; pub/sub then live-delivers 2
    (already seen) + 3 + 4. When the pub/sub queue drains the job flips terminal,
    so the next heartbeat tick closes the stream. seq 2 must appear exactly once."""
    job_id = _make_job(factory, status="running")
    _add_logs(factory, job_id, [("stdout", "l1"), ("stdout", "l2")])

    async def never_disconnected():
        return False

    def finish():
        with factory() as db:
            db.get(CommandJob, job_id).status = "success"
            db.commit()

    reader = FakeReader(
        [_pub(2, "l2"), _pub(3, "l3"), _pub(4, "l4", "stderr")], on_exhaust=finish
    )
    frames = asyncio.run(
        _collect(job_log_stream(job_id, 0, factory, reader, never_disconnected))
    )

    # seq 2 arrives via replay AND pub/sub but is emitted exactly once.
    seqs = [p["seq"] for p in _log_payloads(frames)]
    assert seqs == [1, 2, 3, 4]
    assert any(f.startswith("event: end") for f in frames)
    assert any(f == ": heartbeat\n\n" for f in frames)


def test_resume_after_seq_skips_replayed_lines(factory):
    job_id = _make_job(factory, status="success")
    _add_logs(factory, job_id, [("stdout", f"line{i}") for i in range(1, 6)])

    async def never_disconnected():
        return False

    frames = asyncio.run(
        _collect(
            job_log_stream(job_id, 3, factory, FakeReader([]), never_disconnected)
        )
    )
    # Client resumes at seq 3 → only 4 and 5 replay, no gap, no repeat.
    assert [p["seq"] for p in _log_payloads(frames)] == [4, 5]


def test_stops_when_client_disconnects(factory):
    job_id = _make_job(factory, status="running")
    _add_logs(factory, job_id, [("stdout", "hello")])

    async def disconnected():
        return True  # gone immediately after replay

    frames = asyncio.run(
        _collect(job_log_stream(job_id, 0, factory, FakeReader([]), disconnected))
    )
    # Replayed the one line, then returned without an end event (client left).
    assert [p["seq"] for p in _log_payloads(frames)] == [1]
    assert not any(f.startswith("event: end") for f in frames)


# --------------------------------------------------------------------------- #
# HTTP surface: auth + 404.
# --------------------------------------------------------------------------- #


@pytest.fixture
def server_id(db_session):
    server = Server(name="vm-alpha", hostname="10.0.0.5", ssh_port=22)
    db_session.add(server)
    db_session.commit()
    return server.id


def test_command_endpoint_requires_auth(client, server_id):
    assert client.get("/api/jobs/1/logs/stream").status_code == 401
    assert client.get("/api/jobs/1/command").status_code == 401


def test_command_endpoint_404_for_unknown_job(client):
    login(client, "readonly@example.com")
    assert client.get("/api/jobs/999/command").status_code == 404
    assert client.get("/api/jobs/999/logs/stream").status_code == 404


def test_command_endpoint_renders_masked_command(client, db_session, server_id):
    login(client, "admin@example.com")
    job = CommandJob(
        server_id=server_id,
        action_name="system.echo_demo",
        status="success",
        params_sanitized={"message": "hello world"},
    )
    db_session.add(job)
    db_session.commit()
    resp = client.get(f"/api/jobs/{job.id}/command", headers=csrf_headers(client))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["command"] == "echo 'hello world'"
    assert body["argv"] == ["echo", "hello world"]
