"""Bench discovery (session 1.6): pure parsing, the SSH gather over a fake
capture, the DB upsert/vanish pass, and one end-to-end run through the
JobRunner — no Redis/RQ/SSH touched."""

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core import discovery
from app.core.discovery import (
    BenchInfo,
    DiscoveryError,
    build_inspect_argv,
    gather,
    parse_bench_version,
    parse_bench_version_plain,
    parse_common_site_config,
    parse_inspect,
    parse_inventory,
    persist,
    validate_base_paths,
)
from app.core.jobs import CaptureResult, InMemoryJobBackend, JobRunner
from app.db import Base
from app.models import CommandJob, LogEntry, Server
from app.models.bench import Bench

# -- sample remote output ---------------------------------------------------- #

BENCH16 = "/home/frappe/frappe-bench-16"
BENCH15 = "/home/frappe/frappe-bench-15"

INVENTORY_OUT = f"BENCH\t{BENCH16}\nBENCH\t{BENCH15}\n"

INSPECT_16 = """PROD=0
---COMMON_SITE_CONFIG---
{"webserver_port": 8000, "socketio_port": 9000, "file_watcher_port": 6787,
 "redis_cache": "redis://localhost:13000", "redis_queue": "redis://localhost:11000",
 "redis_socketio": "redis://localhost:12000"}
---BENCH_VERSION---
[{"name": "frappe", "version": "16.25.0"}, {"name": "erpnext", "version": "16.25.0"}]
---PYTHON---
Python 3.14.0
---NODE---
v24.1.0
---END---
"""

# v15: production, and its bench CLI does not support --format json (empty here),
# so gather falls back to the plain `bench version`.
INSPECT_15 = """PROD=1
---COMMON_SITE_CONFIG---
{"webserver_port": 8001, "socketio_port": 9001, "file_watcher_port": 6788,
 "redis_cache": "redis://localhost:13001", "redis_queue": "redis://localhost:11001"}
---BENCH_VERSION---
---PYTHON---
Python 3.11.9
---NODE---
v18.20.0
---END---
"""

VERSION_PLAIN_15 = "frappe 15.42.1\nerpnext 15.42.1\n"


def make_capture(inventory=INVENTORY_OUT, inspects=None, plain=None):
    """Build a fake async capture keyed on the argv it receives."""
    inspects = inspects if inspects is not None else {BENCH16: INSPECT_16, BENCH15: INSPECT_15}
    plain = plain if plain is not None else {BENCH15: VERSION_PLAIN_15}

    async def capture(argv, *, cwd=None, timeout=120.0):
        if argv[:2] == ["bench", "version"]:
            return CaptureResult(0, plain.get(cwd, ""), "")
        script = argv[2] if len(argv) > 2 else ""
        if "scan " in script or "emit_bench" in script:
            return CaptureResult(0, inventory, "")
        path = argv[4] if len(argv) > 4 else ""
        return CaptureResult(0, inspects.get(path, ""), "")

    return capture


# -- pure parsing ------------------------------------------------------------ #


def test_parse_inventory_dedups_and_strips():
    out = parse_inventory("BENCH\t/a/bench/\nnoise\nBENCH\t/a/bench\nBENCH\t/b/x\n")
    assert out == ["/a/bench", "/b/x"]


def test_parse_common_site_config_handles_ints_and_redis_urls():
    body = INSPECT_16.split("---COMMON_SITE_CONFIG---")[1].split("---")[0]
    ports = parse_common_site_config(body)
    assert ports["webserver_port"] == 8000
    assert ports["socketio_port"] == 9000
    assert ports["file_watcher_port"] == 6787
    assert ports["redis_cache_port"] == 13000
    assert ports["redis_queue_port"] == 11000
    assert ports["redis_socketio_port"] == 12000


def test_parse_common_site_config_socketio_falls_back_to_queue():
    ports = parse_common_site_config('{"redis_queue": "redis://localhost:11001"}')
    assert ports["redis_socketio_port"] == 11001


