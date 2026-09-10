"""restic weekly snapshots: retention + integrity check (session 4.2 — Full-system DR).

Builds on the 4.1 restic suite (tests/test_restic.py — reusing its in-memory
executor, DB fixtures and secret-leak assertions) and covers the 4.2 additions:

- the pure retention/check helpers (keep-flag builder, check summariser, restic
  lock-error detection);
- the two new actions (`restic.forget`, `restic.check`) driven through the REAL
  JobContext over the in-memory executor, including the destructive "refuse to
  prune with no policy" floor and the breach alert on a failed check;
- the scheduler dispatch of the server-targeted restic DR actions;
- the API surface (retention config round-trip, forget/check launches + guards).

Golden-rule-6 stays in force: no secret ever reaches an argv, a job param, or a
log/notification line — only the 0600 env file restic sources.
"""

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.routes.jobs import get_job_runner
from app.core import restic as rst
from app.core.commands import RenderError, get_template, render
from app.core.jobs import CaptureResult, InMemoryJobBackend, JobRunner
from app.core.scheduler import ScheduleError, _build_restic_fire, fire_schedule
from app.core.security import get_secrets_service
from app.db import Base
from app.models import CommandJob, Notification, User
from app.models.restic import ResticRepo
from app.models.schedule import SCHEDULE_ACTIONS, Schedule

# Reuse the 4.1 harness plain helpers (constants, fake executor, seed helpers).
# The pytest fixtures (sf / rc_client / api_env) are redefined locally below —
# pytest fixtures cannot be shared by import without F811 shadowing.
from tests.conftest import csrf_headers, login
from tests.test_restic import (
    ACCESS_KEY,
    PASSWORD,
    SECRET_KEY,
    ResticExecutor,
    _no_secret_on_any_argv,
    _repo,
    _run,
    _server,
    _target,
)

REPO_URI = "s3:https://minio.local:9000/fdm/restic/server"


@pytest.fixture
def sf():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    yield factory
    engine.dispose()


@pytest.fixture
def rc_client(client, db_session):
    runner = JobRunner(
        lambda: db_session, InMemoryJobBackend(), enqueue=lambda job: None,
        secrets=get_secrets_service(),
    )
    client.app.dependency_overrides[get_job_runner] = lambda: runner
    yield client
    client.app.dependency_overrides.pop(get_job_runner, None)


@pytest.fixture
def api_env(db_session):
    sid = _server(db_session, name="vm-a", hostname="10.0.0.1")
    tid = _target(db_session)
    return {"server_id": sid, "target_id": tid}


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #


def test_build_forget_keep_args_maps_every_dimension():
    repo = ResticRepo(
        server_id=1, prefix="p",
        retention_keep_last=3, retention_keep_daily=7,
        retention_keep_weekly=4, retention_keep_monthly=6,
    )
    assert rst.build_forget_keep_args(repo) == [
        "--keep-last", "3", "--keep-daily", "7",
        "--keep-weekly", "4", "--keep-monthly", "6",
    ]


def test_build_forget_keep_args_partial_policy():
    repo = ResticRepo(server_id=1, prefix="p", retention_keep_weekly=4)
    assert rst.build_forget_keep_args(repo) == ["--keep-weekly", "4"]


def test_build_forget_keep_args_empty_policy_refuses():
    """An empty policy must never reach `forget --prune` — it would delete every
    snapshot. The builder raises so the destructive sweep is refused."""
    repo = ResticRepo(server_id=1, prefix="p")
    with pytest.raises(rst.ResticError):
        rst.build_forget_keep_args(repo)


def test_build_forget_keep_args_rejects_zero():
    repo = ResticRepo(server_id=1, prefix="p", retention_keep_last=0)
    with pytest.raises(rst.ResticError):
        rst.build_forget_keep_args(repo)


def test_summarize_check_pass_and_fail():
    ok, summary = rst.summarize_check(0, "using temporary cache\nno errors were found\n")
    assert ok is True and "no errors were found" in summary
    ok, summary = rst.summarize_check(1, "pack 1a2b: Fatal: repository contains errors")
    assert ok is False and ("error" in summary.lower() or "fatal" in summary.lower())


