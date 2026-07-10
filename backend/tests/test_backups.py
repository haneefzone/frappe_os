"""Backups + guided restore (session 1.11): the artifact parser + integrity
helpers, the command templates (safe render), the backup engine + restore
orchestrator over in-memory fakes (dev Redis dance, pre-restore backup,
encryption_key copy + migrate — gotcha #7), and the API surface (RBAC, the
downgrade guard, the destructive-restore confirm, 409). No Redis/RQ/SSH."""

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.routes.jobs import get_job_runner
from app.core import backups as bk
from app.core.commands import RenderError, get_template, render
from app.core.commands.actions import _MODE_PROBE
from app.core.commands.templates import MASK
from app.core.jobs import CaptureResult, InMemoryJobBackend, JobRunner
from app.core.security import get_secrets_service
from app.db import Base
from app.models import CommandJob, CommandStep, Server
from app.models.backup import Backup
from app.models.bench import Bench
from app.models.site import Site
from tests.conftest import csrf_headers, login

BENCH_PATH = "/home/frappe/frappe-bench"
BK = f"{BENCH_PATH}/sites/test1.localhost/private/backups"
PFX = "20260710_101500-test1_localhost"
SHA_DB = "a" * 64
SHA_PUB = "b" * 64
SHA_PRIV = "c" * 64
SHA_CFG = "d" * 64

# A realistic ARTIFACT_INSPECT_SCRIPT output for a full (with-files) backup.
INSPECT_WITH_FILES = (
    f"ART\tdatabase.sql.gz\t{BK}/{PFX}-database.sql.gz\t1048576\t{SHA_DB}\n"
    f"ART\tfiles.tar\t{BK}/{PFX}-files.tar\t2048\t{SHA_PUB}\n"
    f"ART\tprivate-files.tar\t{BK}/{PFX}-private-files.tar\t512\t{SHA_PRIV}\n"
    f"ART\tsite_config_backup.json\t{BK}/{PFX}-site_config_backup.json\t256\t{SHA_CFG}\n"
)
INSPECT_DB_ONLY = f"ART\tdatabase.sql.gz\t{BK}/{PFX}-database.sql.gz\t1048576\t{SHA_DB}\n"
CONFIG_JSON = '{"db_name": "x", "encryption_key": "SECRETKEY123456"}'


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #


def test_parse_artifacts_full():
    parsed = bk.parse_artifacts(INSPECT_WITH_FILES)
    kinds = {a.kind for a in parsed.artifacts}
    assert kinds == {"database", "public_files", "private_files", "config"}
    assert parsed.has_files
    assert parsed.total_size == 1048576 + 2048 + 512 + 256
    db = parsed.by_kind("database")
    assert db.checksum_sha256 == SHA_DB
    assert db.path.endswith("-database.sql.gz")


def test_parse_artifacts_db_only_has_no_files():
    parsed = bk.parse_artifacts(INSPECT_DB_ONLY)
    assert not parsed.has_files
    assert [a.kind for a in parsed.artifacts] == ["database"]


def test_parse_artifacts_no_backup_raises():
    with pytest.raises(bk.BackupError):
        bk.parse_artifacts("NO_BACKUP\n")


def test_parse_artifacts_empty_raises():
    with pytest.raises(bk.BackupError):
        bk.parse_artifacts("some noise\n")


def test_parse_checksums_and_verify():
    stdout = f"SUM\t{BK}/{PFX}-database.sql.gz\t{SHA_DB}\nSUM\t/missing\tMISSING\n"
    recomputed = bk.parse_checksums(stdout)
    assert recomputed[f"{BK}/{PFX}-database.sql.gz"] == SHA_DB
    assert recomputed["/missing"] is None


def test_parse_encryption_key():
    assert bk.parse_encryption_key(CONFIG_JSON) == "SECRETKEY123456"
    assert bk.parse_encryption_key('{"db_name":"x"}') is None
    assert bk.parse_encryption_key("not json") is None


