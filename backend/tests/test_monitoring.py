"""Monitoring (session 1.12): probe parsing, sample storage + pruning, the
poll-and-store path over a fake SSH, and the API (series read + service-restart
job with RBAC/validation). No real SSH/Redis/RQ touched."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.routes.jobs import get_job_runner
from app.core.jobs import InMemoryJobBackend, JobRunner
from app.core.monitoring import (
    parse_sample,
    poll_and_store,
    store_sample,
)
from app.db import Base
from app.models import MonitoringSample, Server
from app.models.server import SSHCredential
from tests.conftest import csrf_headers, login

SAMPLE_OUTPUT = "\n".join(
    [
        "CPU1 cpu 100 0 50 800 50 0 0 0 0 0",
        "CPU2 cpu 200 0 100 850 50 0 0 0 0 0",  # busy +200, total +300 -> ~33%
        "MEMTOTAL 16000000",  # 16 GB in kB
        "MEMAVAIL 8000000",  # 8 GB available -> 50% used
        "DISK 104857600 52428800 51%",  # 100 GB total, 50 GB used
        "LOAD 0.42",
        "SVC nginx active",
        "SVC mariadb active",
        "SVC redis-server inactive",
        "SVC supervisor failed",
    ]
)


# --------------------------------------------------------------------------- #
# parse_sample
# --------------------------------------------------------------------------- #


def test_parse_sample_full():
    f = parse_sample(SAMPLE_OUTPUT)
    # CPU idle = idle+iowait: CPU1 800+50=850 (total 1000), CPU2 850+50=900
    # (total 1200). idle delta 50, total delta 200 -> busy = 1-50/200 = 75%.
    assert f.cpu_pct == pytest.approx(75.0, abs=0.2)
    assert f.mem_total_mb == pytest.approx(15625, abs=1)  # 16000000/1024
    assert f.mem_pct == pytest.approx(50.0, abs=0.1)
    assert f.disk_pct == 51.0
    assert f.disk_total_gb == pytest.approx(100.0, abs=0.1)
    assert f.load1 == 0.42
    assert f.services == {
        "nginx": "active",
        "mariadb": "active",
        "redis-server": "inactive",
        "supervisor": "failed",
    }


def test_parse_sample_missing_lines_are_tolerated():
    f = parse_sample("LOAD 1.5\nSVC nginx active")
    assert f.load1 == 1.5
    assert f.cpu_pct is None
    assert f.mem_pct is None
    # Unreported managed services default to "unknown".
    assert f.services["mariadb"] == "unknown"
    assert f.services["nginx"] == "active"


def test_parse_sample_ignores_garbage():
    f = parse_sample("garbage\nCPU1 cpu not numbers\nDISK bad line\n")
    assert f.cpu_pct is None
    assert f.disk_pct is None
    # Still fills the service map with unknowns.
    assert set(f.services) == {"nginx", "mariadb", "redis-server", "supervisor"}


# --------------------------------------------------------------------------- #
# store_sample + pruning
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


def _server(db, name="vm"):
    s = Server(name=name, hostname="10.0.0.5")
    db.add(s)
    db.commit()
    return s.id


def test_store_sample_prunes_old_rows(sf):
    with sf() as db:
        sid = _server(db)
        # An old row well outside the retention window.
        old = MonitoringSample(server_id=sid, ok=True, services={})
        db.add(old)
        db.commit()
        old.ts = datetime.now(UTC) - timedelta(hours=200)
        db.commit()

        store_sample(db, sid, parse_sample(SAMPLE_OUTPUT), ok=True, retention_hours=168)

        rows = db.scalars(
            select(MonitoringSample).where(MonitoringSample.server_id == sid)
        ).all()
        # The 200h-old row is pruned; only the fresh one remains.
        assert len(rows) == 1
        assert rows[0].cpu_pct is not None


def test_store_failed_sample_records_error(sf):
    with sf() as db:
        sid = _server(db)
        s = store_sample(db, sid, None, ok=False, error="connection refused")
        assert s.ok is False
        assert s.error == "connection refused"
        assert s.cpu_pct is None


# --------------------------------------------------------------------------- #
# poll_and_store over a fake SSH
# --------------------------------------------------------------------------- #


class _Out:
    def __init__(self, exit_status, stdout, stderr=""):
        self.exit_status = exit_status
        self.stdout = stdout
        self.stderr = stderr


class FakeSSH:
    def __init__(self, out=None, raise_on_connect=False):
        self._out = out
        self._raise = raise_on_connect

    async def connect(self, server, cred):
        if self._raise:
            raise OSError("connection refused")
        return object()

    async def run(self, conn, argv, timeout=30.0):
        return self._out


def _server_with_cred(db):
    s = Server(name="vm", hostname="10.0.0.7", status="unknown")
    s.credential = SSHCredential(username="frappe", auth_type="key")
    db.add(s)
    db.commit()
    return s


def test_poll_and_store_success(sf):
    with sf() as db:
        server = _server_with_cred(db)
        ssh = FakeSSH(out=_Out(0, SAMPLE_OUTPUT))
        sample = asyncio.run(poll_and_store(ssh, db, server, retention_hours=168))
        assert sample.ok is True
        assert sample.cpu_pct is not None
        assert server.status == "online"
        assert server.last_seen is not None


def test_poll_and_store_failure_marks_offline(sf):
    with sf() as db:
        server = _server_with_cred(db)
        ssh = FakeSSH(raise_on_connect=True)
        sample = asyncio.run(poll_and_store(ssh, db, server))
        assert sample.ok is False
        assert "refused" in (sample.error or "")
        assert server.status == "offline"


# --------------------------------------------------------------------------- #
# API surface
# --------------------------------------------------------------------------- #


@pytest.fixture
def mon_client(client, db_session):
    runner = JobRunner(lambda: db_session, InMemoryJobBackend(), enqueue=lambda job: None)
    client.app.dependency_overrides[get_job_runner] = lambda: runner
    yield client
    client.app.dependency_overrides.pop(get_job_runner, None)


@pytest.fixture
def api_server(db_session):
    s = Server(name="vm-a", hostname="10.0.0.1")
    s.credential = SSHCredential(username="frappe", auth_type="key")
    db_session.add(s)
    db_session.commit()
    db_session.add(
        MonitoringSample(
            server_id=s.id, ok=True, cpu_pct=12.0, mem_pct=40.0, disk_pct=55.0,
            services={"nginx": "active"},
        )
    )
    db_session.commit()
    return s.id


def test_monitoring_series_returns_latest(mon_client, api_server):
    login(mon_client, "readonly@example.com")
    resp = mon_client.get(f"/api/servers/{api_server}/monitoring?hours=24")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["latest"]["cpu_pct"] == 12.0
    assert len(body["samples"]) == 1


def test_restart_service_creates_job(mon_client, api_server):
    login(mon_client, "admin@example.com")
    resp = mon_client.post(
        f"/api/servers/{api_server}/services/nginx/restart", headers=csrf_headers(mon_client)
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["action_name"] == "server.restart_service"


def test_restart_service_rejects_unknown_service(mon_client, api_server):
    login(mon_client, "admin@example.com")
    resp = mon_client.post(
        f"/api/servers/{api_server}/services/postgres/restart",
        headers=csrf_headers(mon_client),
    )
    assert resp.status_code == 422, resp.text


def test_restart_service_forbidden_for_readonly(mon_client, api_server):
    login(mon_client, "readonly@example.com")
    resp = mon_client.post(
        f"/api/servers/{api_server}/services/nginx/restart",
        headers=csrf_headers(mon_client),
    )
    assert resp.status_code == 403, resp.text
