"""Sites (session 1.8): site discovery parsing/persist, worker-side secret
resolution, the `bench new-site` render (gotcha #4), the site.create dev-bench
Redis dance (gotcha #3) end to end over fakes, the toggles, and the API surface
(list/create/toggle + RBAC + the server MariaDB-root-password field). No
Redis/RQ/SSH touched."""

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.routes.jobs import get_job_runner
from app.core import discovery
from app.core.commands import MASK, RenderError, get_template, render
from app.core.commands.actions import _MODE_PROBE
from app.core.jobs import CaptureResult, InMemoryJobBackend, JobRunner
from app.core.secrets_resolve import (
    SecretResolutionError,
    decrypt_job_secrets,
    encrypt_job_secrets,
    resolve_secrets,
)
from app.core.security import get_secrets_service
from app.db import Base
from app.models import CommandJob, LogEntry, Server
from app.models.bench import Bench
from app.models.site import Site
from tests.conftest import csrf_headers, login

BENCH_PATH = "/home/frappe/frappe-bench"

INSPECT_WITH_SITES = """PROD=0
---COMMON_SITE_CONFIG---
{"webserver_port": 8000, "redis_cache": "redis://localhost:13000",
 "redis_queue": "redis://localhost:11000"}
---BENCH_VERSION---
[{"name": "frappe", "version": "16.25.0"}]
---PYTHON---
Python 3.14.0
---NODE---
v24.1.0
---SITES---
SITE\ttest1.localhost\t
SITE\tprod.example.com\t1
---END---
"""


# --------------------------------------------------------------------------- #
# Pure parsing
# --------------------------------------------------------------------------- #


def test_parse_sites_reads_names_and_maintenance():
    sites = discovery.parse_sites(
        "SITE\ta.localhost\t\nSITE\tb.localhost\t1\nSITE\tc.localhost\ttrue\n"
    )
    by_name = {s.name: s.maintenance_mode for s in sites}
    assert by_name == {
        "a.localhost": False,
        "b.localhost": True,
        "c.localhost": True,
    }


def test_parse_sites_dedupes_and_ignores_noise():
    sites = discovery.parse_sites("noise\nSITE\ta.localhost\t\nSITE\ta.localhost\t1\n")
    assert [s.name for s in sites] == ["a.localhost"]


def test_parse_inspect_includes_sites():
    info = discovery.parse_inspect(INSPECT_WITH_SITES, BENCH_PATH)
    assert {s.name for s in info.sites} == {"test1.localhost", "prod.example.com"}
    maint = {s.name: s.maintenance_mode for s in info.sites}
    assert maint["prod.example.com"] is True
    assert maint["test1.localhost"] is False


# --------------------------------------------------------------------------- #
# Persist: site upsert + vanish + single-site register
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


def _server(db, mariadb_pw="rootpw"):
    s = Server(name="vm", hostname="10.0.0.9")
    if mariadb_pw is not None:
        s.mariadb_root_password_enc = get_secrets_service().encrypt(mariadb_pw)
    db.add(s)
    db.commit()
    return s.id


def test_persist_creates_and_vanishes_sites(sf):
    with sf() as db:
        server_id = _server(db)
        info = discovery.parse_inspect(INSPECT_WITH_SITES, BENCH_PATH)
        info.path = BENCH_PATH
        discovery.persist(db, server_id, [info])

        bench = db.scalars(select(Bench)).one()
        sites = db.scalars(select(Site).where(Site.bench_id == bench.id)).all()
        assert {s.name for s in sites} == {"test1.localhost", "prod.example.com"}
        prod = next(s for s in sites if s.name == "prod.example.com")
        assert prod.maintenance_mode is True

        # Re-discover with prod.example.com gone -> it is marked missing, not dropped.
        info2 = discovery.parse_inspect(INSPECT_WITH_SITES, BENCH_PATH)
        info2.path = BENCH_PATH
        info2.sites = [s for s in info2.sites if s.name == "test1.localhost"]
        discovery.persist(db, server_id, [info2])
        gone = db.scalars(
            select(Site).where(Site.name == "prod.example.com")
        ).one()
        assert gone.status == "missing"


