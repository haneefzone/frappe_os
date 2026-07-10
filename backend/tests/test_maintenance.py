"""Maintenance actions (session 1.10): the site maintenance ops
(migrate / clear-cache / clear-website-cache) with the dev-bench Redis dance,
bench build, the mode-aware bench.restart (prod supervisorctl vs dev informative
failure), bulk migrate-all iterating sites as steps, and bench.update with the
db-only safety backup step FIRST. Plus the API surface (RBAC + 409). No
Redis/RQ/SSH touched — everything runs over in-memory fakes."""

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.routes.jobs import get_job_runner
from app.core.commands import RenderError, get_template, render
from app.core.commands.actions import _MODE_PROBE
from app.core.jobs import CaptureResult, InMemoryJobBackend, JobRunner
from app.db import Base
from app.models import CommandJob, CommandStep, Server
from app.models.bench import Bench
from app.models.site import Site
from tests.conftest import csrf_headers, login

BENCH_PATH = "/home/frappe/frappe-bench"


# --------------------------------------------------------------------------- #
# Render / registry
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "action,verb",
    [
        ("site.migrate", "migrate"),
        ("site.clear_cache", "clear-cache"),
        ("site.clear_website_cache", "clear-website-cache"),
    ],
)
def test_site_maintenance_templates_render_bench_site_verb(action, verb):
    rc = render(get_template(action), {"site": "test1.localhost", "bench_path": BENCH_PATH})
    assert rc.argv == ["bench", "--site", "test1.localhost", verb]
    assert rc.cwd == BENCH_PATH


def test_supervisor_restart_render_and_group_shape():
    rc = render(get_template("bench.supervisor_restart"), {"group": "frappe-bench:*"})
    assert rc.argv == ["sudo", "-n", "supervisorctl", "restart", "frappe-bench:*"]


@pytest.mark.parametrize("bad", ["frappe-bench", "frappe bench:*", "x;rm:*", "../a:*"])
def test_supervisor_group_rejects_bad_shapes(bad):
    with pytest.raises(RenderError):
        render(get_template("bench.supervisor_restart"), {"group": bad})


def test_site_migrate_rejects_shell_metacharacters():
    with pytest.raises(RenderError):
        render(get_template("site.migrate"), {"site": "a;rm -rf /", "bench_path": BENCH_PATH})


# --------------------------------------------------------------------------- #
# Fakes + harness (mirrors test_sites.py)
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


class MaintExecutor:
    """Fake executor: `capture` answers the dev/prod probe; `run` records every
    streamed argv and returns a configurable exit code for a chosen command."""

    def __init__(self, *, prod=False, fail_on=None, fail_code=1):
        self._prod = prod
        self._fail_on = fail_on  # a predicate over argv, or None
        self._fail_code = fail_code
        self.streamed: list[list[str]] = []

    async def capture(self, argv, *, cwd=None, timeout=120.0):
        if argv[:2] == ["bash", "-c"] and argv[2] == _MODE_PROBE:
            return CaptureResult(0, "PROD" if self._prod else "DEV", "")
        raise AssertionError(f"unexpected capture {argv}")

    async def run(self, argv, *, cwd, run_as, on_line, cancel_check):
        self.streamed.append(list(argv))
        if self._fail_on is not None and self._fail_on(argv):
            return self._fail_code
        return 0


def fake_factory(executor):
    class _CM:
        async def __aenter__(self):
            return executor

        async def __aexit__(self, *exc):
            return False

    return lambda server: _CM()


def _server(db):
    s = Server(name="vm", hostname="10.0.0.9")
    db.add(s)
    db.commit()
    return s.id


def _bench(db, server_id, *, prod=False):
    bench = Bench(
        server_id=server_id,
        path=BENCH_PATH,
        name="frappe-bench",
        is_production=prod,
        redis_queue_port=11000,
        redis_cache_port=13000,
    )
    db.add(bench)
    db.commit()
    return bench.id


def _add_site(db, bench_id, name):
    db.add(Site(bench_id=bench_id, name=name, status="active"))
    db.commit()


def _run(sf, *, action, server_id, target_type, target_id, params, executor):
    runner = JobRunner(sf, InMemoryJobBackend(), enqueue=lambda job: None)
    with sf() as db:
        job = runner.create(
            db,
            action_name=action,
            server_id=server_id,
            target_type=target_type,
            target_id=target_id,
            params=params,
            priority="default",
            created_by=None,
        )
        job_id = job.id
    runner.run_job(job_id, executor_factory=fake_factory(executor))
    return job_id


