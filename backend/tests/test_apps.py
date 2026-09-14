"""Apps (session 1.9): repo-source host-allowlist validation, app-version
parsing + matrix upkeep, the get-app deploy-key dance (staged via `capture` so
the key never hits a log), the site.install_app orchestrator (get + install
wrapped in the 1.8 Redis dance), uninstall, branch listing, and the full API
surface (sources CRUD + install/uninstall + RBAC + write-only deploy key). No
Redis/RQ/SSH touched."""

import json

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.routes.jobs import get_job_runner
from app.core import appsources
from app.core import backups as bk
from app.core.appsources import RepoSourceError, validate_repo_source
from app.core.commands import MASK, get_template, render
from app.core.commands.actions import _MODE_PROBE, _WRITE_KEY_SCRIPT
from app.core.jobs import CaptureResult, InMemoryJobBackend, JobRunner
from app.core.secrets_resolve import resolve_secrets
from app.core.security import get_secrets_service
from app.db import Base
from app.models import CommandJob, LogEntry, Server
from app.models.app import AppSource, InstalledApp
from app.models.backup import Backup
from app.models.bench import Bench
from app.models.site import Site
from tests.conftest import csrf_headers, login

BENCH_PATH = "/home/frappe/frappe-bench"
# A realistic ARTIFACT_INSPECT_SCRIPT output for the uninstall pre-op backup.
_BK = f"{BENCH_PATH}/sites/test1.localhost/private/backups"
_PFX = "20260710_000000"
INSPECT_WITH_FILES = (
    f"ART\tdatabase.sql.gz\t{_BK}/{_PFX}-database.sql.gz\t1048576\t{'a' * 64}\n"
    f"ART\tfiles.tar\t{_BK}/{_PFX}-files.tar\t2048\t{'b' * 64}\n"
    f"ART\tprivate-files.tar\t{_BK}/{_PFX}-private-files.tar\t512\t{'c' * 64}\n"
    f"ART\tsite_config_backup.json\t{_BK}/{_PFX}-site_config_backup.json\t256\t{'d' * 64}\n"
)
FAKE_KEY = (
    "-----BEGIN OPENSSH PRIVATE KEY-----\n"
    "abcDEF123/+==\nline2\n-----END OPENSSH PRIVATE KEY-----\n"
)


# --------------------------------------------------------------------------- #
# Repo-source host allowlist (golden rule 1)
# --------------------------------------------------------------------------- #

ALLOW = {"github.com", "gitlab.com"}


def test_marketplace_name_bypasses_host_check():
    r = validate_repo_source("erpnext", allowlist=ALLOW)
    assert r.kind == "marketplace" and r.argument == "erpnext" and not r.is_ssh


@pytest.mark.parametrize(
    "url,kind,is_ssh",
    [
        ("https://github.com/frappe/erpnext", "github", False),
        ("https://github.com/frappe/erpnext.git", "github", False),
        ("git@github.com:acme/custom_app.git", "github", True),
        ("https://gitlab.com/group/proj", "gitlab", False),
        ("git@gitlab.com:group/proj.git", "gitlab", True),
    ],
)
def test_allowlisted_urls_classified(url, kind, is_ssh):
    r = validate_repo_source(url, allowlist=ALLOW)
    assert r.kind == kind and r.is_ssh is is_ssh and r.argument == url


@pytest.mark.parametrize(
    "bad",
    [
        "https://evil.com/x/y",
        "git@bitbucket.org:a/b.git",
        "https://github.evil.com/a/b",
        "ftp://github.com/a/b",
        "not a url with spaces",
    ],
)
def test_bad_source_rejected(bad):
    with pytest.raises(RepoSourceError):
        validate_repo_source(bad, allowlist=ALLOW)


# --------------------------------------------------------------------------- #
# Version parsing + matrix upkeep
# --------------------------------------------------------------------------- #

BENCH_VERSION_OUT = "frappe 16.25.0\nerpnext 16.20.1\nhrms 16.10.0\n"


def test_parse_app_version_picks_the_app():
    assert appsources.parse_app_version(BENCH_VERSION_OUT, "erpnext") == "16.20.1"
    assert appsources.parse_app_version(BENCH_VERSION_OUT, "frappe") == "16.25.0"
    assert appsources.parse_app_version(BENCH_VERSION_OUT, "missing") is None


@pytest.fixture
def sf():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    yield factory
    engine.dispose()


