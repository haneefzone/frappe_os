"""Multi-server hardening (session 2.6):

- SSH connection-pool limits: the per-server session semaphore caps concurrency,
  QUEUES (never fails) work under load, exposes the peak, and applies backpressure
  as a clean SessionPoolTimeout — never a hang — when a slot can't free in time;
  and SSHService.run/write_file are actually wired through it.
- backup.move_across_servers: streams an offsite backup's artifacts from S3 down
  onto another server, re-verifies each sha256 on arrival, refuses a mismatch,
  and registers the moved copy; the API enforces RBAC + the offsite/other-server
  preconditions.
- per-server dashboard rollup: capacity/health, bench/site counts, site up/down,
  24h job outcomes and the backup footprint, plus the read-only endpoint.

No Redis/RQ/boto3/SSH — the semaphore is driven directly, S3 goes through an
injected fake client, and the move runs over an in-memory executor.
"""

from __future__ import annotations

import asyncio
import hashlib

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.routes.jobs import get_job_runner
from app.core import storage as st
from app.core.jobs import CaptureResult, InMemoryJobBackend, JobRunner
from app.core.security import get_secrets_service
from app.core.server_rollup import server_dashboard
from app.core.ssh import SessionPoolTimeout, SSHService
from app.db import Base
from app.models import CommandJob, Server
from app.models.backup import Backup
from app.models.bench import Bench
from app.models.monitoring import MonitoringSample
from app.models.site import Site
from app.models.storage import StorageTarget
from app.models.uptime import UptimeSample
from tests.conftest import csrf_headers, login
from tests.test_backups import BENCH_PATH, fake_factory
from tests.test_storage import FakeS3Client


@pytest.fixture
def sf():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    yield factory
    engine.dispose()


# --------------------------------------------------------------------------- #
# 1. Connection-pool limits (unit-testable, per the seed)
# --------------------------------------------------------------------------- #


def test_pool_caps_concurrency_and_queues_all_work():
    """With a cap of 2, six concurrent session-holders never exceed 2 in flight
    and ALL six complete — the excess queued (waited), it did not fail."""

    async def body():
        svc = SSHService(get_secrets_service(), max_sessions_per_server=2, acquire_timeout=5)
        active = 0
        peak = 0
        done: list[int] = []

        async def worker(i: int) -> None:
            nonlocal active, peak
            async with svc.limit_sessions(1):
                active += 1
                peak = max(peak, active)
                await asyncio.sleep(0.01)
                active -= 1
            done.append(i)

        await asyncio.gather(*(worker(i) for i in range(6)))
        assert peak == 2  # cap respected, and actually reached
        assert sorted(done) == list(range(6))  # queued, not failed
        assert svc.pool_stats(1)["peak"] == 2
        assert svc.pool_stats(1)["active"] == 0

    asyncio.run(body())


def test_pool_backpressure_times_out_cleanly():
    """When no slot frees within the acquire timeout the waiter gets a clean
    SessionPoolTimeout (backpressure) instead of hanging forever."""

    async def body():
        svc = SSHService(
            get_secrets_service(), max_sessions_per_server=1, acquire_timeout=0.05
        )
        async with svc.limit_sessions(1):
            with pytest.raises(SessionPoolTimeout):
                async with svc.limit_sessions(1):
                    pass  # pragma: no cover - never reached

    asyncio.run(body())


def test_pools_are_per_server_independent():
    """A saturated server never blocks work on a different server."""

    async def body():
        svc = SSHService(get_secrets_service(), max_sessions_per_server=1, acquire_timeout=2)
        async with svc.limit_sessions(1):
            # server 1 is full, but server 2 has its own slot.
            async with svc.limit_sessions(2):
                assert svc.pool_stats(1)["active"] == 1
                assert svc.pool_stats(2)["active"] == 1

    asyncio.run(body())


def test_unmetered_connection_runs_when_no_server_id():
    """A bare connection with no stashed server id runs through the null slot
    (a hand-built test conn is never blocked by accounting it never joined)."""

    async def body():
        svc = SSHService(get_secrets_service(), max_sessions_per_server=1)
        conn = object()  # no _fdm_server_id
        async with svc._session_slot(conn):
            pass  # no exception, no accounting

    asyncio.run(body())


