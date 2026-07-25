"""Safe update pipeline (session 3.3): clone → staging → verify → promote +
rollback. Covers template rendering/injection, the three new actions end-to-end
over in-memory fakes (no Redis/RQ/SSH), and the server-side promote guardrails.
"""

import json

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core import backups as bk
from app.core.commands import RenderError, get_template, render
from app.core.commands.actions import _MODE_PROBE
from app.core.jobs import CaptureResult, InMemoryJobBackend, JobRunner
from app.core.security import get_secrets_service
from app.db import Base
from app.models import CommandJob, Server
from app.models.bench import Bench
from app.models.site import Site
from app.models.update_pipeline import UpdatePipeline
from tests.conftest import csrf_headers, login

BENCH_PATH = "/home/frappe/frappe-bench"
PFX = "20260725_000000-test1_localhost"
BK = f"{BENCH_PATH}/sites/test1.localhost/private/backups"
SHA_DB, SHA_PUB, SHA_PRIV, SHA_CFG = "a" * 64, "b" * 64, "c" * 64, "d" * 64

INSPECT_WITH_FILES = (
    f"ART\tdatabase.sql.gz\t{BK}/{PFX}-database.sql.gz\t1048576\t{SHA_DB}\n"
    f"ART\tfiles.tar\t{BK}/{PFX}-files.tar\t2048\t{SHA_PUB}\n"
    f"ART\tprivate-files.tar\t{BK}/{PFX}-private-files.tar\t512\t{SHA_PRIV}\n"
    f"ART\tsite_config_backup.json\t{BK}/{PFX}-site_config_backup.json\t256\t{SHA_CFG}\n"
)
CONFIG_JSON = '{"db_name": "x", "encryption_key": "SECRETKEY123456"}'


# --------------------------------------------------------------------------- #
# Template rendering + injection safety
# --------------------------------------------------------------------------- #


def test_ping_and_scheduler_templates_render():
    args = {"site": "s.localhost", "bench_path": BENCH_PATH}
    assert render(get_template("site.ping"), args).argv == [
        "bench", "--site", "s.localhost", "execute", "frappe.ping"
    ]
    assert render(get_template("site.scheduler_status"), args).argv == [
        "bench", "--site", "s.localhost", "scheduler", "status"
    ]


def test_count_doctype_template_renders_json_args():
    rc = render(
        get_template("site.count_doctype"),
        {"site": "s.localhost", "bench_path": BENCH_PATH, "doctype": "User"},
    )
    assert rc.argv == [
        "bench", "--site", "s.localhost", "execute", "frappe.client.get_count",
        "--args", '["User"]',
    ]


def test_count_doctype_rejects_injection():
    with pytest.raises(RenderError):
        render(
            get_template("site.count_doctype"),
            {"site": "s.localhost", "bench_path": BENCH_PATH, "doctype": 'User"];x'},
        )


def test_scrub_rejects_shell_metacharacters():
    with pytest.raises(RenderError):
        render(
            get_template("site.scrub"),
            {"site": "s.localhost", "bench_path": BENCH_PATH, "method": "a;rm -rf /"},
        )


def test_clone_and_promote_are_locked_nonidempotent():
    for name in ("site.clone_to_staging", "site.promote_update", "site.verify_checklist"):
        t = get_template(name)
        assert t.requires_lock is True
        assert t.idempotent is False


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


class UpdatesExecutor:
    """`capture` answers the dev/prod probe, the artifact-inspect script, `cat
    <config>`, and the read-only verify probes (ping/scheduler/get_count). `run`
    records every streamed argv and can be told to fail a chosen command."""

    def __init__(self, *, prod=False, ping_ok=True, scheduler_ok=True,
                 counts=None, fail_on=None):
        self._prod = prod
        self._ping_ok = ping_ok
        self._scheduler_ok = scheduler_ok
        self._counts = counts or {}
        self._fail_on = fail_on
        self.streamed: list[list[str]] = []

    async def capture(self, argv, *, cwd=None, timeout=120.0):
        if argv[:2] == ["bash", "-c"]:
            script = argv[2]
            if script == _MODE_PROBE:
                return CaptureResult(0, "PROD" if self._prod else "DEV", "")
            if script == bk.ARTIFACT_INSPECT_SCRIPT:
                return CaptureResult(0, INSPECT_WITH_FILES, "")
        if argv[:1] == ["cat"]:
            return CaptureResult(0, CONFIG_JSON, "")
        if argv[:1] == ["bench"] and argv[3:5] == ["execute", "frappe.ping"]:
            return CaptureResult(0 if self._ping_ok else 1,
                                 "pong" if self._ping_ok else "Traceback", "")
        if argv[:1] == ["bench"] and argv[3:4] == ["scheduler"]:
            return CaptureResult(0, "Scheduler is enabled for site" if self._scheduler_ok
                                 else "Scheduler is disabled for site", "")
        if argv[:1] == ["bench"] and argv[4:5] == ["frappe.client.get_count"]:
            site = argv[2]
            return CaptureResult(0, str(self._counts.get(site, 100)), "")
        raise AssertionError(f"unexpected capture {argv}")

    async def run(self, argv, *, cwd, run_as, on_line, cancel_check):
        self.streamed.append(list(argv))
        if self._fail_on is not None and self._fail_on(argv):
            return 1
        return 0