def test_upsert_site_one_registers_without_touching_siblings(sf):
    with sf() as db:
        server_id = _server(db)
        bench = Bench(server_id=server_id, path=BENCH_PATH, name="frappe-bench")
        db.add(bench)
        db.commit()
        db.add(Site(bench_id=bench.id, name="existing.localhost", status="active"))
        db.commit()

        site = discovery.upsert_site_one(db, bench.id, "test1.localhost")
        assert site.status == "active"
        # The sibling is untouched (no vanish pass).
        sib = db.scalars(select(Site).where(Site.name == "existing.localhost")).one()
        assert sib.status == "active"


# --------------------------------------------------------------------------- #
# Secret resolution
# --------------------------------------------------------------------------- #


def test_job_secret_bundle_roundtrips():
    svc = get_secrets_service()
    token = encrypt_job_secrets(svc, {"admin_pw": "S3cret!"})
    assert token is not None and "S3cret!" not in token
    assert decrypt_job_secrets(svc, token) == {"admin_pw": "S3cret!"}
    assert encrypt_job_secrets(svc, {}) is None
    assert decrypt_job_secrets(svc, None) == {}


def test_resolve_secrets_mixes_job_and_server_sources(sf):
    svc = get_secrets_service()
    template = get_template("site.create")
    with sf() as db:
        server_id = _server(db, mariadb_pw="rootpw")
        server = db.get(Server, server_id)
        bundle = encrypt_job_secrets(svc, {"admin_pw": "adminpw"})
        resolved = resolve_secrets(
            template, secrets_enc=bundle, server=server, secrets=svc
        )
        assert resolved == {"db_root_pw": "rootpw", "admin_pw": "adminpw"}


def test_resolve_secrets_errors_when_server_has_no_root_pw(sf):
    svc = get_secrets_service()
    template = get_template("site.create")
    with sf() as db:
        server_id = _server(db, mariadb_pw=None)
        server = db.get(Server, server_id)
        bundle = encrypt_job_secrets(svc, {"admin_pw": "adminpw"})
        with pytest.raises(SecretResolutionError, match="mariadb_root_password"):
            resolve_secrets(template, secrets_enc=bundle, server=server, secrets=svc)


def test_resolve_secrets_errors_when_admin_pw_missing(sf):
    svc = get_secrets_service()
    template = get_template("site.create")
    with sf() as db:
        server_id = _server(db)
        server = db.get(Server, server_id)
        with pytest.raises(SecretResolutionError, match="admin_pw"):
            resolve_secrets(template, secrets_enc=None, server=server, secrets=svc)


# --------------------------------------------------------------------------- #
# `bench new-site` render (gotcha #4)
# --------------------------------------------------------------------------- #


def test_new_site_render_is_non_interactive_and_masks_secrets():
    rc = render(
        get_template("site.new"),
        {
            "site": "test1.localhost",
            "db_root_pw": "rootpw",
            "admin_pw": "adminpw",
            "bench_path": BENCH_PATH,
        },
    )
    # Every flag that prevents an interactive prompt is present (gotcha #4).
    assert rc.argv == [
        "bench", "new-site", "test1.localhost",
        "--mariadb-root-username", "root",
        "--mariadb-root-password", "rootpw",
        "--admin-password", "adminpw",
        "--mariadb-user-host-login-scope=%",
    ]
    assert rc.cwd == BENCH_PATH
    # Display masks both passwords; the persisted params too.
    assert "rootpw" not in rc.display and "adminpw" not in rc.display
    assert rc.params_sanitized["db_root_pw"] == MASK
    assert rc.params_sanitized["admin_pw"] == MASK


@pytest.mark.parametrize("bad", ["Test.Localhost", "a b", "x;rm -rf /", "../evil", "a`id`"])
def test_site_name_rejected(bad):
    with pytest.raises(RenderError):
        render(
            get_template("site.new"),
            {"site": bad, "db_root_pw": "p", "admin_pw": "p", "bench_path": BENCH_PATH},
        )


