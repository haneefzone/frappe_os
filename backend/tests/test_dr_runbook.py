"""Tests for the DR runbook generator (session 4.3, DOO-159).

Covers:
- generate_runbook() service: md / pdf, fleet + single-server scope
- content: inventory rows, restore steps, secret-escrow safety (no secrets leak)
- POST /api/dr-runbook/generate: RBAC, headers, AuditLog content-hash recording
"""

import hashlib
from datetime import UTC, datetime, timedelta

import pytest

from app.core.dr_runbook import generate_runbook
from app.models import (
    AuditLog,
    Backup,
    BackupPolicy,
    Bench,
    ComplianceStatus,
    Domain,
    ResticRepo,
    Server,
    Site,
    StorageTarget,
)
from tests.conftest import csrf_headers, login

NOW = datetime(2026, 7, 23, 10, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _seed_fleet(db, *, name="dr.localhost"):
    """One server → bench → site with a policy, status, backup, domain, repo."""
    target = StorageTarget(
        name=f"s3-{name}", provider="aws", bucket="fdm-backups", region="me-central-1"
    )
    db.add(target)
    db.commit()

    srv = Server(
        name=f"vm-{name}", hostname="10.9.9.9", ssh_port=22,
        os_version="Ubuntu 24.04", env_tag="prod",
    )
    db.add(srv)
    db.commit()

    repo = ResticRepo(
        server_id=srv.id, storage_target_id=target.id, prefix=f"restic/{name}",
        initialized=True, last_backup_at=NOW - timedelta(hours=2),
        last_check_at=NOW - timedelta(days=1),
    )
    db.add(repo)

    bench = Bench(
        server_id=srv.id, path=f"/home/frappe/{name}-bench", name=f"{name}-bench",
        frappe_version="16.0.0", webserver_port=8000, socketio_port=9000,
    )
    db.add(bench)
    db.commit()

    site = Site(bench_id=bench.id, name=name, status="active")
    db.add(site)
    db.commit()

    db.add(Domain(site_id=site.id, domain=f"www.{name}", is_primary=True, ssl_enabled=True))
    db.add(
        BackupPolicy(
            site_id=site.id, rpo_hours=12, retention_days=30,
            require_offsite=True, enabled=True,
        )
    )
    db.add(
        ComplianceStatus(
            site_id=site.id, state="compliant",
            last_backup_at=NOW - timedelta(hours=2), evaluated_at=NOW,
        )
    )
    db.add(
        Backup(
            site_id=site.id, bench_id=bench.id, type="full", status="success",
            restore_tested=True, storage_state="offsite",
            created_at=NOW - timedelta(hours=2),
        )
    )
    db.commit()
    return srv, bench, site


# ---------------------------------------------------------------------------
# Service tests
# ---------------------------------------------------------------------------


class TestGenerateRunbookService:
    def test_md_empty_fleet(self, db_session):
        result = generate_runbook(db_session, fmt="md", now=NOW)
        assert result.media_type.startswith("text/markdown")
        assert result.server_count == 0
        text = result.content.decode("utf-8")
        assert "Disaster Recovery Runbook" in text
        assert "Recovery objectives" in text

    def test_md_with_fleet_data(self, db_session):
        srv, bench, site = _seed_fleet(db_session)
        result = generate_runbook(db_session, fmt="md", now=NOW)
        text = result.content.decode("utf-8")
        assert result.server_count == 1
        assert result.site_count == 1
        assert srv.name in text
        assert site.name in text
        assert "restore-tested ✓" in text
        # restore procedure references the encryption_key gotcha + no-downgrade rule
        assert "encryption_key" in text
        assert "NEVER downgrade" in text

    def test_pdf_returns_pdf_bytes(self, db_session):
        _seed_fleet(db_session)
        result = generate_runbook(db_session, fmt="pdf", now=NOW)
        assert result.media_type == "application/pdf"
        assert result.content[:4] == b"%PDF"

    def test_single_server_scope(self, db_session):
        srv, _, _ = _seed_fleet(db_session, name="a.localhost")
        # second server that must NOT appear in a scoped runbook
        other = Server(name="vm-other", hostname="10.0.0.2")
        db_session.add(other)
        db_session.commit()

        result = generate_runbook(db_session, fmt="md", server_id=srv.id, now=NOW)
        text = result.content.decode("utf-8")
        assert result.scope == srv.name
        assert result.server_count == 1
        assert srv.name in text
        assert "vm-other" not in text

    def test_unknown_server_raises(self, db_session):
        with pytest.raises(ValueError, match="Unknown server_id"):
            generate_runbook(db_session, fmt="md", server_id=999999, now=NOW)

    def test_invalid_format_raises(self, db_session):
        with pytest.raises(ValueError, match="Unknown format"):
            generate_runbook(db_session, fmt="docx", now=NOW)

    def test_no_secret_material_in_output(self, db_session):
        """The runbook points at escrow, never at secret values."""
        _seed_fleet(db_session)
        text = generate_runbook(db_session, fmt="md", now=NOW).content.decode("utf-8")
        # escrow pointers are present; secret column values are not rendered
        assert "master-key-escrow.md" in text
        assert "FDM_SECRET_KEY" in text
        # sanity: the words that would flag a leaked value column are absent
        assert "password_enc" not in text
        assert "secret_key_enc" not in text

    def test_content_hash_is_sha256(self, db_session):
        result = generate_runbook(db_session, fmt="md", now=NOW)
        assert result.content_hash == hashlib.sha256(result.content).hexdigest()

    def test_filename_scope_and_ext(self, db_session):
        srv, _, _ = _seed_fleet(db_session, name="b.localhost")
        md = generate_runbook(db_session, fmt="md", server_id=srv.id, now=NOW)
        assert md.filename.endswith(".md")
        pdf = generate_runbook(db_session, fmt="pdf", now=NOW)
        assert pdf.filename.startswith("dr-runbook-fleet-")
        assert pdf.filename.endswith(".pdf")


# ---------------------------------------------------------------------------
# API tests
# ---------------------------------------------------------------------------


class TestDrRunbookEndpoint:
    def _login(self, client, role):
        assert login(client, f"{role}@example.com").status_code == 200

    def test_admin_can_generate_md(self, client):
        self._login(client, "admin")
        resp = client.post(
            "/api/dr-runbook/generate", json={"format": "md"}, headers=csrf_headers(client)
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/markdown")
        assert len(resp.headers["X-Content-Hash-SHA256"]) == 64

    def test_developer_can_generate_pdf(self, client):
        self._login(client, "developer")
        resp = client.post(
            "/api/dr-runbook/generate", json={"format": "pdf"}, headers=csrf_headers(client)
        )
        assert resp.status_code == 200
        assert resp.content[:4] == b"%PDF"

    def test_readonly_cannot_generate(self, client):
        self._login(client, "readonly")
        resp = client.post(
            "/api/dr-runbook/generate", json={"format": "md"}, headers=csrf_headers(client)
        )
        assert resp.status_code == 403

    def test_unauthenticated_401(self, client):
        resp = client.post("/api/dr-runbook/generate", json={"format": "md"})
        assert resp.status_code == 401

    def test_invalid_format_422(self, client):
        self._login(client, "admin")
        resp = client.post(
            "/api/dr-runbook/generate", json={"format": "docx"}, headers=csrf_headers(client)
        )
        assert resp.status_code == 422

    def test_unknown_server_422(self, client):
        self._login(client, "admin")
        resp = client.post(
            "/api/dr-runbook/generate",
            json={"format": "md", "server_id": 424242},
            headers=csrf_headers(client),
        )
        assert resp.status_code == 422

    def test_generation_is_audited(self, client, db_session):
        self._login(client, "admin")
        resp = client.post(
            "/api/dr-runbook/generate", json={"format": "md"}, headers=csrf_headers(client)
        )
        assert resp.status_code == 200
        delivered = resp.headers["X-Content-Hash-SHA256"]

        from sqlalchemy import select
        log = db_session.scalar(
            select(AuditLog)
            .where(AuditLog.action == "dr.generate_runbook")
            .order_by(AuditLog.ts.desc())
        )
        assert log is not None
        assert log.params_masked.get("content_hash_sha256") == delivered
        assert log.params_masked.get("format") == "md"

    def test_hash_header_matches_body(self, client):
        self._login(client, "admin")
        resp = client.post(
            "/api/dr-runbook/generate", json={"format": "md"}, headers=csrf_headers(client)
        )
        assert resp.headers["X-Content-Hash-SHA256"] == hashlib.sha256(resp.content).hexdigest()

    def test_content_disposition_filename(self, client):
        self._login(client, "admin")
        resp = client.post(
            "/api/dr-runbook/generate", json={"format": "pdf"}, headers=csrf_headers(client)
        )
        cd = resp.headers.get("content-disposition", "")
        assert "dr-runbook" in cd
        assert ".pdf" in cd