# --------------------------------------------------------------------------- #
# Site maintenance: dev Redis dance + prod skips it
# --------------------------------------------------------------------------- #


def test_dev_migrate_runs_redis_dance(sf):
    with sf() as db:
        server_id = _server(db)
        _bench(db, server_id, prod=False)
    ex = MaintExecutor(prod=False)
    job_id = _run(
        sf,
        action="site.migrate",
        server_id=server_id,
        target_type="site",
        target_id=f"{BENCH_PATH}::test1.localhost",
        params={"site": "test1.localhost", "bench_path": BENCH_PATH},
        executor=ex,
    )
    with sf() as db:
        assert db.get(CommandJob, job_id).status == "success"
    # redis start (x2) BEFORE the migrate, redis shutdown (x2) AFTER.
    kinds = [a[0] for a in ex.streamed]
    assert kinds == ["redis-server", "redis-server", "bench", "redis-cli", "redis-cli"]
    migrate = next(a for a in ex.streamed if a[0] == "bench")
    assert migrate == ["bench", "--site", "test1.localhost", "migrate"]


def test_prod_clear_cache_skips_redis_dance(sf):
    with sf() as db:
        server_id = _server(db)
        _bench(db, server_id, prod=True)
    ex = MaintExecutor(prod=True)
    _run(
        sf,
        action="site.clear_cache",
        server_id=server_id,
        target_type="site",
        target_id=f"{BENCH_PATH}::test1.localhost",
        params={"site": "test1.localhost", "bench_path": BENCH_PATH},
        executor=ex,
    )
    # Only the one bench command — no redis start/stop on a production bench.
    assert [a[0] for a in ex.streamed] == ["bench"]
    assert ex.streamed[0] == ["bench", "--site", "test1.localhost", "clear-cache"]


def test_migrate_shuts_redis_down_even_on_failure(sf):
    with sf() as db:
        server_id = _server(db)
        _bench(db, server_id, prod=False)
    ex = MaintExecutor(prod=False, fail_on=lambda a: a[:2] == ["bench", "--site"])
    job_id = _run(
        sf,
        action="site.migrate",
        server_id=server_id,
        target_type="site",
        target_id=f"{BENCH_PATH}::test1.localhost",
        params={"site": "test1.localhost", "bench_path": BENCH_PATH},
        executor=ex,
    )
    with sf() as db:
        job = db.get(CommandJob, job_id)
        assert job.status == "failure"
        assert job.retry_count == 0  # non-idempotent: not auto-retried
    assert [a[0] for a in ex.streamed][-2:] == ["redis-cli", "redis-cli"]


# --------------------------------------------------------------------------- #
# bench.build
# --------------------------------------------------------------------------- #


def test_bench_build_runs_single_command(sf):
    with sf() as db:
        server_id = _server(db)
        _bench(db, server_id)
    ex = MaintExecutor()
    job_id = _run(
        sf,
        action="bench.build",
        server_id=server_id,
        target_type="bench",
        target_id=BENCH_PATH,
        params={"bench_path": BENCH_PATH},
        executor=ex,
    )
    with sf() as db:
        assert db.get(CommandJob, job_id).status == "success"
    assert ex.streamed == [["bench", "build"]]


# --------------------------------------------------------------------------- #
# bench.restart — prod supervisorctl vs dev informative failure
# --------------------------------------------------------------------------- #


def test_prod_restart_uses_supervisorctl_group(sf):
    with sf() as db:
        server_id = _server(db)
        _bench(db, server_id, prod=True)
    ex = MaintExecutor(prod=True)
    job_id = _run(
        sf,
        action="bench.restart",
        server_id=server_id,
        target_type="bench",
        target_id=BENCH_PATH,
        params={"bench_path": BENCH_PATH},
        executor=ex,
    )
    with sf() as db:
        assert db.get(CommandJob, job_id).status == "success"
    assert ex.streamed == [["sudo", "-n", "supervisorctl", "restart", "frappe-bench:*"]]


