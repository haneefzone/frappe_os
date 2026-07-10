"""Production setup (session 2.5): converting a dev bench to production with a
config pre-backup, a TIME-BOXED sudoers elevation that is always revoked, an
is_production toggle, and a before/after config diff. Plus the pure manifest/diff
helpers and the API surface (RBAC + 409). No SSH/root touched — everything runs
over in-memory fakes."""

import json

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.routes.jobs import get_job_runner
from app.core.commands import RenderError, get_template, render
from app.core.commands.actions import _MODE_PROBE, _parse_bench_bin
from app.core.jobs import CaptureResult, InMemoryJobBackend, JobRunner
from app.core.production import diff_manifests, parse_manifest
from app.db import Base
from app.models import CommandJob, CommandStep, Server
from app.models.bench import Bench
from tests.conftest import csrf_headers, login

BENCH_PATH = "/home/frappe/frappe-bench"
BENCH_BIN = "/usr/local/bin/bench"


# --------------------------------------------------------------------------- #
# Pure manifest / diff helpers
# --------------------------------------------------------------------------- #


def _man(*pairs: tuple[str, str]) -> str:
    return "".join(f"{sha}  {path}\n" for path, sha in pairs)


def test_parse_manifest_keeps_valid_hashes_only():
    text = (
        f"{'a' * 64}  /etc/nginx/nginx.conf\n"
        "\n"  # blank
        "PRODUCTION setup running\n"  # chatter, no hash
        f"{'z' * 64}  /etc/nginx/bad.conf\n"  # not hex -> dropped
        f"{'b' * 64}  /etc/supervisor/supervisord.conf\n"
    )
    m = parse_manifest(text)
    assert m == {
        "/etc/nginx/nginx.conf": "a" * 64,
        "/etc/supervisor/supervisord.conf": "b" * 64,
    }


def test_diff_manifests_classifies_added_removed_changed_unchanged():
    before = parse_manifest(
        _man(
            ("/etc/nginx/nginx.conf", "1" * 64),
            ("/etc/nginx/gone.conf", "2" * 64),
            ("/etc/supervisor/supervisord.conf", "3" * 64),
        )
    )
    after = parse_manifest(
        _man(
            ("/etc/nginx/nginx.conf", "9" * 64),  # changed
            ("/etc/supervisor/supervisord.conf", "3" * 64),  # unchanged
            ("/etc/nginx/sites-enabled/frappe.conf", "4" * 64),  # added
        )
    )
    d = diff_manifests(before, after)
    assert d.added == ["/etc/nginx/sites-enabled/frappe.conf"]
    assert d.removed == ["/etc/nginx/gone.conf"]
    assert d.changed == ["/etc/nginx/nginx.conf"]
    assert d.unchanged == 1
    assert d.total_changes == 3


def test_parse_bench_bin_reads_absolute_path_only():
    assert _parse_bench_bin("noise\nBENCH_BIN=/usr/local/bin/bench\n") == "/usr/local/bin/bench"
    assert _parse_bench_bin("BENCH_BIN=relative/bench\n") is None
    assert _parse_bench_bin("no line here\n") is None


# --------------------------------------------------------------------------- #
# Template render / validation
# --------------------------------------------------------------------------- #


def test_setup_production_run_renders_sudo_bench():
    rc = render(
        get_template("bench.setup_production_run"),
        {"bench_bin": BENCH_BIN, "production_user": "frappe", "bench_path": BENCH_PATH},
    )
    assert rc.argv == ["sudo", "-n", BENCH_BIN, "setup", "production", "frappe"]
    assert rc.cwd == BENCH_PATH


def test_nginx_test_renders_absolute_path():
    rc = render(get_template("bench.nginx_test"), {})
    assert rc.argv == ["sudo", "-n", "/usr/sbin/nginx", "-t"]


@pytest.mark.parametrize("bad", ["frappe;rm -rf /", "root user", "-frappe", "a" * 40, ""])
def test_setup_production_rejects_bad_username(bad):
    with pytest.raises(RenderError):
        render(
            get_template("bench.setup_production"),
            {"bench_path": BENCH_PATH, "production_user": bad},
        )