def test_summarize_check_fallback_summaries():
    ok, summary = rst.summarize_check(0, "")
    assert ok is True and summary
    ok, summary = rst.summarize_check(2, "")
    assert ok is False and "exit 2" in summary


def test_is_restic_lock_error():
    assert rst.is_restic_lock_error("unable to create lock in backend: already exists")
    assert rst.is_restic_lock_error("repository is already locked exclusively by PID 9")
    assert not rst.is_restic_lock_error("no errors were found")
    assert not rst.is_restic_lock_error("Fatal: repository contains errors")


# --------------------------------------------------------------------------- #
# Registry / render
# --------------------------------------------------------------------------- #


def test_forget_and_check_templates_registered():
    for action in ("restic.forget", "restic.check"):
        t = get_template(action)
        assert t.required_permission == "server:manage"
        assert t.secret_params == set()
    # forget is destructive → non-idempotent (engine never auto-retries it).
    assert get_template("restic.forget").idempotent is False
    # check is non-idempotent too (a failed check must not silently re-alert).
    assert get_template("restic.check").idempotent is False


def test_check_template_renders_subset_and_rejects_bad_subset():
    rc = render(get_template("restic.check"), {"repo": REPO_URI, "subset": "5%"})
    assert rc.argv == ["restic", "-r", REPO_URI, "check", "--read-data-subset", "5%"]
    for bad in ("5", "5%%", "all", "$(id)", "5% ; rm"):
        with pytest.raises(RenderError):
            render(get_template("restic.check"), {"repo": REPO_URI, "subset": bad})


# --------------------------------------------------------------------------- #
# Action harness — forget + check over the in-memory executor
# --------------------------------------------------------------------------- #


class RetentionExecutor(ResticExecutor):
    """Extends the 4.1 fake executor to answer the `forget` + `check` restic
    subcommands (the base raises on an unknown subcommand)."""

    def __init__(self, *, forget_exit=0, forget_stdout="1 snapshots have been removed",
                 check_exit=0, check_stdout="no errors were found", **kw):
        super().__init__(**kw)
        self.forget_exit = forget_exit
        self.forget_stdout = forget_stdout
        self.check_exit = check_exit
        self.check_stdout = check_stdout

    async def capture(self, argv, *, cwd=None, timeout=120.0):
        if argv[:3] == ["bash", "-c", rst_wrap()]:
            sub = self._restic_subcmd(argv)
            if sub == "forget":
                self.captured.append(list(argv))
                return CaptureResult(self.forget_exit, self.forget_stdout, "")
            if sub == "check":
                self.captured.append(list(argv))
                return CaptureResult(self.check_exit, self.check_stdout, "")
        return await super().capture(argv, cwd=cwd, timeout=timeout)


def rst_wrap():
    from app.core.commands.actions import _RESTIC_ENV_WRAP

    return _RESTIC_ENV_WRAP


def _seed(db, **repo_kw):
    sid = _server(db, hostname="prod-01")
    tid = _target(db)
    rid = _repo(db, sid, tid, initialized=repo_kw.pop("initialized", True))
    if repo_kw:
        repo = db.get(ResticRepo, rid)
        for k, v in repo_kw.items():
            setattr(repo, k, v)
        db.commit()
    return sid


def test_forget_action_prunes_with_policy_and_records_evidence(sf):
    with sf() as db:
        sid = _seed(db, retention_keep_last=3, retention_keep_daily=7)
    ex = RetentionExecutor()
    job_id = _run(sf, action="restic.forget", server_id=sid,
                  params={"repo": REPO_URI}, executor=ex)
    with sf() as db:
        assert db.get(CommandJob, job_id).status == "success"
        repo = db.scalars(select(ResticRepo)).first()
        assert repo.last_forget_at is not None
    forget_argv = next(
        a for a in ex.captured if a[:3] == ["bash", "-c", rst_wrap()] and "forget" in a
    )
    # The per-repo policy became restic --keep-* flags on the argv, plus --prune.
    assert "--keep-last" in forget_argv and "3" in forget_argv
    assert "--keep-daily" in forget_argv and "7" in forget_argv
    assert "--prune" in forget_argv and "fdm-config-tier" in forget_argv
    _no_secret_on_any_argv(ex)