def _bench(db, *, prod=False, mariadb_pw="rootpw"):
    s = Server(name="vm", hostname="10.0.0.9")
    if mariadb_pw is not None:
        s.mariadb_root_password_enc = get_secrets_service().encrypt(mariadb_pw)
    db.add(s)
    db.commit()
    bench = Bench(
        server_id=s.id, path=BENCH_PATH, name="frappe-bench", is_production=prod,
        redis_queue_port=11000, redis_cache_port=13000, webserver_port=8000,
    )
    db.add(bench)
    db.commit()
    db.add(Site(bench_id=bench.id, name="test1.localhost"))
    db.commit()
    return s.id, bench.id


def test_upsert_and_remove_installed_app(sf):
    with sf() as db:
        _, bench_id = _bench(db)
        site_id = db.scalars(select(Site.id)).first()
        row = appsources.upsert_installed_app(
            db, site_id=site_id, bench_id=bench_id, app_name="erpnext",
            branch="version-16", version="16.20.1",
        )
        assert row.version == "16.20.1"
        # Re-install updates in place (unique on site+app), not a duplicate.
        again = appsources.upsert_installed_app(
            db, site_id=site_id, bench_id=bench_id, app_name="erpnext", version="16.21.0",
        )
        assert again.id == row.id and again.version == "16.21.0"
        assert db.scalars(select(InstalledApp)).all().__len__() == 1
        assert appsources.remove_installed_app(db, site_id=site_id, app_name="erpnext")
        assert db.scalars(select(InstalledApp)).all() == []
        assert not appsources.remove_installed_app(db, site_id=site_id, app_name="erpnext")


# --------------------------------------------------------------------------- #
# Template renders (golden rule 1)
# --------------------------------------------------------------------------- #


def test_get_app_render():
    r = render(
        get_template("app.get"),
        {"branch": "version-16", "source": "erpnext", "bench_path": BENCH_PATH},
    )
    assert r.argv == ["bench", "get-app", "--branch", "version-16", "erpnext"]
    assert r.cwd == BENCH_PATH


def test_install_render():
    r = render(
        get_template("app.install"),
        {"site": "test1.localhost", "app": "erpnext", "bench_path": BENCH_PATH},
    )
    assert r.argv == ["bench", "--site", "test1.localhost", "install-app", "erpnext"]


def test_uninstall_render_is_non_interactive():
    r = render(
        get_template("app.uninstall"),
        {"site": "test1.localhost", "app": "erpnext", "bench_path": BENCH_PATH},
    )
    assert r.argv[-1] == "--yes"  # never prompts / hangs the job
    assert r.argv[:5] == ["bench", "--site", "test1.localhost", "uninstall-app", "erpnext"]


@pytest.mark.parametrize("bad_app", ["Erp Next", "erp-next", "erp;rm", "UPPER"])
def test_install_rejects_bad_app_name(bad_app):
    from app.core.commands import RenderError

    with pytest.raises(RenderError):
        render(
            get_template("app.install"),
            {"site": "test1.localhost", "app": bad_app, "bench_path": BENCH_PATH},
        )


# --------------------------------------------------------------------------- #
# Secret resolution: optional deploy key
# --------------------------------------------------------------------------- #


def test_optional_deploy_key_absent_is_skipped():
    svc = get_secrets_service()
    tmpl = get_template("site.install_app")
    # No job secrets at all (public repo) -> deploy_key simply not resolved.
    resolved = resolve_secrets(tmpl, secrets_enc=None, server=None, secrets=svc)
    assert resolved == {}


def test_deploy_key_resolved_when_supplied():
    from app.core.secrets_resolve import encrypt_job_secrets

    svc = get_secrets_service()
    tmpl = get_template("site.install_app")
    enc = encrypt_job_secrets(svc, {"deploy_key": FAKE_KEY})
    resolved = resolve_secrets(tmpl, secrets_enc=enc, server=None, secrets=svc)
    assert resolved == {"deploy_key": FAKE_KEY}


# --------------------------------------------------------------------------- #
# Orchestrator end-to-end over fakes
# --------------------------------------------------------------------------- #


