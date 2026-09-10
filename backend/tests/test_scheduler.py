"""Scheduler (session 2.1): cadence math, retention planning, the dispatch/tick
core (over an in-memory job backend + injected clock), the retention-sweep action
(over a fake executor), and the /api/schedules surface (RBAC, validation, run-now).
No Redis/RQ/SSH/wall-clock — every timing is an injected `now`."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.routes.jobs import get_job_runner
from app.core import backups as bk
from app.core import scheduler as sch
from app.core.jobs import InMemoryJobBackend, JobRunner
from app.core.scheduler import (
    ScheduleError,
    compute_next_run,
    dispatch_schedule,
    tick,
    validate_cadence,
)
from app.core.security import get_secrets_service
from app.db import Base
from app.models import CommandJob, CommandStep, Server
from app.models.backup import Backup
from app.models.bench import Bench
from app.models.restic import ResticRepo
from app.models.schedule import Schedule
from app.models.site import Site
from app.models.storage import StorageTarget
from tests.conftest import csrf_headers, login

BENCH_PATH = "/home/frappe/frappe-bench"
BK = f"{BENCH_PATH}/sites/test1.localhost/private/backups"

NOW = datetime(2026, 7, 10, 10, 0, tzinfo=UTC)


def _naive(dt: datetime) -> datetime:
    """Drop tzinfo for comparison (SQLite reads DateTime columns back tz-naive)."""
    return dt.replace(tzinfo=None) if dt.tzinfo else dt


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #


@pytest.fixture
def sf():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    yield factory
    engine.dispose()


def _setup_site(db) -> tuple[int, int, int]:
    s = Server(name="vm", hostname="10.0.0.9")
    db.add(s)
    db.commit()
    bench = Bench(
        server_id=s.id, path=BENCH_PATH, name="frappe-bench",
        frappe_version="16.2.0", redis_queue_port=11000, redis_cache_port=13000,
    )
    db.add(bench)
    db.commit()
    site = Site(bench_id=bench.id, name="test1.localhost", status="active")
    db.add(site)
    db.commit()
    return s.id, bench.id, site.id


def _setup_restic_server(db, *, hostname="prod-1.local") -> tuple[int, int]:
    """A server with a ready (target-attached, password-set) restic repo —
    session 4.2's weekly backup/forget/check schedules are server-targeted."""
    secrets = get_secrets_service()
    s = Server(name="prod-1", hostname=hostname)
    db.add(s)
    db.commit()
    target = StorageTarget(
        name="minio", provider="minio", endpoint_url="https://minio.local:9000",
        bucket="fdm", region="me-central-1",
        access_key_enc=secrets.encrypt("AKIA_TEST"),
        secret_key_enc=secrets.encrypt("s3cr3t"),
    )
    db.add(target)
    db.commit()
    repo = ResticRepo(
        server_id=s.id, storage_target_id=target.id, prefix="restic/prod-1",
        password_enc=secrets.encrypt("repo-pw"), initialized=True,
    )
    db.add(repo)
    db.commit()
    return s.id, repo.id


def _runner(sf) -> JobRunner:
    return JobRunner(
        sf, InMemoryJobBackend(), enqueue=lambda job: None, secrets=get_secrets_service()
    )