def test_forget_action_refuses_when_no_policy(sf):
    with sf() as db:
        sid = _seed(db)  # no retention_* set
    ex = RetentionExecutor()
    job_id = _run(sf, action="restic.forget", server_id=sid,
                  params={"repo": REPO_URI}, executor=ex)
    with sf() as db:
        assert db.get(CommandJob, job_id).status == "failure"
        assert db.scalars(select(ResticRepo)).first().last_forget_at is None
    # No restic forget ever ran (refused before touching the repo).
    assert not any(
        a[:3] == ["bash", "-c", rst_wrap()] and "forget" in a for a in ex.captured
    )


def test_check_action_pass_records_ok_no_alert(sf):
    with sf() as db:
        sid = _seed(db)
    ex = RetentionExecutor(check_exit=0, check_stdout="no errors were found")
    job_id = _run(sf, action="restic.check", server_id=sid,
                  params={"repo": REPO_URI, "subset": "5%"}, executor=ex)
    with sf() as db:
        assert db.get(CommandJob, job_id).status == "success"
        repo = db.scalars(select(ResticRepo)).first()
        assert repo.last_check_ok is True
        assert repo.last_check_at is not None
        assert "no errors" in (repo.last_check_summary or "")
        # A passing check raises no breach notification.
        assert db.scalars(select(Notification)).all() == []
    _no_secret_on_any_argv(ex)


def test_check_action_failure_records_and_raises_breach_alert(sf):
    with sf() as db:
        sid = _seed(db)
        # An active user so the shared channel groundwork has a recipient.
        db.add(User(email="ops@example.com", full_name="Ops", is_active=True,
                    password_hash="x", role_id=1))
        db.commit()
    ex = RetentionExecutor(check_exit=1, check_stdout="Fatal: repository contains errors")
    job_id = _run(sf, action="restic.check", server_id=sid,
                  params={"repo": REPO_URI, "subset": "5%"}, executor=ex)
    with sf() as db:
        # A genuine check failure fails the job AND records the breach.
        assert db.get(CommandJob, job_id).status == "failure"
        repo = db.scalars(select(ResticRepo)).first()
        assert repo.last_check_ok is False
        assert repo.last_check_at is not None
        # The breach alert went out over the reused notification channel.
        notes = db.scalars(select(Notification)).all()
        assert any(n.event_type == "restic.check_failed" for n in notes)
        # The notification body carries no credential (rule 6).
        blob = " ".join((n.title or "") + (n.body or "") for n in notes)
        assert PASSWORD not in blob and SECRET_KEY not in blob and ACCESS_KEY not in blob


def test_check_action_lock_clash_is_not_a_breach(sf):
    """A restic repo-lock clash (a concurrent op holds the repo) fails the job but
    is NOT recorded as an integrity failure and raises no (false) breach alert."""
    with sf() as db:
        sid = _seed(db)
        db.add(User(email="ops2@example.com", full_name="Ops2", is_active=True,
                    password_hash="x", role_id=1))
        db.commit()
    ex = RetentionExecutor(
        check_exit=1, check_stdout="unable to create lock in backend: repository is already locked"
    )
    job_id = _run(sf, action="restic.check", server_id=sid,
                  params={"repo": REPO_URI, "subset": "5%"}, executor=ex)
    with sf() as db:
        assert db.get(CommandJob, job_id).status == "failure"
        repo = db.scalars(select(ResticRepo)).first()
        # Inconclusive: no result recorded, no breach alert. (The generic
        # job.failure notification the engine emits is fine — only a restic
        # integrity breach must be absent.)
        assert repo.last_check_ok is None
        notes = db.scalars(select(Notification)).all()
        assert not any(n.event_type == "restic.check_failed" for n in notes)