class _MeteredFakeConn:
    """A fake AsyncSSH connection that records how many `run`s overlap, to prove
    SSHService.run is wired through the per-server session cap."""

    def __init__(self, server_id: int, limit: int) -> None:
        self._fdm_server_id = server_id
        self._fdm_session_limit = limit
        self.concurrent = 0
        self.peak = 0

    async def run(self, command, check=False, timeout=30.0):  # noqa: N803
        self.concurrent += 1
        self.peak = max(self.peak, self.concurrent)
        await asyncio.sleep(0.01)
        self.concurrent -= 1

        class _R:
            exit_status = 0
            stdout = ""
            stderr = ""

        return _R()


def test_ssh_run_is_wired_through_the_cap():
    """Two concurrent SSHService.run calls on a cap-1 server never overlap on the
    wire — the second waits for the first to release its slot."""

    async def body():
        svc = SSHService(get_secrets_service(), max_sessions_per_server=1, acquire_timeout=5)
        conn = _MeteredFakeConn(server_id=1, limit=1)
        await asyncio.gather(svc.run(conn, ["true"]), svc.run(conn, ["true"]))
        assert conn.peak == 1

    asyncio.run(body())


# --------------------------------------------------------------------------- #
# 2. backup.move_across_servers
# --------------------------------------------------------------------------- #

_MOVE_BYTES = {
    "database": b"MOVED-DATABASE-BYTES",
    "config": b'{"encryption_key": "k"}',
}
_KEYS = {"database": "tenant-a/backup-1/db.sql.gz", "config": "tenant-a/backup-1/cfg.json"}
# The bucket keyed by object key (what the action looks up), not by artifact kind.
_OBJECTS = {_KEYS[k]: _MOVE_BYTES[k] for k in _MOVE_BYTES}
_SRC_DIR = "/home/frappe/frappe-bench/sites/test1.localhost/private/backups"
_SRC_PFX = "20260711_100000-test1_localhost"
_SRC_PATH = {
    "database": f"{_SRC_DIR}/{_SRC_PFX}-database.sql.gz",
    "config": f"{_SRC_DIR}/{_SRC_PFX}-site_config_backup.json",
}


class _FakeBody:
    def __init__(self, data: bytes) -> None:
        self._data = data
        self._pos = 0

    def read(self, n: int = -1) -> bytes:
        if n is None or n < 0:
            chunk = self._data[self._pos :]
            self._pos = len(self._data)
            return chunk
        chunk = self._data[self._pos : self._pos + n]
        self._pos += len(chunk)
        return chunk

    def close(self) -> None:
        pass


class FakeS3Download(FakeS3Client):
    """FakeS3Client + get_object streaming, optionally corrupting one object to
    exercise the checksum-refusal path."""

    def __init__(self, objects: dict[str, bytes], *, corrupt: bool = False) -> None:
        super().__init__()
        self._objects = dict(objects)
        if corrupt:
            first = next(iter(self._objects))
            self._objects[first] = self._objects[first] + b"-TAMPERED"

    def get_object(self, Bucket, Key):  # noqa: N803
        return {"Body": _FakeBody(self._objects[Key])}


class MoveExecutor:
    """In-memory executor for the move: `write_file` records the landed bytes,
    `capture` answers `sha256sum` from them (so on-arrival verify is real)."""

    def __init__(self) -> None:
        self.written: dict[str, bytes] = {}

    async def run(self, argv, *, cwd, run_as, on_line, cancel_check):
        return 0

    async def capture(self, argv, *, cwd=None, timeout=120.0):
        if argv[:1] == ["sha256sum"]:
            dest = argv[-1]
            data = self.written.get(dest, b"")
            return CaptureResult(0, f"{hashlib.sha256(data).hexdigest()}  {dest}\n", "")
        raise AssertionError(f"unexpected capture {argv}")

    async def write_file(self, path, chunks):
        buf = bytearray()
        async for chunk in chunks:
            buf += chunk
        self.written[path] = bytes(buf)
        return 0

    async def read_file(self, path, *, chunk_size=65536):  # pragma: no cover - unused
        yield b""