def test_dev_restart_fails_informatively(sf):
    with sf() as db:
        server_id = _server(db)
        _bench(db, server_id, prod=False)
    ex = MaintExecutor(prod=False)
    job_id = _run(
        sf,
        action="bench.restart",
        server_id=server_id,
        target_type="bench",
        target_id=BENCH_PATH,
        params={"bench_path": BENCH_PATH},
        executor=ex,
    )
    with sf() as db:
        job = db.get(CommandJob, job_id)
        assert job.status == "failure"
        assert job.retry_count == 0  # informative failure isn't retried
    # It never tried to shell out to supervisorctl on a dev bench.
    assert ex.streamed == []


# --------------------------------------------------------------------------- #
# bench.migrate_all — sites as steps, per-site failure isolation
# --------------------------------------------------------------------------- #


def test_migrate_all_iterates_sites_as_steps(sf):
    with sf() as db:
        server_id = _server(db)
        bench_id = _bench(db, server_id, prod=False)
        _add_site(db, bench_id, "a.localhost")
        _add_site(db, bench_id, "b.localhost")
    ex = MaintExecutor(prod=False)
    job_id = _run(
        sf,
        action="bench.migrate_all",
        server_id=server_id,
        target_type="bench",
        target_id=BENCH_PATH,
        params={"bench_path": BENCH_PATH},
        executor=ex,
    )
    with sf() as db:
        assert db.get(CommandJob, job_id).status == "success"
        steps = db.scalars(
            select(CommandStep).where(CommandStep.job_id == job_id).order_by(CommandStep.order)
        ).all()
        names = [s.name for s in steps]
        assert "Migrate a.localhost" in names and "Migrate b.localhost" in names
    # Redis dance runs ONCE around both migrations.
    kinds = [a[0] for a in ex.streamed]
    assert kinds.count("redis-server") == 2 and kinds.count("redis-cli") == 2
    migrates = [a for a in ex.streamed if a[:2] == ["bench", "--site"]]
    assert {a[2] for a in migrates} == {"a.localhost", "b.localhost"}


def test_migrate_all_one_site_fails_others_run_job_fails(sf):
    with sf() as db:
        server_id = _server(db)
        bench_id = _bench(db, server_id, prod=False)
        _add_site(db, bench_id, "a.localhost")
        _add_site(db, bench_id, "b.localhost")
    # Fail only b.localhost's migrate.
    ex = MaintExecutor(
        prod=False, fail_on=lambda a: a[:3] == ["bench", "--site", "b.localhost"]
    )
    job_id = _run(
        sf,
        action="bench.migrate_all",
        server_id=server_id,
        target_type="bench",
        target_id=BENCH_PATH,
        params={"bench_path": BENCH_PATH},
        executor=ex,
    )
    with sf() as db:
        assert db.get(CommandJob, job_id).status == "failure"
    # Both sites were still attempted (one bad site doesn't sink the batch).
    migrates = [a for a in ex.streamed if a[:2] == ["bench", "--site"]]
    assert {a[2] for a in migrates} == {"a.localhost", "b.localhost"}
    # Redis still shut down afterwards.
    assert [a[0] for a in ex.streamed][-2:] == ["redis-cli", "redis-cli"]


# --------------------------------------------------------------------------- #
# bench.update — safety backup FIRST, then update
# --------------------------------------------------------------------------- #


def test_update_backs_up_before_updating(sf):
    with sf() as db:
        server_id = _server(db)
        bench_id = _bench(db, server_id, prod=False)
        _add_site(db, bench_id, "test1.localhost")
    ex = MaintExecutor(prod=False)
    job_id = _run(
        sf,
        action="bench.update",
        server_id=server_id,
        target_type="bench",
        target_id=BENCH_PATH,
        params={"bench_path": BENCH_PATH},
        executor=ex,
    )
    with sf() as db:
        assert db.get(CommandJob, job_id).status == "success"
        steps = db.scalars(
            select(CommandStep).where(CommandStep.job_id == job_id).order_by(CommandStep.order)
        ).all()
        labels = [s.name for s in steps]
    # The safety backup step comes strictly before the update step.
    backup_i = next(i for i, n in enumerate(labels) if n.startswith("Safety backup"))
    update_i = next(i for i, n in enumerate(labels) if n.startswith("Update bench"))
    assert backup_i < update_i
    # The db-only backup command ran, then bench update.
    backup = next(a for a in ex.streamed if a[:2] == ["bench", "--site"])
    assert backup == ["bench", "--site", "test1.localhost", "backup"]
    assert ["bench", "update"] in ex.streamed
    assert ex.streamed.index(backup) < ex.streamed.index(["bench", "update"])


