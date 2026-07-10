"""Audit log (session 1.12): the shared writer, the JobRunner funnel that audits
every job-backed mutation, non-job endpoint audit (server register), and the
Audit API (list + filters + CSV). Verifies rule 2 — no state change without a row."""

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.audit import mask_params, record_audit
from app.core.jobs import InMemoryJobBackend, JobRunner
from app.db import Base
from app.models import AuditLog, Server
from tests.conftest import csrf_headers, login


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
# mask_params + record_audit
# --------------------------------------------------------------------------- #


def test_mask_params_masks_secret_looking_keys():
    masked = mask_params(
        {"site": "dev.localhost", "admin_password": "hunter2", "api_token": "abc"}
    )
    assert masked["site"] == "dev.localhost"
    assert masked["admin_password"] == "••••"
    assert masked["api_token"] == "••••"


def test_record_audit_writes_row(sf):
    with sf() as db:
        row = record_audit(
            db,
            action="server.register",
            summary="Registered server vm",
            entity_type="server",
            entity_id=7,
            params={"password": "secret"},
            source_ip="1.2.3.4",
        )
        assert row is not None
        assert row.entity_id == "7"  # coerced to text
        assert row.params_masked["password"] == "••••"
        assert row.source_ip == "1.2.3.4"


def test_record_audit_never_raises_on_write_failure(sf):
    # A write failure (here: a non-JSON-serialisable param) must not raise into
    # the caller — the mutation already happened; losing one audit row is better
    # than a 500. record_audit swallows it and returns None.
    with sf() as db:
        result = record_audit(
            db, action="x", summary="y", params={"bad": {1, 2, 3}}, already_masked=True
        )
        assert result is None


# --------------------------------------------------------------------------- #
# JobRunner funnel: every job writes exactly one audit row
# --------------------------------------------------------------------------- #


def test_job_creation_writes_audit(sf):
    runner = JobRunner(sf, InMemoryJobBackend(), enqueue=lambda job: None)
    with sf() as db:
        server = Server(name="vm", hostname="10.0.0.1")
        db.add(server)
        db.commit()
        job = runner.create(
            db,
            action_name="system.echo_demo",
            server_id=server.id,
            target_type="server",
            target_id=str(server.id),
            params={"message": "hello"},
            priority="default",
            created_by=None,
        )
        rows = db.scalars(
            select(AuditLog).where(AuditLog.job_id == job.id)
        ).all()
        assert len(rows) == 1
        assert rows[0].action == "system.echo_demo"
        assert rows[0].result == "enqueued"


# --------------------------------------------------------------------------- #
# Non-job endpoint audit + Audit API
# --------------------------------------------------------------------------- #


def _register_server(client):
    body = {
        "name": "vm-audit",
        "hostname": "10.0.0.9",
        "ssh_port": 22,
        "env_tag": "dev",
        "credential": {"username": "frappe", "auth_type": "key", "private_key": "KEY"},
    }
    return client.post("/api/servers", json=body, headers=csrf_headers(client))


def test_server_register_writes_audit_and_appears_in_api(client, db_session):
    login(client, "admin@example.com")
    assert _register_server(client).status_code == 201

    # A row exists in the DB...
    rows = db_session.scalars(
        select(AuditLog).where(AuditLog.action == "server.register")
    ).all()
    assert len(rows) == 1
    assert rows[0].source_ip is not None

    # ...and the Audit API surfaces it.
    resp = client.get("/api/audit?action=server.register")
    assert resp.status_code == 200, resp.text
    page = resp.json()
    assert page["total"] == 1
    entry = page["entries"][0]
    assert entry["action"] == "server.register"
    assert entry["user_email"] == "admin@example.com"


def test_audit_login_records_success(client):
    login(client, "admin@example.com")
    resp = client.get("/api/audit?action=auth.login")
    assert resp.status_code == 200
    entries = resp.json()["entries"]
    assert any(e["result"] == "ok" for e in entries)


def test_audit_filter_and_csv_export(client, db_session):
    login(client, "admin@example.com")
    _register_server(client)

    # CSV export returns text/csv with the header + the register row.
    resp = client.get("/api/audit.csv?action=server.register")
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/csv")
    text = resp.content.decode()
    assert "action" in text.splitlines()[0]
    assert "server.register" in text


def test_audit_readonly_can_view(client, db_session):
    login(client, "readonly@example.com")
    assert client.get("/api/audit").status_code == 200