def _two_server_env(db):
    """Source server A (with an offsite backup) + destination server B (with the
    same site name), so a move A→B has somewhere to land + register."""
    secrets = get_secrets_service()
    a = Server(name="srv-a", hostname="10.0.0.1")
    b = Server(name="srv-b", hostname="10.0.0.2")
    db.add_all([a, b])
    db.commit()
    bench_a = Bench(server_id=a.id, path=BENCH_PATH, name="fb-a", frappe_version="16.2.0")
    bench_b = Bench(
        server_id=b.id, path="/home/frappe/bench-b", name="fb-b", frappe_version="16.2.0"
    )
    db.add_all([bench_a, bench_b])
    db.commit()
    site_a = Site(bench_id=bench_a.id, name="test1.localhost", status="active")
    site_b = Site(bench_id=bench_b.id, name="test1.localhost", status="active")
    db.add_all([site_a, site_b])
    db.commit()
    target = StorageTarget(
        name="offsite", provider="minio", bucket="fdm-backups", path_prefix="tenant-a",
        use_ssl=True, enabled=True,
        access_key_enc=secrets.encrypt("AK"), secret_key_enc=secrets.encrypt("SK"),
    )
    db.add(target)
    db.commit()
    artifacts = [
        {"kind": k, "path": _SRC_PATH[k], "size_bytes": len(_MOVE_BYTES[k]),
         "checksum_sha256": hashlib.sha256(_MOVE_BYTES[k]).hexdigest()}
        for k in _MOVE_BYTES
    ]
    src = Backup(
        site_id=site_a.id, bench_id=bench_a.id, type="with-files", status="success",
        storage_state="offsite", storage_target_id=target.id, object_keys=dict(_KEYS),
        frappe_version="16.2.0", size_bytes=sum(a["size_bytes"] for a in artifacts),
        artifacts=artifacts,
    )
    db.add(src)
    db.commit()
    return {
        "a": a.id, "b": b.id, "bench_b": bench_b.id, "site_b": site_b.id,
        "backup_id": src.id, "target_id": target.id,
    }


def _run_move(sf, *, env, executor, monkeypatch, corrupt=False):
    fake = FakeS3Download(_OBJECTS, corrupt=corrupt)
    monkeypatch.setattr(st, "_boto3_client", lambda cfg: fake)
    runner = JobRunner(
        sf, InMemoryJobBackend(), enqueue=lambda job: None, secrets=get_secrets_service()
    )
    dest_dir = "/home/frappe/bench-b/sites/test1.localhost/private/backups"
    with sf() as db:
        job = runner.create(
            db, action_name="backup.move_across_servers", server_id=env["b"],
            target_type="site", target_id="/home/frappe/bench-b::test1.localhost",
            params={
                "backup_id": str(env["backup_id"]),
                "storage_target_id": str(env["target_id"]),
                "dest_dir": dest_dir,
                "target_site": "test1.localhost",
                "target_bench_id": str(env["bench_b"]),
            },
            priority="default", created_by=None,
        )
        job_id = job.id
    runner.run_job(job_id, executor_factory=fake_factory(executor))
    return job_id, dest_dir


def test_move_transfers_verifies_and_registers(sf, monkeypatch):
    with sf() as db:
        env = _two_server_env(db)
    ex = MoveExecutor()
    job_id, dest_dir = _run_move(sf, env=env, executor=ex, monkeypatch=monkeypatch)

    with sf() as db:
        assert db.get(CommandJob, job_id).status == "success"
        moved = db.scalars(
            select(Backup).where(Backup.moved_from_backup_id == env["backup_id"])
        ).first()
        assert moved is not None
        assert moved.bench_id == env["bench_b"]
        assert moved.site_id == env["site_b"]
        assert moved.status == "success"
        assert moved.storage_state == "local"
        assert moved.source_server_id == env["a"]
        assert {a["kind"] for a in moved.artifacts} == {"database", "config"}
        # Convenience columns point at the landed files on server B.
        assert moved.db_path.startswith(dest_dir)
        assert moved.config_path.startswith(dest_dir)
    # The bytes physically landed, unchanged, on the destination.
    moved_db = f"{dest_dir}/{_SRC_PFX}-database.sql.gz"
    assert ex.written[moved_db] == _MOVE_BYTES["database"]
    assert (
        hashlib.sha256(ex.written[moved_db]).hexdigest()
        == hashlib.sha256(_MOVE_BYTES["database"]).hexdigest()
    )