class AppExecutor:
    """Fake remote executor for the app actions. Records captured + streamed
    argvs; answers the mode probe, `bench version`, key staging and ls-remote."""

    def __init__(
        self, *, prod=False, install_exit=0, backup_exit=0, branches=("main", "version-16")
    ):
        self._prod = prod
        self._install_exit = install_exit
        self._backup_exit = backup_exit
        self._branches = branches
        self.captured: list[list[str]] = []
        self.streamed: list[list[str]] = []

    async def capture(self, argv, *, cwd=None, timeout=120.0):
        self.captured.append(list(argv))
        if argv[:2] == ["bash", "-c"] and argv[2] == _MODE_PROBE:
            return CaptureResult(0, "PROD" if self._prod else "DEV", "")
        if argv[:2] == ["bash", "-c"] and argv[2] == _WRITE_KEY_SCRIPT:
            return CaptureResult(0, "", "")
        if argv[:2] == ["bash", "-c"] and argv[2] == bk.ARTIFACT_INSPECT_SCRIPT:
            return CaptureResult(0, INSPECT_WITH_FILES, "")
        if argv[:2] == ["bench", "version"]:
            return CaptureResult(0, BENCH_VERSION_OUT, "")
        if argv[:1] == ["rm"]:
            return CaptureResult(0, "", "")
        if argv[0] == "git" or (argv[0] == "env" and "git" in argv):
            lines = "".join(f"sha{i}\trefs/heads/{b}\n" for i, b in enumerate(self._branches))
            return CaptureResult(0, lines, "")
        raise AssertionError(f"unexpected capture {argv}")

    async def run(self, argv, *, cwd, run_as, on_line, cancel_check):
        self.streamed.append(list(argv))
        if argv[:2] == ["bench", "get-app"] or (argv[0] == "env" and "get-app" in argv):
            on_line("stdout", "cloning app repo")
            return 0
        if "backup" in argv:
            on_line("stdout", "backing up site")
            return self._backup_exit
        if "install-app" in argv:
            on_line("stdout", "installing app on site")
            return self._install_exit
        if "uninstall-app" in argv:
            on_line("stdout", "removing app from site")
            return 0
        return 0


def fake_factory(executor):
    class _CM:
        async def __aenter__(self):
            return executor

        async def __aexit__(self, *exc):
            return False

    return lambda server: _CM()


def _run(sf, *, action, server_id, target_id, params, user_secrets=None):
    runner = JobRunner(sf, InMemoryJobBackend(), enqueue=lambda job: None)
    with sf() as db:
        job = runner.create(
            db, action_name=action, server_id=server_id, target_type="site",
            target_id=target_id, params=params, user_secrets=user_secrets,
            priority="high", created_by=None,
        )
        job_id = job.id
    return runner, job_id


def test_marketplace_install_runs_get_then_install_and_registers(sf):
    with sf() as db:
        server_id, bench_id = _bench(db, prod=False)
        src = AppSource(name="erpnext", repo_url="erpnext", kind="marketplace")
        db.add(src)
        db.commit()
        src_id = src.id
    ex = AppExecutor(prod=False)
    runner, job_id = _run(
        sf, action="site.install_app", server_id=server_id,
        target_id=f"{BENCH_PATH}::test1.localhost",
        params={
            "site": "test1.localhost", "bench_path": BENCH_PATH, "app": "erpnext",
            "source": "erpnext", "branch": "version-16", "source_name": "erpnext",
        },
    )
    runner.run_job(job_id, executor_factory=fake_factory(ex))

    with sf() as db:
        job = db.get(CommandJob, job_id)
        assert job.status == "success", job.status
        # get-app ran (version-16), then the Redis dance around install-app.
        kinds = [a[0] for a in ex.streamed]
        assert kinds == [
            "bench", "redis-server", "redis-server", "bench", "redis-cli", "redis-cli"
        ], ex.streamed
        get = ex.streamed[0]
        assert get == ["bench", "get-app", "--branch", "version-16", "erpnext"]
        # The matrix cell is registered, linked to its source, with a version.
        ia = db.scalars(select(InstalledApp)).one()
        assert ia.app_name == "erpnext" and ia.version == "16.20.1"
        assert ia.app_source_id == src_id and ia.branch == "version-16"


def test_install_without_source_skips_get(sf):
    with sf() as db:
        server_id, _ = _bench(db, prod=False)
    ex = AppExecutor(prod=False)
    runner, job_id = _run(
        sf, action="site.install_app", server_id=server_id,
        target_id=f"{BENCH_PATH}::test1.localhost",
        params={"site": "test1.localhost", "bench_path": BENCH_PATH, "app": "payments"},
    )
    runner.run_job(job_id, executor_factory=fake_factory(ex))
    with sf() as db:
        assert db.get(CommandJob, job_id).status == "success"
    # No get-app; just the Redis dance around install-app.
    assert [a[0] for a in ex.streamed] == [
        "redis-server", "redis-server", "bench", "redis-cli", "redis-cli"
    ], ex.streamed


