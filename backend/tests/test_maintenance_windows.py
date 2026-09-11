"""Maintenance windows (session 3.5).

Tests:
- MaintenanceWindow.is_active_at() — correct active/inactive detection.
- JobRunner.create() enforcement guard — blocks/allows based on active windows.
- API CRUD endpoints — RBAC, list/create/patch/delete.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.commands.registry import get_template
from app.core.jobs import InMemoryJobBackend, JobRunner, MaintenanceWindowBlocked
from app.db import Base
from app.models import Server
from app.models.maintenance_window import DANGER_CLASSES, MaintenanceWindow


# --------------------------------------------------------------------------- #
# is_active_at: unit tests (no DB needed)
# --------------------------------------------------------------------------- #


class _WindowStub:
    """Plain-Python stub for is_active_at unit tests — no SQLAlchemy session needed."""

    def __init__(self, *, cron: str, duration_minutes: int = 60, timezone: str = "UTC", enabled: bool = True):
        self.cron = cron
        self.duration_minutes = duration_minutes
        self.timezone = timezone
        self.enabled = enabled

    def is_active_at(self, at: datetime | None = None) -> bool:
        return MaintenanceWindow.is_active_at(self, at)  # type: ignore[arg-type]


def _make_window(*, cron: str, duration_minutes: int = 60, timezone: str = "UTC") -> "_WindowStub":
    return _WindowStub(cron=cron, duration_minutes=duration_minutes, timezone=timezone)


def test_is_active_at_disabled_never_active():
    mw = _WindowStub(cron="0 0 * * *", enabled=False)
    assert mw.is_active_at(datetime(2024, 1, 1, 0, 30, tzinfo=UTC)) is False


def test_is_active_at_inside_window():
    # Window fires at 09:00 UTC every weekday, lasts 60 minutes.
    mw = _make_window(cron="0 9 * * 1-5", duration_minutes=60)
    # Monday 09:30 UTC — inside the window.
    at = datetime(2024, 1, 1, 9, 30, tzinfo=UTC)  # 2024-01-01 is a Monday
    assert mw.is_active_at(at) is True


def test_is_active_at_outside_window():
    # Window fires at 09:00 UTC every weekday, lasts 60 minutes.
    mw = _make_window(cron="0 9 * * 1-5", duration_minutes=60)
    # Monday 10:01 UTC — outside the window.
    at = datetime(2024, 1, 1, 10, 1, tzinfo=UTC)
    assert mw.is_active_at(at) is False


def test_is_active_at_exactly_on_trigger():
    mw = _make_window(cron="0 2 * * *", duration_minutes=120)
    # Exactly at trigger time — window just opened.
    at = datetime(2024, 1, 1, 2, 0, 0, tzinfo=UTC)
    assert mw.is_active_at(at) is True


def test_is_active_at_at_window_end_boundary():
    mw = _make_window(cron="0 2 * * *", duration_minutes=120)
    # Exactly 120 minutes after trigger — window just closed (exclusive upper bound).
    at = datetime(2024, 1, 1, 4, 0, 0, tzinfo=UTC)
    assert mw.is_active_at(at) is False

def test_is_active_at_just_inside_window_end():
    mw = _make_window(cron="0 2 * * *", duration_minutes=120)
    # 1 second before window closes — still active.
    at = datetime(2024, 1, 1, 3, 59, 59, tzinfo=UTC)
    assert mw.is_active_at(at) is True


def test_is_active_at_bad_cron_returns_false():
    mw = _make_window(cron="not-a-cron")
    assert mw.is_active_at(datetime(2024, 1, 1, 12, 0, tzinfo=UTC)) is False


# --------------------------------------------------------------------------- #
# Fixture: in-memory DB + JobRunner
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


class NoopExecutor:
    """Fake SSH executor: immediate success, no SSH."""

    async def run(self, _argv, _cwd, _run_as, _on_line, _timeout, _cancel_check): ...

    async def capture(self, _argv, _cwd, _run_as, _timeout): ...


def _runner(sf, monkeypatch):
    backend = InMemoryJobBackend()
    runner = JobRunner(
        session_factory=sf,
        backend=backend,
        enqueue=lambda job: None,
    )
    return runner


def _seed_server(db) -> Server:
    srv = Server(
        name="test-srv",
        hostname="127.0.0.1",
        ssh_port=22,
        env_tag="dev",
    )
    db.add(srv)
    db.commit()
    db.refresh(srv)
    return srv


def _seed_window(db, server_id: int, *, active: bool, danger_class: str = "update") -> MaintenanceWindow:
    # Use a cron that is always or never active based on the `active` flag.
    if active:
        # "every minute" — will always have a recent trigger
        cron = "* * * * *"
        duration_minutes = 60
    else:
        # "at midnight on Feb 30" — never fires
        cron = "0 0 30 2 *"
        duration_minutes = 1
    mw = MaintenanceWindow(
        name="Test Window",
        server_id=server_id,
        cron=cron,
        duration_minutes=duration_minutes,
        timezone="UTC",
        blocked_danger_classes=[danger_class],
        enabled=True,
    )
    db.add(mw)
    db.commit()
    db.refresh(mw)
    return mw


# --------------------------------------------------------------------------- #
# Enforcement guard in JobRunner.create()
# --------------------------------------------------------------------------- #


def test_update_job_blocked_by_active_window(sf, monkeypatch):
    runner = _runner(sf, monkeypatch)
    with sf() as db:
        srv = _seed_server(db)
        _seed_window(db, srv.id, active=True, danger_class="update")
        with pytest.raises(MaintenanceWindowBlocked) as exc_info:
            runner.create(
                db,
                action_name="bench.update",
                server_id=srv.id,
                target_type="bench",
                target_id="/home/frappe/frappe-bench",
                params={"bench_path": "/home/frappe/frappe-bench"},
                priority="default",
                created_by=None,
            )
    assert exc_info.value.danger_class == "update"
    assert "update" in str(exc_info.value)


def test_update_job_allowed_when_window_inactive(sf, monkeypatch):
    runner = _runner(sf, monkeypatch)
    with sf() as db:
        srv = _seed_server(db)
        _seed_window(db, srv.id, active=False, danger_class="update")
        # Should not raise — the window is inactive.
        job = runner.create(
            db,
            action_name="bench.update",
            server_id=srv.id,
            target_type="bench",
            target_id="/home/frappe/frappe-bench",
            params={"bench_path": "/home/frappe/frappe-bench"},
            priority="default",
            created_by=None,
        )
    assert job.action_name == "bench.update"


def test_update_job_allowed_when_no_windows(sf, monkeypatch):
    runner = _runner(sf, monkeypatch)
    with sf() as db:
        srv = _seed_server(db)
        # No windows at all.
        job = runner.create(
            db,
            action_name="bench.update",
            server_id=srv.id,
            target_type="bench",
            target_id="/home/frappe/frappe-bench",
            params={"bench_path": "/home/frappe/frappe-bench"},
            priority="default",
            created_by=None,
        )
    assert job.action_name == "bench.update"


def test_window_blocks_only_matching_danger_class(sf, monkeypatch):
    """A restore-class window must not block an update job."""
    runner = _runner(sf, monkeypatch)
    with sf() as db:
        srv = _seed_server(db)
        _seed_window(db, srv.id, active=True, danger_class="restore")
        # bench.update has danger_class="update", not "restore" — must pass.
        job = runner.create(
            db,
            action_name="bench.update",
            server_id=srv.id,
            target_type="bench",
            target_id="/home/frappe/frappe-bench",
            params={"bench_path": "/home/frappe/frappe-bench"},
            priority="default",
            created_by=None,
        )
    assert job.action_name == "bench.update"


def test_disabled_window_does_not_block(sf, monkeypatch):
    runner = _runner(sf, monkeypatch)
    with sf() as db:
        srv = _seed_server(db)
        mw = _seed_window(db, srv.id, active=True, danger_class="update")
        mw.enabled = False
        db.commit()
        # Window disabled — should not block.
        job = runner.create(
            db,
            action_name="bench.update",
            server_id=srv.id,
            target_type="bench",
            target_id="/home/frappe/frappe-bench",
            params={"bench_path": "/home/frappe/frappe-bench"},
            priority="default",
            created_by=None,
        )
    assert job.action_name == "bench.update"


# --------------------------------------------------------------------------- #
# Danger-class catalogue
# --------------------------------------------------------------------------- #


def test_danger_classes_contains_expected_values():
    assert set(DANGER_CLASSES) == {"update", "restore", "production_setup"}


@pytest.mark.parametrize(
    "action_name,expected_class",
    [
        ("bench.update", "update"),
        ("site.clone_to_staging", "update"),
        ("site.promote_update", "update"),
        ("site.restore", "restore"),
        ("backup.restore_test", "restore"),
        ("bench.setup_production", "production_setup"),
    ],
)
def test_dangerous_templates_have_correct_danger_class(action_name, expected_class):
    template = get_template(action_name)
    assert template.danger_class == expected_class


# --------------------------------------------------------------------------- #
# API: CRUD (HTTP client)
# --------------------------------------------------------------------------- #


def test_maintenance_window_api_crud(client, db_session):
    """Create → get → list → patch → delete via the HTTP API."""
    from tests.conftest import csrf_headers, login

    login(client, "admin@example.com")
    headers = csrf_headers(client)

    # Create a server first.
    srv_resp = client.post(
        "/api/servers",
        json={
            "name": "mw-srv",
            "hostname": "10.0.0.1",
            "ssh_port": 22,
            "credential": {"username": "frappe", "auth_type": "key", "generate": True},
        },
        headers=headers,
    )
    assert srv_resp.status_code in (200, 201), srv_resp.text
    srv_id = srv_resp.json()["id"]

    # Create a maintenance window.
    body = {
        "name": "Business hours — no updates",
        "server_id": srv_id,
        "cron": "0 9 * * 1-5",
        "duration_minutes": 480,
        "timezone": "Asia/Dubai",
        "blocked_danger_classes": ["update"],
        "enabled": True,
    }
    resp = client.post("/api/maintenance-windows", json=body, headers=headers)
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["name"] == body["name"]
    assert data["blocked_danger_classes"] == ["update"]
    mw_id = data["id"]

    # Get.
    resp = client.get(f"/api/maintenance-windows/{mw_id}", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["cron"] == "0 9 * * 1-5"

    # List.
    resp = client.get("/api/maintenance-windows", headers=headers)
    assert resp.status_code == 200
    ids = [w["id"] for w in resp.json()]
    assert mw_id in ids

    # List filtered by server.
    resp = client.get(f"/api/maintenance-windows?server_id={srv_id}", headers=headers)
    assert resp.status_code == 200
    assert all(w["server_id"] == srv_id for w in resp.json())

    # Patch.
    resp = client.patch(
        f"/api/maintenance-windows/{mw_id}",
        json={"name": "Updated name", "enabled": False},
        headers=headers,
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "Updated name"
    assert resp.json()["enabled"] is False

    # Delete.
    resp = client.delete(f"/api/maintenance-windows/{mw_id}", headers=headers)
    assert resp.status_code == 204

    # Confirm gone.
    resp = client.get(f"/api/maintenance-windows/{mw_id}", headers=headers)
    assert resp.status_code == 404


def test_read_only_cannot_create_maintenance_window(client, db_session):
    from tests.conftest import csrf_headers, login

    login(client, "readonly@example.com")
    headers = csrf_headers(client)
    resp = client.post(
        "/api/maintenance-windows",
        json={
            "name": "x",
            "server_id": 999,
            "cron": "0 9 * * *",
            "duration_minutes": 60,
            "timezone": "UTC",
            "blocked_danger_classes": [],
        },
        headers=headers,
    )
    assert resp.status_code == 403


def test_invalid_danger_class_rejected(client, db_session):
    from tests.conftest import csrf_headers, login

    login(client, "admin@example.com")
    headers = csrf_headers(client)
    resp = client.post(
        "/api/maintenance-windows",
        json={
            "name": "x",
            "server_id": 1,
            "cron": "0 9 * * *",
            "duration_minutes": 60,
            "timezone": "UTC",
            "blocked_danger_classes": ["not_a_real_class"],
        },
        headers=headers,
    )
    assert resp.status_code == 422


# --------------------------------------------------------------------------- #
# API: enforcement — blocked job returns 409, not 500                          #
# --------------------------------------------------------------------------- #


@pytest.fixture
def _mw_jobs_client(client, db_session):
    """TestClient with an in-memory job runner (no Redis/RQ/SSH) for enforcement tests."""
    from app.api.routes.jobs import get_job_runner
    from app.core.jobs import InMemoryJobBackend, JobRunner

    runner = JobRunner(lambda: db_session, InMemoryJobBackend(), enqueue=lambda job: None)
    client.app.dependency_overrides[get_job_runner] = lambda: runner
    yield client, db_session, runner
    client.app.dependency_overrides.pop(get_job_runner, None)


def _create_always_active_window(
    client, db_session, *, server_id: int, danger_class: str = "update"
) -> int:
    """Create a maintenance window that fires every minute — always active."""
    from tests.conftest import csrf_headers, login

    login(client, "admin@example.com")
    headers = csrf_headers(client)
    resp = client.post(
        "/api/maintenance-windows",
        json={
            "name": "always-active-test-window",
            "server_id": server_id,
            "cron": "* * * * *",
            "duration_minutes": 10080,  # 7 days (schema max); with per-minute cron, always active
            "timezone": "UTC",
            "blocked_danger_classes": [danger_class],
            "enabled": True,
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def test_active_window_blocks_job_create_returns_409(_mw_jobs_client):
    """POST /api/jobs on a danger-class action while a matching window is active → 409, not 500."""
    from app.models import Server
    from tests.conftest import csrf_headers, login

    client, db_session, _runner = _mw_jobs_client

    server = Server(name="mw-enforce-test", hostname="10.0.1.1", ssh_port=22)
    db_session.add(server)
    db_session.commit()

    _create_always_active_window(client, db_session, server_id=server.id, danger_class="update")

    login(client, "admin@example.com")
    resp = client.post(
        "/api/jobs",
        json={
            "action_name": "bench.update",
            "server_id": server.id,
            "params": {"bench_path": "/home/frappe/frappe-bench"},
        },
        headers=csrf_headers(client),
    )
    assert resp.status_code == 409, resp.text
    error = resp.json()["error"]
    assert error["code"] == "maintenance_window_blocked"
    assert error["danger_class"] == "update"
    assert isinstance(error["window_id"], int)
    assert error["window_name"] == "always-active-test-window"


def test_active_window_blocks_job_retry_returns_409(_mw_jobs_client):
    """Retry a danger-class job while a matching window is active → 409, not 500."""
    from app.models import Server
    from tests.conftest import csrf_headers, login

    client, db_session, runner = _mw_jobs_client

    server = Server(name="mw-retry-test", hostname="10.0.1.2", ssh_port=22)
    db_session.add(server)
    db_session.commit()

    login(client, "admin@example.com")

    # Create and cancel a job before the window exists so it ends in cancelled state.
    resp = client.post(
        "/api/jobs",
        json={
            "action_name": "bench.update",
            "server_id": server.id,
            "params": {"bench_path": "/home/frappe/frappe-bench"},
        },
        headers=csrf_headers(client),
    )
    assert resp.status_code == 201, resp.text
    job_id = resp.json()["id"]

    # Cancel it so it's in a retryable terminal state.
    client.post(f"/api/jobs/{job_id}/cancel", headers=csrf_headers(client))

    # Now activate the window and attempt retry.
    _create_always_active_window(client, db_session, server_id=server.id, danger_class="update")

    resp = client.post(f"/api/jobs/{job_id}/retry", headers=csrf_headers(client))
    assert resp.status_code == 409, resp.text
    error = resp.json()["error"]
    assert error["code"] == "maintenance_window_blocked"
    assert error["danger_class"] == "update"