def test_check_action_transient_error_is_not_a_breach(sf):
    """A connectivity/transient failure (S3 unreachable) means the check never
    verified anything — fail the job, but do NOT record an integrity result or fire
    the DR breach alert (which would be a false DR-panic)."""
    with sf() as db:
        sid = _seed(db)
        db.add(User(email="ops3@example.com", full_name="Ops3", is_active=True,
                    password_hash="x", role_id=1))
        db.commit()
    ex = RetentionExecutor(
        check_exit=1,
        check_stdout="Fatal: unable to open repository at s3:...: dial tcp: i/o timeout",
    )
    job_id = _run(sf, action="restic.check", server_id=sid,
                  params={"repo": REPO_URI, "subset": "5%"}, executor=ex)
    with sf() as db:
        assert db.get(CommandJob, job_id).status == "failure"
        repo = db.scalars(select(ResticRepo)).first()
        # Inconclusive: no ok result recorded, no false integrity breach alert.
        assert repo.last_check_ok is None
        notes = db.scalars(select(Notification)).all()
        assert not any(n.event_type == "restic.check_failed" for n in notes)


# --------------------------------------------------------------------------- #
# Scheduler dispatch (server-targeted restic DR actions)
# --------------------------------------------------------------------------- #


def test_restic_actions_are_schedulable():
    for action in ("restic.backup", "restic.forget", "restic.check"):
        assert action in SCHEDULE_ACTIONS


def _schedule(action, target_id, **kw):
    return Schedule(
        name=f"weekly {action}", target_type="server", target_id=target_id,
        action_name=action, cron="0 3 * * 0", timezone="Asia/Dubai",
        priority="low", enabled=True, **kw,
    )


def test_build_restic_fire_backup_resolves_repo_and_host(sf):
    with sf() as db:
        sid = _seed(db)
        server_id, target_id, params, side = _build_restic_fire(
            db, _schedule("restic.backup", sid)
        )
    assert server_id == sid and side is None
    assert params["repo"].startswith("s3:") and params["host"] == "prod-01"


def test_build_restic_fire_check_carries_subset(sf):
    with sf() as db:
        sid = _seed(db)
        _, _, params, _ = _build_restic_fire(db, _schedule("restic.check", sid))
    assert params["subset"] == rst.DEFAULT_CHECK_SUBSET


def test_build_restic_fire_forget_has_no_keep_params(sf):
    """Retention lives on the repo; the fire threads no keep-* params (the action
    reads the policy off the ResticRepo)."""
    with sf() as db:
        sid = _seed(db, retention_keep_last=5)
        _, _, params, _ = _build_restic_fire(db, _schedule("restic.forget", sid))
    assert set(params) == {"repo"}


def test_build_restic_fire_uninitialised_repo_raises(sf):
    with sf() as db:
        sid = _seed(db, initialized=False)
        with pytest.raises(ScheduleError):
            _build_restic_fire(db, _schedule("restic.backup", sid))


def test_build_restic_fire_no_repo_raises(sf):
    with sf() as db:
        sid = _server(db)  # server but no restic repo configured
        with pytest.raises(ScheduleError):
            _build_restic_fire(db, _schedule("restic.check", sid))


def test_build_restic_fire_forget_without_policy_pauses(sf):
    """A `restic.forget` fire on a repo with no retention policy must raise
    ScheduleError (→ schedule pauses, next_run cleared) rather than enqueue a job
    doomed to fail at runtime. Mirrors the API launch guard (422 in forget_config).
    A `restic.check` on the same policy-less repo still fires fine."""
    with sf() as db:
        sid = _seed(db)  # ready repo, but no retention_* set
        with pytest.raises(ScheduleError):
            _build_restic_fire(db, _schedule("restic.forget", sid))
        # Same repo: check/backup are unaffected by the missing retention policy.
        _, _, params, _ = _build_restic_fire(db, _schedule("restic.check", sid))
        assert params["subset"] == rst.DEFAULT_CHECK_SUBSET