def fake_factory(executor):
    class _CM:
        async def __aenter__(self):
            return executor

        async def __aexit__(self, *exc):
            return False

    return lambda server: _CM()


@pytest.fixture
def sf():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    yield factory
    engine.dispose()


def _world(db, *, source_env="prod"):
    """One server, a source bench+site and a staging bench, sharing the server."""
    s = Server(name="vm", hostname="10.0.0.9")
    s.mariadb_root_password_enc = get_secrets_service().encrypt("rootpw")
    db.add(s)
    db.commit()
    src_bench = Bench(server_id=s.id, path=BENCH_PATH, name="frappe-bench",
                      frappe_version="16.2.0", redis_queue_port=11000, redis_cache_port=13000)
    stg_bench = Bench(server_id=s.id, path="/home/frappe/staging-bench", name="staging-bench",
                      frappe_version="16.2.0", redis_queue_port=11100, redis_cache_port=13100)
    db.add_all([src_bench, stg_bench])
    db.commit()
    site = Site(bench_id=src_bench.id, name="test1.localhost", status="active",
                environment=source_env)
    db.add(site)
    db.commit()
    return s.id, src_bench.id, stg_bench.id, site.id


def _run(sf, *, action, server_id, target_id, params, executor, user_secrets=None):
    runner = JobRunner(
        sf, InMemoryJobBackend(), enqueue=lambda job: None, secrets=get_secrets_service()
    )
    with sf() as db:
        job = runner.create(
            db, action_name=action, server_id=server_id, target_type="site",
            target_id=target_id, params=params, priority="default", created_by=None,
            user_secrets=user_secrets,
        )
        job_id = job.id
    runner.run_job(job_id, executor_factory=fake_factory(executor))
    return job_id


# --------------------------------------------------------------------------- #
# Clone-to-staging
# --------------------------------------------------------------------------- #


def test_clone_backs_up_source_then_restores_into_staging(sf):
    with sf() as db:
        server_id, src_bench_id, stg_bench_id, site_id = _world(db)
        p = UpdatePipeline(
            source_site_id=site_id, source_bench_id=src_bench_id,
            staging_bench_id=stg_bench_id, staging_site_name="staging.localhost",
            phase="cloning",
        )
        db.add(p)
        db.commit()
        pid = p.id
    ex = UpdatesExecutor(prod=False)
    job_id = _run(
        sf, action="site.clone_to_staging", server_id=server_id,
        target_id="/home/frappe/staging-bench::staging.localhost",
        params={
            "source_site": "test1.localhost", "source_bench_path": BENCH_PATH,
            "site": "staging.localhost", "bench_path": "/home/frappe/staging-bench",
            "pipeline_id": str(pid),
        },
        user_secrets={"admin_pw": "adminpass"},
        executor=ex,
    )
    with sf() as db:
        assert db.get(CommandJob, job_id).status == "success"
        # A staging site row was registered, tagged staging.
        stg = db.scalars(select(Site).where(Site.name == "staging.localhost")).first()
        assert stg is not None and stg.environment == "staging"
        p = db.get(UpdatePipeline, pid)
        assert p.phase == "cloned" and p.staging_site_id == stg.id
    kinds = list(ex.streamed)
    # The source backup (bench backup) runs BEFORE the staging new-site/restore.
    backup_idx = next(
        i for i, a in enumerate(kinds) if a[:2] == ["bench", "--site"] and "backup" in a
    )
    newsite_idx = next(i for i, a in enumerate(kinds) if a[:2] == ["bench", "new-site"])
    restore_idx = next(i for i, a in enumerate(kinds) if "restore" in a)
    assert backup_idx < newsite_idx < restore_idx
    assert any(a[:2] == ["bench", "--site"] and a[-1] == "migrate" for a in kinds)


def test_clone_runs_scrub_hook_when_method_given(sf):
    with sf() as db:
        server_id, src_bench_id, stg_bench_id, site_id = _world(db)
    ex = UpdatesExecutor(prod=False)
    _run(
        sf, action="site.clone_to_staging", server_id=server_id,
        target_id="/home/frappe/staging-bench::staging.localhost",
        params={
            "source_site": "test1.localhost", "source_bench_path": BENCH_PATH,
            "site": "staging.localhost", "bench_path": "/home/frappe/staging-bench",
            "scrub_method": "fdm_hooks.privacy.mask_pii",
        },
        user_secrets={"admin_pw": "adminpass"},
        executor=ex,
    )
    assert any(
        a[:5] == ["bench", "--site", "staging.localhost", "execute", "fdm_hooks.privacy.mask_pii"]
        for a in ex.streamed
    )