def test_setup_production_run_rejects_dotdot_bench_bin():
    with pytest.raises(RenderError):
        render(
            get_template("bench.setup_production_run"),
            {
                "bench_bin": "/usr/../bin/bench",
                "production_user": "frappe",
                "bench_path": BENCH_PATH,
            },
        )


# --------------------------------------------------------------------------- #
# Fakes + harness (mirrors test_maintenance.py)
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


class ProdExecutor:
    """Fake executor for the production-setup flow.

    `capture` answers the dev/prod probe and the fdm-elevate subcommands
    (backup/grant/manifest/revoke). `run` records every streamed argv and can be
    told to fail a chosen command. Every fdm-elevate invocation is recorded so a
    test can assert the grant→run→revoke ordering and that revoke always runs.
    """

    def __init__(self, *, prod=False, fail_on=None, fail_code=1, grant_fails=False):
        self._prod = prod
        self._fail_on = fail_on
        self._fail_code = fail_code
        self._grant_fails = grant_fails
        self.streamed: list[list[str]] = []
        self.captured: list[list[str]] = []
        # before-manifest returned by `backup`, after-manifest returned by `manifest`.
        self.before = _man(
            ("/etc/nginx/nginx.conf", "1" * 64),
            ("/etc/supervisor/supervisord.conf", "2" * 64),
        )
        self.after = _man(
            ("/etc/nginx/nginx.conf", "9" * 64),  # changed
            ("/etc/supervisor/supervisord.conf", "2" * 64),  # unchanged
            ("/etc/nginx/sites-enabled/frappe-bench.conf", "3" * 64),  # added
        )

    async def capture(self, argv, *, cwd=None, timeout=120.0):
        if argv[:2] == ["bash", "-c"] and argv[2] == _MODE_PROBE:
            return CaptureResult(0, "PROD" if self._prod else "DEV", "")
        if argv[:3] == ["sudo", "-n", "/usr/local/sbin/fdm-elevate"]:
            self.captured.append(list(argv))
            sub = argv[3]
            if sub == "backup":
                return CaptureResult(0, self.before, "")
            if sub == "grant":
                if self._grant_fails:
                    return CaptureResult(1, "", "denied")
                return CaptureResult(0, f"BENCH_BIN={BENCH_BIN}\n", "")
            if sub == "manifest":
                return CaptureResult(0, self.after, "")
            if sub == "revoke":
                return CaptureResult(0, "", "")
        raise AssertionError(f"unexpected capture {argv}")

    async def run(self, argv, *, cwd, run_as, on_line, cancel_check):
        self.streamed.append(list(argv))
        if self._fail_on is not None and self._fail_on(argv):
            return self._fail_code
        return 0

    def elevate_subs(self) -> list[str]:
        return [a[3] for a in self.captured]


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
    )
    db.add(bench)
    db.commit()
    return bench.id


def _run(sf, *, server_id, params, executor):
    runner = JobRunner(sf, InMemoryJobBackend(), enqueue=lambda job: None)
    with sf() as db:
        job = runner.create(
            db,
            action_name="bench.setup_production",
            server_id=server_id,
            target_type="bench",
            target_id=BENCH_PATH,
            params=params,
            priority="high",
            created_by=None,
        )
        job_id = job.id
    runner.run_job(job_id, executor_factory=fake_factory(executor))
    return job_id


def _steps(sf, job_id):
    with sf() as db:
        return [
            s.name
            for s in db.scalars(
                select(CommandStep)
                .where(CommandStep.job_id == job_id)
                .order_by(CommandStep.order)
            ).all()
        ]


# --------------------------------------------------------------------------- #
# Happy path: capture → grant → run → toggle → diff → nginx -t → revoke
# --------------------------------------------------------------------------- #