def test_store_install_installs_dependencies_first(sf):
    """DOO-1192: a dep_plan makes the SAME job fetch+install each dependency
    before the primary app, deps-first, on one lock/progress surface."""
    with sf() as db:
        server_id, _ = _bench(db, prod=False)
    ex = AppExecutor(prod=False)
    runner, job_id = _run(
        sf, action="site.install_app", server_id=server_id,
        target_id=f"{BENCH_PATH}::test1.localhost",
        params={
            "site": "test1.localhost", "bench_path": BENCH_PATH, "app": "hrms",
            "source": "https://github.com/frappe/hrms", "branch": "version-15",
            "dep_plan": "erpnext~https://github.com/frappe/erpnext~version-15",
        },
    )
    runner.run_job(job_id, executor_factory=fake_factory(ex))
    with sf() as db:
        assert db.get(CommandJob, job_id).status == "success"
        installs = [a for a in ex.streamed if "install-app" in a]
        assert [a[-1] for a in installs] == ["erpnext", "hrms"]  # dependency first
        gets = [a for a in ex.streamed if "get-app" in a]
        assert gets[0][-1] == "https://github.com/frappe/hrms"  # primary get first
        assert any(a[-1] == "https://github.com/frappe/erpnext" for a in gets)
        rows = {r.app_name: r for r in db.scalars(select(InstalledApp)).all()}
        assert set(rows) == {"erpnext", "hrms"}
        assert rows["erpnext"].branch == "version-15"


def test_prod_install_skips_redis_dance(sf):
    with sf() as db:
        server_id, _ = _bench(db, prod=True)
    ex = AppExecutor(prod=True)
    runner, job_id = _run(
        sf, action="site.install_app", server_id=server_id,
        target_id=f"{BENCH_PATH}::test1.localhost",
        params={"site": "test1.localhost", "bench_path": BENCH_PATH, "app": "payments"},
    )
    runner.run_job(job_id, executor_factory=fake_factory(ex))
    with sf() as db:
        assert db.get(CommandJob, job_id).status == "success"
    assert [a[0] for a in ex.streamed] == ["bench"], ex.streamed  # install only


def test_private_get_app_deploy_key_dance_and_redaction(sf):
    with sf() as db:
        server_id, _ = _bench(db, prod=False)
    ex = AppExecutor(prod=False)
    runner, job_id = _run(
        sf, action="site.install_app", server_id=server_id,
        target_id=f"{BENCH_PATH}::test1.localhost",
        params={
            "site": "test1.localhost", "bench_path": BENCH_PATH, "app": "custom_app",
            "source": "git@github.com:acme/custom_app.git", "branch": "main",
        },
        user_secrets={"deploy_key": FAKE_KEY},
    )
    runner.run_job(job_id, executor_factory=fake_factory(ex))

    with sf() as db:
        job = db.get(CommandJob, job_id)
        assert job.status == "success", job.status
        # The key was carried encrypted, never in the clear on the job.
        assert job.secrets_enc and "OPENSSH" not in job.secrets_enc
        # get-app streamed with the GIT_SSH_COMMAND env prefix pointing at a
        # temp key path — the key MATERIAL is never a streamed argv element.
        get = next(a for a in ex.streamed if "get-app" in a)
        assert get[0] == "env" and get[1].startswith("GIT_SSH_COMMAND=ssh -i /tmp/fdm-deploykey-")
        assert all("OPENSSH" not in " ".join(a) for a in ex.streamed)
        # The key was staged (base64) via CAPTURE, not streamed, and removed.
        staged = [a for a in ex.captured if a[:3] == ["bash", "-c", _WRITE_KEY_SCRIPT]]
        assert len(staged) == 1
        assert any(a[:1] == ["rm"] for a in ex.captured)
        # Logs never contain the key material (rule 6).
        logs = " ".join(
            e.content for e in db.scalars(select(LogEntry).where(LogEntry.job_id == job_id))
        )
        assert "OPENSSH" not in logs and "line2" not in logs


def test_install_failure_still_shuts_redis_down(sf):
    with sf() as db:
        server_id, _ = _bench(db, prod=False)
    ex = AppExecutor(prod=False, install_exit=1)
    runner, job_id = _run(
        sf, action="site.install_app", server_id=server_id,
        target_id=f"{BENCH_PATH}::test1.localhost",
        params={"site": "test1.localhost", "bench_path": BENCH_PATH, "app": "payments"},
    )
    runner.run_job(job_id, executor_factory=fake_factory(ex))
    with sf() as db:
        assert db.get(CommandJob, job_id).status == "failure"
    # Redis was still shut down (finally), so a later `bench start` can bind.
    assert [a[0] for a in ex.streamed] == [
        "redis-server", "redis-server", "bench", "redis-cli", "redis-cli"
    ], ex.streamed