# --------------------------------------------------------------------------- #
# site.create end to end over fakes (the Redis dance)
# --------------------------------------------------------------------------- #


class SiteExecutor:
    """Fake executor: `capture` answers the dev/prod probe; `run` records every
    streamed argv (redis start, bench new-site, redis shutdown) and returns a
    configurable new-site exit code."""

    def __init__(self, *, prod=False, new_site_exit=0):
        self._prod = prod
        self._new_site_exit = new_site_exit
        self.streamed: list[list[str]] = []
        self.lines: list[str] = []

    async def capture(self, argv, *, cwd=None, timeout=120.0):
        if argv[:2] == ["bash", "-c"] and argv[2] == _MODE_PROBE:
            return CaptureResult(0, "PROD" if self._prod else "DEV", "")
        raise AssertionError(f"unexpected capture {argv}")

    async def run(self, argv, *, cwd, run_as, on_line, cancel_check):
        self.streamed.append(list(argv))
        if argv[:2] == ["bench", "new-site"]:
            # Echo a line containing the secret so redaction can be asserted.
            on_line("stdout", "creating site with admin password adminpw")
            return self._new_site_exit
        return 0


def fake_factory(executor):
    class _CM:
        async def __aenter__(self):
            return executor

        async def __aexit__(self, *exc):
            return False

    return lambda server: _CM()


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


def _run_create(sf, server_id, executor, *, site="test1.localhost"):
    runner = JobRunner(sf, InMemoryJobBackend(), enqueue=lambda job: None)
    with sf() as db:
        job = runner.create(
            db,
            action_name="site.create",
            server_id=server_id,
            target_type="bench",
            target_id=BENCH_PATH,
            params={"site": site, "bench_path": BENCH_PATH},
            user_secrets={"admin_pw": "adminpw"},
            priority="high",
            created_by=None,
        )
        job_id = job.id
        assert job.secrets_enc  # admin password carried encrypted, not in clear
        assert "adminpw" not in (job.secrets_enc or "")
        assert job.params_sanitized["admin_pw"] == MASK
    runner.run_job(job_id, executor_factory=fake_factory(executor))
    return job_id


def test_dev_site_create_runs_redis_dance_and_registers(sf):
    with sf() as db:
        server_id = _server(db)
        _bench(db, server_id, prod=False)
    ex = SiteExecutor(prod=False)
    job_id = _run_create(sf, server_id, ex)

    with sf() as db:
        job = db.get(CommandJob, job_id)
        assert job.status == "success", job.status
        # Redis started (queue + cache) BEFORE new-site, shut down AFTER.
        kinds = [a[0] for a in ex.streamed]
        assert kinds == [
            "redis-server", "redis-server", "bench", "redis-cli", "redis-cli"
        ], ex.streamed
        # Ports for the shutdown come from the discovered bench (11000/13000).
        shutdowns = [a for a in ex.streamed if a[0] == "redis-cli"]
        assert {a[2] for a in shutdowns} == {"11000", "13000"}
        # bench new-site ran non-interactively with the resolved root password.
        new_site = next(a for a in ex.streamed if a[:2] == ["bench", "new-site"])
        assert "--mariadb-root-username" in new_site and "rootpw" in new_site
        # The site was registered.
        site = db.scalars(select(Site).where(Site.name == "test1.localhost")).one()
        assert site.status == "active"
        # Secrets never leak into the persisted logs (rule 6).
        logs = " ".join(
            log.content for log in db.scalars(
                select(LogEntry).where(LogEntry.job_id == job_id)
            ).all()
        )
        assert "adminpw" not in logs and "rootpw" not in logs
        assert MASK in logs


def test_prod_site_create_skips_redis_dance(sf):
    with sf() as db:
        server_id = _server(db)
        _bench(db, server_id, prod=True)
    ex = SiteExecutor(prod=True)
    job_id = _run_create(sf, server_id, ex)

    with sf() as db:
        assert db.get(CommandJob, job_id).status == "success"
    # No redis start/stop on a production bench — only the one new-site command.
    assert [a[0] for a in ex.streamed] == ["bench"], ex.streamed