def test_setup_production_full_flow(sf):
    with sf() as db:
        server_id = _server(db)
        bench_id = _bench(db, server_id, prod=False)
    ex = ProdExecutor(prod=False)
    job_id = _run(
        sf,
        server_id=server_id,
        params={"bench_path": BENCH_PATH, "production_user": "frappe"},
        executor=ex,
    )
    with sf() as db:
        job = db.get(CommandJob, job_id)
        assert job.status == "success"
        # The bench is now production.
        assert db.get(Bench, bench_id).is_production is True

    # Elevation lifecycle: backup + grant BEFORE the run, revoke AFTER, exactly once.
    subs = ex.elevate_subs()
    assert subs.count("grant") == 1 and subs.count("revoke") == 1
    assert subs.index("backup") < subs.index("grant")
    assert subs.index("grant") < subs.index("revoke")
    # setup production ran with the exact reported bench binary, then nginx -t.
    assert ["sudo", "-n", BENCH_BIN, "setup", "production", "frappe"] in ex.streamed
    assert ["sudo", "-n", "/usr/sbin/nginx", "-t"] in ex.streamed
    setup_i = ex.streamed.index(["sudo", "-n", BENCH_BIN, "setup", "production", "frappe"])
    nginx_i = ex.streamed.index(["sudo", "-n", "/usr/sbin/nginx", "-t"])
    assert setup_i < nginx_i

    steps = _steps(sf, job_id)
    assert "Capture nginx/supervisor config (pre-backup)" in steps
    assert "Diff nginx/supervisor config (before vs after)" in steps
    assert "Revoke temporary elevation" in steps


def test_setup_production_emits_config_diff(sf):
    """The before→after diff is emitted as a machine-readable result line."""
    from app.models import LogEntry

    with sf() as db:
        server_id = _server(db)
        _bench(db, server_id, prod=False)
    ex = ProdExecutor(prod=False)
    job_id = _run(
        sf,
        server_id=server_id,
        params={"bench_path": BENCH_PATH, "production_user": "frappe"},
        executor=ex,
    )
    with sf() as db:
        lines = [
            e.content
            for e in db.scalars(
                select(LogEntry).where(LogEntry.job_id == job_id)
            ).all()
        ]
    result = next(c for c in lines if c.startswith("POSTCONFIG_RESULT "))
    payload = json.loads(result[len("POSTCONFIG_RESULT ") :])
    assert payload["added"] == ["/etc/nginx/sites-enabled/frappe-bench.conf"]
    assert payload["changed"] == ["/etc/nginx/nginx.conf"]
    assert payload["removed"] == []
    assert payload["total_changes"] == 2


# --------------------------------------------------------------------------- #
# Refuse to convert a bench already in production
# --------------------------------------------------------------------------- #


def test_setup_production_refuses_when_already_prod(sf):
    with sf() as db:
        server_id = _server(db)
        _bench(db, server_id, prod=True)
    ex = ProdExecutor(prod=True)
    job_id = _run(
        sf,
        server_id=server_id,
        params={"bench_path": BENCH_PATH, "production_user": "frappe"},
        executor=ex,
    )
    with sf() as db:
        assert db.get(CommandJob, job_id).status == "failure"
    # Never touched the elevation helper or ran the conversion.
    assert ex.captured == []
    assert ex.streamed == []


# --------------------------------------------------------------------------- #
# Elevation is ALWAYS revoked, even when the conversion fails
# --------------------------------------------------------------------------- #


def test_setup_production_revokes_elevation_on_run_failure(sf):
    with sf() as db:
        server_id = _server(db)
        bench_id = _bench(db, server_id, prod=False)
    # Fail the actual `setup production` command.
    ex = ProdExecutor(prod=False, fail_on=lambda a: a[2:4] == [BENCH_BIN, "setup"])
    job_id = _run(
        sf,
        server_id=server_id,
        params={"bench_path": BENCH_PATH, "production_user": "frappe"},
        executor=ex,
    )
    with sf() as db:
        job = db.get(CommandJob, job_id)
        assert job.status == "failure"
        assert job.retry_count == 0  # non-idempotent: not auto-retried
        # Conversion failed → bench stays dev.
        assert db.get(Bench, bench_id).is_production is False
    # The temporary elevation was still revoked in the finally.
    assert ex.elevate_subs()[-1] == "revoke"


