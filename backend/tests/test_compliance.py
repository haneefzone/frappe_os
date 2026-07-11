"""Backup compliance evaluator + policy API (session 2.3, DOO-134).

Two layers, matching the session's "PARTIAL" test-target:
- evaluator unit tests with seeded backups + a fake clock (no Redis/worker), and
- policy-CRUD + summary + on-demand-evaluate API tests through the TestClient.

The acceptance flow is exercised end-to-end at the core layer with a fake clock
(set 24h RPO → fresh backup compliant → skip past RPO → breach + a breach event
row for 3.1), and again through the API's POST /api/compliance/evaluate.
"""

from datetime import UTC, datetime, timedelta

import pytest

from app.core import compliance as comp
from app.models import (
    Backup,
    BackupPolicy,
    Bench,
    ComplianceBreachEvent,
    ComplianceStatus,
    Server,
    Site,
)
from tests.conftest import csrf_headers, login

NOW = datetime(2026, 7, 11, 12, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _site(db, *, name="test1.localhost", created_at=None) -> Site:
    # Unique server/bench per call so a test can create several sites.
    slug = name.split(".")[0]
    s = Server(name=f"vm-{slug}", hostname="10.0.0.9")
    db.add(s)
    db.commit()
    bench = Bench(server_id=s.id, path=f"/home/frappe/{slug}-bench", name=f"{slug}-bench",
                  frappe_version="16.2.0")
    db.add(bench)
    db.commit()
    site = Site(bench_id=bench.id, name=name, status="active")
    if created_at is not None:
        site.created_at = created_at
    db.add(site)
    db.commit()
    return site


def _backup(db, site, *, created_at, status="success", storage_state="local") -> Backup:
    row = Backup(
        site_id=site.id, bench_id=site.bench_id, type="db", status=status,
        size_bytes=1024, storage_state=storage_state, created_at=created_at,
    )
    db.add(row)
    db.commit()
    return row


def _policy(db, site, **over) -> BackupPolicy:
    p = BackupPolicy(site_id=site.id, rpo_hours=over.pop("rpo_hours", 24), **over)
    db.add(p)
    db.commit()
    return p


# --------------------------------------------------------------------------- #
# evaluate_policy — the per-site pure computation
# --------------------------------------------------------------------------- #


def test_fresh_backup_within_rpo_is_compliant(db_session):
    site = _site(db_session)
    _backup(db_session, site, created_at=NOW - timedelta(hours=2))
    policy = _policy(db_session, site, rpo_hours=24)
    res = comp.evaluate_policy(db_session, policy, now=NOW)
    assert res["state"] == "compliant"
    assert res["breaches"] == []
    assert res["last_backup_at"] == NOW - timedelta(hours=2)


def test_no_backup_breaches_rpo(db_session):
    site = _site(db_session)
    policy = _policy(db_session, site, rpo_hours=24)
    res = comp.evaluate_policy(db_session, policy, now=NOW)
    assert res["state"] == "breached"
    assert [b["code"] for b in res["breaches"]] == ["rpo"]
    assert res["last_backup_at"] is None


def test_stale_backup_breaches_rpo(db_session):
    site = _site(db_session)
    _backup(db_session, site, created_at=NOW - timedelta(hours=30))
    policy = _policy(db_session, site, rpo_hours=24)
    res = comp.evaluate_policy(db_session, policy, now=NOW)
    assert res["state"] == "breached"
    codes = [b["code"] for b in res["breaches"]]
    assert codes == ["rpo"]
    assert "RPO 24h" in res["breaches"][0]["detail"]


def test_only_successful_backups_count_for_rpo(db_session):
    site = _site(db_session)
    # A recent *failed* backup must not satisfy RPO.
    _backup(db_session, site, created_at=NOW - timedelta(hours=1), status="failed")
    _backup(db_session, site, created_at=NOW - timedelta(hours=40))
    policy = _policy(db_session, site, rpo_hours=24)
    res = comp.evaluate_policy(db_session, policy, now=NOW)
    assert res["state"] == "breached"
    assert res["last_backup_at"] == NOW - timedelta(hours=40)


def test_require_offsite_breaches_when_local(db_session):
    site = _site(db_session)
    _backup(db_session, site, created_at=NOW - timedelta(hours=1), storage_state="local")
    policy = _policy(db_session, site, rpo_hours=24, require_offsite=True)
    res = comp.evaluate_policy(db_session, policy, now=NOW)
    assert res["state"] == "breached"
    assert [b["code"] for b in res["breaches"]] == ["offsite"]


def test_require_offsite_compliant_when_offsite(db_session):
    site = _site(db_session)
    _backup(db_session, site, created_at=NOW - timedelta(hours=1), storage_state="offsite")
    policy = _policy(db_session, site, rpo_hours=24, require_offsite=True)
    res = comp.evaluate_policy(db_session, policy, now=NOW)
    assert res["state"] == "compliant"


def test_retention_breach_when_history_too_short(db_session):
    # Site is old (created 40d ago) but its oldest backup is only 3d old — the
    # promised 30d of history is not there.
    site = _site(db_session, created_at=NOW - timedelta(days=40))
    _backup(db_session, site, created_at=NOW - timedelta(hours=1))
    _backup(db_session, site, created_at=NOW - timedelta(days=3))
    policy = _policy(db_session, site, rpo_hours=24, retention_days=30)
    res = comp.evaluate_policy(db_session, policy, now=NOW)
    assert res["state"] == "breached"
    assert [b["code"] for b in res["breaches"]] == ["retention"]


def test_new_site_cannot_breach_retention(db_session):
    # Site is only 3d old, so it cannot yet be expected to hold 30d of history.
    site = _site(db_session, created_at=NOW - timedelta(days=3))
    _backup(db_session, site, created_at=NOW - timedelta(hours=1))
    _backup(db_session, site, created_at=NOW - timedelta(days=3))
    policy = _policy(db_session, site, rpo_hours=24, retention_days=30)
    res = comp.evaluate_policy(db_session, policy, now=NOW)
    assert res["state"] == "compliant"


def test_retention_satisfied_when_history_reaches_back(db_session):
    site = _site(db_session, created_at=NOW - timedelta(days=60))
    _backup(db_session, site, created_at=NOW - timedelta(hours=1))
    _backup(db_session, site, created_at=NOW - timedelta(days=31))
    policy = _policy(db_session, site, rpo_hours=24, retention_days=30)
    res = comp.evaluate_policy(db_session, policy, now=NOW)
    assert res["state"] == "compliant"


# --------------------------------------------------------------------------- #
# evaluate_all — persistence, counts, edge-triggered breach events
# --------------------------------------------------------------------------- #


def test_evaluate_all_persists_status_and_counts(db_session):
    ok_site = _site(db_session, name="ok.localhost")
    _backup(db_session, ok_site, created_at=NOW - timedelta(hours=1))
    _policy(db_session, ok_site, rpo_hours=24)

    bad_site = _site(db_session, name="bad.localhost")
    _policy(db_session, bad_site, rpo_hours=24)  # no backup → breach

    summary = comp.evaluate_all(db_session, now=NOW)
    assert summary == {
        "policied": 2, "compliant": 1, "breached": 1, "events_emitted": 1
    }

    statuses = {s.site_id: s for s in db_session.query(ComplianceStatus).all()}
    assert statuses[ok_site.id].state == "compliant"
    assert statuses[bad_site.id].state == "breached"
    assert statuses[ok_site.id].evaluated_at.replace(tzinfo=UTC) == NOW


def test_breach_event_is_edge_triggered(db_session):
    site = _site(db_session)
    _policy(db_session, site, rpo_hours=24)  # no backup → breached

    # First evaluation: transition unknown→breached emits one event.
    comp.evaluate_all(db_session, now=NOW)
    assert db_session.query(ComplianceBreachEvent).count() == 1

    # Second evaluation, still breached: no new event (not re-alerted every tick).
    comp.evaluate_all(db_session, now=NOW + timedelta(hours=1))
    assert db_session.query(ComplianceBreachEvent).count() == 1

    # Recover with a fresh backup → compliant, still no new event.
    _backup(db_session, site, created_at=NOW + timedelta(hours=2))
    comp.evaluate_all(db_session, now=NOW + timedelta(hours=2))
    assert db_session.query(ComplianceBreachEvent).count() == 1
    assert db_session.query(ComplianceStatus).one().state == "compliant"

    # Break again (skip past RPO) → a new transition emits a second event.
    comp.evaluate_all(db_session, now=NOW + timedelta(hours=30))
    assert db_session.query(ComplianceBreachEvent).count() == 2
    evt = db_session.query(ComplianceBreachEvent).order_by(
        ComplianceBreachEvent.id.desc()
    ).first()
    assert evt.site_name == "test1.localhost"
    assert evt.consumed_at is None  # groundwork: 3.1 stamps this


def test_disabled_policy_excluded_from_evaluation_and_counts(db_session):
    site = _site(db_session)
    _policy(db_session, site, rpo_hours=24, enabled=False)  # no backup, but disabled
    summary = comp.evaluate_all(db_session, now=NOW)
    assert summary["policied"] == 0
    assert db_session.query(ComplianceBreachEvent).count() == 0
    assert comp.compliance_counts(db_session) == (0, 0)


def test_acceptance_rpo_flow(db_session):
    """Set 24h RPO; a fresh backup is compliant; skipping past RPO flags a breach,
    drops the compliant count, and writes a breach event row for 3.1."""
    site = _site(db_session)
    _policy(db_session, site, rpo_hours=24)
    fresh = _backup(db_session, site, created_at=NOW)

    comp.evaluate_all(db_session, now=NOW + timedelta(hours=1))
    assert comp.compliance_counts(db_session) == (1, 1)  # compliant / policied
    assert db_session.query(ComplianceBreachEvent).count() == 0

    # 25h after the only backup → past the 24h RPO.
    comp.evaluate_all(db_session, now=fresh.created_at + timedelta(hours=25))
    assert comp.compliance_counts(db_session) == (0, 1)  # % drops to 0
    events = db_session.query(ComplianceBreachEvent).all()
    assert len(events) == 1
    assert [b["code"] for b in events[0].breaches] == ["rpo"]


# --------------------------------------------------------------------------- #
# Policy API
# --------------------------------------------------------------------------- #


@pytest.fixture
def api_site(db_session):
    return _site(db_session)


def test_put_policy_admin_creates(client, api_site, db_session):
    login(client, "admin@example.com")
    r = client.put(
        f"/api/sites/{api_site.id}/policy",
        json={"rpo_hours": 24, "retention_days": 30, "require_offsite": True},
        headers=csrf_headers(client),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["rpo_hours"] == 24
    assert body["retention_days"] == 30
    assert body["require_offsite"] is True
    # Persisted + audited.
    assert db_session.query(BackupPolicy).filter_by(site_id=api_site.id).count() == 1


def test_put_policy_is_upsert(client, api_site):
    login(client, "admin@example.com")
    client.put(f"/api/sites/{api_site.id}/policy", json={"rpo_hours": 24},
               headers=csrf_headers(client))
    r = client.put(f"/api/sites/{api_site.id}/policy", json={"rpo_hours": 12},
                   headers=csrf_headers(client))
    assert r.status_code == 200
    assert r.json()["rpo_hours"] == 12


def test_put_policy_readonly_denied(client, api_site):
    login(client, "readonly@example.com")
    r = client.put(f"/api/sites/{api_site.id}/policy", json={"rpo_hours": 24},
                   headers=csrf_headers(client))
    assert r.status_code == 403


def test_get_policy_404_when_none(client, api_site):
    login(client, "admin@example.com")
    r = client.get(f"/api/sites/{api_site.id}/policy")
    assert r.status_code == 404


def test_delete_policy(client, api_site, db_session):
    login(client, "admin@example.com")
    client.put(f"/api/sites/{api_site.id}/policy", json={"rpo_hours": 24},
               headers=csrf_headers(client))
    r = client.delete(f"/api/sites/{api_site.id}/policy", headers=csrf_headers(client))
    assert r.status_code == 204
    assert db_session.query(BackupPolicy).filter_by(site_id=api_site.id).count() == 0


def test_compliance_summary_and_evaluate(client, api_site, db_session):
    login(client, "admin@example.com")
    # Policy + a stale backup so the site is breached.
    client.put(f"/api/sites/{api_site.id}/policy", json={"rpo_hours": 24},
               headers=csrf_headers(client))
    _backup(db_session, api_site, created_at=datetime.now(UTC) - timedelta(hours=48))

    r = client.post("/api/compliance/evaluate", headers=csrf_headers(client))
    assert r.status_code == 200, r.text
    summary = r.json()
    assert summary["policied"] == 1
    assert summary["breached"] == 1
    assert summary["compliance_pct"] == 0
    assert summary["statuses"][0]["state"] == "breached"
    assert summary["statuses"][0]["site_name"] == api_site.name
    # A breach event row exists for session 3.1.
    assert db_session.query(ComplianceBreachEvent).count() == 1


def test_compliance_summary_empty_is_100(client):
    login(client, "readonly@example.com")
    r = client.get("/api/compliance")
    assert r.status_code == 200
    assert r.json() == {
        "policied": 0, "compliant": 0, "breached": 0,
        "compliance_pct": 100, "statuses": [],
    }