def test_site_create_shuts_redis_down_even_when_new_site_fails(sf):
    with sf() as db:
        server_id = _server(db)
        _bench(db, server_id, prod=False)
    ex = SiteExecutor(prod=False, new_site_exit=1)
    job_id = _run_create(sf, server_id, ex)

    with sf() as db:
        job = db.get(CommandJob, job_id)
        assert job.status == "failure"
        # Non-idempotent: a determinate new-site failure is not auto-retried.
        assert job.retry_count == 0
        # Nothing registered, but the redis we started was still shut back down.
        assert db.scalars(select(Site)).first() is None
    assert [a[0] for a in ex.streamed][-2:] == ["redis-cli", "redis-cli"]


# --------------------------------------------------------------------------- #
# Toggles update the site row
# --------------------------------------------------------------------------- #


class ToggleExecutor:
    def __init__(self, exit_code=0):
        self._exit = exit_code
        self.streamed = []

    async def run(self, argv, *, cwd, run_as, on_line, cancel_check):
        self.streamed.append(list(argv))
        return self._exit


def _run_toggle(sf, server_id, bench_id, action, params):
    runner = JobRunner(sf, InMemoryJobBackend(), enqueue=lambda job: None)
    with sf() as db:
        job = runner.create(
            db,
            action_name=action,
            server_id=server_id,
            target_type="site",
            target_id=f"{BENCH_PATH}::{params['site']}",
            params=params,
            priority="high",
            created_by=None,
        )
        job_id = job.id
    runner.run_job(job_id, executor_factory=fake_factory(ToggleExecutor()))
    return job_id


def test_scheduler_toggle_updates_row(sf):
    with sf() as db:
        server_id = _server(db)
        bench_id = _bench(db, server_id)
        db.add(Site(bench_id=bench_id, name="test1.localhost", scheduler_enabled=None))
        db.commit()
    _run_toggle(
        sf, server_id, bench_id, "site.set_scheduler",
        {"site": "test1.localhost", "bench_path": BENCH_PATH, "state": "enable"},
    )
    with sf() as db:
        site = db.scalars(select(Site).where(Site.name == "test1.localhost")).one()
        assert site.scheduler_enabled is True


def test_maintenance_toggle_updates_row(sf):
    with sf() as db:
        server_id = _server(db)
        bench_id = _bench(db, server_id)
        db.add(Site(bench_id=bench_id, name="test1.localhost", maintenance_mode=False))
        db.commit()
    _run_toggle(
        sf, server_id, bench_id, "site.set_maintenance",
        {"site": "test1.localhost", "bench_path": BENCH_PATH, "state": "on"},
    )
    with sf() as db:
        site = db.scalars(select(Site).where(Site.name == "test1.localhost")).one()
        assert site.maintenance_mode is True


# --------------------------------------------------------------------------- #
# API surface
# --------------------------------------------------------------------------- #


@pytest.fixture
def sites_client(client, db_session):
    runner = JobRunner(lambda: db_session, InMemoryJobBackend(), enqueue=lambda job: None)
    client.app.dependency_overrides[get_job_runner] = lambda: runner
    yield client
    client.app.dependency_overrides.pop(get_job_runner, None)


@pytest.fixture
def api_bench(db_session):
    s = Server(name="vm-a", hostname="10.0.0.1")
    s.mariadb_root_password_enc = get_secrets_service().encrypt("rootpw")
    db_session.add(s)
    db_session.commit()
    bench = Bench(server_id=s.id, path=BENCH_PATH, name="frappe-bench", webserver_port=8000)
    db_session.add(bench)
    db_session.commit()
    db_session.add(Site(bench_id=bench.id, name="test1.localhost"))
    db_session.commit()
    return {"server_id": s.id, "bench_id": bench.id}


