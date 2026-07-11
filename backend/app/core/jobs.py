"""The job engine (CLAUDE.md "Job engine pattern" + golden rules 1-4).

`JobRunner.create()` validates a request against the template registry, persists
a `CommandJob`, acquires a Redis lock for the target, and enqueues
`execute_job(job_id)` on the priority queue — returning immediately (rule 3).

The RQ worker calls `execute_job`, which runs the action's steps, streams output
to `LogEntry` rows + a Redis pub/sub channel, retries idempotent failures up to
three times, and honours cancellation. RQ workers are synchronous processes, so
the AsyncSSH parts are driven with `asyncio.run()` (CLAUDE.md architecture note).

Everything external (Redis lock/pub-sub, RQ enqueue, SSH execution) is behind a
small interface so the whole engine runs in tests with in-memory fakes.
"""

from __future__ import annotations

import time
import traceback
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload, sessionmaker

from app.core.commands import RenderedCommand, get_template, render
from app.core.ssh import SSHService
from app.models import CommandJob, CommandStep, LogEntry, Server

MAX_AUTO_RETRIES = 3
JOB_TIMEOUT_SECONDS = 4 * 3600  # rule 3: long bench ops must survive 4h.
# Locks outlive at most one 4h job; the TTL is a deadlock backstop if a worker
# dies without releasing (the row it points at is still inspectable).
LOCK_TTL_SECONDS = JOB_TIMEOUT_SECONDS + 3600

_UNSET = object()


def _now() -> datetime:
    return datetime.now(UTC)


class JobCancelled(Exception):
    """Raised inside execution when a cancel was requested for the job."""


class LockConflict(Exception):
    """Another live job already holds the target lock (rule 4 -> HTTP 409)."""

    def __init__(self, blocking_job_id: int) -> None:
        self.blocking_job_id = blocking_job_id
        super().__init__(f"target is locked by job {blocking_job_id}")


# --------------------------------------------------------------------------- #
# Backend: Redis lock, cancel signalling and log pub/sub behind one interface.
# --------------------------------------------------------------------------- #


class JobBackend(Protocol):
    def acquire_lock(self, key: str, job_id: int) -> int | None:
        """Return None on success, or the job id already holding `key`."""
        ...

    def release_lock(self, key: str, job_id: int) -> None: ...

    def request_cancel(self, job_id: int) -> None: ...

    def is_cancel_requested(self, job_id: int) -> bool: ...

    def clear_cancel(self, job_id: int) -> None: ...

    def publish_log(self, job_id: int, payload: str) -> None: ...


class RedisJobBackend:
    """Production backend keyed on a live Redis connection."""

    def __init__(self, client) -> None:
        self._r = client

    @staticmethod
    def _lock(key: str) -> str:
        return f"fdm:joblock:{key}"

    @staticmethod
    def _cancel(job_id: int) -> str:
        return f"fdm:jobcancel:{job_id}"

    def acquire_lock(self, key: str, job_id: int) -> int | None:
        acquired = self._r.set(self._lock(key), str(job_id), nx=True, ex=LOCK_TTL_SECONDS)
        if acquired:
            return None
        holder = self._r.get(self._lock(key))
        if holder is None:  # released in the race; try once more
            acquired = self._r.set(self._lock(key), str(job_id), nx=True, ex=LOCK_TTL_SECONDS)
            if acquired:
                return None
            holder = self._r.get(self._lock(key))
        return int(holder) if holder is not None else -1

    def release_lock(self, key: str, job_id: int) -> None:
        # Delete only if we still hold it (avoid dropping a re-acquired lock).
        script = (
            "if redis.call('get', KEYS[1]) == ARGV[1] "
            "then return redis.call('del', KEYS[1]) else return 0 end"
        )
        self._r.eval(script, 1, self._lock(key), str(job_id))

    def request_cancel(self, job_id: int) -> None:
        self._r.set(self._cancel(job_id), "1", ex=LOCK_TTL_SECONDS)

    def is_cancel_requested(self, job_id: int) -> bool:
        return self._r.get(self._cancel(job_id)) is not None

    def clear_cancel(self, job_id: int) -> None:
        self._r.delete(self._cancel(job_id))

    def publish_log(self, job_id: int, payload: str) -> None:
        self._r.publish(f"job:{job_id}:logs", payload)