def test_uninstall_removes_matrix_row(sf):
    with sf() as db:
        server_id, bench_id = _bench(db, prod=False)
        site_id = db.scalars(select(Site.id)).first()
        appsources.upsert_installed_app(
            db, site_id=site_id, bench_id=bench_id, app_name="erpnext", version="16.20.1"
        )
    ex = AppExecutor(prod=False)
    runner, job_id = _run(
        sf, action="app.uninstall", server_id=server_id,
        target_id=f"{BENCH_PATH}::test1.localhost",
        params={"site": "test1.localhost", "app": "erpnext", "bench_path": BENCH_PATH},
    )
    runner.run_job(job_id, executor_factory=fake_factory(ex))
    with sf() as db:
        assert db.get(CommandJob, job_id).status == "success"
        assert db.scalars(select(InstalledApp)).all() == []
    # Rule 5: an automatic pre-op backup (with files) runs BEFORE the uninstall,
    # both wrapped in the dev Redis dance. The backup command is the first bench
    # invocation and the uninstall the second.
    assert [a[0] for a in ex.streamed] == [
        "redis-server", "redis-server", "bench", "bench", "redis-cli", "redis-cli"
    ], ex.streamed
    bench_cmds = [a for a in ex.streamed if a[0] == "bench"]
    assert "backup" in bench_cmds[0] and "--with-files" in bench_cmds[0]
    assert "uninstall-app" in bench_cmds[1]
    # The backup was recorded as a visible Backup row taken by this job.
    with sf() as db:
        b = db.scalars(select(Backup)).one()
        assert b.taken_by_job_id == job_id and b.status == "success"
        assert b.type == "with-files"


def test_uninstall_aborts_when_preop_backup_fails(sf):
    """Rule 5: a failed pre-op backup aborts the uninstall — the app stays
    installed and `uninstall-app` never runs (same posture as pre-restore)."""
    with sf() as db:
        server_id, bench_id = _bench(db, prod=False)
        site_id = db.scalars(select(Site.id)).first()
        appsources.upsert_installed_app(
            db, site_id=site_id, bench_id=bench_id, app_name="erpnext", version="16.20.1"
        )
    ex = AppExecutor(prod=False, backup_exit=1)
    runner, job_id = _run(
        sf, action="app.uninstall", server_id=server_id,
        target_id=f"{BENCH_PATH}::test1.localhost",
        params={"site": "test1.localhost", "app": "erpnext", "bench_path": BENCH_PATH},
    )
    runner.run_job(job_id, executor_factory=fake_factory(ex))
    with sf() as db:
        assert db.get(CommandJob, job_id).status == "failure"
        # The app is still installed — the uninstall was aborted.
        assert len(db.scalars(select(InstalledApp)).all()) == 1
        # The pre-op backup left a visible failed record, not a vanished row.
        assert db.scalars(select(Backup)).one().status == "failed"
    # backup command ran and failed; uninstall-app NEVER ran; Redis still shut down.
    assert not any("uninstall-app" in a for a in ex.streamed), ex.streamed
    assert [a[0] for a in ex.streamed] == [
        "redis-server", "redis-server", "bench", "redis-cli", "redis-cli"
    ], ex.streamed


def test_list_branches_emits_result(sf):
    with sf() as db:
        server_id, _ = _bench(db)
    ex = AppExecutor(branches=("main", "develop", "version-16"))
    runner, job_id = _run(
        sf, action="app.list_branches", server_id=server_id,
        target_id=str(1),
        params={"url": "https://github.com/frappe/erpnext"},
    )
    # list_branches is not site-locked; target_type doesn't matter for the fake.
    runner.run_job(job_id, executor_factory=fake_factory(ex))
    with sf() as db:
        job = db.get(CommandJob, job_id)
        assert job.status == "success", job.status
        result_lines = [
            e.content for e in db.scalars(select(LogEntry).where(LogEntry.job_id == job_id))
            if e.content.startswith("BRANCHES_RESULT ")
        ]
        assert result_lines
        branches = json.loads(result_lines[0].split(" ", 1)[1])
        assert branches == ["develop", "main", "version-16"]


# --------------------------------------------------------------------------- #
# API surface
# --------------------------------------------------------------------------- #


@pytest.fixture
def apps_client(client, db_session):
    runner = JobRunner(lambda: db_session, InMemoryJobBackend(), enqueue=lambda job: None)
    client.app.dependency_overrides[get_job_runner] = lambda: runner
    yield client
    client.app.dependency_overrides.pop(get_job_runner, None)


