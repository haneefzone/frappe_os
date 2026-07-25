"""Reports suite (session 6.2, DOO-256): catalogue, RBAC, generation, download.

The seven report generators are exercised end-to-end through the synchronous
CSV path so each one is proven to generate against the seeded DB, produce a
`ReportRun` with an integrity hash, and hand back a downloadable artifact whose
CSV opens cleanly. The async job path, the PDF renderer, the per-report RBAC
gate (Read-only → 403 on the two sensitive exports), and the run-list filter are
covered separately.
"""

import csv
import io

import pytest

from app.api.routes.jobs import get_job_runner
from app.config import get_settings
from app.core.jobs import InMemoryJobBackend, JobRunner
from app.core.reports import all_reports
from tests.conftest import csrf_headers, login

ALL_IDS = {
    "fleet_summary",
    "backup_evidence",
    "job_history",
    "app_versions",
    "ssl_expiry",
    "user_activity",
    "server_capacity",
}
SENSITIVE_IDS = {"backup_evidence", "user_activity"}
NON_SENSITIVE_IDS = ALL_IDS - SENSITIVE_IDS


@pytest.fixture(autouse=True)
def _isolate_reports_dir(tmp_path):
    """Point artifact output at a throwaway dir so tests never litter the repo."""
    settings = get_settings()
    original = settings.reports_dir
    settings.reports_dir = str(tmp_path / "reports")
    yield
    settings.reports_dir = original


@pytest.fixture
def reports_client(client, db_session):
    """The shared TestClient with the job runner overridden to an in-memory
    backend (the async run path enqueues a report.generate job)."""
    runner = JobRunner(lambda: db_session, InMemoryJobBackend(), enqueue=lambda job: None)
    client.app.dependency_overrides[get_job_runner] = lambda: runner
    yield client
    client.app.dependency_overrides.pop(get_job_runner, None)


def _run_sync(client, report_id: str):
    return client.post(
        f"/api/reports/{report_id}/run?sync=true",
        json={"format": "csv"},
        headers=csrf_headers(client),
    )


# --------------------------------------------------------------------------- #
# Registry / catalogue
# --------------------------------------------------------------------------- #


def test_registry_has_seven_reports():
    assert {r.id for r in all_reports()} == ALL_IDS


def test_list_reports_admin_sees_all(reports_client):
    login(reports_client, "admin@example.com")
    resp = reports_client.get("/api/reports")
    assert resp.status_code == 200, resp.text
    assert {r["id"] for r in resp.json()} == ALL_IDS


def test_list_reports_readonly_hides_sensitive(reports_client):
    login(reports_client, "readonly@example.com")
    resp = reports_client.get("/api/reports")
    assert resp.status_code == 200, resp.text
    ids = {r["id"] for r in resp.json()}
    assert ids == NON_SENSITIVE_IDS
    assert not (ids & SENSITIVE_IDS)


def test_list_reports_unauthenticated_401(reports_client):
    assert reports_client.get("/api/reports").status_code == 401


# --------------------------------------------------------------------------- #
# Generation — every report, synchronous CSV
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("report_id", sorted(ALL_IDS))
def test_report_generates_csv_admin(reports_client, report_id):
    login(reports_client, "admin@example.com")
    resp = _run_sync(reports_client, report_id)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["report_id"] == report_id
    assert body["status"] == "success"
    assert body["format"] == "csv"
    assert isinstance(body["row_count"], int) and body["row_count"] >= 0
    assert len(body["sha256"]) == 64
    assert body["artifact_bytes"] > 0
    assert body["download_url"] == f"/api/report-runs/{body['id']}/download"

    # The artifact downloads and parses as CSV.
    dl = reports_client.get(body["download_url"])
    assert dl.status_code == 200, dl.text
    assert dl.headers["content-type"].startswith("text/csv")
    rows = list(csv.reader(io.StringIO(dl.content.decode())))
    assert len(rows) >= 1  # at least the evidence/summary header block


