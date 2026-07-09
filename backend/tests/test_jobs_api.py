"""Job engine API: RBAC gating, validation (404/422), lock conflict (409 with
blocking id), list filters, detail, cancel and retry — with an in-memory runner
so no Redis/RQ/SSH is touched."""

import pytest

from app.api.routes.jobs import get_job_runner
from app.core.jobs import InMemoryJobBackend, JobRunner
from app.models import Server
from tests.conftest import csrf_headers, login


@pytest.fixture
def jobs_client(client, db_session):
    """The shared TestClient with the jobs runner overridden to an in-memory
    backend (one instance per test, so lock state persists across requests)."""
    runner = JobRunner(lambda: db_session, InMemoryJobBackend(), enqueue=lambda job: None)
    client.app.dependency_overrides[get_job_runner] = lambda: runner
    yield client
    client.app.dependency_overrides.pop(get_job_runner, None)


@pytest.fixture
def server_id(db_session):
    server = Server(name="vm-alpha", hostname="10.0.0.5", ssh_port=22)
    db_session.add(server)
    db_session.commit()
    return server.id


def _post_job(client, **overrides):
    body = {"action_name": "system.echo_demo", "server_id": overrides.pop("server_id"),
            "params": {"message": "hello"}, **overrides}
    return client.post("/api/jobs", json=body, headers=csrf_headers(client))


def test_create_returns_pending_job_with_masked_params(jobs_client, server_id):
    login(jobs_client, "admin@example.com")
    resp = _post_job(jobs_client, server_id=server_id)
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["status"] == "pending"
    assert data["action_name"] == "system.echo_demo"
    assert data["params_sanitized"] == {"message": "hello"}
    assert data["steps"] == []


def test_readonly_cannot_launch_jobs(jobs_client, server_id):
    login(jobs_client, "readonly@example.com")
    assert _post_job(jobs_client, server_id=server_id).status_code == 403


def test_developer_can_launch(jobs_client, server_id):
    login(jobs_client, "developer@example.com")
    assert _post_job(jobs_client, server_id=server_id).status_code == 201


def test_unknown_action_is_404(jobs_client, server_id):
    login(jobs_client, "admin@example.com")
    resp = _post_job(jobs_client, server_id=server_id, action_name="does.not.exist")
    assert resp.status_code == 404


def test_missing_server_is_404(jobs_client):
    login(jobs_client, "admin@example.com")
    resp = _post_job(jobs_client, server_id=999999)
    assert resp.status_code == 404


def test_injection_param_is_422(jobs_client, server_id):
    login(jobs_client, "admin@example.com")
    resp = _post_job(jobs_client, server_id=server_id, params={"message": "; rm -rf /"})
    assert resp.status_code == 422


def test_concurrent_same_target_returns_409_with_blocking_id(jobs_client, server_id):
    login(jobs_client, "admin@example.com")
    first = _post_job(jobs_client, server_id=server_id)
    assert first.status_code == 201
    second = _post_job(jobs_client, server_id=server_id)
    assert second.status_code == 409
    assert second.json()["error"]["blocking_job_id"] == first.json()["id"]


def test_detect_tools_is_lock_free(jobs_client, server_id):
    login(jobs_client, "admin@example.com")
    body = {"action_name": "server.detect_tools", "server_id": server_id, "params": {}}
    a = jobs_client.post("/api/jobs", json=body, headers=csrf_headers(jobs_client))
    b = jobs_client.post("/api/jobs", json=body, headers=csrf_headers(jobs_client))
    assert a.status_code == 201 and b.status_code == 201


def test_get_detail_includes_steps_and_params(jobs_client, server_id):
    login(jobs_client, "admin@example.com")
    job_id = _post_job(jobs_client, server_id=server_id).json()["id"]
    resp = jobs_client.get(f"/api/jobs/{job_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == job_id
    assert "steps" in data and data["params_sanitized"] == {"message": "hello"}


def test_list_filters_by_status_and_mine(jobs_client, server_id, db_session):
    login(jobs_client, "admin@example.com")
    _post_job(jobs_client, server_id=server_id)
    # Everything is pending so far.
    running = jobs_client.get("/api/jobs?status=running").json()
    assert running == []
    pending = jobs_client.get("/api/jobs?status=pending").json()
    assert len(pending) == 1
    mine = jobs_client.get("/api/jobs?mine=true").json()
    assert len(mine) == 1


def test_cancel_pending_job(jobs_client, server_id):
    login(jobs_client, "admin@example.com")
    job_id = _post_job(jobs_client, server_id=server_id).json()["id"]
    resp = jobs_client.post(f"/api/jobs/{job_id}/cancel", headers=csrf_headers(jobs_client))
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"


def test_readonly_cannot_cancel(jobs_client, server_id):
    login(jobs_client, "admin@example.com")
    job_id = _post_job(jobs_client, server_id=server_id).json()["id"]
    login(jobs_client, "readonly@example.com")
    resp = jobs_client.post(f"/api/jobs/{job_id}/cancel", headers=csrf_headers(jobs_client))
    assert resp.status_code == 403


def test_retry_terminal_job_creates_new_pending(jobs_client, server_id):
    login(jobs_client, "admin@example.com")
    job_id = _post_job(jobs_client, server_id=server_id).json()["id"]
    # Cancel it (a real terminal transition that also frees the target lock),
    # then retry — the new job must be able to re-lock the same target.
    jobs_client.post(f"/api/jobs/{job_id}/cancel", headers=csrf_headers(jobs_client))

    resp = jobs_client.post(f"/api/jobs/{job_id}/retry", headers=csrf_headers(jobs_client))
    assert resp.status_code == 201, resp.text
    new = resp.json()
    assert new["id"] != job_id
    assert new["status"] == "pending"
    assert new["action_name"] == "system.echo_demo"


def test_retry_non_terminal_job_is_409(jobs_client, server_id):
    login(jobs_client, "admin@example.com")
    job_id = _post_job(jobs_client, server_id=server_id).json()["id"]  # still pending
    resp = jobs_client.post(f"/api/jobs/{job_id}/retry", headers=csrf_headers(jobs_client))
    assert resp.status_code == 409