@pytest.mark.parametrize(
    "src,tgt,ok",
    [
        ("16.2.0", "16.5.0", True),   # same major
        ("15.0.0", "16.0.0", True),   # forward (older onto newer) is allowed
        ("16.0.0", "15.0.0", False),  # downgrade blocked (gotcha #7)
        (None, "16.0.0", True),       # unknown never blocks
        ("16.0.0", None, True),
    ],
)
def test_restore_compatibility(src, tgt, ok):
    assert bk.check_restore_compatibility(src, tgt).ok is ok


# --------------------------------------------------------------------------- #
# Templates render safely
# --------------------------------------------------------------------------- #


def test_backup_templates_render():
    db = render(
        get_template("site.backup_db"),
        {"site": "test1.localhost", "bench_path": BENCH_PATH},
    )
    assert db.argv == ["bench", "--site", "test1.localhost", "backup"]
    files = render(
        get_template("site.backup_files"),
        {"site": "test1.localhost", "bench_path": BENCH_PATH},
    )
    assert files.argv == ["bench", "--site", "test1.localhost", "backup", "--with-files"]


def test_restore_templates_render():
    rdb = render(
        get_template("site.restore_db"),
        {"site": "test2.localhost", "bench_path": BENCH_PATH, "db_path": f"{BK}/db.sql.gz"},
    )
    assert rdb.argv == [
        "bench", "--site", "test2.localhost", "--force", "restore", f"{BK}/db.sql.gz",
    ]
    rf = render(
        get_template("site.restore_files"),
        {
            "site": "test2.localhost",
            "bench_path": BENCH_PATH,
            "db_path": f"{BK}/db.sql.gz",
            "public_files": f"{BK}/files.tar",
            "private_files": f"{BK}/private-files.tar",
        },
    )
    assert "--with-public-files" in rf.argv and "--with-private-files" in rf.argv


def test_set_encryption_key_masks_secret():
    rc = render(
        get_template("site.set_encryption_key"),
        {"site": "test2.localhost", "bench_path": BENCH_PATH, "key": "SECRETKEY123456"},
    )
    assert rc.argv[-1] == "SECRETKEY123456"  # real value in argv
    assert MASK in rc.display and "SECRETKEY123456" not in rc.display  # masked in display


@pytest.mark.parametrize("bad", ["../etc/passwd", "/a/../b"])
def test_restore_db_path_rejects_dotdot(bad):
    with pytest.raises(RenderError):
        render(
            get_template("site.restore_db"),
            {"site": "t.localhost", "bench_path": BENCH_PATH, "db_path": bad},
        )


def test_backup_with_files_enum_rejects_other():
    with pytest.raises(RenderError):
        render(
            get_template("site.backup"),
            {"site": "t.localhost", "bench_path": BENCH_PATH, "with_files": "yes"},
        )


# --------------------------------------------------------------------------- #
# Fakes + harness
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


class BackupExecutor:
    """Fake executor: `capture` answers the dev/prod probe, the artifact-inspect
    and checksum-verify scripts, and `cat <config>`; `run` records streamed argv."""

    def __init__(self, *, prod=False, inspect=INSPECT_WITH_FILES, config=CONFIG_JSON, fail_on=None):
        self._prod = prod
        self._inspect = inspect
        self._config = config
        self._fail_on = fail_on
        self.streamed: list[list[str]] = []

    async def capture(self, argv, *, cwd=None, timeout=120.0):
        if argv[:2] == ["bash", "-c"]:
            script = argv[2]
            if script == _MODE_PROBE:
                return CaptureResult(0, "PROD" if self._prod else "DEV", "")
            if script == bk.ARTIFACT_INSPECT_SCRIPT:
                return CaptureResult(0, self._inspect, "")
            if script == bk.CHECKSUM_VERIFY_SCRIPT:
                # Echo back the stored sha for each path arg (argv[4:] are paths).
                lines = "".join(f"SUM\t{p}\t{_sha_for(p)}\n" for p in argv[4:])
                return CaptureResult(0, lines, "")
        if argv[:1] == ["cat"]:
            return CaptureResult(0, self._config, "")
        raise AssertionError(f"unexpected capture {argv}")

    async def run(self, argv, *, cwd, run_as, on_line, cancel_check):
        self.streamed.append(list(argv))
        if self._fail_on is not None and self._fail_on(argv):
            return 1
        return 0