def test_parse_common_site_config_bad_json_is_all_none():
    ports = parse_common_site_config("not json {")
    assert set(ports.values()) == {None}


def test_parse_bench_version_json_list_and_dict():
    assert parse_bench_version('[{"name":"frappe","version":"16.25.0"}]') == "16.25.0"
    assert parse_bench_version('{"frappe":"15.1.0"}') == "15.1.0"
    assert parse_bench_version("") is None


def test_parse_bench_version_plain():
    assert parse_bench_version_plain(VERSION_PLAIN_15) == "15.42.1"
    assert parse_bench_version_plain("erpnext 15.0.0") is None


def test_parse_inspect_builds_full_info():
    info = parse_inspect(INSPECT_16, BENCH16)
    assert info.name == "frappe-bench-16"
    assert info.frappe_version == "16.25.0"
    assert info.python_version == "3.14.0"
    assert info.node_version == "24.1.0"
    assert info.is_production is False
    assert info.ports["webserver_port"] == 8000


def test_validate_base_paths_rejects_metacharacters():
    with pytest.raises(DiscoveryError):
        validate_base_paths(["/home; rm -rf /"])
    with pytest.raises(DiscoveryError):
        validate_base_paths(["relative/path"])


def test_validate_base_paths_defaults_and_dedups():
    assert validate_base_paths(None) == list(discovery.DEFAULT_BASE_PATHS)
    assert validate_base_paths(["/opt/", "/opt", "/srv"]) == ["/opt", "/srv"]


@pytest.mark.parametrize("bad", ["/home/../opt", "/..", "/opt/.."])
def test_validate_base_paths_rejects_dotdot(bad):
    # DOO-107: keep discovery consistent with the create/preflight path guard.
    with pytest.raises(DiscoveryError, match="'\\.\\.'"):
        validate_base_paths([bad])


def test_build_inspect_argv_rejects_bad_path():
    with pytest.raises(DiscoveryError):
        build_inspect_argv("/home/`whoami`")
    with pytest.raises(DiscoveryError, match="'\\.\\.'"):
        build_inspect_argv("/home/frappe/../etc")


# -- gather (SSH half over a fake capture) ----------------------------------- #


def test_gather_returns_two_benches_with_versions_and_prod_flag():
    import asyncio

    infos = asyncio.run(gather(make_capture(), ["/home"]))
    by_name = {i.name: i for i in infos}
    assert set(by_name) == {"frappe-bench-16", "frappe-bench-15"}
    assert by_name["frappe-bench-16"].frappe_version == "16.25.0"
    # v15 came from the plain fallback because its --format json was empty.
    assert by_name["frappe-bench-15"].frappe_version == "15.42.1"
    assert by_name["frappe-bench-15"].is_production is True
    assert by_name["frappe-bench-15"].ports["redis_queue_port"] == 11001


# -- persist (DB upsert + vanish) -------------------------------------------- #


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as session:
        server = Server(name="vm", hostname="10.0.0.9")
        session.add(server)
        session.commit()
        session.server_id = server.id  # type: ignore[attr-defined]
        yield session
    engine.dispose()


def _info(path, version, **ports):
    import posixpath

    return BenchInfo(
        path=path,
        name=posixpath.basename(path),
        frappe_version=version,
        ports={
            "webserver_port": ports.get("webserver_port"),
            "socketio_port": None,
            "redis_cache_port": None,
            "redis_queue_port": None,
            "redis_socketio_port": None,
            "file_watcher_port": None,
        },
    )