def _mk_backup(db, *, site_id, bench_id, created_at, status="success", n=1) -> Backup:
    """A success backup with realistic artifact paths (so retention rm has
    private/backups paths to validate)."""
    ts = created_at.strftime("%Y%m%d_%H%M%S")
    pfx = f"{BK}/{ts}-test1_localhost"
    row = Backup(
        site_id=site_id, bench_id=bench_id, type="db", status=status,
        db_path=f"{pfx}-database.sql.gz",
        config_path=f"{pfx}-site_config_backup.json",
        size_bytes=1024 * n,
        artifacts=[{"kind": "database", "path": f"{pfx}-database.sql.gz",
                    "size_bytes": 1024 * n, "checksum_sha256": "a" * 64}],
        created_at=created_at,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


class SweepExecutor:
    """Fake executor for the retention sweep: records `rm` streams, no capture."""

    def __init__(self, fail_on=None):
        self.streamed: list[list[str]] = []
        self._fail_on = fail_on

    async def run(self, argv, *, cwd, run_as, on_line, cancel_check):
        self.streamed.append(list(argv))
        if self._fail_on is not None and self._fail_on(argv):
            return 1
        return 0

    async def capture(self, argv, *, cwd=None, timeout=120.0):  # pragma: no cover
        raise AssertionError(f"retention sweep should not capture: {argv}")


def _fake_factory(executor):
    class _CM:
        async def __aenter__(self):
            return executor

        async def __aexit__(self, *exc):
            return False

    return lambda server: _CM()


# --------------------------------------------------------------------------- #
# Cadence
# --------------------------------------------------------------------------- #


def test_interval_next_run():
    s = Schedule(interval_seconds=3600)
    assert compute_next_run(s, after=NOW) == NOW + timedelta(hours=1)


def test_cron_next_run_in_dubai_tz():
    # 02:00 Asia/Dubai (UTC+4) == 22:00 UTC the day before; after 10:00 UTC on
    # the 10th the next 02:00 Dubai is the 11th 02:00 == 22:00 UTC on the 10th.
    s = Schedule(cron="0 2 * * *", timezone="Asia/Dubai")
    assert compute_next_run(s, after=NOW) == datetime(2026, 7, 10, 22, 0, tzinfo=UTC)


def test_cron_respects_utc_when_tz_utc():
    s = Schedule(cron="0 2 * * *", timezone="UTC")
    assert compute_next_run(s, after=NOW) == datetime(2026, 7, 11, 2, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    "cron,interval,tz",
    [
        ("0 2 * * *", 60, "UTC"),  # both
        (None, None, "UTC"),  # neither
        ("not a cron", None, "UTC"),  # bad cron
        ("0 2 * * *", None, "Mars/Phobos"),  # bad tz
        (None, 0, "UTC"),  # non-positive interval
    ],
)
def test_validate_cadence_rejects(cron, interval, tz):
    with pytest.raises(ScheduleError):
        validate_cadence(cron=cron, interval_seconds=interval, timezone=tz)


def test_compute_next_run_requires_aware():
    s = Schedule(interval_seconds=60)
    with pytest.raises(ScheduleError):
        compute_next_run(s, after=datetime(2026, 7, 10, 10, 0))  # naive


# --------------------------------------------------------------------------- #
# Retention planning
# --------------------------------------------------------------------------- #


def test_plan_retention_keep_last():
    rows = [
        Backup(id=i, status="success", type="db", size_bytes=1,
               created_at=NOW - timedelta(days=i))
        for i in range(1, 6)  # id 1 newest .. id 5 oldest
    ]
    keep, remove = bk.plan_retention(rows, keep_last=2, keep_days=None, now=NOW)
    assert [b.id for b in keep] == [1, 2]
    assert [b.id for b in remove] == [3, 4, 5]


def test_plan_retention_keep_days_union():
    rows = [
        Backup(id=1, status="success", type="db", size_bytes=1, created_at=NOW),
        Backup(id=2, status="success", type="db", size_bytes=1,
               created_at=NOW - timedelta(days=2)),
        Backup(id=3, status="success", type="db", size_bytes=1,
               created_at=NOW - timedelta(days=10)),
    ]
    # keep_days=3 keeps ids 1,2; keep_last=1 keeps id 1 -> union {1,2}; id 3 pruned.
    keep, remove = bk.plan_retention(rows, keep_last=1, keep_days=3, now=NOW)
    assert {b.id for b in keep} == {1, 2}
    assert [b.id for b in remove] == [3]


def test_plan_retention_never_removes_newest_or_only():
    only = [Backup(id=1, status="success", type="db", size_bytes=1, created_at=NOW)]
    keep, remove = bk.plan_retention(only, keep_last=0, keep_days=None, now=NOW)
    assert [b.id for b in keep] == [1]
    assert remove == []


def test_plan_retention_ignores_pending_and_failed():
    rows = [
        Backup(id=1, status="success", type="db", size_bytes=1, created_at=NOW),
        Backup(id=2, status="failed", type="db", size_bytes=1,
               created_at=NOW - timedelta(days=1)),
        Backup(id=3, status="pending", type="db", size_bytes=1,
               created_at=NOW - timedelta(days=2)),
    ]
    keep, remove = bk.plan_retention(rows, keep_last=1, keep_days=None, now=NOW)
    assert [b.id for b in keep] == [1]
    assert remove == []


def test_retention_artifact_paths_dedupes():
    b = Backup(
        id=1, status="success", type="db",
        db_path=f"{BK}/x-database.sql.gz",
        artifacts=[{"kind": "database", "path": f"{BK}/x-database.sql.gz"}],
    )
    assert bk.retention_artifact_paths(b) == [f"{BK}/x-database.sql.gz"]


# --------------------------------------------------------------------------- #
# Dispatch / tick
# --------------------------------------------------------------------------- #


def test_dispatch_backup_creates_job_and_pending_backup(sf):
    with sf() as db:
        _server, bench_id, site_id = _setup_site(db)
        sched = Schedule(
            name="nightly", target_type="site", target_id=site_id,
            action_name="site.backup", interval_seconds=3600, timezone="UTC",
            priority="low", with_files=False, enabled=True,
            next_run_at=NOW - timedelta(seconds=1),
        )
        db.add(sched)
        db.commit()
        sched_id = sched.id
    runner = _runner(sf)
    with sf() as db:
        sched = db.get(Schedule, sched_id)
        job = dispatch_schedule(db, runner, sched, now=NOW)
        assert job is not None
        assert job.action_name == "site.backup"
        assert job.status == "pending"
        # last_run recorded + next_run advanced one interval forward from now.
        assert sched.last_run_job_id == job.id
        assert sched.last_run_at == NOW
        assert sched.next_run_at == NOW + timedelta(hours=1)
    with sf() as db:
        rows = db.scalars(select(Backup).where(Backup.site_id == site_id)).all()
        assert len(rows) == 1
        assert rows[0].status == "pending"
        assert rows[0].taken_by_job_id == job.id


# --------------------------------------------------------------------------- #
# 4.2 — restic.backup / restic.forget / restic.check server-targeted dispatch
# --------------------------------------------------------------------------- #


def test_dispatch_restic_backup_weekly_creates_job_with_repo_and_host(sf):
    with sf() as db:
        server_id, _repo_id = _setup_restic_server(db)
        sched = Schedule(
            name="weekly config snapshot", target_type="server", target_id=server_id,
            action_name="restic.backup", cron="0 2 * * 0", timezone="UTC",
            priority="low", enabled=True, next_run_at=NOW - timedelta(seconds=1),
        )
        db.add(sched)
        db.commit()
        sched_id = sched.id
    runner = _runner(sf)
    with sf() as db:
        sched = db.get(Schedule, sched_id)
        job = dispatch_schedule(db, runner, sched, now=NOW)
        assert job is not None
        assert job.action_name == "restic.backup"
        assert job.server_id == server_id
        params = job.params_sanitized
        assert params["repo"] == "s3:https://minio.local:9000/fdm/restic/prod-1"
        assert params["host"] == "prod-1.local"
        # next_run_at advances to the next cron fire after `now`: cron
        # "0 2 * * 0" is Sunday 02:00, and NOW is Fri 2026-07-10 10:00, so the
        # next occurrence is Sun 2026-07-12 02:00 (not a naive now+7d).
        assert sched.next_run_at == datetime(2026, 7, 12, 2, 0, tzinfo=UTC)


def test_dispatch_restic_forget_and_check_carry_repo_param_only(sf):
    with sf() as db:
        server_id, _repo_id = _setup_restic_server(db)
        forget = Schedule(
            name="weekly retention", target_type="server", target_id=server_id,
            action_name="restic.forget", cron="0 3 * * 0", timezone="UTC",
            enabled=True, next_run_at=NOW - timedelta(seconds=1),
        )
        check = Schedule(
            name="weekly integrity check", target_type="server", target_id=server_id,
            action_name="restic.check", cron="0 4 * * 0", timezone="UTC",
            enabled=True, next_run_at=NOW - timedelta(seconds=1),
        )
        db.add_all([forget, check])
        db.commit()
        forget_id, check_id = forget.id, check.id
    runner = _runner(sf)
    with sf() as db:
        forget_job = dispatch_schedule(db, runner, db.get(Schedule, forget_id), now=NOW)
        check_job = dispatch_schedule(db, runner, db.get(Schedule, check_id), now=NOW)
        assert set(forget_job.params_sanitized) == {"repo"}
        assert set(check_job.params_sanitized) == {"repo"}
        assert forget_job.server_id == server_id == check_job.server_id


def test_dispatch_restic_action_requires_server_target_type(sf):
    with sf() as db:
        _s, _b, site_id = _setup_site(db)
        sched = Schedule(
            name="bad target", target_type="site", target_id=site_id,
            action_name="restic.backup", interval_seconds=3600, timezone="UTC",
            enabled=True, next_run_at=NOW - timedelta(seconds=1),
        )
        db.add(sched)
        db.commit()
        sched_id = sched.id
    runner = _runner(sf)
    with sf() as db:
        sched = db.get(Schedule, sched_id)
        job = dispatch_schedule(db, runner, sched, now=NOW)
        assert job is None
        assert sched.next_run_at is None  # paused: bad target_type never fires


def test_dispatch_restic_action_pauses_when_no_repo_configured(sf):
    with sf() as db:
        s = Server(name="bare", hostname="bare.local")
        db.add(s)
        db.commit()
        sched = Schedule(
            name="no repo yet", target_type="server", target_id=s.id,
            action_name="restic.check", interval_seconds=3600, timezone="UTC",
            enabled=True, next_run_at=NOW - timedelta(seconds=1),
        )
        db.add(sched)
        db.commit()
        sched_id = sched.id
    runner = _runner(sf)
    with sf() as db:
        sched = db.get(Schedule, sched_id)
        job = dispatch_schedule(db, runner, sched, now=NOW)
        assert job is None
        assert sched.next_run_at is None


def test_tick_fires_due_skips_future_and_disabled(sf):
    with sf() as db:
        _s, bench_id, site_id = _setup_site(db)
        due = Schedule(name="due", target_type="site", target_id=site_id,
                       action_name="site.backup", interval_seconds=3600, timezone="UTC",
                       enabled=True, next_run_at=NOW - timedelta(minutes=5))
        future = Schedule(name="future", target_type="site", target_id=site_id,
                          action_name="site.backup", interval_seconds=3600, timezone="UTC",
                          enabled=True, next_run_at=NOW + timedelta(hours=1))
        disabled = Schedule(name="off", target_type="site", target_id=site_id,
                            action_name="site.backup", interval_seconds=3600, timezone="UTC",
                            enabled=False, next_run_at=NOW - timedelta(minutes=5))
        db.add_all([due, future, disabled])
        db.commit()
        due_id = due.id
    runner = _runner(sf)
    fired = tick(sf, runner, now=NOW)
    assert fired == [due_id]


def test_dispatch_lockconflict_skips_and_advances(sf):
    with sf() as db:
        _s, bench_id, site_id = _setup_site(db)
        a = Schedule(name="a", target_type="site", target_id=site_id,
                     action_name="site.backup", interval_seconds=3600, timezone="UTC",
                     enabled=True, next_run_at=NOW - timedelta(minutes=5))
        b = Schedule(name="b", target_type="site", target_id=site_id,
                     action_name="site.backup", interval_seconds=3600, timezone="UTC",
                     enabled=True, next_run_at=NOW - timedelta(minutes=5))
        db.add_all([a, b])
        db.commit()
        b_id = b.id
    # Shared backend so the first fire's lock persists into the second.
    runner = JobRunner(sf, InMemoryJobBackend(), enqueue=lambda job: None,
                       secrets=get_secrets_service())
    fired = tick(sf, runner, now=NOW)
    assert len(fired) == 1  # only one of the two same-target schedules fired
    with sf() as db:
        # The skipped schedule advanced its next_run (not stuck in the past).
        # SQLite reads DateTime back tz-naive, so compare on the naive UTC value.
        other = db.get(Schedule, b_id)
        assert _naive(other.next_run_at) == _naive(NOW + timedelta(hours=1))


def test_dispatch_stale_target_pauses(sf):
    with sf() as db:
        sched = Schedule(name="ghost", target_type="site", target_id=99999,
                         action_name="site.backup", interval_seconds=3600, timezone="UTC",
                         enabled=True, next_run_at=NOW - timedelta(minutes=1))
        db.add(sched)
        db.commit()
        sched_id = sched.id
    runner = _runner(sf)
    with sf() as db:
        sched = db.get(Schedule, sched_id)
        job = dispatch_schedule(db, runner, sched, now=NOW)
        assert job is None
        assert sched.next_run_at is None  # paused until an operator fixes it


# --------------------------------------------------------------------------- #
# Retention sweep action (worker execution over a fake executor)
# --------------------------------------------------------------------------- #


def _run_sweep(sf, *, site_id, bench_id, params, executor):
    runner = _runner(sf)
    with sf() as db:
        job = runner.create(
            db, action_name="backup.retention_sweep", server_id=1, target_type="site",
            target_id=f"{BENCH_PATH}::test1.localhost", params=params,
            priority="low", created_by=None,
        )
        job_id = job.id
    runner.run_job(job_id, executor_factory=_fake_factory(executor))
    return job_id


def test_retention_sweep_removes_only_beyond_window(sf):
    with sf() as db:
        _s, bench_id, site_id = _setup_site(db)
        for i in range(4):  # 4 backups, day 0 (newest) .. day 3 (oldest)
            _mk_backup(db, site_id=site_id, bench_id=bench_id,
                       created_at=NOW - timedelta(days=i), n=i + 1)
    ex = SweepExecutor()
    job_id = _run_sweep(
        sf, site_id=site_id, bench_id=bench_id,
        params={"site": "test1.localhost", "bench_path": BENCH_PATH, "keep_last": "2"},
        executor=ex,
    )
    with sf() as db:
        assert db.get(CommandJob, job_id).status == "success"
        remaining = db.scalars(select(Backup).where(Backup.site_id == site_id)).all()
        assert len(remaining) == 2  # the 2 newest kept
    # Two rm streams (one per removed backup), each targeting private/backups paths.
    rms = [s for s in ex.streamed if s[:2] == ["rm", "-f"]]
    assert len(rms) == 2
    for cmd in rms:
        assert all("/private/backups/" in p for p in cmd[2:])


def test_retention_sweep_noop_when_within_policy(sf):
    with sf() as db:
        _s, bench_id, site_id = _setup_site(db)
        _mk_backup(db, site_id=site_id, bench_id=bench_id, created_at=NOW)
    ex = SweepExecutor()
    job_id = _run_sweep(
        sf, site_id=site_id, bench_id=bench_id,
        params={"site": "test1.localhost", "bench_path": BENCH_PATH, "keep_last": "5"},
        executor=ex,
    )
    with sf() as db:
        assert db.get(CommandJob, job_id).status == "success"
        assert len(db.scalars(select(Backup).where(Backup.site_id == site_id)).all()) == 1
    assert not [s for s in ex.streamed if s[:2] == ["rm", "-f"]]


def test_retention_sweep_never_deletes_only_backup(sf):
    with sf() as db:
        _s, bench_id, site_id = _setup_site(db)
        _mk_backup(db, site_id=site_id, bench_id=bench_id, created_at=NOW)
    ex = SweepExecutor()
    _run_sweep(
        sf, site_id=site_id, bench_id=bench_id,
        params={"site": "test1.localhost", "bench_path": BENCH_PATH, "keep_last": "1"},
        executor=ex,
    )
    with sf() as db:
        assert len(db.scalars(select(Backup).where(Backup.site_id == site_id)).all()) == 1


def test_retention_sweep_logs_dry_run_first(sf):
    with sf() as db:
        _s, bench_id, site_id = _setup_site(db)
        for i in range(3):
            _mk_backup(db, site_id=site_id, bench_id=bench_id,
                       created_at=NOW - timedelta(days=i))
    ex = SweepExecutor()
    job_id = _run_sweep(
        sf, site_id=site_id, bench_id=bench_id,
        params={"site": "test1.localhost", "bench_path": BENCH_PATH, "keep_last": "1"},
        executor=ex,
    )
    with sf() as db:
        names = [
            s.name for s in db.scalars(
                select(CommandStep).where(CommandStep.job_id == job_id)
                .order_by(CommandStep.order)
            ).all()
        ]
    # The dry-run evaluation step runs BEFORE the destructive removal step.
    assert names[0] == "Evaluate retention policy"
    assert names[1].startswith("Remove ")


# --------------------------------------------------------------------------- #
# API surface
# --------------------------------------------------------------------------- #


@pytest.fixture
def api_env(db_session):
    """A site to schedule against, shared by the API tests (uses the conftest
    `client`'s db_session)."""
    s = Server(name="vm", hostname="10.0.0.9")
    db_session.add(s)
    db_session.commit()
    bench = Bench(server_id=s.id, path=BENCH_PATH, name="frappe-bench",
                  frappe_version="16.2.0")
    db_session.add(bench)
    db_session.commit()
    site = Site(bench_id=bench.id, name="test1.localhost", status="active")
    db_session.add(site)
    db_session.commit()
    return {"site_id": site.id}


@pytest.fixture
def sch_client(client, db_session):
    """conftest `client` with an in-memory JobRunner override so run-now doesn't
    touch Redis/RQ."""
    runner = JobRunner(
        lambda: db_session, InMemoryJobBackend(), enqueue=lambda job: None,
        secrets=get_secrets_service(),
    )
    client.app.dependency_overrides[get_job_runner] = lambda: runner
    return client


def _create_payload(site_id, **over):
    base = {
        "name": "Nightly backup",
        "target_type": "site",
        "target_id": site_id,
        "action_name": "site.backup",
        "cron": "0 2 * * *",
        "timezone": "Asia/Dubai",
        "priority": "low",
        "with_files": True,
    }
    base.update(over)
    return base


def test_create_schedule_admin(sch_client, api_env):
    login(sch_client, "admin@example.com")
    r = sch_client.post("/api/schedules", json=_create_payload(api_env["site_id"]),
                        headers=csrf_headers(sch_client))
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["action_name"] == "site.backup"
    assert body["next_run_at"] is not None
    assert body["target_label"] == "test1.localhost"
    assert body["enabled"] is True


def test_create_schedule_denied_for_operator(sch_client, api_env, db_session):
    # Operator lacks schedule:manage.
    from app.core.security import hash_password
    from app.models import Role, User
    from tests.conftest import PASSWORD
    op_role = db_session.scalars(select(Role).where(Role.name == "Operator")).first()
    db_session.add(User(email="op@example.com", password_hash=hash_password(PASSWORD),
                        full_name="Op", is_active=True, role_id=op_role.id))
    db_session.commit()
    login(sch_client, "op@example.com")
    r = sch_client.post("/api/schedules", json=_create_payload(api_env["site_id"]),
                        headers=csrf_headers(sch_client))
    assert r.status_code == 403


def test_create_retention_requires_keep_param(sch_client, api_env):
    login(sch_client, "admin@example.com")
    r = sch_client.post(
        "/api/schedules",
        json=_create_payload(api_env["site_id"], action_name="backup.retention_sweep",
                             with_files=False),
        headers=csrf_headers(sch_client),
    )
    assert r.status_code == 422


def test_create_bad_cron_rejected(sch_client, api_env):
    login(sch_client, "admin@example.com")
    r = sch_client.post(
        "/api/schedules",
        json=_create_payload(api_env["site_id"], cron="nope nope"),
        headers=csrf_headers(sch_client),
    )
    assert r.status_code == 422


def test_readonly_can_list_not_create(sch_client, api_env):
    login(sch_client, "readonly@example.com")
    assert sch_client.get("/api/schedules").status_code == 200
    r = sch_client.post("/api/schedules", json=_create_payload(api_env["site_id"]),
                        headers=csrf_headers(sch_client))
    assert r.status_code == 403


def test_disable_makes_non_due(sch_client, api_env, db_session):
    login(sch_client, "admin@example.com")
    r = sch_client.post("/api/schedules",
                        json=_create_payload(api_env["site_id"], cron=None, interval_seconds=60),
                        headers=csrf_headers(sch_client))
    sid = r.json()["id"]
    r2 = sch_client.post(f"/api/schedules/{sid}/enabled", json={"enabled": False},
                         headers=csrf_headers(sch_client))
    assert r2.status_code == 200 and r2.json()["enabled"] is False
    # Even with next_run_at in the past, a disabled schedule is not due.
    schedule = db_session.get(Schedule, sid)
    schedule.next_run_at = NOW - timedelta(days=1)
    db_session.commit()
    assert sch.due_schedules(db_session, now=NOW) == []


def test_run_now_operator_allowed_for_backup(sch_client, api_env, db_session):
    from app.core.security import hash_password
    from app.models import Role, User
    from tests.conftest import PASSWORD
    op_role = db_session.scalars(select(Role).where(Role.name == "Operator")).first()
    db_session.add(User(email="op2@example.com", password_hash=hash_password(PASSWORD),
                        full_name="Op", is_active=True, role_id=op_role.id))
    db_session.commit()
    # Admin creates the schedule…
    login(sch_client, "admin@example.com")
    payload = _create_payload(api_env["site_id"], cron=None, interval_seconds=3600)
    sid = sch_client.post(
        "/api/schedules", json=payload, headers=csrf_headers(sch_client)
    ).json()["id"]
    # …Operator runs it now.
    login(sch_client, "op2@example.com")
    r = sch_client.post(f"/api/schedules/{sid}/run-now", headers=csrf_headers(sch_client))
    assert r.status_code == 201, r.text
    assert r.json()["action_name"] == "site.backup"
    # Firing did NOT advance the periodic next_run (run-now is out of band).
    schedule = db_session.get(Schedule, sid)
    assert schedule.last_run_job_id is not None


def test_run_now_denied_for_readonly(sch_client, api_env):
    login(sch_client, "admin@example.com")
    payload = _create_payload(api_env["site_id"], cron=None, interval_seconds=3600)
    sid = sch_client.post(
        "/api/schedules", json=payload, headers=csrf_headers(sch_client)
    ).json()["id"]
    login(sch_client, "readonly@example.com")
    r = sch_client.post(f"/api/schedules/{sid}/run-now", headers=csrf_headers(sch_client))
    assert r.status_code == 403