@pytest.fixture
def api_env(db_session):
    s = Server(name="vm-a", hostname="10.0.0.1")
    s.mariadb_root_password_enc = get_secrets_service().encrypt("rootpw")
    db_session.add(s)
    db_session.commit()
    bench = Bench(server_id=s.id, path=BENCH_PATH, name="frappe-bench", webserver_port=8000)
    db_session.add(bench)
    db_session.commit()
    site = Site(bench_id=bench.id, name="test1.localhost")
    db_session.add(site)
    db_session.commit()
    return {"server_id": s.id, "bench_id": bench.id, "site_id": site.id}


def test_create_public_source(apps_client, api_env):
    login(apps_client, "developer@example.com")
    resp = apps_client.post(
        "/api/app-sources",
        json={"name": "erpnext", "repo_url": "https://github.com/frappe/erpnext",
              "default_branch": "version-16"},
        headers=csrf_headers(apps_client),
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["kind"] == "github" and data["has_deploy_key"] is False


def test_create_private_source_keeps_key_write_only(apps_client, api_env, db_session):
    login(apps_client, "developer@example.com")
    resp = apps_client.post(
        "/api/app-sources",
        json={"name": "custom_app", "repo_url": "git@github.com:acme/custom_app.git",
              "is_private": True, "deploy_key": FAKE_KEY, "default_branch": "main"},
        headers=csrf_headers(apps_client),
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["has_deploy_key"] is True
    assert "deploy_key" not in data and "OPENSSH" not in resp.text
    # Stored encrypted, not in the clear.
    src = db_session.scalars(select(AppSource).where(AppSource.name == "custom_app")).one()
    assert src.deploy_key_enc and "OPENSSH" not in src.deploy_key_enc
    assert get_secrets_service().decrypt(src.deploy_key_enc) == FAKE_KEY


def test_create_source_rejects_bad_host(apps_client, api_env):
    login(apps_client, "developer@example.com")
    resp = apps_client.post(
        "/api/app-sources",
        json={"name": "evil", "repo_url": "https://evil.com/a/b"},
        headers=csrf_headers(apps_client),
    )
    assert resp.status_code == 422
    assert "allowlist" in resp.text


def test_readonly_cannot_create_source(apps_client, api_env):
    login(apps_client, "readonly@example.com")
    resp = apps_client.post(
        "/api/app-sources",
        json={"name": "x", "repo_url": "erpnext"},
        headers=csrf_headers(apps_client),
    )
    assert resp.status_code == 403


def test_install_endpoint_launches_job(apps_client, api_env):
    login(apps_client, "developer@example.com")
    resp = apps_client.post(
        f"/api/sites/{api_env['site_id']}/apps",
        json={"source": "erpnext", "branch": "version-16"},
        headers=csrf_headers(apps_client),
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["status"] == "pending" and data["action_name"] == "site.install_app"
    assert data["params_sanitized"]["app"] == "erpnext"


def test_install_requires_app_name_for_url(apps_client, api_env):
    login(apps_client, "developer@example.com")
    resp = apps_client.post(
        f"/api/sites/{api_env['site_id']}/apps",
        json={"source": "https://github.com/acme/thing", "branch": "main"},
        headers=csrf_headers(apps_client),
    )
    assert resp.status_code == 422
    assert "module" in resp.text


def test_install_from_private_source_carries_key_encrypted(apps_client, api_env, db_session):
    src = AppSource(
        name="custom_app", repo_url="git@github.com:acme/custom_app.git",
        kind="github", is_private=True, default_branch="main",
        deploy_key_enc=get_secrets_service().encrypt(FAKE_KEY),
    )
    db_session.add(src)
    db_session.commit()
    login(apps_client, "developer@example.com")
    resp = apps_client.post(
        f"/api/sites/{api_env['site_id']}/apps",
        json={"app_source_id": src.id},
        headers=csrf_headers(apps_client),
    )
    assert resp.status_code == 201, resp.text
    job_id = resp.json()["id"]
    job = db_session.get(CommandJob, job_id)
    assert job.secrets_enc and "OPENSSH" not in job.secrets_enc
    assert job.params_sanitized["deploy_key"] == MASK
    assert job.params_sanitized["source_name"] == "custom_app"


def test_uninstall_requires_typed_name(apps_client, api_env):
    login(apps_client, "admin@example.com")
    resp = apps_client.request(
        "DELETE",
        f"/api/sites/{api_env['site_id']}/apps/erpnext",
        json={"confirm_name": "wrong"},
        headers=csrf_headers(apps_client),
    )
    assert resp.status_code == 422
    assert "exact app name" in resp.text


def test_uninstall_needs_danger_permission(apps_client, api_env):
    # Developer has app:manage but NOT danger.
    login(apps_client, "developer@example.com")
    resp = apps_client.request(
        "DELETE",
        f"/api/sites/{api_env['site_id']}/apps/erpnext",
        json={"confirm_name": "erpnext"},
        headers=csrf_headers(apps_client),
    )
    assert resp.status_code == 403


def test_admin_uninstall_launches_job(apps_client, api_env):
    login(apps_client, "admin@example.com")
    resp = apps_client.request(
        "DELETE",
        f"/api/sites/{api_env['site_id']}/apps/erpnext",
        json={"confirm_name": "erpnext"},
        headers=csrf_headers(apps_client),
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["action_name"] == "app.uninstall"


def test_installed_matrix_lists_rows(apps_client, api_env, db_session):
    appsources.upsert_installed_app(
        db_session, site_id=api_env["site_id"], bench_id=api_env["bench_id"],
        app_name="erpnext", branch="version-16", version="16.20.1",
    )
    login(apps_client, "readonly@example.com")
    resp = apps_client.get("/api/installed-apps")
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    assert len(rows) == 1
    assert rows[0]["app_name"] == "erpnext"
    assert rows[0]["site_name"] == "test1.localhost"
    assert rows[0]["version"] == "16.20.1"


def test_branches_endpoint_launches_job(apps_client, api_env):
    login(apps_client, "developer@example.com")
    resp = apps_client.post(
        "/api/app-sources/branches",
        json={"server_id": api_env["server_id"], "repo_url": "https://github.com/frappe/erpnext"},
        headers=csrf_headers(apps_client),
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["action_name"] == "app.list_branches"


# --------------------------------------------------------------------------- #
# App store: catalog + unified install path (DOO-1192)
# --------------------------------------------------------------------------- #

from app.core.marketplace import AppRecord, get_registry_cache  # noqa: E402

_ERP15 = {"version": "15.5.0", "branch": "version-15", "commit": "c15",
          "frappe_core": ">=15.0.0,<16.0.0", "channel": "stable"}
_HRMS16 = {"version": "16.0.0", "branch": "version-16", "commit": "c16",
           "frappe_core": ">=16.0.0", "channel": "stable"}
_HRMS15 = {"version": "15.9.0", "branch": "version-15", "commit": "h15",
           "frappe_core": ">=15.0.0,<16.0.0", "channel": "stable",
           "dependencies": {"erpnext": ">=15.0.0,<16.0.0"}}


class _StubRegistry:
    """A registry cache stub — no clone, no network."""

    def __init__(self, records):
        self._recs = {r.name: r for r in records}
        self.fresh_calls = 0

    def ensure_fresh(self):
        self.fresh_calls += 1

    def records(self):
        return list(self._recs.values())

    def record(self, name):
        return self._recs.get(name)


def _store_rec(name, releases):
    return AppRecord(
        name=name,
        repo=f"https://github.com/frappe/{name}",
        releases=tuple(releases),
        meta={
            "name": name, "title": name.upper(), "description": "d",
            "categories": ["x"], "stars": 1,
        },
    )


def _use_registry(client, records, bench_version="15.42.1", *, bench_id=None, db_session=None):
    if bench_id is not None and db_session is not None:
        bench = db_session.get(Bench, bench_id)
        bench.frappe_version = bench_version
        db_session.commit()
    reg = _StubRegistry(records)
    client.app.dependency_overrides[get_registry_cache] = lambda: reg
    return reg


def test_catalog_lists_installable_and_incompatible(apps_client, api_env, db_session):
    _use_registry(
        apps_client,
        [_store_rec("erpnext", [_ERP15]), _store_rec("hrms", [_HRMS16])],
        bench_id=api_env["bench_id"], db_session=db_session,
    )
    login(apps_client, "readonly@example.com")  # READ is enough to browse
    resp = apps_client.get(f"/api/store/catalog?site={api_env['site_id']}")
    assert resp.status_code == 200, resp.text
    by = {a["name"]: a for a in resp.json()}
    assert by["erpnext"]["is_installable"] is True
    assert by["erpnext"]["branch"] == "version-15"
    assert by["erpnext"]["installed"] is False
    assert by["hrms"]["is_installable"] is False
    assert ">=16.0.0" in by["hrms"]["incompatible_reason"]
    apps_client.app.dependency_overrides.pop(get_registry_cache, None)


def test_catalog_installed_flag_is_site_scoped(apps_client, api_env, db_session):
    # erpnext installed on THIS site → flagged installed; bench-wide would be the
    # same here, but the flag must track the site the catalog was asked for.
    appsources.upsert_installed_app(
        db_session, site_id=api_env["site_id"], bench_id=api_env["bench_id"],
        app_name="erpnext", branch="version-15", version="15.5.0",
    )
    _use_registry(apps_client, [_store_rec("erpnext", [_ERP15])],
                  bench_id=api_env["bench_id"], db_session=db_session)
    login(apps_client, "developer@example.com")
    resp = apps_client.get(f"/api/store/catalog?site={api_env['site_id']}")
    assert resp.status_code == 200, resp.text
    by = {a["name"]: a for a in resp.json()}
    assert by["erpnext"]["installed"] is True
    apps_client.app.dependency_overrides.pop(get_registry_cache, None)


def test_catalog_unknown_frappe_version_is_409(apps_client, api_env, db_session):
    _use_registry(apps_client, [_store_rec("erpnext", [_ERP15])])  # bench version left None
    login(apps_client, "developer@example.com")
    resp = apps_client.get(f"/api/store/catalog?site={api_env['site_id']}")
    assert resp.status_code == 409
    apps_client.app.dependency_overrides.pop(get_registry_cache, None)


def test_store_install_launches_job_with_pinned_branch(apps_client, api_env, db_session):
    _use_registry(apps_client, [_store_rec("erpnext", [_ERP15])],
                  bench_id=api_env["bench_id"], db_session=db_session)
    login(apps_client, "developer@example.com")
    resp = apps_client.post(
        f"/api/sites/{api_env['site_id']}/apps",
        json={"store_app": "erpnext"},
        headers=csrf_headers(apps_client),
    )
    assert resp.status_code == 201, resp.text
    p = resp.json()["params_sanitized"]
    assert p["app"] == "erpnext"
    assert p["source"] == "https://github.com/frappe/erpnext"
    assert p["branch"] == "version-15"  # pinned to the resolved release, not a default
    assert "dep_plan" not in p
    apps_client.app.dependency_overrides.pop(get_registry_cache, None)


def test_store_install_encodes_ordered_dependencies(apps_client, api_env, db_session):
    _use_registry(
        apps_client,
        [_store_rec("hrms", [_HRMS15]), _store_rec("erpnext", [_ERP15])],
        bench_id=api_env["bench_id"], db_session=db_session,
    )
    login(apps_client, "developer@example.com")
    resp = apps_client.post(
        f"/api/sites/{api_env['site_id']}/apps",
        json={"store_app": "hrms"},
        headers=csrf_headers(apps_client),
    )
    assert resp.status_code == 201, resp.text
    p = resp.json()["params_sanitized"]
    assert p["app"] == "hrms" and p["branch"] == "version-15"
    # erpnext (the dependency) is installed first, encoded app~source~branch.
    assert p["dep_plan"] == "erpnext~https://github.com/frappe/erpnext~version-15"
    apps_client.app.dependency_overrides.pop(get_registry_cache, None)


def test_store_install_already_installed_is_409(apps_client, api_env, db_session):
    appsources.upsert_installed_app(
        db_session, site_id=api_env["site_id"], bench_id=api_env["bench_id"],
        app_name="erpnext", branch="version-15", version="15.5.0",
    )
    _use_registry(apps_client, [_store_rec("erpnext", [_ERP15])],
                  bench_id=api_env["bench_id"], db_session=db_session)
    login(apps_client, "developer@example.com")
    resp = apps_client.post(
        f"/api/sites/{api_env['site_id']}/apps",
        json={"store_app": "erpnext"},
        headers=csrf_headers(apps_client),
    )
    assert resp.status_code == 409
    assert "already installed" in resp.text
    apps_client.app.dependency_overrides.pop(get_registry_cache, None)


def test_store_install_incompatible_app_is_422(apps_client, api_env, db_session):
    _use_registry(apps_client, [_store_rec("hrms", [_HRMS16])],
                  bench_id=api_env["bench_id"], db_session=db_session)
    login(apps_client, "developer@example.com")
    resp = apps_client.post(
        f"/api/sites/{api_env['site_id']}/apps",
        json={"store_app": "hrms"},
        headers=csrf_headers(apps_client),
    )
    assert resp.status_code == 422
    assert "compatible" in resp.text
    apps_client.app.dependency_overrides.pop(get_registry_cache, None)


def test_store_install_unknown_app_is_422(apps_client, api_env, db_session):
    _use_registry(apps_client, [_store_rec("erpnext", [_ERP15])],
                  bench_id=api_env["bench_id"], db_session=db_session)
    login(apps_client, "developer@example.com")
    resp = apps_client.post(
        f"/api/sites/{api_env['site_id']}/apps",
        json={"store_app": "nope"},
        headers=csrf_headers(apps_client),
    )
    assert resp.status_code == 422
    assert "catalog" in resp.text
    apps_client.app.dependency_overrides.pop(get_registry_cache, None)