@pytest.mark.parametrize("report_id", sorted(ALL_IDS))
def test_report_renders_pdf(db_session, report_id):
    """Every report renders a valid PDF (pure reportlab, opens cleanly)."""
    from datetime import UTC, datetime

    from app.core.reports import build
    from app.core.reports.render import render_pdf

    report, resolved, result = build(
        db_session, report_id, {}, now=datetime.now(UTC)
    )
    pdf = render_pdf(
        report,
        result,
        generated_at=datetime.now(UTC),
        generated_by="tester@example.com",
        params=resolved,
        window=None,
    )
    assert pdf[:4] == b"%PDF"
    assert pdf.rstrip().endswith(b"%%EOF")


def test_evidence_report_carries_self_describing_header(reports_client):
    """backup_evidence (evidence=True) must state generated-at/by, range, rows."""
    login(reports_client, "admin@example.com")
    resp = _run_sync(reports_client, "backup_evidence")
    assert resp.status_code == 200, resp.text
    run_id = resp.json()["id"]
    text = reports_client.get(f"/api/report-runs/{run_id}/download").content.decode()
    assert "# Report" in text
    assert "# Generated at" in text
    assert "# Generated by" in text
    assert "# Date range" in text
    assert "# Rows" in text


# --------------------------------------------------------------------------- #
# RBAC
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("report_id", sorted(SENSITIVE_IDS))
def test_readonly_forbidden_on_sensitive_reports(reports_client, report_id):
    login(reports_client, "readonly@example.com")
    resp = _run_sync(reports_client, report_id)
    assert resp.status_code == 403, resp.text


def test_readonly_can_run_non_sensitive_report(reports_client):
    login(reports_client, "readonly@example.com")
    resp = _run_sync(reports_client, "fleet_summary")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "success"


def test_run_unknown_report_404(reports_client):
    login(reports_client, "admin@example.com")
    resp = reports_client.post(
        "/api/reports/does_not_exist/run?sync=true",
        json={"format": "csv"},
        headers=csrf_headers(reports_client),
    )
    assert resp.status_code == 404, resp.text


# --------------------------------------------------------------------------- #
# Async job path + PDF
# --------------------------------------------------------------------------- #


def test_pdf_run_enqueues_job_202(reports_client):
    login(reports_client, "admin@example.com")
    resp = reports_client.post(
        "/api/reports/fleet_summary/run",
        json={"format": "pdf"},
        headers=csrf_headers(reports_client),
    )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["report_id"] == "fleet_summary"
    assert isinstance(body["job_id"], int)


def test_emailed_run_forces_job_path(reports_client):
    """Recipients present → always a job (the worker sends mail), never sync."""
    login(reports_client, "admin@example.com")
    resp = reports_client.post(
        "/api/reports/fleet_summary/run?sync=true",
        json={"format": "csv", "recipients": "ops@example.com"},
        headers=csrf_headers(reports_client),
    )
    assert resp.status_code == 202, resp.text
    assert isinstance(resp.json()["job_id"], int)


# --------------------------------------------------------------------------- #
# Run history
# --------------------------------------------------------------------------- #


def test_report_runs_list_filters_sensitive_for_readonly(reports_client):
    # Admin generates a sensitive run.
    login(reports_client, "admin@example.com")
    admin_run = _run_sync(reports_client, "backup_evidence").json()["id"]
    _run_sync(reports_client, "fleet_summary")

    # Read-only must not see the sensitive run in the list, nor download it.
    login(reports_client, "readonly@example.com")
    listed = reports_client.get("/api/report-runs")
    assert listed.status_code == 200, listed.text
    ids = {row["report_id"] for row in listed.json()}
    assert "backup_evidence" not in ids
    assert "fleet_summary" in ids

    denied = reports_client.get(f"/api/report-runs/{admin_run}/download")
    assert denied.status_code == 403, denied.text


def test_download_missing_run_404(reports_client):
    login(reports_client, "admin@example.com")
    assert reports_client.get("/api/report-runs/999999/download").status_code == 404
