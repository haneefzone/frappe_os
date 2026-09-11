"""Scheduled restore-test automation (session 3.4): restore a backup into an
ephemeral scratch site → verify → ALWAYS destroy the scratch → stamp the
restore-tested badge.

Covers template rendering/injection, the RestoreTestAction end to end over
in-memory fakes (no Redis/RQ/SSH) — the happy path, the destroy-on-failure path
(QA's focus), a verification failure, and the disk preflight — plus the pure
selection (`select_due`) + result-recording logic and the dashboard feed.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core import restore_tests as rt
from app.core.commands import RenderError, get_template, render
from app.core.commands.actions import _MODE_PROBE, _parse_df_avail_kb
from app.core.jobs import CaptureResult, InMemoryJobBackend, JobRunner
from app.core.security import get_secrets_service
from app.db import Base
from app.models import CommandJob, Server
from app.models.backup import Backup
from app.models.bench import Bench
from app.models.compliance import BackupPolicy
from app.models.site import Site

BENCH_PATH = "/home/frappe/frappe-bench"
SOURCE = "test1.localhost"
BK = f"{BENCH_PATH}/sites/{SOURCE}/private/backups"
DB_PATH = f"{BK}/20260911_000000-test1_localhost-database.sql.gz"
CFG_PATH = f"{BK}/20260911_000000-test1_localhost-site_config_backup.json"
CONFIG_JSON = '{"db_name": "x", "encryption_key": "SECRETKEY123456"}'
# df -Pk: Filesystem 1024-blocks Used Available Capacity Mounted-on
_DF_HDR = "Filesystem 1024-blocks Used Available Capacity Mounted on\n"
DF_ROOMY = _DF_HDR + "/dev/sda1 10000000 100000 9000000 2% /\n"
DF_FULL = _DF_HDR + "/dev/sda1 10000000 9999000 1000 99% /\n"


# --------------------------------------------------------------------------- #
# Template rendering + injection safety
# --------------------------------------------------------------------------- #


def test_disk_free_and_drop_templates_render():
    assert render(get_template("server.disk_free"), {"path": BENCH_PATH}).argv == [
        "df", "-Pk", BENCH_PATH
    ]
    rc = render(
        get_template("site.drop"),
        {"site": "scratch.localhost", "bench_path": BENCH_PATH, "db_root_pw": "rootpw"},
    )
    assert rc.argv == [
        "bench", "drop-site", "scratch.localhost", "--force", "--no-backup",
        "--root-login", "root", "--root-password", "rootpw",
    ]
    # The root password is masked in the display, never echoed.
    assert "rootpw" not in rc.display and "••••" in rc.display


def test_drop_site_rejects_injection():
    with pytest.raises(RenderError):
        render(
            get_template("site.drop"),
            {"site": "a;rm -rf /", "bench_path": BENCH_PATH, "db_root_pw": "x"},
        )


def test_restore_test_template_is_locked_and_nonidempotent():
    t = get_template("backup.restore_test")
    assert t.requires_lock is True and t.idempotent is False
    # site.drop is destructive → danger permission as a standalone action.
    assert get_template("site.drop").required_permission == "danger"


def test_parse_df_avail_kb():
    assert _parse_df_avail_kb(DF_ROOMY) == 9000000
    assert _parse_df_avail_kb(DF_FULL) == 1000
    assert _parse_df_avail_kb("") is None
    assert _parse_df_avail_kb(None) is None


# --------------------------------------------------------------------------- #
# select_due (pure selection) + record_result
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


def _world(db):
    s = Server(name="vm", hostname="10.0.0.9")
    s.mariadb_root_password_enc = get_secrets_service().encrypt("rootpw")
    db.add(s)
    db.commit()
    bench = Bench(server_id=s.id, path=BENCH_PATH, name="frappe-bench",
                  frappe_version="16.2.0", redis_queue_port=11000, redis_cache_port=13000)
    db.add(bench)
    db.commit()
    site = Site(bench_id=bench.id, name=SOURCE, status="active", environment="prod")
    db.add(site)
    db.commit()
    return s.id, bench.id, site.id


def _backup(db, site_id, bench_id, *, status="success", db_path=DB_PATH,
            size_bytes=1_000_000, created_at=None, tested_at=None):
    b = Backup(site_id=site_id, bench_id=bench_id, type="db", status=status,
               db_path=db_path, config_path=CFG_PATH, size_bytes=size_bytes,
               frappe_version="16")
    if created_at:
        b.created_at = created_at
    if tested_at:
        b.restore_tested_at = tested_at
    db.add(b)
    db.commit()
    return b


def _policy(db, site_id, *, require=True, interval=7, enabled=True):
    p = BackupPolicy(site_id=site_id, rpo_hours=24, require_restore_test=require,
                     restore_test_interval_days=interval, enabled=enabled)
    db.add(p)
    db.commit()
    if interval is None:
        # The column has a Python + server default of 7, so force a real NULL to
        # exercise the "on-demand only" (sweep-skips) path.
        p.restore_test_interval_days = None
        db.commit()
    return p


def test_select_due_picks_never_tested_backup(sf):
    with sf() as db:
        _, bench_id, site_id = _world(db)
        _policy(db, site_id)
        b = _backup(db, site_id, bench_id)
        due = rt.select_due(db, now=datetime.now(UTC))
        assert [d.backup.id for d in due] == [b.id]


def test_select_due_skips_recently_tested(sf):
    with sf() as db:
        _, bench_id, site_id = _world(db)
        _policy(db, site_id, interval=7)
        _backup(db, site_id, bench_id, tested_at=datetime.now(UTC) - timedelta(days=2))
        assert rt.select_due(db, now=datetime.now(UTC)) == []


def test_select_due_includes_stale_test(sf):
    with sf() as db:
        _, bench_id, site_id = _world(db)
        _policy(db, site_id, interval=7)
        b = _backup(db, site_id, bench_id,
                    tested_at=datetime.now(UTC) - timedelta(days=10))
        due = rt.select_due(db, now=datetime.now(UTC))
        assert [d.backup.id for d in due] == [b.id]


def test_select_due_skips_when_require_flag_off(sf):
    with sf() as db:
        _, bench_id, site_id = _world(db)
        _policy(db, site_id, require=False)  # require_restore_test off → never due
        _backup(db, site_id, bench_id)
        assert rt.select_due(db, now=datetime.now(UTC)) == []


def test_select_due_skips_null_interval(sf):
    with sf() as db:
        _, bench_id, site_id = _world(db)
        _policy(db, site_id, interval=None)  # on-demand only → sweep skips it
        _backup(db, site_id, bench_id)
        assert rt.select_due(db, now=datetime.now(UTC)) == []


def test_select_due_skips_disabled_policy(sf):
    with sf() as db:
        _, bench_id, site_id = _world(db)
        _policy(db, site_id, enabled=False)
        _backup(db, site_id, bench_id)
        assert rt.select_due(db, now=datetime.now(UTC)) == []


def test_select_due_needs_a_successful_backup(sf):
    with sf() as db:
        _, bench_id, site_id = _world(db)
        _policy(db, site_id)
        _backup(db, site_id, bench_id, status="failed")
        assert rt.select_due(db, now=datetime.now(UTC)) == []


def test_record_result_stamps_badge(sf):
    with sf() as db:
        _, bench_id, site_id = _world(db)
        b = _backup(db, site_id, bench_id)
        now = datetime.now(UTC)
        rt.record_result(db, b, passed=True, detail="all green", now=now, job_id=7)
        assert b.restore_test_status == "passed" and b.restore_tested is True
        assert b.restore_test_job_id == 7 and b.restore_test_detail == "all green"
        rt.record_result(db, b, passed=False, detail="boom", now=now, job_id=8)
        assert b.restore_test_status == "failed" and b.restore_tested is False
        assert rt.failed_backups(db) == [b]


# --------------------------------------------------------------------------- #
# RestoreTestAction end to end (in-memory fakes)
# --------------------------------------------------------------------------- #


class RTExecutor:
    """`capture` answers the dev/prod probe, `cat <config>`, `df`, and the verify
    probes (ping/scheduler/get_count). `run` records every streamed argv and can
    be told to fail a chosen command."""

    def __init__(self, *, prod=True, ping_ok=True, scheduler_ok=True,
                 counts=None, df=DF_ROOMY, fail_on=None):
        self._prod = prod
        self._ping_ok = ping_ok
        self._scheduler_ok = scheduler_ok
        self._counts = counts or {}
        self._df = df
        self._fail_on = fail_on
        self.streamed: list[list[str]] = []

    async def capture(self, argv, *, cwd=None, timeout=120.0):
        if argv[:2] == ["bash", "-c"] and argv[2] == _MODE_PROBE:
            return CaptureResult(0, "PROD" if self._prod else "DEV", "")
        if argv[:1] == ["cat"]:
            return CaptureResult(0, CONFIG_JSON, "")
        if argv[:1] == ["df"]:
            return CaptureResult(0, self._df, "")
        if argv[:1] == ["bench"] and argv[3:5] == ["execute", "frappe.ping"]:
            return CaptureResult(0 if self._ping_ok else 1,
                                 "pong" if self._ping_ok else "Traceback", "")
        if argv[:1] == ["bench"] and argv[3:4] == ["scheduler"]:
            return CaptureResult(0, "Scheduler is enabled for site" if self._scheduler_ok
                                 else "Scheduler is disabled for site", "")
        if argv[:1] == ["bench"] and argv[4:5] == ["frappe.client.get_count"]:
            return CaptureResult(0, str(self._counts.get(argv[2], 100)), "")
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


def _run(sf, *, server_id, params, executor, user_secrets=None):
    runner = JobRunner(
        sf, InMemoryJobBackend(), enqueue=lambda job: None, secrets=get_secrets_service()
    )
    with sf() as db:
        job = runner.create(
            db, action_name="backup.restore_test", server_id=server_id,
            target_type="site",
            target_id=f"{BENCH_PATH}::restore-test::{SOURCE}",
            params=params, priority="default", created_by=None,
            user_secrets=user_secrets or {"admin_pw": "adminpass"},
        )
        job_id = job.id
    runner.run_job(job_id, executor_factory=fake_factory(executor))
    return job_id


def _dropped_sites(ex):
    return [a[2] for a in ex.streamed if a[:2] == ["bench", "drop-site"]]


def test_restore_test_happy_path_creates_verifies_and_destroys_scratch(sf):
    with sf() as db:
        server_id, bench_id, site_id = _world(db)
        b = _backup(db, site_id, bench_id)
        bid = b.id
    ex = RTExecutor(counts={SOURCE: 100})  # scratch defaults to 100 too → sane
    job_id = _run(
        sf, server_id=server_id,
        params={"site": SOURCE, "bench_path": BENCH_PATH, "backup_id": str(bid)},
        executor=ex,
    )
    with sf() as db:
        assert db.get(CommandJob, job_id).status == "success"
        b = db.get(Backup, bid)
        assert b.restore_test_status == "passed"
        assert b.restore_tested is True and b.restore_tested_at is not None
    # A scratch site was created, restored, migrated, then DESTROYED.
    assert any(a[:2] == ["bench", "new-site"] for a in ex.streamed)
    assert any("restore" in a for a in ex.streamed)
    dropped = _dropped_sites(ex)
    assert len(dropped) == 1
    scratch = dropped[0]
    # The scratch is an rt-* site, and the SOURCE site is NEVER dropped.
    assert scratch.startswith("rt-") and scratch != SOURCE
    # new-site + restore all target the scratch, never the source.
    assert all(SOURCE not in a for a in ex.streamed if a[:2] == ["bench", "new-site"])


def test_restore_test_destroys_scratch_even_when_restore_fails(sf):
    """QA's focus: induce a restore failure → badge FAILED, scratch STILL
    destroyed (finally), job fails loudly, no orphaned site."""
    with sf() as db:
        server_id, bench_id, site_id = _world(db)
        b = _backup(db, site_id, bench_id)
        bid = b.id
    ex = RTExecutor(fail_on=lambda a: "restore" in a)  # the restore step fails
    job_id = _run(
        sf, server_id=server_id,
        params={"site": SOURCE, "bench_path": BENCH_PATH, "backup_id": str(bid)},
        executor=ex,
    )
    with sf() as db:
        assert db.get(CommandJob, job_id).status == "failure"
        b = db.get(Backup, bid)
        assert b.restore_test_status == "failed"
        assert b.restore_tested is False and b.restore_tested_at is not None
    # Scratch was created (new-site ran) AND destroyed despite the failure.
    assert any(a[:2] == ["bench", "new-site"] for a in ex.streamed)
    assert len(_dropped_sites(ex)) == 1


def test_restore_test_fails_when_scratch_does_not_boot(sf):
    with sf() as db:
        server_id, bench_id, site_id = _world(db)
        b = _backup(db, site_id, bench_id)
        bid = b.id
    ex = RTExecutor(ping_ok=False)  # scratch never boots
    job_id = _run(
        sf, server_id=server_id,
        params={"site": SOURCE, "bench_path": BENCH_PATH, "backup_id": str(bid)},
        executor=ex,
    )
    with sf() as db:
        assert db.get(CommandJob, job_id).status == "failure"
        assert db.get(Backup, bid).restore_test_status == "failed"
    assert len(_dropped_sites(ex)) == 1  # still destroyed


def test_restore_test_flags_gross_row_loss(sf):
    with sf() as db:
        server_id, bench_id, site_id = _world(db)
        b = _backup(db, site_id, bench_id)
        bid = b.id
    # scratch restored with far fewer rows than source → data-loss flag.
    ex = RTExecutor(counts={SOURCE: 1000})  # scratch defaults to 100 << 1000/2
    _run(
        sf, server_id=server_id,
        params={"site": SOURCE, "bench_path": BENCH_PATH, "backup_id": str(bid)},
        executor=ex,
    )
    with sf() as db:
        assert db.get(Backup, bid).restore_test_status == "failed"
    assert len(_dropped_sites(ex)) == 1


def test_restore_test_disk_preflight_refuses_without_creating_scratch(sf):
    with sf() as db:
        server_id, bench_id, site_id = _world(db)
        # 5 GB backup, only ~1 MB free → preflight fails before any site work.
        b = _backup(db, site_id, bench_id, size_bytes=5_000_000_000)
        bid = b.id
    ex = RTExecutor(df=DF_FULL)
    job_id = _run(
        sf, server_id=server_id,
        params={"site": SOURCE, "bench_path": BENCH_PATH, "backup_id": str(bid)},
        executor=ex,
    )
    with sf() as db:
        assert db.get(CommandJob, job_id).status == "failure"
        b = db.get(Backup, bid)
        assert b.restore_test_status == "failed"
        assert "disk preflight" in (b.restore_test_detail or "")
    # No scratch was created (nothing to destroy) — preflight stopped first.
    assert not any(a[:2] == ["bench", "new-site"] for a in ex.streamed)
    assert _dropped_sites(ex) == []


def test_restore_test_picks_latest_backup_when_id_omitted(sf):
    with sf() as db:
        server_id, bench_id, site_id = _world(db)
        _backup(db, site_id, bench_id, created_at=datetime(2026, 1, 1, tzinfo=UTC))
        newer = _backup(db, site_id, bench_id,
                        created_at=datetime(2026, 9, 1, tzinfo=UTC))
        newer_id = newer.id
    ex = RTExecutor()
    _run(
        sf, server_id=server_id,
        params={"site": SOURCE, "bench_path": BENCH_PATH},  # no backup_id
        executor=ex,
    )
    with sf() as db:
        assert db.get(Backup, newer_id).restore_test_status == "passed"


def test_restore_test_never_targets_source_site(sf):
    with sf() as db:
        server_id, bench_id, site_id = _world(db)
        b = _backup(db, site_id, bench_id)
        bid = b.id
    ex = RTExecutor()
    job_id = _run(
        sf, server_id=server_id,
        params={"site": SOURCE, "bench_path": BENCH_PATH, "backup_id": str(bid),
                "scratch_site": SOURCE},  # malicious/mistaken: scratch == source
        executor=ex,
    )
    with sf() as db:
        assert db.get(CommandJob, job_id).status == "failure"
    # Refused before any site operation — the source is never dropped.
    assert _dropped_sites(ex) == []
    assert not any(a[:2] == ["bench", "new-site"] for a in ex.streamed)