def _sha_for(path: str) -> str:
    if path.endswith("-database.sql.gz"):
        return SHA_DB
    if path.endswith("-files.tar"):
        return SHA_PUB
    if path.endswith("-private-files.tar"):
        return SHA_PRIV
    return SHA_CFG


def fake_factory(executor):
    class _CM:
        async def __aenter__(self):
            return executor

        async def __aexit__(self, *exc):
            return False

    return lambda server: _CM()


def _setup(db, *, root_pw=True, version="16.2.0"):
    s = Server(name="vm", hostname="10.0.0.9")
    if root_pw:
        s.mariadb_root_password_enc = get_secrets_service().encrypt("rootpw")
    db.add(s)
    db.commit()
    bench = Bench(
        server_id=s.id, path=BENCH_PATH, name="frappe-bench",
        frappe_version=version, redis_queue_port=11000, redis_cache_port=13000,
    )
    db.add(bench)
    db.commit()
    site = Site(bench_id=bench.id, name="test1.localhost", status="active")
    db.add(site)
    db.commit()
    return s.id, bench.id, site.id


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


def _steps(sf, job_id):
    with sf() as db:
        return [
            s.name
            for s in db.scalars(
                select(CommandStep).where(CommandStep.job_id == job_id).order_by(CommandStep.order)
            ).all()
        ]


# --------------------------------------------------------------------------- #
# Backup engine
# --------------------------------------------------------------------------- #


def test_backup_engine_records_artifacts(sf):
    with sf() as db:
        server_id, bench_id, site_id = _setup(db)
        row = bk.create_pending_backup(
            db, site_id=site_id, bench_id=bench_id,
            backup_type="with-files", taken_by_job_id=None,
        )
        backup_id = row.id
    ex = BackupExecutor(prod=False)
    job_id = _run(
        sf, action="site.backup", server_id=server_id,
        target_id=f"{BENCH_PATH}::test1.localhost",
        params={
            "site": "test1.localhost", "bench_path": BENCH_PATH,
            "with_files": "1", "backup_id": str(backup_id),
        },
        executor=ex,
    )
    with sf() as db:
        assert db.get(CommandJob, job_id).status == "success"
        b = db.get(Backup, backup_id)
        assert b.status == "success"
        assert b.type == "with-files"
        assert b.db_path.endswith("-database.sql.gz")
        assert b.config_path.endswith("-site_config_backup.json")
        assert b.size_bytes == 1048576 + 2048 + 512 + 256
        assert b.frappe_version == "16.2.0"
        assert len(b.artifacts) == 4
    # Full backup command ran, wrapped in the dev Redis dance.
    assert ["bench", "--site", "test1.localhost", "backup", "--with-files"] in ex.streamed
    assert [a[0] for a in ex.streamed][:2] == ["redis-server", "redis-server"]
    assert [a[0] for a in ex.streamed][-2:] == ["redis-cli", "redis-cli"]


def test_backup_failure_marks_row_failed(sf):
    with sf() as db:
        server_id, bench_id, site_id = _setup(db)
        row = bk.create_pending_backup(
            db, site_id=site_id, bench_id=bench_id,
            backup_type="db", taken_by_job_id=None,
        )
        backup_id = row.id
    ex = BackupExecutor(prod=False, fail_on=lambda a: a[:2] == ["bench", "--site"])
    job_id = _run(
        sf, action="site.backup", server_id=server_id,
        target_id=f"{BENCH_PATH}::test1.localhost",
        params={
            "site": "test1.localhost", "bench_path": BENCH_PATH,
            "with_files": "0", "backup_id": str(backup_id),
        },
        executor=ex,
    )
    with sf() as db:
        assert db.get(CommandJob, job_id).status == "failure"
        assert db.get(Backup, backup_id).status == "failed"
    # The Redis was still shut down afterwards.
    assert [a[0] for a in ex.streamed][-2:] == ["redis-cli", "redis-cli"]


# --------------------------------------------------------------------------- #
# Validate
# --------------------------------------------------------------------------- #