def test_persist_inserts_then_marks_vanished_missing(db):
    sid = db.server_id
    summary = persist(db, sid, [_info(BENCH16, "16.25.0", webserver_port=8000),
                                _info(BENCH15, "15.42.1", webserver_port=8001)])
    assert (summary.added, summary.updated, summary.missing) == (2, 0, 0)
    assert summary.total_active == 2
    rows = db.scalars(select(Bench).where(Bench.server_id == sid)).all()
    assert {r.name for r in rows} == {"frappe-bench-16", "frappe-bench-15"}
    assert all(r.status == "active" for r in rows)

    # Re-discover with only the v16 bench: v15's dir vanished -> marked missing,
    # v16 updated in place (no duplicate row).
    summary2 = persist(db, sid, [_info(BENCH16, "16.26.0", webserver_port=8000)])
    assert (summary2.added, summary2.updated, summary2.missing) == (0, 1, 1)
    rows = {r.name: r for r in db.scalars(select(Bench).where(Bench.server_id == sid)).all()}
    assert len(rows) == 2  # no duplicate
    assert rows["frappe-bench-16"].status == "active"
    assert rows["frappe-bench-16"].frappe_version == "16.26.0"
    assert rows["frappe-bench-15"].status == "missing"


def test_persist_reactivates_a_returning_bench(db):
    sid = db.server_id
    persist(db, sid, [_info(BENCH15, "15.42.1")])
    persist(db, sid, [])  # vanished
    assert db.scalars(select(Bench)).one().status == "missing"
    persist(db, sid, [_info(BENCH15, "15.42.1")])  # came back
    assert db.scalars(select(Bench)).one().status == "active"


# -- end-to-end through the JobRunner ---------------------------------------- #


class CaptureExecutor:
    """A JobRunner executor that answers capture() from a scripted capture and
    refuses to stream (discovery only ever captures)."""

    def __init__(self, capture):
        self._capture = capture

    async def capture(self, argv, *, cwd=None, timeout=120.0):
        return await self._capture(argv, cwd=cwd, timeout=timeout)

    async def run(self, *a, **k):  # pragma: no cover - discovery never streams
        raise AssertionError("discovery must not stream")


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


def _run_discovery(sf, server_id, capture):
    runner = JobRunner(sf, InMemoryJobBackend(), enqueue=lambda job: None)
    with sf() as db:
        job = runner.create(
            db,
            action_name="bench.discover",
            server_id=server_id,
            target_type="server",
            target_id=None,
            params={},
            priority="default",
            created_by=None,
        )
        job_id = job.id
    runner.run_job(job_id, executor_factory=fake_factory(CaptureExecutor(capture)))
    return job_id


def test_discovery_job_upserts_benches_and_then_marks_missing(sf):
    with sf() as db:
        server = Server(name="vm-alpha", hostname="10.0.0.5")
        db.add(server)
        db.commit()
        server_id = server.id

    job_id = _run_discovery(sf, server_id, make_capture())

    with sf() as db:
        job = db.get(CommandJob, job_id)
        assert job.status == "success", job.status
        benches = db.scalars(select(Bench).where(Bench.server_id == server_id)).all()
        by_name = {b.name: b for b in benches}
        assert by_name["frappe-bench-16"].frappe_version == "16.25.0"
        assert by_name["frappe-bench-16"].webserver_port == 8000
        assert by_name["frappe-bench-15"].frappe_version == "15.42.1"
        assert by_name["frappe-bench-15"].is_production is True
        assert all(b.status == "active" for b in benches)
        assert all(b.discovered_at is not None for b in benches)
        logs = db.scalars(select(LogEntry).where(LogEntry.job_id == job_id)).all()
        assert any("Inventory updated" in log.content for log in logs)

    # Acceptance: delete a bench dir on the VM and re-discover -> marked missing.
    only16 = make_capture(inventory=f"BENCH\t{BENCH16}\n")
    _run_discovery(sf, server_id, only16)
    with sf() as db:
        found = db.scalars(select(Bench).where(Bench.server_id == server_id)).all()
        rows = {b.name: b for b in found}
        assert rows["frappe-bench-15"].status == "missing"
        assert rows["frappe-bench-16"].status == "active"
