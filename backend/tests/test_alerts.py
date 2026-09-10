"""AlertRule engine (session 3.1, FDM 3.1).

Covers the acceptance gates from the ticket:
- a `disk_pct > 85` breach fires exactly one email and one webhook;
- the webhook signature verifies with the shared secret, a tampered body fails;
- cooldown suppresses repeat firing within the window; recovery + re-breach after
  the window fires again;
- no plaintext secret in the DB (only a Fernet token) or on the firing row;
- the Open Alerts KPI + Incidents feed reflect active alerts;
- the CRUD API encrypts the webhook secret, never echoes it, and is RBAC-gated
  (Read-only can never mutate — golden rule 7).

All channel I/O goes through injected senders — no SMTP/network.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.core import alerts as al
from app.core.security import get_secrets_service
from app.models import AlertFiring, AlertRule, MonitoringSample, Server
from tests.conftest import csrf_headers, login

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

BASE = datetime(2026, 7, 25, 12, 0, 0, tzinfo=UTC)


def _server(db, name="vm-a", hostname="10.0.0.1") -> Server:
    s = Server(name=name, hostname=hostname)
    db.add(s)
    db.commit()
    return s


def _sample(db, server_id, *, disk_pct=None, cpu_pct=None, mem_pct=None, ts=BASE, ok=True):
    row = MonitoringSample(
        server_id=server_id, ok=ok, disk_pct=disk_pct, cpu_pct=cpu_pct, mem_pct=mem_pct, ts=ts
    )
    db.add(row)
    db.commit()
    return row


def _rule(db, secrets, **kw) -> AlertRule:
    defaults = dict(
        name="disk-high",
        metric="disk_pct",
        comparator=">",
        threshold=85.0,
        scope="global",
        cooldown_minutes=30,
        channel_email=True,
        channel_webhook=True,
        email_to="ops@example.com",
        webhook_url="https://hooks.example/alert",
        webhook_secret_enc=secrets.encrypt("shhh-signing-secret"),
        enabled=True,
    )
    defaults.update(kw)
    rule = AlertRule(**defaults)
    db.add(rule)
    db.commit()
    return rule


class _Capture:
    """Injectable email + webhook sinks + a synchronous `deliver` for the sweep."""

    def __init__(self, db, secrets):
        self.db = db
        self.secrets = secrets
        self.emails: list[tuple] = []
        self.webhooks: list[tuple] = []

    def send_email(self, recipients, subject, body, db):
        self.emails.append((recipients, subject, body))

    def post_webhook(self, url, body, signature, **kw):
        self.webhooks.append((url, body, signature))
        self.webhook_kwargs = kw

    def deliver(self, firing_id):
        firing = self.db.get(AlertFiring, firing_id)
        al.deliver_firing(self.db, firing, secrets=self.secrets)


@pytest.fixture
def cap(db_session, monkeypatch):
    secrets = get_secrets_service()
    c = _Capture(db_session, secrets)
    monkeypatch.setattr(al, "send_email", c.send_email)
    monkeypatch.setattr(al, "post_webhook", c.post_webhook)
    return c


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #


def test_compare_truth_table():
    assert al.compare(90, ">", 85)
    assert not al.compare(80, ">", 85)
    assert al.compare(85, ">=", 85)
    assert al.compare(10, "<", 20)
    assert al.compare(20, "<=", 20)
    assert al.compare(5, "==", 5)
    assert not al.compare(5, "==", 6)
    assert not al.compare(5, "!!", 6)  # unknown comparator -> False


def test_sign_and_verify_roundtrip():
    body = b'{"metric":"disk_pct"}'
    sig = al.sign_payload("secret", body)
    assert sig.startswith("sha256=")
    assert al.verify_signature("secret", body, sig)
    # Tampered body fails.
    assert not al.verify_signature("secret", body + b"x", sig)
    # Wrong secret fails.
    assert not al.verify_signature("other", body, sig)
    assert not al.verify_signature("secret", body, "")


def test_latest_sample_picks_most_recent_ok(db_session):
    s = _server(db_session)
    _sample(db_session, s.id, disk_pct=10, ts=BASE - timedelta(minutes=2))
    _sample(db_session, s.id, disk_pct=90, ts=BASE)
    _sample(db_session, s.id, disk_pct=99, ts=BASE - timedelta(minutes=1), ok=False)
    latest = al.latest_sample(db_session, s.id)
    assert latest.disk_pct == 90  # newest successful, ignoring the failed poll


# --------------------------------------------------------------------------- #
# Evaluation + dispatch
# --------------------------------------------------------------------------- #


def test_breach_fires_exactly_one_email_and_one_webhook(db_session, cap):
    s = _server(db_session)
    _sample(db_session, s.id, disk_pct=90, ts=BASE)
    _rule(db_session, cap.secrets)

    summary = al.evaluate_alerts(db_session, now=BASE, deliver=cap.deliver)

    assert summary["fired"] == 1
    assert len(cap.emails) == 1
    assert len(cap.webhooks) == 1
    # One firing recorded, with both channels marked delivered ok.
    firing = db_session.query(AlertFiring).one()
    assert firing.value == 90
    chans = {c["channel"]: c["ok"] for c in firing.channels}
    assert chans == {"email": True, "webhook": True}
    assert firing.delivered_at is not None


def test_webhook_signature_verifies_and_tamper_fails(db_session, cap):
    s = _server(db_session)
    _sample(db_session, s.id, disk_pct=91, ts=BASE)
    _rule(db_session, cap.secrets)

    al.evaluate_alerts(db_session, now=BASE, deliver=cap.deliver)

    url, body, signature = cap.webhooks[0]
    assert al.verify_signature("shhh-signing-secret", body, signature)
    assert not al.verify_signature("shhh-signing-secret", body + b"!", signature)


def test_cooldown_suppresses_then_refires_after_window(db_session, cap):
    s = _server(db_session)
    _sample(db_session, s.id, disk_pct=90, ts=BASE)
    _rule(db_session, cap.secrets, cooldown_minutes=30)

    # First breach fires.
    al.evaluate_alerts(db_session, now=BASE, deliver=cap.deliver)
    assert len(cap.webhooks) == 1

    # Sustained breach 10 min later — within the cooldown window: suppressed.
    s2 = al.evaluate_alerts(db_session, now=BASE + timedelta(minutes=10), deliver=cap.deliver)
    assert s2["fired"] == 0
    assert len(cap.webhooks) == 1

    # Recovery: disk drops below threshold. Resolves the open incident.
    _sample(db_session, s.id, disk_pct=40, ts=BASE + timedelta(minutes=20))
    s3 = al.evaluate_alerts(db_session, now=BASE + timedelta(minutes=20), deliver=cap.deliver)
    assert s3["resolved"] == 1
    assert al.open_alerts_count(db_session) == 0

    # Re-breach after the window has elapsed: fires again.
    _sample(db_session, s.id, disk_pct=95, ts=BASE + timedelta(minutes=40))
    s4 = al.evaluate_alerts(db_session, now=BASE + timedelta(minutes=40), deliver=cap.deliver)
    assert s4["fired"] == 1
    assert len(cap.webhooks) == 2


def test_no_breach_no_fire(db_session, cap):
    s = _server(db_session)
    _sample(db_session, s.id, disk_pct=50, ts=BASE)
    _rule(db_session, cap.secrets)
    summary = al.evaluate_alerts(db_session, now=BASE, deliver=cap.deliver)
    assert summary["fired"] == 0
    assert cap.webhooks == []
    assert al.open_alerts_count(db_session) == 0


def test_secret_never_stored_plaintext(db_session, cap):
    s = _server(db_session)
    _sample(db_session, s.id, disk_pct=90, ts=BASE)
    rule = _rule(db_session, cap.secrets)
    al.evaluate_alerts(db_session, now=BASE, deliver=cap.deliver)

    # The stored column is a Fernet token, not the plaintext.
    assert rule.webhook_secret_enc != "shhh-signing-secret"
    assert cap.secrets.decrypt(rule.webhook_secret_enc) == "shhh-signing-secret"
    # The firing's channel outcomes carry no secret material.
    firing = db_session.query(AlertFiring).one()
    assert "shhh-signing-secret" not in repr(firing.channels)


def test_server_scoped_rule_only_evaluates_its_server(db_session, cap):
    a = _server(db_session, name="vm-a", hostname="10.0.0.1")
    b = _server(db_session, name="vm-b", hostname="10.0.0.2")
    _sample(db_session, a.id, disk_pct=95, ts=BASE)
    _sample(db_session, b.id, disk_pct=95, ts=BASE)
    _rule(db_session, cap.secrets, scope="server", scope_server_id=b.id)

    al.evaluate_alerts(db_session, now=BASE, deliver=cap.deliver)
    firing = db_session.query(AlertFiring).one()
    assert firing.server_id == b.id  # vm-a ignored


def test_open_alerts_and_incidents_feed(db_session, cap):
    s = _server(db_session)
    _sample(db_session, s.id, disk_pct=90, ts=BASE)
    _rule(db_session, cap.secrets)
    al.evaluate_alerts(db_session, now=BASE, deliver=cap.deliver)

    assert al.open_alerts_count(db_session) == 1
    incidents = al.recent_incidents(db_session, limit=10)
    assert len(incidents) == 1
    assert incidents[0].metric == "disk_pct"


# --------------------------------------------------------------------------- #
# Webhook retry/backoff (pure)
# --------------------------------------------------------------------------- #


def test_webhook_retries_then_succeeds(monkeypatch):
    calls = {"n": 0}
    sleeps: list[float] = []

    class _Resp:
        def raise_for_status(self):
            return None

    def flaky_post(url, content, headers, timeout):
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("connection reset")
        return _Resp()

    monkeypatch.setattr(al.httpx, "post", flaky_post)
    al.post_webhook("https://x/y", b"body", "sha256=abc", sleep=sleeps.append)
    assert calls["n"] == 3
    assert sleeps == [0.5, 1.0]  # backoff between the 3 attempts


def test_webhook_raises_after_exhausting_retries(monkeypatch):
    def dead_post(url, content, headers, timeout):
        raise RuntimeError("no route to host")

    monkeypatch.setattr(al.httpx, "post", dead_post)
    with pytest.raises(RuntimeError):
        al.post_webhook("https://x/y", b"body", "sha256=abc", sleep=lambda _: None)


# --------------------------------------------------------------------------- #
# CRUD API + RBAC
# --------------------------------------------------------------------------- #


def test_create_rule_encrypts_secret_and_never_echoes(client, db_session):
    login(client, "admin@example.com")
    resp = client.post(
        "/api/alerts",
        headers=csrf_headers(client),
        json={
            "name": "disk-warn",
            "metric": "disk_pct",
            "comparator": ">",
            "threshold": 85,
            "scope": "global",
            "cooldown_minutes": 30,
            "channel_webhook": True,
            "webhook_url": "https://hooks.example/x",
            "webhook_secret": "top-secret",
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["webhook_secret_set"] is True
    assert "webhook_secret" not in body
    assert "top-secret" not in resp.text
    # Stored as a Fernet token, not plaintext.
    rule = db_session.get(AlertRule, body["id"])
    assert rule.webhook_secret_enc and rule.webhook_secret_enc != "top-secret"


def test_create_requires_at_least_one_channel(client):
    login(client, "admin@example.com")
    resp = client.post(
        "/api/alerts",
        headers=csrf_headers(client),
        json={"name": "x", "metric": "cpu_pct", "comparator": ">", "threshold": 90},
    )
    assert resp.status_code == 422


def test_readonly_cannot_create_rule(client):
    login(client, "readonly@example.com")
    resp = client.post(
        "/api/alerts",
        headers=csrf_headers(client),
        json={
            "name": "y", "metric": "cpu_pct", "comparator": ">", "threshold": 90,
            "channel_email": True, "email_to": "a@b.c",
        },
    )
    assert resp.status_code == 403


def test_readonly_can_list_rules(client, db_session):
    _rule(db_session, get_secrets_service(), name="visible")
    login(client, "readonly@example.com")
    resp = client.get("/api/alerts")
    assert resp.status_code == 200
    assert any(r["name"] == "visible" for r in resp.json())


def test_update_clears_secret_with_empty_string(client, db_session):
    rule = _rule(db_session, get_secrets_service(), name="edit-me")
    assert rule.webhook_secret_enc is not None
    login(client, "admin@example.com")
    resp = client.patch(
        f"/api/alerts/{rule.id}",
        headers=csrf_headers(client),
        json={"webhook_secret": ""},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["webhook_secret_set"] is False
    db_session.refresh(rule)
    assert rule.webhook_secret_enc is None


def test_incidents_endpoint_reports_open_and_feed(client, db_session, monkeypatch):
    secrets = get_secrets_service()
    s = _server(db_session)
    _sample(db_session, s.id, disk_pct=92, ts=BASE)
    _rule(db_session, secrets)
    cap = _Capture(db_session, secrets)
    monkeypatch.setattr(al, "send_email", cap.send_email)
    monkeypatch.setattr(al, "post_webhook", cap.post_webhook)
    al.evaluate_alerts(db_session, now=BASE, deliver=cap.deliver)

    login(client, "readonly@example.com")
    resp = client.get("/api/alerts/incidents")
    assert resp.status_code == 200
    body = resp.json()
    assert body["open_alerts"] == 1
    assert len(body["incidents"]) == 1
    assert body["incidents"][0]["metric"] == "disk_pct"


def test_test_endpoint_fires_synthetic_alert(client, db_session, monkeypatch):
    secrets = get_secrets_service()
    rule = _rule(db_session, secrets, name="probe")
    cap = _Capture(db_session, secrets)
    monkeypatch.setattr(al, "send_email", cap.send_email)
    monkeypatch.setattr(al, "post_webhook", cap.post_webhook)

    login(client, "admin@example.com")
    resp = client.post(f"/api/alerts/{rule.id}/test", headers=csrf_headers(client))
    assert resp.status_code == 200, resp.text
    assert len(cap.emails) == 1
    assert len(cap.webhooks) == 1


def test_test_endpoint_uses_tight_webhook_bound(client, db_session, monkeypatch):
    """The /test endpoint runs inline in the request thread, so a dead webhook
    must fail fast rather than holding the worker for the sweep's ~31s worst
    case (DOO-437)."""
    secrets = get_secrets_service()
    rule = _rule(db_session, secrets, name="probe")
    cap = _Capture(db_session, secrets)
    monkeypatch.setattr(al, "send_email", cap.send_email)
    monkeypatch.setattr(al, "post_webhook", cap.post_webhook)

    login(client, "admin@example.com")
    resp = client.post(f"/api/alerts/{rule.id}/test", headers=csrf_headers(client))
    assert resp.status_code == 200, resp.text
    assert cap.webhook_kwargs["timeout_seconds"] == al.TEST_WEBHOOK_TIMEOUT_SECONDS
    assert cap.webhook_kwargs["max_attempts"] == al.TEST_WEBHOOK_MAX_ATTEMPTS
    assert al.TEST_WEBHOOK_TIMEOUT_SECONDS < al._WEBHOOK_TIMEOUT_SECONDS
    assert al.TEST_WEBHOOK_MAX_ATTEMPTS < al._WEBHOOK_MAX_ATTEMPTS


def test_sweep_delivery_keeps_default_webhook_bound(db_session, cap):
    """The sweep's delivery path (deliver_firing with no explicit bounds) keeps
    the full retry budget — only the inline /test path is tightened."""
    rule = _rule(db_session, cap.secrets, name="probe-sweep")
    firing = AlertFiring(
        rule_id=rule.id,
        rule_name=rule.name,
        server_id=None,
        server_name="test",
        metric=rule.metric,
        comparator=rule.comparator,
        threshold=rule.threshold,
        value=rule.threshold,
        channels=[],
    )
    db_session.add(firing)
    db_session.commit()
    db_session.refresh(firing)

    al.deliver_firing(db_session, firing, secrets=cap.secrets)

    assert cap.webhook_kwargs["timeout_seconds"] == al._WEBHOOK_TIMEOUT_SECONDS
    assert cap.webhook_kwargs["max_attempts"] == al._WEBHOOK_MAX_ATTEMPTS
