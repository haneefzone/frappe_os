"""Bench inventory API: listing (grouped-by-server flat list), detail, discovery
launch with RBAC/validation, all with an in-memory job runner (no Redis/SSH)."""

import pytest

from app.api.routes.jobs import get_job_runner
from app.core.jobs import InMemoryJobBackend, JobRunner
from app.models import Server
from app.models.bench import Bench
from tests.conftest import csrf_headers, login


@pytest.fixture
def benches_client(client, db_session):
    runner = JobRunner(lambda: db_session, InMemoryJobBackend(), enqueue=lambda job: None)
    client.app.dependency_overrides[get_job_runner] = lambda: runner
    yield client
    client.app.dependency_overrides.pop(get_job_runner, None)


@pytest.fixture
def two_servers_with_benches(db_session):
    a = Server(name="vm-a", hostname="10.0.0.1")
    b = Server(name="vm-b", hostname="10.0.0.2")
    db_session.add_all([a, b])
    db_session.commit()
    db_session.add_all(
        [
            Bench(server_id=a.id, name="bench-16", path="/home/frappe/bench-16",
                  frappe_version="16.25.0", webserver_port=8000, status="active"),
            Bench(server_id=a.id, name="bench-15", path="/home/frappe/bench-15",
                  frappe_version="15.42.1", webserver_port=8001, status="missing"),
            Bench(server_id=b.id, name="bench-14", path="/home/frappe/bench-14",
                  frappe_version="14.9.0", webserver_port=8002, status="active"),
        ]
    )
    db_session.commit()
    return {"a": a.id, "b": b.id}


def test_list_all_benches(benches_client, two_servers_with_benches):
    login(benches_client, "readonly@example.com")
    resp = benches_client.get("/api/benches")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 3
    # Ports arrive nested under `ports` for the popover.
    row = next(r for r in data if r["name"] == "bench-16")
    assert row["ports"]["webserver_port"] == 8000
    assert row["frappe_version"] == "16.25.0"


def test_list_benches_filtered_by_server(benches_client, two_servers_with_benches):
    login(benches_client, "readonly@example.com")
    resp = benches_client.get(f"/api/benches?server={two_servers_with_benches['b']}")
    assert resp.status_code == 200
    data = resp.json()
    assert [r["name"] for r in data] == ["bench-14"]


def test_get_bench_detail(benches_client, two_servers_with_benches, db_session):
    login(benches_client, "readonly@example.com")
    bench = db_session.query(Bench).filter_by(name="bench-15").one()
    resp = benches_client.get(f"/api/benches/{bench.id}")
    assert resp.status_code == 200
    assert resp.json()["status"] == "missing"


def test_get_unknown_bench_is_404(benches_client, two_servers_with_benches):
    login(benches_client, "readonly@example.com")
    assert benches_client.get("/api/benches/999999").status_code == 404


def test_discover_launches_pending_job(benches_client, two_servers_with_benches):
    login(benches_client, "developer@example.com")
    sid = two_servers_with_benches["a"]
    resp = benches_client.post(
        f"/api/servers/{sid}/discover-benches", json={}, headers=csrf_headers(benches_client)
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["status"] == "pending"
    assert data["action_name"] == "bench.discover"
    assert data["server_id"] == sid


def test_discover_with_base_paths_passes_validated_param(benches_client, two_servers_with_benches):
    login(benches_client, "developer@example.com")
    sid = two_servers_with_benches["a"]
    resp = benches_client.post(
        f"/api/servers/{sid}/discover-benches",
        json={"base_paths": ["/opt", "/srv"]},
        headers=csrf_headers(benches_client),
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["params_sanitized"]["base_paths"] == "/opt,/srv"


def test_discover_rejects_bad_base_path(benches_client, two_servers_with_benches):
    login(benches_client, "developer@example.com")
    sid = two_servers_with_benches["a"]
    resp = benches_client.post(
        f"/api/servers/{sid}/discover-benches",
        json={"base_paths": ["/opt; rm -rf /"]},
        headers=csrf_headers(benches_client),
    )
    assert resp.status_code == 422


def test_readonly_cannot_discover(benches_client, two_servers_with_benches):
    login(benches_client, "readonly@example.com")
    sid = two_servers_with_benches["a"]
    resp = benches_client.post(
        f"/api/servers/{sid}/discover-benches", json={}, headers=csrf_headers(benches_client)
    )
    assert resp.status_code == 403


def test_discover_unknown_server_is_404(benches_client):
    login(benches_client, "admin@example.com")
    resp = benches_client.post(
        "/api/servers/999999/discover-benches", json={}, headers=csrf_headers(benches_client)
    )
    assert resp.status_code == 404


def test_discover_second_run_conflicts_409(benches_client, two_servers_with_benches):
    login(benches_client, "developer@example.com")
    sid = two_servers_with_benches["a"]
    first = benches_client.post(
        f"/api/servers/{sid}/discover-benches", json={}, headers=csrf_headers(benches_client)
    )
    assert first.status_code == 201
    second = benches_client.post(
        f"/api/servers/{sid}/discover-benches", json={}, headers=csrf_headers(benches_client)
    )
    assert second.status_code == 409
    assert second.json()["error"]["blocking_job_id"] == first.json()["id"]