def test_move_refuses_on_checksum_mismatch(sf, monkeypatch):
    with sf() as db:
        env = _two_server_env(db)
    ex = MoveExecutor()
    job_id, _ = _run_move(sf, env=env, executor=ex, monkeypatch=monkeypatch, corrupt=True)

    with sf() as db:
        assert db.get(CommandJob, job_id).status == "failure"
        # No moved copy registered — a corrupt transfer never becomes a backup.
        moved = db.scalars(
            select(Backup).where(Backup.moved_from_backup_id == env["backup_id"])
        ).first()
        assert moved is None


# --------------------------------------------------------------------------- #
# 2b. Move API (RBAC + preconditions)
# --------------------------------------------------------------------------- #


@pytest.fixture
def move_client(client, db_session):
    runner = JobRunner(
        lambda: db_session, InMemoryJobBackend(), enqueue=lambda job: None,
        secrets=get_secrets_service(),
    )
    client.app.dependency_overrides[get_job_runner] = lambda: runner
    yield client
    client.app.dependency_overrides.pop(get_job_runner, None)


@pytest.fixture
def move_env(db_session):
    with_session = db_session
    return _two_server_env(with_session)


def test_move_api_enqueues_for_developer(move_client, db_session, move_env):
    login(move_client, "developer@example.com")
    resp = move_client.post(
        f"/api/backups/{move_env['backup_id']}/move",
        json={"target_bench_id": move_env["bench_b"]},
        headers=csrf_headers(move_client),
    )
    assert resp.status_code == 201, resp.text
    job = db_session.scalars(
        select(CommandJob).where(CommandJob.action_name == "backup.move_across_servers")
    ).first()
    assert job is not None and job.server_id == move_env["b"]


def test_move_api_denied_without_transfer_permission(move_client, move_env):
    # Read-only (no backup:transfer) can never launch a cross-server move.
    login(move_client, "readonly@example.com")
    resp = move_client.post(
        f"/api/backups/{move_env['backup_id']}/move",
        json={"target_bench_id": move_env["bench_b"]},
        headers=csrf_headers(move_client),
    )
    assert resp.status_code == 403


def test_move_api_rejects_local_only_backup(move_client, db_session, move_env):
    src = db_session.get(Backup, move_env["backup_id"])
    src.storage_state = "local"
    src.object_keys = {}
    db_session.commit()
    login(move_client, "developer@example.com")
    resp = move_client.post(
        f"/api/backups/{move_env['backup_id']}/move",
        json={"target_bench_id": move_env["bench_b"]},
        headers=csrf_headers(move_client),
    )
    assert resp.status_code == 422
    assert "offsite" in resp.json()["error"]["message"].lower()


def test_move_api_rejects_same_server(move_client, db_session, move_env):
    # A second bench on server A — moving onto the same server is refused.
    bench_a2 = Bench(
        server_id=move_env["a"], path="/home/frappe/fb-a2", name="fb-a2", frappe_version="16.2.0"
    )
    db_session.add(bench_a2)
    db_session.commit()
    Site(bench_id=bench_a2.id, name="test1.localhost", status="active")
    db_session.add(Site(bench_id=bench_a2.id, name="test1.localhost", status="active"))
    db_session.commit()
    login(move_client, "developer@example.com")
    resp = move_client.post(
        f"/api/backups/{move_env['backup_id']}/move",
        json={"target_bench_id": bench_a2.id},
        headers=csrf_headers(move_client),
    )
    assert resp.status_code == 422
    assert "already on that server" in resp.json()["error"]["message"].lower()