def test_validate_all_ok(sf):
    with sf() as db:
        server_id, bench_id, site_id = _setup(db)
        row = Backup(
            site_id=site_id, bench_id=bench_id, type="with-files", status="success",
            db_path=f"{BK}/{PFX}-database.sql.gz",
            artifacts=[
                {"kind": "database", "path": f"{BK}/{PFX}-database.sql.gz",
                 "size_bytes": 1, "checksum_sha256": SHA_DB},
                {"kind": "config", "path": f"{BK}/{PFX}-site_config_backup.json",
                 "size_bytes": 1, "checksum_sha256": SHA_CFG},
            ],
        )
        db.add(row)
        db.commit()
        backup_id = row.id
    ex = BackupExecutor()
    job_id = _run(
        sf, action="backup.validate", server_id=server_id,
        target_id=f"{BENCH_PATH}::test1.localhost",
        params={"backup_id": str(backup_id), "site": "test1.localhost", "bench_path": BENCH_PATH},
        executor=ex,
    )
    with sf() as db:
        assert db.get(CommandJob, job_id).status == "success"


# --------------------------------------------------------------------------- #
# Restore: same-site (destructive) — pre-restore backup + encryption_key + migrate
# --------------------------------------------------------------------------- #


def _success_backup(db, site_id, bench_id):
    row = Backup(
        site_id=site_id, bench_id=bench_id, type="with-files", status="success",
        frappe_version="16.2.0",
        db_path=f"{BK}/{PFX}-database.sql.gz",
        public_files_path=f"{BK}/{PFX}-files.tar",
        private_files_path=f"{BK}/{PFX}-private-files.tar",
        config_path=f"{BK}/{PFX}-site_config_backup.json",
        artifacts=[
            {"kind": "database", "path": f"{BK}/{PFX}-database.sql.gz",
             "size_bytes": 1, "checksum_sha256": SHA_DB}
        ],
    )
    db.add(row)
    db.commit()
    return row.id


def test_restore_same_site_pre_backup_key_migrate(sf):
    with sf() as db:
        server_id, bench_id, site_id = _setup(db)
        backup_id = _success_backup(db, site_id, bench_id)
    ex = BackupExecutor(prod=False)
    job_id = _run(
        sf, action="site.restore", server_id=server_id,
        target_id=f"{BENCH_PATH}::test1.localhost",
        params={
            "site": "test1.localhost", "bench_path": BENCH_PATH, "mode": "same_site",
            "with_files": "1", "backup_id": str(backup_id),
            "db_path": f"{BK}/{PFX}-database.sql.gz",
            "public_files": f"{BK}/{PFX}-files.tar",
            "private_files": f"{BK}/{PFX}-private-files.tar",
            "config_path": f"{BK}/{PFX}-site_config_backup.json",
        },
        executor=ex,
    )
    with sf() as db:
        assert db.get(CommandJob, job_id).status == "success"
        # Source backup is now restore-tested.
        assert db.get(Backup, backup_id).restore_tested is True
        # An automatic pre-restore backup row was created (2 rows now).
        rows = db.scalars(select(Backup)).all()
        assert len(rows) == 2
        pre = [r for r in rows if r.id != backup_id][0]
        assert pre.taken_by_job_id == job_id
        assert pre.status == "success"
    labels = _steps(sf, job_id)
    # Pre-restore backup strictly before the restore (acceptance).
    pre_i = next(i for i, n in enumerate(labels) if n.startswith("Pre-restore backup"))
    restore_i = next(i for i, n in enumerate(labels) if n.startswith("Restore"))
    assert pre_i < restore_i
    # encryption_key copied (gotcha #7), then migrate.
    assert [
        "bench", "--site", "test1.localhost", "set-config", "encryption_key", "SECRETKEY123456",
    ] in ex.streamed
    assert ["bench", "--site", "test1.localhost", "migrate"] in ex.streamed
    # Restore ran with --force + files.
    restore = next(a for a in ex.streamed if "restore" in a)
    assert "--force" in restore and "--with-public-files" in restore