def test_list_sites_enriched(sites_client, api_bench):
    login(sites_client, "readonly@example.com")
    resp = sites_client.get("/api/sites")
    assert resp.status_code == 200, resp.text
    site = resp.json()[0]
    assert site["name"] == "test1.localhost"
    assert site["bench_name"] == "frappe-bench"
    assert site["url"] == "http://10.0.0.1:8000"
    assert site["server_env_tag"] == "dev"


def test_create_site_launches_pending_job(sites_client, api_bench):
    login(sites_client, "developer@example.com")
    resp = sites_client.post(
        "/api/sites",
        json={"bench_id": api_bench["bench_id"], "name": "new.localhost",
              "admin_password": "S3cret!"},
        headers=csrf_headers(sites_client),
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["status"] == "pending"
    assert data["action_name"] == "site.create"
    assert data["target_id"] == BENCH_PATH
    # The admin password never round-trips in the sanitized params.
    assert data["params_sanitized"]["admin_pw"] == MASK


def test_create_site_422_when_server_has_no_root_pw(sites_client, db_session):
    s = Server(name="vm-b", hostname="10.0.0.2")  # no MariaDB root password set
    db_session.add(s)
    db_session.commit()
    bench = Bench(server_id=s.id, path="/home/frappe/b2", name="b2")
    db_session.add(bench)
    db_session.commit()
    login(sites_client, "developer@example.com")
    resp = sites_client.post(
        "/api/sites",
        json={"bench_id": bench.id, "name": "x.localhost", "admin_password": "p"},
        headers=csrf_headers(sites_client),
    )
    assert resp.status_code == 422
    assert "mariadb_root_password" in resp.text


def test_create_site_rejects_bad_name_422(sites_client, api_bench):
    login(sites_client, "developer@example.com")
    resp = sites_client.post(
        "/api/sites",
        json={"bench_id": api_bench["bench_id"], "name": "Bad Name",
              "admin_password": "p"},
        headers=csrf_headers(sites_client),
    )
    assert resp.status_code == 422


def test_readonly_cannot_create_site(sites_client, api_bench):
    login(sites_client, "readonly@example.com")
    resp = sites_client.post(
        "/api/sites",
        json={"bench_id": api_bench["bench_id"], "name": "x.localhost",
              "admin_password": "p"},
        headers=csrf_headers(sites_client),
    )
    assert resp.status_code == 403


def test_toggle_endpoints_launch_jobs(sites_client, api_bench, db_session):
    site_id = db_session.scalars(select(Site.id)).first()
    login(sites_client, "developer@example.com")
    sched = sites_client.post(
        f"/api/sites/{site_id}/scheduler",
        json={"enabled": True},
        headers=csrf_headers(sites_client),
    )
    assert sched.status_code == 201, sched.text
    assert sched.json()["action_name"] == "site.set_scheduler"

    maint = sites_client.post(
        f"/api/sites/{site_id}/maintenance",
        json={"enabled": False},
        headers=csrf_headers(sites_client),
    )
    assert maint.status_code == 201
    assert maint.json()["action_name"] == "site.set_maintenance"


def test_second_create_conflicts_409(sites_client, api_bench):
    login(sites_client, "developer@example.com")
    body = {"bench_id": api_bench["bench_id"], "name": "dup.localhost", "admin_password": "p"}
    first = sites_client.post("/api/sites", json=body, headers=csrf_headers(sites_client))
    assert first.status_code == 201
    second = sites_client.post("/api/sites", json=body, headers=csrf_headers(sites_client))
    assert second.status_code == 409


# --------------------------------------------------------------------------- #
# Server MariaDB-root-password field
# --------------------------------------------------------------------------- #


def test_server_mariadb_root_password_is_write_only(client):
    login(client, "admin@example.com")
    resp = client.post(
        "/api/servers",
        json={
            "name": "vm-maria", "hostname": "10.0.0.5",
            "mariadb_root_password": "rootpw",
            "credential": {"username": "frappe", "auth_type": "key", "generate": True},
        },
        headers=csrf_headers(client),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["has_mariadb_root_password"] is True
    # The plaintext is never echoed anywhere in the response.
    assert "rootpw" not in resp.text