def test_setup_production_revokes_elevation_on_nginx_failure(sf):
    with sf() as db:
        server_id = _server(db)
        bench_id = _bench(db, server_id, prod=False)
    # setup production succeeds but nginx -t fails.
    ex = ProdExecutor(prod=False, fail_on=lambda a: a[-1] == "-t")
    job_id = _run(
        sf,
        server_id=server_id,
        params={"bench_path": BENCH_PATH, "production_user": "frappe"},
        executor=ex,
    )
    with sf() as db:
        assert db.get(CommandJob, job_id).status == "failure"
        # The bench WAS converted (setup production succeeded before nginx -t).
        assert db.get(Bench, bench_id).is_production is True
    assert ex.elevate_subs()[-1] == "revoke"


def test_setup_production_grant_failure_aborts_before_run(sf):
    with sf() as db:
        server_id = _server(db)
        bench_id = _bench(db, server_id, prod=False)
    ex = ProdExecutor(prod=False, grant_fails=True)
    job_id = _run(
        sf,
        server_id=server_id,
        params={"bench_path": BENCH_PATH, "production_user": "frappe"},
        executor=ex,
    )
    with sf() as db:
        assert db.get(CommandJob, job_id).status == "failure"
        assert db.get(Bench, bench_id).is_production is False
    # Grant failed → the conversion never ran.
    assert not any(a[2:4] == [BENCH_BIN, "setup"] for a in ex.streamed)
    # Pre-backup happened before the failed grant.
    assert ex.elevate_subs()[0] == "backup"


# --------------------------------------------------------------------------- #
# API surface + RBAC
# --------------------------------------------------------------------------- #


@pytest.fixture
def prod_client(client, db_session):
    runner = JobRunner(lambda: db_session, InMemoryJobBackend(), enqueue=lambda job: None)
    client.app.dependency_overrides[get_job_runner] = lambda: runner
    yield client
    client.app.dependency_overrides.pop(get_job_runner, None)


@pytest.fixture
def api_fixture(db_session):
    s = Server(name="vm-a", hostname="10.0.0.1")
    db_session.add(s)
    db_session.commit()
    bench = Bench(server_id=s.id, path=BENCH_PATH, name="frappe-bench")
    db_session.add(bench)
    db_session.commit()
    return {"server_id": s.id, "bench_id": bench.id}


def test_setup_production_endpoint_launches_job(prod_client, api_fixture):
    login(prod_client, "developer@example.com")
    resp = prod_client.post(
        f"/api/benches/{api_fixture['bench_id']}/setup-production",
        json={},
        headers=csrf_headers(prod_client),
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["action_name"] == "bench.setup_production"


def test_setup_production_rejects_bad_user_422(prod_client, api_fixture):
    login(prod_client, "developer@example.com")
    resp = prod_client.post(
        f"/api/benches/{api_fixture['bench_id']}/setup-production",
        json={"production_user": "root; rm -rf /"},
        headers=csrf_headers(prod_client),
    )
    assert resp.status_code == 422


def test_readonly_cannot_setup_production(prod_client, api_fixture):
    login(prod_client, "readonly@example.com")
    resp = prod_client.post(
        f"/api/benches/{api_fixture['bench_id']}/setup-production",
        json={},
        headers=csrf_headers(prod_client),
    )
    assert resp.status_code == 403


def test_second_setup_production_conflicts_409(prod_client, api_fixture):
    login(prod_client, "developer@example.com")
    first = prod_client.post(
        f"/api/benches/{api_fixture['bench_id']}/setup-production",
        json={},
        headers=csrf_headers(prod_client),
    )
    assert first.status_code == 201
    second = prod_client.post(
        f"/api/benches/{api_fixture['bench_id']}/setup-production",
        json={},
        headers=csrf_headers(prod_client),
    )
    assert second.status_code == 409


def test_setup_production_missing_bench_404(prod_client, api_fixture):
    login(prod_client, "developer@example.com")
    resp = prod_client.post(
        "/api/benches/999999/setup-production", json={}, headers=csrf_headers(prod_client)
    )
    assert resp.status_code == 404