def test_restore_aborts_when_pre_backup_fails(sf):
    with sf() as db:
        server_id, bench_id, site_id = _setup(db)
        backup_id = _success_backup(db, site_id, bench_id)
    # Fail the pre-restore backup command (bench backup --with-files).
    ex = BackupExecutor(
        prod=False, fail_on=lambda a: a[:2] == ["bench", "--site"] and "backup" in a
    )
    job_id = _run(
        sf, action="site.restore", server_id=server_id,
        target_id=f"{BENCH_PATH}::test1.localhost",
        params={
            "site": "test1.localhost", "bench_path": BENCH_PATH, "mode": "same_site",
            "with_files": "1", "backup_id": str(backup_id),
            "db_path": f"{BK}/{PFX}-database.sql.gz",
            "config_path": f"{BK}/{PFX}-site_config_backup.json",
        },
        executor=ex,
    )
    with sf() as db:
        assert db.get(CommandJob, job_id).status == "failure"
    # The restore itself never ran.
    assert not any("restore" in a for a in ex.streamed)


# --------------------------------------------------------------------------- #
# Restore: new-site
# --------------------------------------------------------------------------- #


def test_restore_new_site_creates_then_restores(sf):
    with sf() as db:
        server_id, bench_id, site_id = _setup(db)
        backup_id = _success_backup(db, site_id, bench_id)
    ex = BackupExecutor(prod=False)
    job_id = _run(
        sf, action="site.restore", server_id=server_id,
        target_id=f"{BENCH_PATH}::test2.localhost",
        params={
            "site": "test2.localhost", "bench_path": BENCH_PATH, "mode": "new_site",
            "with_files": "1", "backup_id": str(backup_id),
            "db_path": f"{BK}/{PFX}-database.sql.gz",
            "public_files": f"{BK}/{PFX}-files.tar",
            "private_files": f"{BK}/{PFX}-private-files.tar",
            "config_path": f"{BK}/{PFX}-site_config_backup.json",
        },
        executor=ex,
        user_secrets={"admin_pw": "AdminPw123!"},
    )
    with sf() as db:
        assert db.get(CommandJob, job_id).status == "success"
        # Target site registered.
        assert db.scalars(select(Site).where(Site.name == "test2.localhost")).first() is not None
        # No pre-restore backup for a fresh site (still just the source backup).
        assert len(db.scalars(select(Backup)).all()) == 1
    # new-site ran; no pre-restore "Pre-restore backup" step.
    assert any(a[:2] == ["bench", "new-site"] for a in ex.streamed)
    assert not any(n.startswith("Pre-restore backup") for n in _steps(sf, job_id))


# --------------------------------------------------------------------------- #
# API surface
# --------------------------------------------------------------------------- #


@pytest.fixture
def bk_client(client, db_session):
    runner = JobRunner(
        lambda: db_session, InMemoryJobBackend(), enqueue=lambda job: None,
        secrets=get_secrets_service(),
    )
    client.app.dependency_overrides[get_job_runner] = lambda: runner
    yield client
    client.app.dependency_overrides.pop(get_job_runner, None)


@pytest.fixture
def api_env(db_session):
    s = Server(name="vm-a", hostname="10.0.0.1")
    s.mariadb_root_password_enc = get_secrets_service().encrypt("rootpw")
    db_session.add(s)
    db_session.commit()
    bench = Bench(server_id=s.id, path=BENCH_PATH, name="frappe-bench", frappe_version="16.2.0")
    db_session.add(bench)
    db_session.commit()
    site = Site(bench_id=bench.id, name="test1.localhost", status="active")
    db_session.add(site)
    db_session.commit()
    return {"server_id": s.id, "bench_id": bench.id, "site_id": site.id}


def _api_backup(db_session, env, **kw):
    row = Backup(
        site_id=env["site_id"], bench_id=env["bench_id"], type="with-files",
        status=kw.get("status", "success"), frappe_version=kw.get("version", "16.2.0"),
        db_path=f"{BK}/{PFX}-database.sql.gz",
        config_path=f"{BK}/{PFX}-site_config_backup.json",
        artifacts=[
            {"kind": "database", "path": f"{BK}/{PFX}-database.sql.gz",
             "size_bytes": 1, "checksum_sha256": SHA_DB}
        ],
    )
    db_session.add(row)
    db_session.commit()
    return row.id