class InMemoryJobBackend:
    """Deterministic backend for tests (no Redis)."""

    def __init__(self) -> None:
        self._locks: dict[str, int] = {}
        self._cancels: set[int] = set()
        self.published: list[tuple[int, str]] = []

    def acquire_lock(self, key: str, job_id: int) -> int | None:
        holder = self._locks.get(key)
        if holder is not None:
            return holder
        self._locks[key] = job_id
        return None

    def release_lock(self, key: str, job_id: int) -> None:
        if self._locks.get(key) == job_id:
            del self._locks[key]

    def request_cancel(self, job_id: int) -> None:
        self._cancels.add(job_id)

    def is_cancel_requested(self, job_id: int) -> bool:
        return job_id in self._cancels

    def clear_cancel(self, job_id: int) -> None:
        self._cancels.discard(job_id)

    def publish_log(self, job_id: int, payload: str) -> None:
        self.published.append((job_id, payload))


# --------------------------------------------------------------------------- #
# Log writer: batched persistence (25 lines / 500 ms) + pub/sub + redaction.
# --------------------------------------------------------------------------- #


class LogWriter:
    """Accumulates streamed lines, flushing to `log_entries` in batches while
    publishing each line to Redis pub/sub for live tailing. Any secret value is
    redacted before a line is stored or published (rule 6)."""

    def __init__(
        self,
        db: Session,
        job_id: int,
        *,
        backend: JobBackend | None = None,
        secrets: tuple[str, ...] = (),
        batch_size: int = 25,
        flush_interval: float = 0.5,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._db = db
        self._job_id = job_id
        self._backend = backend
        self._secrets = [s for s in secrets if s]
        self._batch_size = batch_size
        self._flush_interval = flush_interval
        self._clock = clock
        self._buffer: list[LogEntry] = []
        self._last_flush = clock()
        existing = db.scalar(
            select(func.max(LogEntry.seq)).where(LogEntry.job_id == job_id)
        )
        self._seq = int(existing or 0)

    def _redact(self, content: str) -> str:
        from app.core.commands import MASK

        for secret in self._secrets:
            content = content.replace(secret, MASK)
        return content

    def append(self, stream: str, content: str) -> None:
        content = self._redact(content)
        self._seq += 1
        seq = self._seq
        self._buffer.append(
            LogEntry(job_id=self._job_id, seq=seq, stream=stream, content=content, ts=_now())
        )
        if self._backend is not None:
            import json

            self._backend.publish_log(
                self._job_id,
                json.dumps({"seq": seq, "stream": stream, "content": content}),
            )
        if len(self._buffer) >= self._batch_size or (
            self._clock() - self._last_flush >= self._flush_interval
        ):
            self.flush()

    def flush(self) -> None:
        if not self._buffer:
            return
        self._db.add_all(self._buffer)
        self._db.commit()
        self._buffer.clear()
        self._last_flush = self._clock()


# --------------------------------------------------------------------------- #
# Execution context handed to Action.run().
# --------------------------------------------------------------------------- #


@dataclass
class CaptureResult:
    """A fully-collected command result, for actions that must parse output
    (e.g. bench discovery reading `bench version --format json`) rather than
    only stream it line by line."""

    exit_code: int
    stdout: str
    stderr: str


class RemoteExecutor(Protocol):
    async def run(
        self,
        argv: list[str],
        *,
        cwd: str | None,
        run_as: str | None,
        on_line: Callable[[str, str], Awaitable[None] | None],
        cancel_check: Callable[[], bool] | None,
    ) -> int: ...

    async def capture(
        self, argv: list[str], *, cwd: str | None = None, timeout: float = 120.0
    ) -> CaptureResult:
        """Run a fixed argv and return its collected output (no streaming)."""
        ...

    def read_file(self, path: str, *, chunk_size: int = 65536) -> AsyncIterator[bytes]:
        """Yield the raw bytes of a remote file over SSH, in chunks — for
        streaming a backup artifact to offsite storage without buffering the
        whole file (session 2.2)."""
        ...

    async def write_file(self, path: str, chunks: AsyncIterator[bytes]) -> int:
        """Stream `chunks` into a remote file over SSH (binary-safe), returning
        the exit status — the write side of a cross-server backup move (2.6)."""
        ...


class LocalExecutor:
    """Executor stand-in for a platform-local job (session 6.2).

    A local template's Action does all its work in-process (query the DB, render
    a PDF) and must never reach a managed server. Handing it this executor makes
    that contract enforceable: an accidental ctx.stream/ctx.capture raises
    loudly instead of silently opening an SSH connection to nothing.
    """

    async def __aenter__(self) -> LocalExecutor:
        return self

    async def __aexit__(self, *exc_info) -> bool:
        return False

    def _refuse(self, what: str):
        raise RuntimeError(
            f"platform-local job attempted a remote {what}; local actions run "
            "in-process and have no server to execute against"
        )

    async def stream(self, *args, **kwargs):
        self._refuse("stream")

    async def capture(self, *args, **kwargs):
        self._refuse("capture")

    def read_file(self, *args, **kwargs):
        self._refuse("read_file")


def _local_executor_factory(_server) -> LocalExecutor:
    return LocalExecutor()


class JobContextImpl:
    """Concrete `JobContext` (see app/core/commands/actions.py). Owns step
    bookkeeping, line streaming and cancellation checks for one job run."""

    def __init__(
        self,
        db: Session,
        job: CommandJob,
        rendered: RenderedCommand,
        run_as: str | None,
        executor: RemoteExecutor,
        log_writer: LogWriter,
        backend: JobBackend | None,
        attempt: int = 1,
    ) -> None:
        self._db = db
        self._job = job
        self.rendered = rendered
        self.run_as = run_as
        self._executor = executor
        self._log = log_writer
        self._backend = backend
        # `order` is per-attempt (restarts at 1 each retry); `attempt` disambiguates
        # steps that reuse an order across auto-retries for the timeline UI (DOO-96).
        self._attempt = attempt
        self._order = 0

    @property
    def session(self) -> Session:
        """The worker's DB session. Only inventory/discovery actions that produce
        rows (e.g. `bench.discover` upserting Bench rows) use this; command
        actions stay DB-free and talk only to steps/stream/emit."""
        return self._db

    @property
    def server_id(self) -> int | None:
        """None for a platform-local job (6.2) — it targets no managed server."""
        return self._job.server_id

    @property
    def job_id(self) -> int:
        return self._job.id

    def _cancelled(self) -> bool:
        return self._backend is not None and self._backend.is_cancel_requested(self._job.id)

    @contextmanager
    def step(self, name: str):
        if self._cancelled():
            raise JobCancelled()
        self._order += 1
        step = CommandStep(
            job_id=self._job.id,
            name=name,
            attempt=self._attempt,
            order=self._order,
            status="running",
            started_at=_now(),
        )
        self._db.add(step)
        self._db.commit()
        try:
            yield step
        except Exception:
            self._log.flush()
            step.status = "failure"
            step.ended_at = _now()
            step.error_traceback = traceback.format_exc()
            self._db.commit()
            raise
        else:
            self._log.flush()
            step.status = "success"
            step.ended_at = _now()
            self._db.commit()

    async def stream(
        self, argv: list[str], *, cwd: str | None = None, run_as: object = _UNSET
    ) -> int:
        effective_run_as = self.run_as if run_as is _UNSET else run_as  # type: ignore[assignment]

        def on_line(stream_name: str, text: str) -> None:
            self._log.append(stream_name, text)

        return await self._executor.run(
            argv,
            cwd=cwd,
            run_as=effective_run_as,
            on_line=on_line,
            cancel_check=self._cancelled,
        )

    async def capture(
        self, argv: list[str], *, cwd: str | None = None, timeout: float = 120.0
    ) -> CaptureResult:
        return await self._executor.capture(argv, cwd=cwd, timeout=timeout)

    def read_file(self, path: str, *, chunk_size: int = 65536) -> AsyncIterator[bytes]:
        return self._executor.read_file(path, chunk_size=chunk_size)

    async def write_file(self, path: str, chunks: AsyncIterator[bytes]) -> int:
        return await self._executor.write_file(path, chunks)

    async def emit(self, text: str, stream: str = "system") -> None:
        self._log.append(stream, text)


# --------------------------------------------------------------------------- #
# SSH-backed executor (production). Opens one pooled connection per job run.
# --------------------------------------------------------------------------- #


class SSHRemoteExecutor:
    def __init__(self, ssh: SSHService, server: Server) -> None:
        self._ssh = ssh
        self._server = server
        self._conn = None

    async def __aenter__(self) -> SSHRemoteExecutor:
        cred = self._server.credential
        if cred is None:
            raise RuntimeError(f"server {self._server.name!r} has no SSH credential")
        self._conn = await self._ssh.connect(self._server, cred)
        return self

    async def __aexit__(self, *exc) -> None:
        await self._ssh.close_all()

    async def run(
        self,
        argv: list[str],
        *,
        cwd: str | None,
        run_as: str | None,
        on_line: Callable[[str, str], Awaitable[None] | None],
        cancel_check: Callable[[], bool] | None,
    ) -> int:
        return await self._ssh.stream(
            self._conn,
            argv,
            cwd=cwd,
            run_as=run_as,
            on_line=on_line,
            cancel_check=cancel_check,
            timeout=JOB_TIMEOUT_SECONDS,
        )

    async def capture(
        self, argv: list[str], *, cwd: str | None = None, timeout: float = 120.0
    ) -> CaptureResult:
        out = await self._ssh.run(self._conn, argv, cwd=cwd, timeout=timeout)
        return CaptureResult(
            exit_code=out.exit_status, stdout=out.stdout, stderr=out.stderr
        )

    async def read_file(self, path: str, *, chunk_size: int = 65536):
        """Stream a remote file's bytes over the job's pooled SSH connection
        (`cat`, binary-safe). Used by the 2.2 offsite upload step. Delegates to
        the SSHService so the read is metered against the server's session cap
        (2.6)."""
        async for chunk in self._ssh.read_file(self._conn, path, chunk_size=chunk_size):
            yield chunk

    async def write_file(self, path: str, chunks) -> int:
        """Stream bytes into a remote file over the job's pooled SSH connection,
        metered against the server's session cap. The write side of a
        cross-server backup move (2.6)."""
        return await self._ssh.write_file(self._conn, path, chunks)


@dataclass
class CreateResult:
    job: CommandJob


class JobRunner:
    """Creates, cancels, retries and executes jobs. Constructed with injectable
    backend/enqueue/executor so the API, the worker and tests share one class."""

    def __init__(
        self,
        session_factory: sessionmaker | Callable[[], Session],
        backend: JobBackend,
        *,
        ssh: SSHService | None = None,
        enqueue: Callable[[CommandJob], str | None] | None = None,
        executor_factory: Callable[[Server], object] | None = None,
        secrets=None,
    ) -> None:
        self._sf = session_factory
        self._backend = backend
        self._ssh = ssh
        self._enqueue = enqueue if enqueue is not None else self._default_enqueue
        self._executor_factory = executor_factory
        self._secrets = secrets

    def _secrets_service(self):
        """The Fernet SecretsService for resolving secret params at render time
        (session 1.8). Falls back to the process-wide service so tests and the
        production runner share one validated key."""
        if self._secrets is None:
            from app.core.security import get_secrets_service

            self._secrets = get_secrets_service()
        return self._secrets

    # -- creation ------------------------------------------------------------ #

    @staticmethod
    def _lock_key(
        server_id: int | None, target_type: str, target_id: str | None, action: str
    ) -> str:
        # rule 4: keyed on the target + action so two dangerous ops on the same
        # target can never run at once. Uses action_name as the action class.
        # A platform-local job (6.2) has no server; "local" keeps the key shape
        # stable and scopes such a lock to the platform rather than a server.
        scope = server_id if server_id is not None else "local"
        return f"{scope}:{target_type}:{target_id or '-'}:{action}"

    def create(
        self,
        db: Session,
        *,
        action_name: str,
        server_id: int | None,
        target_type: str,
        target_id: str | None,
        params: dict,
        priority: str,
        created_by: int | None,
        user_secrets: dict[str, str] | None = None,
        _from_sanitized: bool = False,
    ) -> CommandJob:
        """Validate + persist + lock + enqueue. Raises RenderError (422),
        UnknownAction (404), LockConflict (409) or SecretResolutionError (422).
        Returns the pending job.

        `user_secrets` carries secret params the operator supplied (e.g. a site's
        admin password); they are validated by rendering the real command, then
        Fernet-encrypted onto the job (`secrets_enc`) for the worker — never
        stored in the clear. Server-sourced secrets (e.g. the MariaDB root
        password) are resolved from the Server row here only to validate, and are
        re-resolved by the worker, so they never touch the job row (rule 6).

        `_from_sanitized=True` (set only by `retry`) tells render() the params
        came from a masked `params_sanitized` map, so a secret-bearing template
        fails loud (SecretParamUnresolved) instead of running with `••••`."""
        template = get_template(action_name)  # UnknownAction if missing

        secrets_enc: str | None = None
        if not _from_sanitized and template.secret_sources:
            from app.core.secrets_resolve import encrypt_job_secrets, resolve_secrets

            svc = self._secrets_service()
            server = db.get(Server, server_id)
            # Encrypt the user-supplied job secrets, then resolve every declared
            # secret (job + server-sourced) so render validates against real
            # values. SecretResolutionError bubbles up (a missing server secret
            # or admin password) and the API maps it to HTTP 422.
            secrets_enc = encrypt_job_secrets(svc, user_secrets or {})
            resolved = resolve_secrets(
                template, secrets_enc=secrets_enc, server=server, secrets=svc
            )
            rendered = render(template, {**params, **resolved}, from_sanitized=False)
        else:
            rendered = render(template, params, from_sanitized=_from_sanitized)

        job = CommandJob(
            server_id=server_id,
            target_type=target_type,
            target_id=target_id,
            action_name=action_name,
            priority=priority,
            status="pending",
            params_sanitized=rendered.params_sanitized,
            secrets_enc=secrets_enc,
            retry_count=0,
            created_by=created_by,
        )
        if template.requires_lock:
            job.lock_key = self._lock_key(server_id, target_type, target_id, action_name)
        db.add(job)
        db.commit()
        db.refresh(job)

        if job.lock_key:
            holder = self._backend.acquire_lock(job.lock_key, job.id)
            if holder is not None:
                # Never leave an unrunnable pending row behind.
                db.delete(job)
                db.commit()
                raise LockConflict(holder)

        try:
            rq_job_id = self._enqueue(job)
        except Exception:
            # Enqueue failed: unwind the lock so the target isn't stuck.
            if job.lock_key:
                self._backend.release_lock(job.lock_key, job.id)
            raise
        if rq_job_id:
            job.rq_job_id = rq_job_id
            db.commit()
            db.refresh(job)

        # Rule 2: every job-backed state change writes exactly one audit row.
        # This is the single funnel for command mutations, so no action can land
        # un-audited. params_sanitized already has secrets masked (already_masked).
        # A lazy import avoids an import-time cycle (audit -> api.deps).
        from app.audit import record_audit

        target = f" {target_type} {target_id}" if target_id else f" {target_type}"
        record_audit(
            db,
            action=action_name,
            summary=f"Enqueued {action_name} on{target}",
            user_id=created_by,
            entity_type=target_type,
            entity_id=target_id,
            params=job.params_sanitized or {},
            result="enqueued",
            job_id=job.id,
            already_masked=True,
        )
        return job

    def _default_enqueue(self, job: CommandJob) -> str:
        from redis import Redis
        from rq import Queue

        from app.config import get_settings

        conn = Redis.from_url(get_settings().redis_url)
        queue = Queue(job.priority, connection=conn)
        rq_job = queue.enqueue(
            "app.core.jobs.execute_job",
            job.id,
            job_timeout=JOB_TIMEOUT_SECONDS,
            result_ttl=86400,
            failure_ttl=604800,
        )
        return rq_job.id

    # -- cancellation / retry ------------------------------------------------ #

    def cancel(self, db: Session, job: CommandJob) -> CommandJob:
        """Cancel a job. Pending -> cancelled immediately (lock released, RQ job
        removed); running -> a cancel is signalled and the worker stops at the
        next step boundary / process read (best-effort, rule from the spec)."""
        if job.status in ("success", "failure", "cancelled"):
            return job
        self._backend.request_cancel(job.id)
        if job.status == "pending":
            job.status = "cancelled"
            job.ended_at = _now()
            self._release(job)
            if job.rq_job_id:
                self._cancel_rq(job.rq_job_id)
            db.commit()
            db.refresh(job)
        return job

    def retry(
        self, db: Session, job: CommandJob, *, created_by: int | None
    ) -> CommandJob:
        """Manually re-run a terminal job as a fresh job with the same target and
        params. Auto-retry (idempotent, <=3) is separate and happens in-flight."""
        if job.status not in ("failure", "cancelled"):
            raise ValueError("only failed or cancelled jobs can be retried")
        return self.create(
            db,
            action_name=job.action_name,
            server_id=job.server_id,
            target_type=job.target_type,
            target_id=job.target_id,
            params=dict(job.params_sanitized or {}),
            priority=job.priority,
            created_by=created_by,
            _from_sanitized=True,
        )

    @staticmethod
    def _cancel_rq(rq_job_id: str) -> None:
        try:
            from redis import Redis
            from rq.job import Job

            from app.config import get_settings

            conn = Redis.from_url(get_settings().redis_url)
            Job.fetch(rq_job_id, connection=conn).cancel()
        except Exception:
            pass  # best-effort; the DB status is the source of truth.

    def _release(self, job: CommandJob) -> None:
        if job.lock_key:
            self._backend.release_lock(job.lock_key, job.id)
        self._backend.clear_cancel(job.id)

    # -- execution (worker side) --------------------------------------------- #

    def run_job(self, job_id: int, *, executor_factory=None) -> None:
        """RQ entrypoint body. Loads the job, runs its action with retries, and
        drives it to a terminal state. Always releases the lock."""
        factory = executor_factory or self._executor_factory or self._ssh_executor_factory
        db = self._sf()
        try:
            job = db.get(CommandJob, job_id)
            if job is None:
                return
            if job.status != "pending":
                # Already cancelled before pickup, or a duplicate delivery.
                if job.status == "cancelled":
                    self._release(job)
                return
            if self._backend.is_cancel_requested(job.id):
                self._to_terminal(db, job, "cancelled")
                return

            template = get_template(job.action_name)
            server = db.scalars(
                select(Server)
                .options(joinedload(Server.credential))
                .where(Server.id == job.server_id)
            ).first()
            try:
                # The worker only persists the masked params_sanitized. For a
                # template that declares secret sources, resolve each secret's
                # plaintext (job bundle + the Server row) and render with the real
                # values (session 1.8). Otherwise a secret-bearing template can't
                # be re-rendered safely — fail loud instead of running with `••••`.
                if template.secret_sources:
                    from app.core.secrets_resolve import resolve_secrets

                    resolved = resolve_secrets(
                        template,
                        secrets_enc=job.secrets_enc,
                        server=server,
                        secrets=self._secrets_service(),
                    )
                    rendered = render(
                        template,
                        {**(job.params_sanitized or {}), **resolved},
                        from_sanitized=False,
                    )
                else:
                    rendered = render(
                        template, dict(job.params_sanitized or {}), from_sanitized=True
                    )
            except Exception as exc:
                # RenderError (bad/masked params) or SecretResolutionError (a
                # missing server secret) — a determinate start failure, not a
                # transient one; record a breadcrumb and fail without retrying.
                job.status = "running"
                job.started_at = _now()
                db.commit()
                breadcrumb = LogWriter(db, job.id, backend=self._backend)
                breadcrumb.append("system", f"cannot start job: {exc}")
                breadcrumb.flush()
                self._to_terminal(db, job, "failure", exit_code=1)
                return

            job.status = "running"
            job.started_at = _now()
            db.commit()

            log_writer = LogWriter(
                db, job.id, backend=self._backend, secrets=rendered.secret_values
            )
            action = template.action_class()

            while True:
                try:
                    self._run_once(db, job, server, rendered, action, log_writer, factory)
                    log_writer.flush()
                    self._to_terminal(db, job, "success", exit_code=0)
                    return
                except JobCancelled:
                    log_writer.flush()
                    self._to_terminal(db, job, "cancelled")
                    return
                except Exception:
                    log_writer.flush()
                    if template.idempotent and job.retry_count < MAX_AUTO_RETRIES:
                        job.retry_count += 1
                        db.commit()
                        log_writer.append("system", f"retry {job.retry_count}/{MAX_AUTO_RETRIES}")
                        log_writer.flush()
                        continue
                    self._to_terminal(db, job, "failure", exit_code=1)
                    return
        finally:
            db.close()

    def _run_once(self, db, job, server, rendered, action, log_writer, factory) -> None:
        import asyncio

        # Session 6.2: a platform-local template (report rendering) has no server
        # to reach, so never build an SSH executor for it — `server` is None and
        # any ctx.stream/capture call is a programming error, not a runtime one.
        if get_template(job.action_name).local:
            factory = _local_executor_factory

        async def _amain() -> None:
            async with factory(server) as executor:
                ctx = JobContextImpl(
                    db, job, rendered, get_template(job.action_name).run_as,
                    executor, log_writer, self._backend,
                    # retry_count is 0 on the initial run and is bumped *before*
                    # each retry re-enters _run_once, so attempt is 1-based here.
                    attempt=job.retry_count + 1,
                )
                await action.run(ctx)
                # Drift baseline capture (session 6.7): a managed change to a
                # tracked config artefact must MOVE the baseline (not register as
                # drift). Runs inside the still-open SSH session, attributed to
                # this job. Never fails the job — a capture error is logged and
                # the job still succeeds.
                template = get_template(job.action_name)
                if template.writes_config:
                    try:
                        from app.core.drift import capture_baselines

                        moved = await capture_baselines(ctx, template.writes_config)
                        if moved:
                            await ctx.emit(f"drift baseline updated: {', '.join(moved)}")
                    except Exception:  # noqa: BLE001 — capture must not fail the job
                        import logging

                        logging.getLogger("app.jobs").exception(
                            "drift baseline capture failed for job %s", job.id
                        )

        asyncio.run(_amain())

    def _ssh_executor_factory(self, server: Server):
        if self._ssh is None:
            raise RuntimeError("JobRunner has no SSHService configured for execution")
        return SSHRemoteExecutor(self._ssh, server)

    def _to_terminal(
        self, db: Session, job: CommandJob, status: str, *, exit_code: int | None = None
    ) -> None:
        job.status = status
        job.ended_at = _now()
        if exit_code is not None:
            job.exit_code = exit_code
        self._release(job)
        db.commit()
        # Dispatch in-app / email / webhook notifications for terminal states
        # (success + failure). Import lazily so tests that don't load the full
        # notification stack continue to work without extra fixtures.
        try:
            from app.core.notifications import dispatch_job_event
            dispatch_job_event(db, job_id=job.id, action_name=job.action_name, status=status)
        except Exception:  # noqa: BLE001 — notification failure must never crash the job engine
            import logging
            logging.getLogger("app.jobs").exception(
                "notification dispatch failed for job %s", job.id
            )


def build_runner() -> JobRunner:
    """Production JobRunner: real Redis backend, RQ enqueue, SSH execution."""
    from redis import Redis

    from app.config import get_settings
    from app.core.security import get_secrets_service
    from app.db import SessionLocal

    conn = Redis.from_url(get_settings().redis_url)
    secrets = get_secrets_service()
    return JobRunner(
        SessionLocal,
        RedisJobBackend(conn),
        ssh=SSHService(secrets),
        secrets=secrets,
    )


def execute_job(job_id: int) -> None:
    """RQ job function. Kept tiny so `queue.enqueue('app.core.jobs.execute_job')`
    imports cleanly in the worker."""
    build_runner().run_job(job_id)