def test_fire_schedule_enqueues_restic_backup_commandjob(sf):
    """End-to-end through the real fire path: a due weekly snapshot schedule
    enqueues a locked/audited restic.backup CommandJob (golden rules 2/3)."""
    from datetime import UTC, datetime

    from app.core.jobs import InMemoryJobBackend, JobRunner

    with sf() as db:
        sid = _seed(db)
        sched = _schedule("restic.backup", sid)
        db.add(sched)
        db.commit()
        sched_id = sched.id
    runner = JobRunner(sf, InMemoryJobBackend(), enqueue=lambda job: None,
                       secrets=get_secrets_service())
    with sf() as db:
        sched = db.get(Schedule, sched_id)
        job = fire_schedule(db, runner, sched, now=datetime.now(UTC))
        assert job.action_name == "restic.backup"
        assert job.lock_key  # locked like a hand-launched job
        assert sched.last_run_job_id == job.id


# --------------------------------------------------------------------------- #
# API surface — retention config + forget/check launches
# --------------------------------------------------------------------------- #


def test_configure_persists_retention_and_summary(rc_client, db_session, api_env):
    login(rc_client, "developer@example.com")
    resp = rc_client.put(
        f"/api/servers/{api_env['server_id']}/restic-repo",
        json={
            "storage_target_id": api_env["target_id"],
            "prefix": "restic/vm-a",
            "password": PASSWORD,
            "retention_keep_last": 3,
            "retention_keep_weekly": 4,
        },
        headers=csrf_headers(rc_client),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["retention_keep_last"] == 3
    assert body["retention_keep_weekly"] == 4
    assert body["retention_summary"] == "last 3, 4 weekly"
    # Evidence fields are present for the §6 view even before any run.
    assert body["last_check_ok"] is None and body["last_forget_at"] is None


def test_configure_rejects_zero_retention(rc_client, db_session, api_env):
    login(rc_client, "developer@example.com")
    resp = rc_client.put(
        f"/api/servers/{api_env['server_id']}/restic-repo",
        json={"storage_target_id": api_env["target_id"], "password": PASSWORD,
              "retention_keep_last": 0},
        headers=csrf_headers(rc_client),
    )
    assert resp.status_code == 422


def test_forget_endpoint_422_without_policy_then_launches(rc_client, db_session, api_env):
    _repo(db_session, api_env["server_id"], api_env["target_id"], initialized=True)
    login(rc_client, "developer@example.com")
    h = csrf_headers(rc_client)
    # No policy yet → refuse (422), never enqueues a destructive prune.
    resp = rc_client.post(
        f"/api/servers/{api_env['server_id']}/restic-repo/forget", headers=h
    )
    assert resp.status_code == 422
    # Set a policy, then it launches.
    repo = db_session.scalars(select(ResticRepo)).first()
    repo.retention_keep_last = 5
    db_session.commit()
    resp = rc_client.post(
        f"/api/servers/{api_env['server_id']}/restic-repo/forget", headers=h
    )
    assert resp.status_code == 201, resp.text
    job = db_session.scalars(
        select(CommandJob).where(CommandJob.action_name == "restic.forget")
    ).first()
    assert job is not None and set(job.params_sanitized) == {"repo"}


def test_check_endpoint_launches_with_subset(rc_client, db_session, api_env):
    _repo(db_session, api_env["server_id"], api_env["target_id"], initialized=True)
    login(rc_client, "developer@example.com")
    resp = rc_client.post(
        f"/api/servers/{api_env['server_id']}/restic-repo/check",
        headers=csrf_headers(rc_client),
    )
    assert resp.status_code == 201, resp.text
    job = db_session.scalars(
        select(CommandJob).where(CommandJob.action_name == "restic.check")
    ).first()
    assert job is not None
    assert job.params_sanitized.get("subset") == rst.DEFAULT_CHECK_SUBSET


def test_readonly_cannot_forget_or_check(rc_client, db_session, api_env):
    _repo(db_session, api_env["server_id"], api_env["target_id"], initialized=True)
    # Give forget a valid policy so the request reaches the authz gate (not the
    # 422 policy guard) — proving read-only is refused on a *runnable* request.
    repo = db_session.scalars(select(ResticRepo)).first()
    repo.retention_keep_last = 5
    db_session.commit()
    login(rc_client, "readonly@example.com")
    h = csrf_headers(rc_client)
    for path in ("forget", "check"):
        resp = rc_client.post(
            f"/api/servers/{api_env['server_id']}/restic-repo/{path}", headers=h
        )
        assert resp.status_code == 403, path