def test_create_backup_makes_pending_row_and_job(bk_client, db_session, api_env):
    login(bk_client, "developer@example.com")
    resp = bk_client.post(
        f"/api/sites/{api_env['site_id']}/backups",
        json={"with_files": True},
        headers=csrf_headers(bk_client),
    )
    assert resp.status_code == 201, resp.text
    rows = db_session.scalars(select(Backup)).all()
    assert len(rows) == 1 and rows[0].status == "pending"
    assert rows[0].taken_by_job_id == resp.json()["id"]


def test_create_backup_denied_for_readonly(bk_client, api_env):
    login(bk_client, "readonly@example.com")
    resp = bk_client.post(
        f"/api/sites/{api_env['site_id']}/backups", json={}, headers=csrf_headers(bk_client)
    )
    assert resp.status_code == 403


def test_list_and_filter_backups(bk_client, db_session, api_env):
    _api_backup(db_session, api_env)
    login(bk_client, "readonly@example.com")
    resp = bk_client.get("/api/backups")
    assert resp.status_code == 200
    item = resp.json()[0]
    assert item["site_name"] == "test1.localhost"
    assert "database" in item["available_artifacts"]
    # restore_tested filter
    assert bk_client.get("/api/backups?restore_tested=true").json() == []


def test_compatibility_blocks_downgrade(bk_client, db_session, api_env):
    backup_id = _api_backup(db_session, api_env, version="16.0.0")
    # target bench is v15 -> downgrade
    older = Bench(
        server_id=api_env["server_id"], path="/home/frappe/b15",
        name="b15", frappe_version="15.0.0",
    )
    db_session.add(older)
    db_session.commit()
    login(bk_client, "developer@example.com")
    resp = bk_client.get(f"/api/restores/compatibility?backup_id={backup_id}&bench_id={older.id}")
    assert resp.status_code == 200
    assert resp.json()["ok"] is False


def test_restore_same_site_requires_confirm(bk_client, db_session, api_env):
    # A restore OVER an existing site is destructive -> needs `danger` (admin,
    # same gate as app.uninstall) + the typed target-site-name confirm.
    backup_id = _api_backup(db_session, api_env)
    login(bk_client, "admin@example.com")
    # Missing confirm_name -> 422
    resp = bk_client.post(
        "/api/restores",
        json={"mode": "same_site", "backup_id": backup_id},
        headers=csrf_headers(bk_client),
    )
    assert resp.status_code == 422
    # Correct confirm -> 201
    ok = bk_client.post(
        "/api/restores",
        json={
            "mode": "same_site",
            "backup_id": backup_id,
            "confirm_name": "test1.localhost",
        },
        headers=csrf_headers(bk_client),
    )
    assert ok.status_code == 201, ok.text


def test_restore_over_existing_denied_without_danger(bk_client, db_session, api_env):
    # Developer has backup:restore (for new-site restores) but NOT danger, so a
    # destructive restore-over-existing is refused for them.
    backup_id = _api_backup(db_session, api_env)
    login(bk_client, "developer@example.com")
    resp = bk_client.post(
        "/api/restores",
        json={
            "mode": "same_site",
            "backup_id": backup_id,
            "confirm_name": "test1.localhost",
        },
        headers=csrf_headers(bk_client),
    )
    assert resp.status_code == 403


def test_restore_new_site_requires_admin_password(bk_client, db_session, api_env):
    backup_id = _api_backup(db_session, api_env)
    login(bk_client, "developer@example.com")
    resp = bk_client.post(
        "/api/restores",
        json={
            "mode": "new_site", "backup_id": backup_id,
            "target_bench_id": api_env["bench_id"], "target_site_name": "test2.localhost",
        },
        headers=csrf_headers(bk_client),
    )
    assert resp.status_code == 422
    assert "admin_password" in resp.text


def test_restore_over_existing_denied_for_operator(bk_client, db_session, api_env):
    # Read-only can't restore at all (needs backup:restore); assert 403.
    backup_id = _api_backup(db_session, api_env)
    login(bk_client, "readonly@example.com")
    resp = bk_client.post(
        "/api/restores",
        json={"mode": "same_site", "backup_id": backup_id, "confirm_name": "test1.localhost"},
        headers=csrf_headers(bk_client),
    )
    assert resp.status_code == 403
