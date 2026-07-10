"""Dashboard v1 (session 1.12, B4.1): the aggregation + the documented Fleet
Health formula + the one read endpoint. Real rows only — no placeholder data
beyond the three documented Health components."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.dashboard import build_dashboard
from app.db import Base
from app.models import Backup, Server, Site
from app.models.bench import Bench
from tests.conftest import login


@pytest.fixture
def sf():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    yield factory
    engine.dispose()


def _bench(db):
    s = Server(name="vm", hostname="10.0.0.1", status="online")
    db.add(s)
    db.commit()
    b = Bench(server_id=s.id, path="/home/frappe/frappe-bench", name="frappe-bench")
    db.add(b)
    db.commit()
    return s, b


def test_dashboard_empty_shows_onboarding(sf):
    with sf() as db:
        data = build_dashboard(db)
    assert data["onboarding"]["has_servers"] is False
    # No sites -> nothing to back up is compliant -> full health.
    assert data["kpis"]["fleet_health_pct"] == 100
    assert data["kpis"]["sites_total"] == 0
    assert "add your first server" in data["morning_brief"].lower()
    assert len(data["backup_grid"]) == 7


def test_fleet_health_drops_without_recent_backups(sf):
    with sf() as db:
        _, b = _bench(db)
        db.add(Site(bench_id=b.id, name="a.localhost", status="active"))
        db.add(Site(bench_id=b.id, name="b.localhost", status="active"))
        db.commit()
        data = build_dashboard(db)
    # 2 active sites, 0 backed up -> backup component 0 -> 40+0+20+10 = 70.
    assert data["kpis"]["fleet_health_pct"] == 70
    assert data["kpis"]["backup_compliance_pct"] == 0
    assert data["kpis"]["sites_total"] == 2


def test_fleet_health_full_when_all_backed_up(sf):
    with sf() as db:
        _, b = _bench(db)
        db.add(Site(bench_id=b.id, name="a.localhost", status="active"))
        db.commit()
        site = db.query(Site).first()
        db.add(
            Backup(
                site_id=site.id,
                bench_id=b.id,
                status="success",
                created_at=datetime.now(UTC),
            )
        )
        db.commit()
        data = build_dashboard(db)
    assert data["kpis"]["backup_compliance_pct"] == 100
    assert data["kpis"]["fleet_health_pct"] == 100
    assert data["kpis"]["backups_24h"] == 1


def test_backup_grid_buckets_by_day(sf):
    with sf() as db:
        _, b = _bench(db)
        db.add(Site(bench_id=b.id, name="a.localhost", status="active"))
        db.commit()
        site = db.query(Site).first()
        ok = Backup(site_id=site.id, bench_id=b.id, status="success")
        failed = Backup(site_id=site.id, bench_id=b.id, status="failed")
        db.add_all([ok, failed])
        db.commit()
        data = build_dashboard(db)
    today = datetime.now(UTC).date().isoformat()
    cell = next(c for c in data["backup_grid"] if c["date"] == today)
    assert cell["success"] == 1
    assert cell["failed"] == 1


def test_dashboard_endpoint(client, db_session):
    login(client, "readonly@example.com")
    resp = client.get("/api/dashboard")
    assert resp.status_code == 200, resp.text
    assert "kpis" in resp.json()
    assert "morning_brief" in resp.json()