def test_move_api_requires_dest_site(move_client, db_session, move_env):
    # Destination bench with NO matching site → 404 with guidance.
    bench_c = Bench(
        server_id=move_env["b"], path="/home/frappe/fb-c", name="fb-c", frappe_version="16.2.0"
    )
    db_session.add(bench_c)
    db_session.commit()
    login(move_client, "developer@example.com")
    resp = move_client.post(
        f"/api/backups/{move_env['backup_id']}/move",
        json={"target_bench_id": bench_c.id},
        headers=csrf_headers(move_client),
    )
    assert resp.status_code == 404
    assert "does not exist" in resp.json()["error"]["message"].lower()


# --------------------------------------------------------------------------- #
# 3. Per-server dashboard rollup
# --------------------------------------------------------------------------- #


def _dash_env(db):
    a = Server(name="dash-a", hostname="10.1.0.1", env_tag="prod", status="online")
    b = Server(name="dash-b", hostname="10.1.0.2")  # a second server, must not leak in
    db.add_all([a, b])
    db.commit()
    bench = Bench(server_id=a.id, path=BENCH_PATH, name="fb", frappe_version="16.2.0")
    other_bench = Bench(server_id=b.id, path="/x", name="fb-b", frappe_version="16.2.0")
    db.add_all([bench, other_bench])
    db.commit()
    up_site = Site(bench_id=bench.id, name="up.localhost", status="active")
    down_site = Site(bench_id=bench.id, name="down.localhost", status="active")
    db.add_all([up_site, down_site])
    db.commit()
    # Uptime: one up, one down.
    db.add(UptimeSample(site_id=up_site.id, up=True, status_code=200, latency_ms=12.0))
    db.add(UptimeSample(site_id=down_site.id, up=False, status_code=502, latency_ms=None))
    # Capacity: latest monitoring sample.
    db.add(MonitoringSample(
        server_id=a.id, ok=True, cpu_pct=41.0, mem_pct=55.0, disk_pct=70.0,
        load1=0.8, services={"nginx": "active", "mariadb": "active"},
    ))
    # Backups: two successful on server A, one on the OTHER server (excluded).
    def _bk(bench_id, size):
        return Backup(
            site_id=up_site.id, bench_id=bench_id, type="db", status="success", size_bytes=size
        )

    db.add_all([_bk(bench.id, 1000), _bk(bench.id, 2000), _bk(other_bench.id, 9999)])

    # Jobs 24h on A: one success, one failure; other server's job excluded.
    def _job(server_id, action, status):
        return CommandJob(
            server_id=server_id, target_type="site", action_name=action, status=status
        )

    db.add_all([
        _job(a.id, "site.backup", "success"),
        _job(a.id, "site.migrate", "failure"),
        _job(b.id, "site.backup", "success"),
    ])
    db.commit()
    return {"a": a.id}


def test_server_dashboard_rollup(sf):
    with sf() as db:
        env = _dash_env(db)
        server = db.get(Server, env["a"])
        dash = server_dashboard(db, server)
        assert dash.benches == 1
        assert dash.sites.total == 2 and dash.sites.up == 1 and dash.sites.down == 1
        assert dash.capacity is not None and dash.capacity.cpu_pct == 41.0
        assert dash.capacity.services["nginx"] == "active"
        assert dash.backups.count == 2  # the other server's backup is excluded
        assert dash.backups.total_size_bytes == 3000
        assert dash.jobs_24h.total == 2
        assert dash.jobs_24h.success == 1 and dash.jobs_24h.failure == 1


def test_server_dashboard_endpoint(client, db_session):
    env = _dash_env(db_session)
    login(client, "readonly@example.com")  # READ is enough
    resp = client.get(f"/api/servers/{env['a']}/dashboard")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["server_id"] == env["a"]
    assert body["benches"] == 1
    assert body["sites"]["up"] == 1 and body["sites"]["down"] == 1
    assert body["backups"]["count"] == 2
    assert body["jobs_24h"]["failure"] == 1
    assert body["capacity"]["cpu_pct"] == 41.0


def test_server_dashboard_empty_server(client, db_session):
    s = Server(name="fresh", hostname="10.9.9.9")
    db_session.add(s)
    db_session.commit()
    login(client, "readonly@example.com")
    resp = client.get(f"/api/servers/{s.id}/dashboard")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["benches"] == 0
    assert body["sites"]["total"] == 0
    assert body["capacity"] is None
    assert body["backups"]["count"] == 0