# --------------------------------------------------------------------------- #
# Verification checklist
# --------------------------------------------------------------------------- #


def _logs(sf, job_id):
    from app.models import LogEntry

    with sf() as db:
        rows = db.scalars(
            select(LogEntry).where(LogEntry.job_id == job_id).order_by(LogEntry.seq)
        ).all()
        return "\n".join(r.content for r in rows)


def test_verify_all_green_persists_pipeline(sf):
    with sf() as db:
        server_id, src_bench_id, stg_bench_id, site_id = _world(db)
        stg = Site(bench_id=stg_bench_id, name="staging.localhost", status="active",
                   environment="staging")
        db.add(stg)
        db.commit()
        p = UpdatePipeline(
            source_site_id=site_id, source_bench_id=src_bench_id,
            staging_bench_id=stg_bench_id, staging_site_name="staging.localhost",
            staging_site_id=stg.id, phase="updated",
        )
        db.add(p)
        db.commit()
        pid = p.id
    ex = UpdatesExecutor(ping_ok=True, scheduler_ok=True,
                         counts={"staging.localhost": 100, "test1.localhost": 100})
    job_id = _run(
        sf, action="site.verify_checklist", server_id=server_id,
        target_id="/home/frappe/staging-bench::staging.localhost",
        params={
            "site": "staging.localhost", "bench_path": "/home/frappe/staging-bench",
            "source_site": "test1.localhost", "source_bench_path": BENCH_PATH,
            "pipeline_id": str(pid),
        },
        executor=ex,
    )
    logs = _logs(sf, job_id)
    assert "CHECKLIST_RESULT" in logs
    payload = json.loads(logs.split("CHECKLIST_RESULT ", 1)[1].splitlines()[0])
    assert payload["all_ok"] is True
    keys = {c["key"] for c in payload["checks"]}
    assert keys == {"boots", "migrations", "scheduler", "row_count"}
    with sf() as db:
        p = db.get(UpdatePipeline, pid)
        assert p.checklist_ok is True and p.phase == "verified"


def test_verify_red_when_site_does_not_boot(sf):
    with sf() as db:
        server_id, src_bench_id, stg_bench_id, site_id = _world(db)
        stg = Site(bench_id=stg_bench_id, name="staging.localhost", status="active")
        db.add(stg)
        db.commit()
        p = UpdatePipeline(
            source_site_id=site_id, source_bench_id=src_bench_id,
            staging_bench_id=stg_bench_id, staging_site_name="staging.localhost",
            staging_site_id=stg.id, phase="updated",
        )
        db.add(p)
        db.commit()
        pid = p.id
    ex = UpdatesExecutor(ping_ok=False)
    _run(
        sf, action="site.verify_checklist", server_id=server_id,
        target_id="/home/frappe/staging-bench::staging.localhost",
        params={"site": "staging.localhost", "bench_path": "/home/frappe/staging-bench",
                "pipeline_id": str(pid)},
        executor=ex,
    )
    with sf() as db:
        p = db.get(UpdatePipeline, pid)
        assert p.checklist_ok is False and p.phase == "verify_failed"


# --------------------------------------------------------------------------- #
# Promote + rollback (the safety-critical path)
# --------------------------------------------------------------------------- #


def test_promote_takes_prebackup_first_then_updates(sf):
    with sf() as db:
        server_id, src_bench_id, stg_bench_id, site_id = _world(db)
        p = UpdatePipeline(
            source_site_id=site_id, source_bench_id=src_bench_id,
            staging_bench_id=stg_bench_id, staging_site_name="staging.localhost",
            phase="verified", checklist_ok=True,
        )
        db.add(p)
        db.commit()
        pid = p.id
    ex = UpdatesExecutor(prod=False, ping_ok=True)
    job_id = _run(
        sf, action="site.promote_update", server_id=server_id,
        target_id=f"{BENCH_PATH}::test1.localhost",
        params={"site": "test1.localhost", "bench_path": BENCH_PATH, "pipeline_id": str(pid)},
        executor=ex,
    )
    with sf() as db:
        assert db.get(CommandJob, job_id).status == "success"
        p = db.get(UpdatePipeline, pid)
        assert p.phase == "promoted" and p.pre_backup_id is not None
    # The pre-update backup ran BEFORE `bench update` (the mandatory gate).
    backup_idx = next(i for i, a in enumerate(ex.streamed)
                      if a[:2] == ["bench", "--site"] and "backup" in a)
    update_idx = next(i for i, a in enumerate(ex.streamed) if a == ["bench", "update"])
    assert backup_idx < update_idx