def test_update_aborts_when_safety_backup_fails(sf):
    with sf() as db:
        server_id = _server(db)
        bench_id = _bench(db, server_id, prod=False)
        _add_site(db, bench_id, "test1.localhost")
    ex = MaintExecutor(prod=False, fail_on=lambda a: a[-1] == "backup")
    job_id = _run(
        sf,
        action="bench.update",
        server_id=server_id,
        target_type="bench",
        target_id=BENCH_PATH,
        params={"bench_path": BENCH_PATH},
        executor=ex,
    )
    with sf() as db:
        assert db.get(CommandJob, job_id).status == "failure"
    # bench update never ran because the safety backup failed.
    assert ["bench", "update"] not in ex.streamed
    # Redis still shut down.
    assert [a[0] for a in ex.streamed][-2:] == ["redis-cli", "redis-cli"]


# --------------------------------------------------------------------------- #
# API surface + RBAC
# --------------------------------------------------------------------------- #


@pytest.fixture
def maint_client(client, db_session):
    runner = JobRunner(lambda: db_session, InMemoryJobBackend(), enqueue=lambda job: None)
    client.app.dependency_overrides[get_job_runner] = lambda: runner
    yield client
    client.app.dependency_overrides.pop(get_job_runner, None)


@pytest.fixture
def api_fixture(db_session):
    s = Server(name="vm-a", hostname="10.0.0.1")
    db_session.add(s)
    db_session.commit()
    bench = Bench(server_id=s.id, path=BENCH_PATH, name="frappe-bench", webserver_port=8000)
    db_session.add(bench)
    db_session.commit()
    site = Site(bench_id=bench.id, name="test1.localhost")
    db_session.add(site)
    db_session.commit()
    return {"server_id": s.id, "bench_id": bench.id, "site_id": site.id}


@pytest.mark.parametrize(
    "path,action",
    [
        ("migrate", "site.migrate"),
        ("clear-cache", "site.clear_cache"),
        ("clear-website-cache", "site.clear_website_cache"),
    ],
)
def test_site_maintenance_endpoints_launch_jobs(maint_client, api_fixture, path, action):
    login(maint_client, "developer@example.com")
    resp = maint_client.post(
        f"/api/sites/{api_fixture['site_id']}/{path}",
        json={},
        headers=csrf_headers(maint_client),
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["action_name"] == action


@pytest.mark.parametrize(
    "path,action",
    [
        ("build", "bench.build"),
        ("restart", "bench.restart"),
        ("migrate-all", "bench.migrate_all"),
        ("update", "bench.update"),
    ],
)
def test_bench_maintenance_endpoints_launch_jobs(maint_client, api_fixture, path, action):
    login(maint_client, "developer@example.com")
    resp = maint_client.post(
        f"/api/benches/{api_fixture['bench_id']}/{path}",
        json={},
        headers=csrf_headers(maint_client),
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["action_name"] == action


def test_readonly_cannot_run_maintenance(maint_client, api_fixture):
    login(maint_client, "readonly@example.com")
    site_resp = maint_client.post(
        f"/api/sites/{api_fixture['site_id']}/migrate",
        json={},
        headers=csrf_headers(maint_client),
    )
    assert site_resp.status_code == 403
    bench_resp = maint_client.post(
        f"/api/benches/{api_fixture['bench_id']}/build",
        json={},
        headers=csrf_headers(maint_client),
    )
    assert bench_resp.status_code == 403


def test_second_bench_update_conflicts_409(maint_client, api_fixture):
    login(maint_client, "developer@example.com")
    first = maint_client.post(
        f"/api/benches/{api_fixture['bench_id']}/update",
        json={},
        headers=csrf_headers(maint_client),
    )
    assert first.status_code == 201
    second = maint_client.post(
        f"/api/benches/{api_fixture['bench_id']}/update",
        json={},
        headers=csrf_headers(maint_client),
    )
    assert second.status_code == 409


def test_maintenance_on_missing_site_404(maint_client, api_fixture):
    login(maint_client, "developer@example.com")
    resp = maint_client.post(
        "/api/sites/999999/migrate", json={}, headers=csrf_headers(maint_client)
    )
    assert resp.status_code == 404
