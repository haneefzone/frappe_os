"""JobRunner engine: locking (409), streamed steps/logs, idempotent-only retry,
step-failure traceback, and cancellation transitions — all with in-memory fakes
(no Redis, no RQ, no SSH)."""

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.commands import MASK
from app.core.commands.actions import Action
from app.core.commands.templates import CommandTemplate
from app.core.jobs import (
    InMemoryJobBackend,
    JobRunner,
    LockConflict,
    LogWriter,
)
from app.core.permissions import SERVER_MANAGE
from app.db import Base
from app.models import CommandJob, CommandStep, LogEntry, Server

# -- fixtures ---------------------------------------------------------------- #


@pytest.fixture
def sf():
    """A sessionmaker over one shared in-memory SQLite connection, so the
    runner's own sessions and the test's assertions see the same data."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    yield factory
    engine.dispose()


@pytest.fixture
def server_id(sf):
    with sf() as db:
        server = Server(name="vm-alpha", hostname="10.0.0.5", ssh_port=22)
        db.add(server)
        db.commit()
        return server.id


def make_runner(sf, backend=None):
    return JobRunner(sf, backend or InMemoryJobBackend(), enqueue=lambda job: None)


# -- test doubles ------------------------------------------------------------ #


class FakeExecutor:
    """Records argv calls and replays scripted output lines to the log."""

    def __init__(self, script=None, exit_code=0, raise_exc=None, on_first_line=None):
        self.script = script or []
        self.exit_code = exit_code
        self.raise_exc = raise_exc
        self.on_first_line = on_first_line
        self.calls: list[list[str]] = []

    async def run(self, argv, *, cwd, run_as, on_line, cancel_check):
        self.calls.append(list(argv))
        if self.raise_exc is not None:
            raise self.raise_exc
        for stream_name, text in self.script:
            result = on_line(stream_name, text)
            if result is not None:
                await result
            if self.on_first_line is not None:
                self.on_first_line()
                self.on_first_line = None
            if cancel_check is not None and cancel_check():
                break
        return self.exit_code


def fake_factory(executor):
    class _CM:
        async def __aenter__(self):
            return executor

        async def __aexit__(self, *exc):
            return False

    return lambda server: _CM()


class FailingAction(Action):
    async def run(self, ctx):
        with ctx.step("Do the thing"):
            raise RuntimeError("kaboom-secret-trace")


def _tmpl(name, action_class, *, idempotent):
    return CommandTemplate(
        action_name=name,
        argv=("true",),
        cwd=None,
        params=(),
        action_class=action_class,
        idempotent=idempotent,
        requires_lock=False,
        required_permission=SERVER_MANAGE,
    )


def patch_templates(monkeypatch, templates):
    registry = {t.action_name: t for t in templates}

    def fake_get(name):
        if name in registry:
            return registry[name]
        from app.core.commands.registry import get_template as real

        return real(name)

    monkeypatch.setattr("app.core.jobs.get_template", fake_get)


def _create(runner, sf, server_id, action_name, params=None, target_id=None):
    with sf() as db:
        job = runner.create(
            db,
            action_name=action_name,
            server_id=server_id,
            target_type="server",
            target_id=target_id,
            params=params or {},
            priority="default",
            created_by=None,
        )
        return job.id


# -- lock conflict (rule 4 -> 409) ------------------------------------------- #


def test_second_job_on_same_target_conflicts_with_blocking_id(sf, server_id):
    runner = make_runner(sf)
    first_id = _create(runner, sf, server_id, "system.echo_demo", {"message": "hi"})
    with sf() as db, pytest.raises(LockConflict) as excinfo:
        runner.create(
            db,
            action_name="system.echo_demo",
            server_id=server_id,
            target_type="server",
            target_id=None,
            params={"message": "again"},
            priority="default",
            created_by=None,
        )
    assert excinfo.value.blocking_job_id == first_id
    # The losing attempt left no orphan pending row behind.
    with sf() as db:
        assert db.scalars(select(CommandJob)).all()[0].id == first_id
        assert len(db.scalars(select(CommandJob)).all()) == 1


def test_lock_free_action_allows_concurrent_jobs(sf, server_id):
    runner = make_runner(sf)
    a = _create(runner, sf, server_id, "server.detect_tools")
    b = _create(runner, sf, server_id, "server.detect_tools")
    assert a != b  # detect_tools takes no lock


# -- happy path: pending -> running -> success, 3 steps, streamed logs ------- #


def test_echo_demo_runs_three_steps_and_streams_logs(sf, server_id):
    runner = make_runner(sf)
    job_id = _create(runner, sf, server_id, "system.echo_demo", {"message": "hello"})

    executor = FakeExecutor(script=[("stdout", "one"), ("stdout", "two")])
    runner.run_job(job_id, executor_factory=fake_factory(executor))

    with sf() as db:
        job = db.get(CommandJob, job_id)
        assert job.status == "success"
        assert job.exit_code == 0
        assert job.started_at is not None and job.ended_at is not None

        steps = db.scalars(
            select(CommandStep).where(CommandStep.job_id == job_id).order_by(CommandStep.order)
        ).all()
        assert [s.name for s in steps] == ["Prepare", "Echo message", "Finish"]
        assert all(s.status == "success" for s in steps)

        logs = db.scalars(select(LogEntry).where(LogEntry.job_id == job_id)).all()
        # 3 steps x 2 lines each streamed to the DB.
        assert len(logs) == 6
        assert {log.content for log in logs} == {"one", "two"}


def test_lock_released_after_success(sf, server_id):
    backend = InMemoryJobBackend()
    runner = make_runner(sf, backend)
    job_id = _create(runner, sf, server_id, "system.echo_demo", {"message": "hi"})
    runner.run_job(job_id, executor_factory=fake_factory(FakeExecutor()))
    # A fresh job on the same target now succeeds (lock was freed).
    again = _create(runner, sf, server_id, "system.echo_demo", {"message": "hi"})
    assert again != job_id


# -- step failure marks the job failed with a traceback ---------------------- #


def test_step_failure_marks_job_failed_with_traceback(sf, server_id, monkeypatch):
    patch_templates(monkeypatch, [_tmpl("test.fail_once", FailingAction, idempotent=False)])
    runner = make_runner(sf)
    job_id = _create(runner, sf, server_id, "test.fail_once")

    runner.run_job(job_id, executor_factory=fake_factory(FakeExecutor()))

    with sf() as db:
        job = db.get(CommandJob, job_id)
        assert job.status == "failure"
        assert job.exit_code == 1
        assert job.retry_count == 0  # non-idempotent: no retry
        step = db.scalars(select(CommandStep).where(CommandStep.job_id == job_id)).one()
        assert step.status == "failure"
        assert "kaboom-secret-trace" in (step.error_traceback or "")


# -- retry only for idempotent actions --------------------------------------- #


def test_idempotent_failure_retries_up_to_three_times(sf, server_id, monkeypatch):
    patch_templates(monkeypatch, [_tmpl("test.fail_idem", FailingAction, idempotent=True)])
    runner = make_runner(sf)
    job_id = _create(runner, sf, server_id, "test.fail_idem")

    runner.run_job(job_id, executor_factory=fake_factory(FakeExecutor()))

    with sf() as db:
        job = db.get(CommandJob, job_id)
        assert job.status == "failure"
        assert job.retry_count == 3  # 1 initial + 3 retries, then give up
        steps = db.scalars(
            select(CommandStep)
            .where(CommandStep.job_id == job_id)
            .order_by(CommandStep.attempt, CommandStep.order)
        ).all()
        assert len(steps) == 4  # a step per attempt
        # DOO-96: each attempt rebuilds the context so `order` restarts at 1;
        # `attempt` (1..4) is what disambiguates the otherwise-identical rows.
        assert [s.attempt for s in steps] == [1, 2, 3, 4]
        assert [s.order for s in steps] == [1, 1, 1, 1]


def test_non_idempotent_failure_does_not_retry(sf, server_id, monkeypatch):
    patch_templates(monkeypatch, [_tmpl("test.fail_once", FailingAction, idempotent=False)])
    runner = make_runner(sf)
    job_id = _create(runner, sf, server_id, "test.fail_once")
    runner.run_job(job_id, executor_factory=fake_factory(FakeExecutor()))
    with sf() as db:
        assert db.get(CommandJob, job_id).retry_count == 0


# -- secret-param re-render guard (DOO-94 #1) -------------------------------- #


class NoopAction(Action):
    async def run(self, ctx):  # pragma: no cover - guard trips before we run
        with ctx.step("noop"):
            pass


def _secret_tmpl(name):
    from app.core.commands.templates import ParamSpec

    return CommandTemplate(
        action_name=name,
        argv=("mysql", "--password={password}"),
        cwd=None,
        params=(ParamSpec("password", regex=r".{1,64}", secret=True),),
        action_class=NoopAction,
        idempotent=True,
        requires_lock=False,
        required_permission=SERVER_MANAGE,
    )


def test_manual_retry_of_secret_job_fails_loud_not_masked(sf, server_id, monkeypatch):
    # create() with the real secret succeeds; the persisted params are masked.
    patch_templates(monkeypatch, [_secret_tmpl("test.secret_job")])
    from app.core.commands import SecretParamUnresolved

    runner = make_runner(sf)
    job_id = _create(runner, sf, server_id, "test.secret_job", {"password": "hunter2"})
    with sf() as db:
        job = db.get(CommandJob, job_id)
        assert job.params_sanitized == {"password": MASK}
        job.status = "failure"
        db.commit()
    # Retrying re-renders from those masked params — must refuse, not run `••••`.
    with sf() as db, pytest.raises(SecretParamUnresolved):
        runner.retry(db, db.get(CommandJob, job_id), created_by=None)


def test_worker_of_secret_job_fails_terminally_with_breadcrumb(sf, server_id, monkeypatch):
    patch_templates(monkeypatch, [_secret_tmpl("test.secret_job")])
    runner = make_runner(sf)
    job_id = _create(runner, sf, server_id, "test.secret_job", {"password": "hunter2"})

    runner.run_job(job_id, executor_factory=fake_factory(FakeExecutor()))

    with sf() as db:
        job = db.get(CommandJob, job_id)
        assert job.status == "failure"
        assert job.exit_code == 1
        logs = db.scalars(select(LogEntry).where(LogEntry.job_id == job_id)).all()
        assert any(log.content.startswith("cannot start job:") for log in logs)
        # The mask never leaks into the breadcrumb.
        assert all(MASK not in log.content for log in logs)


# -- cancellation transitions ------------------------------------------------ #


def test_cancel_pending_transitions_to_cancelled_and_releases_lock(sf, server_id):
    backend = InMemoryJobBackend()
    runner = make_runner(sf, backend)
    job_id = _create(runner, sf, server_id, "system.echo_demo", {"message": "hi"})
    with sf() as db:
        job = db.get(CommandJob, job_id)
        runner.cancel(db, job)
        assert job.status == "cancelled"
        assert job.ended_at is not None
    # Lock freed: another job on the same target can be created.
    assert _create(runner, sf, server_id, "system.echo_demo", {"message": "hi"}) != job_id


def test_cancel_before_pickup_is_honoured_by_worker(sf, server_id):
    backend = InMemoryJobBackend()
    runner = make_runner(sf, backend)
    job_id = _create(runner, sf, server_id, "system.echo_demo", {"message": "hi"})
    backend.request_cancel(job_id)  # cancel requested while still queued

    executor = FakeExecutor(script=[("stdout", "should-not-run")])
    runner.run_job(job_id, executor_factory=fake_factory(executor))

    with sf() as db:
        assert db.get(CommandJob, job_id).status == "cancelled"
    assert executor.calls == []  # nothing executed


def test_cancel_midway_stops_at_next_step_boundary(sf, server_id):
    backend = InMemoryJobBackend()
    runner = make_runner(sf, backend)
    job_id = _create(runner, sf, server_id, "system.echo_demo", {"message": "hi"})

    # Request cancel while the first step is streaming; the second step's entry
    # then raises JobCancelled and the job transitions to cancelled.
    executor = FakeExecutor(
        script=[("stdout", "line")], on_first_line=lambda: backend.request_cancel(job_id)
    )
    runner.run_job(job_id, executor_factory=fake_factory(executor))

    with sf() as db:
        job = db.get(CommandJob, job_id)
        assert job.status == "cancelled"
        steps = db.scalars(
            select(CommandStep).where(CommandStep.job_id == job_id).order_by(CommandStep.order)
        ).all()
        # First step completed; the run stopped before finishing all three.
        assert steps[0].name == "Prepare"
        assert len(steps) < 3


# -- log redaction ----------------------------------------------------------- #


def test_log_writer_redacts_secret_values(sf, server_id):
    with sf() as db:
        job = CommandJob(
            server_id=server_id,
            target_type="server",
            action_name="test.secret",
            priority="default",
            status="running",
            params_sanitized={},
        )
        db.add(job)
        db.commit()
        writer = LogWriter(db, job.id, secrets=("s3cr3t",), batch_size=1)
        writer.append("stdout", "connecting with password s3cr3t now")
        writer.flush()
        entry = db.scalars(select(LogEntry).where(LogEntry.job_id == job.id)).one()
        assert "s3cr3t" not in entry.content
        assert MASK in entry.content


def test_log_writer_publishes_each_line(sf, server_id):
    backend = InMemoryJobBackend()
    with sf() as db:
        job = CommandJob(
            server_id=server_id, target_type="server", action_name="x",
            priority="default", status="running", params_sanitized={},
        )
        db.add(job)
        db.commit()
        writer = LogWriter(db, job.id, backend=backend, batch_size=1)
        writer.append("stdout", "hello")
        assert backend.published and backend.published[0][0] == job.id
        assert "hello" in backend.published[0][1]