def test_promote_rolls_back_on_failed_update(sf):
    with sf() as db:
        server_id, src_bench_id, stg_bench_id, site_id = _world(db)
        p = UpdatePipeline(
            source_site_id=site_id, source_bench_id=src_bench_id,
            staging_bench_id=stg_bench_id, staging_site_name="staging.localhost",
            phase="verified", checklist_ok=True,
        )
        db.add(p)
        db.commit()
        pid = p.id
    # Induce a failed `bench update`.
    ex = UpdatesExecutor(prod=False, fail_on=lambda a: a == ["bench", "update"])
    job_id = _run(
        sf, action="site.promote_update", server_id=server_id,
        target_id=f"{BENCH_PATH}::test1.localhost",
        params={"site": "test1.localhost", "bench_path": BENCH_PATH, "pipeline_id": str(pid)},
        executor=ex,
    )
    with sf() as db:
        job = db.get(CommandJob, job_id)
        assert job.status == "failure"  # the update failed
        assert job.retry_count == 0  # destructive: never auto-retried
        p = db.get(UpdatePipeline, pid)
        assert p.phase == "rolled_back" and p.rollback_job_id == job_id
    # Rollback restored the pre-update backup AFTER the failed update.
    update_idx = next(i for i, a in enumerate(ex.streamed) if a == ["bench", "update"])
    restore_idx = next(i for i, a in enumerate(ex.streamed) if "restore" in a)
    assert update_idx < restore_idx


# --------------------------------------------------------------------------- #
# API guardrails
# --------------------------------------------------------------------------- #


@pytest.fixture
def api(client, db_session):
    server = Server(name="vm", hostname="10.0.0.9")
    server.mariadb_root_password_enc = get_secrets_service().encrypt("rootpw")
    db_session.add(server)
    db_session.commit()
    bench = Bench(server_id=server.id, path=BENCH_PATH, name="frappe-bench",
                  frappe_version="16.2.0")
    db_session.add(bench)
    db_session.commit()
    site = Site(bench_id=bench.id, name="prod.localhost", status="active",
                environment="prod")
    db_session.add(site)
    db_session.commit()
    return {"client": client, "db": db_session, "bench_id": bench.id, "site_id": site.id}


def _pipeline(db, api, *, checklist_ok):
    p = UpdatePipeline(
        source_site_id=api["site_id"], source_bench_id=api["bench_id"],
        staging_bench_id=api["bench_id"], staging_site_name="stg.localhost",
        phase="verified" if checklist_ok else "verifying", checklist_ok=checklist_ok,
    )
    db.add(p)
    db.commit()
    return p.id


def test_promote_blocked_without_green_checklist(api):
    c = api["client"]
    login(c, "developer@example.com")
    pid = _pipeline(api["db"], api, checklist_ok=False)
    resp = c.post(f"/api/update-pipelines/{pid}/promote", json={}, headers=csrf_headers(c))
    assert resp.status_code == 409


def test_prod_promote_requires_danger_role(api):
    """A Developer (no `danger`) cannot promote to a prod site even when green."""
    c = api["client"]
    login(c, "developer@example.com")
    pid = _pipeline(api["db"], api, checklist_ok=True)
    resp = c.post(f"/api/update-pipelines/{pid}/promote",
                  json={"confirm_name": "prod.localhost", "signoff": "TICKET-9"},
                  headers=csrf_headers(c))
    assert resp.status_code == 403


def test_prod_promote_requires_confirm_and_signoff(api):
    c = api["client"]
    login(c, "admin@example.com")
    pid = _pipeline(api["db"], api, checklist_ok=True)
    # Missing confirm name -> 422.
    resp = c.post(f"/api/update-pipelines/{pid}/promote",
                  json={"signoff": "TICKET-9"}, headers=csrf_headers(c))
    assert resp.status_code == 422
    # Correct confirm but no sign-off -> 422 (never write prod without sign-off).
    resp = c.post(f"/api/update-pipelines/{pid}/promote",
                  json={"confirm_name": "prod.localhost"}, headers=csrf_headers(c))
    assert resp.status_code == 422


def test_readonly_cannot_set_environment(api):
    c = api["client"]
    login(c, "readonly@example.com")
    resp = c.post(f"/api/sites/{api['site_id']}/environment",
                  json={"environment": "staging"}, headers=csrf_headers(c))
    assert resp.status_code == 403


def test_operator_can_classify_environment(api):
    c = api["client"]
    login(c, "developer@example.com")
    resp = c.post(f"/api/sites/{api['site_id']}/environment",
                  json={"environment": "dev"}, headers=csrf_headers(c))
    assert resp.status_code == 200 and resp.json()["environment"] == "dev"
