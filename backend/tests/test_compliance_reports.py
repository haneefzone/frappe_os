"""Tests for compliance report export endpoints (session 4.4, DOO-160).

Covers:
- generate_report() service: access / backup_evidence / access_review × csv / pdf
- POST /api/compliance-reports/generate: RBAC, response headers, AuditLog recording
- Content-hash tamper-evidence in AuditLog params
"""

import csv
import hashlib
import io
from datetime import UTC, datetime, timedelta

import pytest

from app.core.compliance_export import generate_report
from app.models import (
    AuditLog,
    BackupPolicy,
    Bench,
    ComplianceStatus,
    Server,
    Site,
)
from tests.conftest import csrf_headers, login

NOW = datetime(2026, 7, 23, 10, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _server_bench_site(db, name="report.localhost"):
    srv = Server(name=f"vm-{name}", hostname="10.0.0.1")
    db.add(srv)
    db.commit()
    bench = Bench(
        server_id=srv.id, path=f"/home/frappe/{name}-bench",
        name=f"{name}-bench", frappe_version="16.0.0",
    )
    db.add(bench)
    db.commit()
    site = Site(bench_id=bench.id, name=name, status="active")
    db.add(site)
    db.commit()
    return site



def _seed_compliance(db, site):
    policy = BackupPolicy(site_id=site.id, rpo_hours=24, retention_days=30, enabled=True)
    db.add(policy)
    db.commit()
    status = ComplianceStatus(
        site_id=site.id, state="compliant",
        last_backup_at=NOW - timedelta(hours=1),
        evaluated_at=NOW,
    )
    db.add(status)
    db.commit()
    return policy, status


# ---------------------------------------------------------------------------
# Unit tests — core export service
# ---------------------------------------------------------------------------


class TestGenerateReportService:
    def test_access_csv_empty_range(self, db_session):
        """CSV with no audit rows returns headers only."""
        result = generate_report(
            db_session, report_type="access", fmt="csv",
            since=NOW, until=NOW + timedelta(seconds=1),
        )
        assert result.media_type == "text/csv"
        assert result.row_count == 0
        reader = csv.reader(io.StringIO(result.content.decode("utf-8-sig")))
        rows = list(reader)
        assert rows[0][0] == "Timestamp (UTC)"  # header row present

    def test_access_csv_with_data(self, db_session):
        """Access CSV includes AuditLog rows within date range."""
        row = AuditLog(
            action="site.backup", entity_type="site", entity_id="42",
            summary="Backed up site report.localhost",
            result="enqueued", ts=NOW,
        )
        db_session.add(row)
        db_session.commit()

        result = generate_report(
            db_session, report_type="access", fmt="csv",
            since=NOW - timedelta(minutes=1), until=NOW + timedelta(minutes=1),
        )
        assert result.row_count == 1
        reader = csv.reader(io.StringIO(result.content.decode("utf-8-sig")))
        rows = list(reader)
        assert len(rows) == 2  # header + 1 data row
        assert "site.backup" in rows[1]

    def test_access_pdf_returns_bytes(self, db_session):
        result = generate_report(
            db_session, report_type="access", fmt="pdf",
        )
        assert result.media_type == "application/pdf"
        assert result.content[:4] == b"%PDF"

    def test_backup_evidence_csv(self, db_session):
        site = _server_bench_site(db_session)
        _seed_compliance(db_session, site)

        result = generate_report(db_session, report_type="backup_evidence", fmt="csv")
        assert result.row_count == 1
        reader = csv.reader(io.StringIO(result.content.decode("utf-8-sig")))
        rows = list(reader)
        assert rows[0][0] == "Site"
        assert rows[1][0] == site.name
        assert rows[1][5] == "compliant"

    def test_backup_evidence_pdf(self, db_session):
        _seed_compliance(db_session, _server_bench_site(db_session))
        result = generate_report(db_session, report_type="backup_evidence", fmt="pdf")
        assert result.media_type == "application/pdf"
        assert result.content[:4] == b"%PDF"

    def test_access_review_csv(self, db_session, seeded_users):
        result = generate_report(db_session, report_type="access_review", fmt="csv")
        reader = csv.reader(io.StringIO(result.content.decode("utf-8-sig")))
        rows = list(reader)
        emails = [r[1] for r in rows[1:]]
        assert "admin@example.com" in emails
        assert "developer@example.com" in emails
        assert "readonly@example.com" in emails

    def test_access_review_pdf(self, db_session, seeded_users):
        result = generate_report(db_session, report_type="access_review", fmt="pdf")
        assert result.media_type == "application/pdf"
        assert result.content[:4] == b"%PDF"

    def test_content_hash_is_sha256(self, db_session):
        result = generate_report(db_session, report_type="access", fmt="csv")
        expected = hashlib.sha256(result.content).hexdigest()
        assert result.content_hash == expected

    def test_filename_contains_type(self, db_session):
        result = generate_report(db_session, report_type="backup_evidence", fmt="csv")
        assert "backup-evidence" in result.filename
        assert result.filename.endswith(".csv")

    def test_invalid_type_raises(self, db_session):
        with pytest.raises(ValueError, match="Unknown report_type"):
            generate_report(db_session, report_type="unknown", fmt="csv")

    def test_invalid_format_raises(self, db_session):
        with pytest.raises(ValueError, match="Unknown format"):
            generate_report(db_session, report_type="access", fmt="docx")


# ---------------------------------------------------------------------------
# API tests — POST /api/compliance-reports/generate
# ---------------------------------------------------------------------------


class TestComplianceReportEndpoint:
    def _login(self, client, role="admin"):
        resp = login(client, f"{role}@example.com")
        assert resp.status_code == 200

    def test_admin_can_generate_csv(self, client):
        self._login(client, "admin")
        resp = client.post(
            "/api/compliance-reports/generate",
            json={"report_type": "access", "format": "csv"},
            headers=csrf_headers(client),
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/csv")
        assert "X-Content-Hash-SHA256" in resp.headers
        assert len(resp.headers["X-Content-Hash-SHA256"]) == 64  # sha256 hex

    def test_developer_can_generate(self, client):
        self._login(client, "developer")
        resp = client.post(
            "/api/compliance-reports/generate",
            json={"report_type": "access_review", "format": "pdf"},
            headers=csrf_headers(client),
        )
        assert resp.status_code == 200
        assert resp.content[:4] == b"%PDF"

    def test_readonly_cannot_generate(self, client):
        self._login(client, "readonly")
        resp = client.post(
            "/api/compliance-reports/generate",
            json={"report_type": "access", "format": "csv"},
            headers=csrf_headers(client),
        )
        assert resp.status_code == 403

    def test_unauthenticated_returns_401(self, client):
        resp = client.post(
            "/api/compliance-reports/generate",
            json={"report_type": "access", "format": "csv"},
        )
        assert resp.status_code == 401

    def test_invalid_report_type_422(self, client):
        self._login(client, "admin")
        resp = client.post(
            "/api/compliance-reports/generate",
            json={"report_type": "secret_dump", "format": "csv"},
            headers=csrf_headers(client),
        )
        assert resp.status_code == 422

    def test_invalid_format_422(self, client):
        self._login(client, "admin")
        resp = client.post(
            "/api/compliance-reports/generate",
            json={"report_type": "access", "format": "docx"},
            headers=csrf_headers(client),
        )
        assert resp.status_code == 422

    def test_generation_is_audited(self, client, db_session):
        """Each export records an AuditLog row with the content hash."""
        self._login(client, "admin")
        resp = client.post(
            "/api/compliance-reports/generate",
            json={"report_type": "access", "format": "csv"},
            headers=csrf_headers(client),
        )
        assert resp.status_code == 200
        delivered_hash = resp.headers["X-Content-Hash-SHA256"]

        # AuditLog must have a row for compliance.export_report with the hash.
        from sqlalchemy import select as sel
        log = db_session.scalar(
            sel(AuditLog)
            .where(AuditLog.action == "compliance.export_report")
            .order_by(AuditLog.ts.desc())
        )
        assert log is not None
        assert log.params_masked.get("content_hash_sha256") == delivered_hash
        assert log.params_masked.get("report_type") == "access"
        assert log.params_masked.get("format") == "csv"

    def test_hash_matches_content(self, client):
        self._login(client, "admin")
        resp = client.post(
            "/api/compliance-reports/generate",
            json={"report_type": "access", "format": "csv"},
            headers=csrf_headers(client),
        )
        assert resp.status_code == 200
        delivered_hash = resp.headers["X-Content-Hash-SHA256"]
        computed = hashlib.sha256(resp.content).hexdigest()
        assert delivered_hash == computed

    def test_content_disposition_filename(self, client):
        self._login(client, "admin")
        resp = client.post(
            "/api/compliance-reports/generate",
            json={"report_type": "backup_evidence", "format": "pdf"},
            headers=csrf_headers(client),
        )
        assert resp.status_code == 200
        cd = resp.headers.get("content-disposition", "")
        assert "backup-evidence" in cd
        assert ".pdf" in cd

    def test_date_range_filtering(self, client, db_session):
        """Audit rows outside since/until are excluded from access reports."""
        old = AuditLog(
            action="site.create", entity_type="site", entity_id="1",
            summary="old event", result="ok",
            ts=datetime(2020, 1, 1, tzinfo=UTC),
        )
        recent = AuditLog(
            action="site.backup", entity_type="site", entity_id="2",
            summary="recent event", result="enqueued",
            ts=datetime(2026, 7, 22, 12, 0, tzinfo=UTC),
        )
        db_session.add_all([old, recent])
        db_session.commit()

        self._login(client, "admin")
        resp = client.post(
            "/api/compliance-reports/generate",
            json={
                "report_type": "access",
                "format": "csv",
                "since": "2026-01-01T00:00:00Z",
                "until": "2026-12-31T23:59:59Z",
            },
            headers=csrf_headers(client),
        )
        assert resp.status_code == 200
        reader = csv.reader(io.StringIO(resp.text))
        rows = list(reader)
        summaries = [r[8] for r in rows[1:]]
        assert "recent event" in summaries
        assert "old event" not in summaries
