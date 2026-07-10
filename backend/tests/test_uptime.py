"""Uptime checks (session 2.7): the pure probe classifier over a stub transport,
the check-target builder, sample storage + health denormalisation + pruning, the
rolling-uptime + fleet-uptime math, the API (series + config with RBAC/validation
+ audit), and the Fleet Health uptime component going real. No real HTTP/Redis."""

import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.dashboard import build_dashboard
from app.core.uptime import (
    CheckResult,
    build_check_target,
    check_site,
    fleet_uptime_fraction,
    rolling_uptime,
    store_uptime_sample,
)
from app.db import Base
from app.models import AuditLog, Bench, Server, Site, UptimeSample
from tests.conftest import csrf_headers, login

BENCH_PATH = "/home/frappe/frappe-bench"


# --------------------------------------------------------------------------- #
# check_site — pure classifier over a stub transport
# --------------------------------------------------------------------------- #


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _run(coro):
    return asyncio.run(coro)


def test_check_site_up_on_2xx():
    async def go():
        async with _client(lambda req: httpx.Response(200, text="pong")) as c:
            return await check_site(c, "http://h:8000/api/method/ping", "s.localhost")

    res = _run(go())
    assert res.up is True
    assert res.status_code == 200
    assert res.latency_ms is not None and res.latency_ms >= 0
    assert res.error is None


def test_check_site_down_on_5xx_records_code():
    async def go():
        async with _client(lambda req: httpx.Response(502)) as c:
            return await check_site(c, "http://h/api/method/ping", "s.localhost")

    res = _run(go())
    assert res.up is False
    assert res.status_code == 502
    assert res.error == "HTTP 502"


def test_check_site_sends_host_header():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["host"] = req.headers.get("Host")
        return httpx.Response(200)

    async def go():
        async with _client(handler) as c:
            await check_site(c, "http://10.0.0.1:8000/api/method/ping", "erp.localhost")

    _run(go())
    assert seen["host"] == "erp.localhost"


def test_check_site_down_on_timeout():
    def handler(req):
        raise httpx.TimeoutException("timed out", request=req)

    async def go():
        async with _client(handler) as c:
            return await check_site(c, "http://h/api/method/ping", "s")

    res = _run(go())
    assert res.up is False
    assert res.status_code is None
    assert res.error == "timeout"


def test_check_site_down_on_connect_error():
    def handler(req):
        raise httpx.ConnectError("connection refused", request=req)

    async def go():
        async with _client(handler) as c:
            return await check_site(c, "http://h/api/method/ping", "s")

    res = _run(go())
    assert res.up is False
    assert res.status_code is None
    assert "refused" in (res.error or "")


# --------------------------------------------------------------------------- #
# build_check_target
# --------------------------------------------------------------------------- #


def test_build_target_derives_url_and_host():
    site = Site(name="test1.localhost", bench_id=1)
    bench = Bench(server_id=1, path=BENCH_PATH, name="b", webserver_port=8000)
    server = Server(name="vm", hostname="10.0.0.1")
    url, host = build_check_target(site, bench, server)
    assert url == "http://10.0.0.1:8000/api/method/ping"
    assert host == "test1.localhost"


def test_build_target_without_port():
    site = Site(name="s.localhost", bench_id=1)
    bench = Bench(server_id=1, path=BENCH_PATH, name="b")  # no webserver_port
    server = Server(name="vm", hostname="host.example")
    url, host = build_check_target(site, bench, server)
    assert url == "http://host.example/api/method/ping"


def test_build_target_uses_check_url_override():
    site = Site(name="s.localhost", bench_id=1, check_url="https://erp.example.com/x")
    bench = Bench(server_id=1, path=BENCH_PATH, name="b", webserver_port=8000)
    server = Server(name="vm", hostname="10.0.0.1")
    url, host = build_check_target(site, bench, server)
    assert url == "https://erp.example.com/x"
    assert host is None  # override carries its own Host


# --------------------------------------------------------------------------- #
# store_uptime_sample + health denormalisation + pruning
# --------------------------------------------------------------------------- #


@pytest.fixture
def sf():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    yield factory
    engine.dispose()


def _site(db, name="test1.localhost") -> Site:
    server = Server(name="vm", hostname="10.0.0.1")
    db.add(server)
    db.commit()
    bench = Bench(server_id=server.id, path=BENCH_PATH, name="b", webserver_port=8000)
    db.add(bench)
    db.commit()
    site = Site(bench_id=bench.id, name=name)
    db.add(site)
    db.commit()
    return site


def test_store_sample_sets_health_ok_and_err(sf):
    with sf() as db:
        site = _site(db)
        store_uptime_sample(db, site, CheckResult(up=True, status_code=200, latency_ms=12.0))
        assert db.get(Site, site.id).health == "ok"
        store_uptime_sample(db, site, CheckResult(up=False, error="timeout"))
        assert db.get(Site, site.id).health == "err"


def test_store_sample_prunes_old_rows(sf):
    with sf() as db:
        site = _site(db)
        old = UptimeSample(site_id=site.id, up=True)
        db.add(old)
        db.commit()
        old.ts = datetime.now(UTC) - timedelta(hours=800)  # past a 30d window
        db.commit()

        store_uptime_sample(
            db, site, CheckResult(up=True, status_code=200, latency_ms=5.0),
            retention_hours=720,
        )
        rows = db.scalars(
            select(UptimeSample).where(UptimeSample.site_id == site.id)
        ).all()
        assert len(rows) == 1  # the 800h-old row pruned, fresh one kept


# --------------------------------------------------------------------------- #
# rolling_uptime + fleet_uptime_fraction
# --------------------------------------------------------------------------- #


def _sample(db, site_id, *, up, hours_ago=0.0, latency=10.0):
    s = UptimeSample(
        site_id=site_id, up=up, status_code=200 if up else 500,
        latency_ms=latency if up else None,
    )
    db.add(s)
    db.commit()
    s.ts = datetime.now(UTC) - timedelta(hours=hours_ago)
    db.commit()
    return s


def test_rolling_uptime_percentages(sf):
    with sf() as db:
        site = _site(db)
        # In the last 24h: 3 up, 1 down -> 75%.
        _sample(db, site.id, up=True, hours_ago=1)
        _sample(db, site.id, up=True, hours_ago=2)
        _sample(db, site.id, up=True, hours_ago=3)
        _sample(db, site.id, up=False, hours_ago=4)
        # Older-than-24h but within 30d: another down.
        _sample(db, site.id, up=False, hours_ago=100)

        summary = rolling_uptime(db, site.id)
        assert summary["uptime_24h_pct"] == 75.0
        assert summary["samples_24h"] == 4
        # 30d window: 3 up of 5 -> 60%.
        assert summary["uptime_30d_pct"] == 60.0
        assert summary["samples_30d"] == 5
        # Latest = the most recent (1h-ago up sample).
        assert summary["currently_up"] is True
        assert summary["last_status_code"] == 200


def test_rolling_uptime_no_data_is_none(sf):
    with sf() as db:
        site = _site(db)
        summary = rolling_uptime(db, site.id)
        assert summary["uptime_24h_pct"] is None
        assert summary["currently_up"] is None
        assert summary["last_checked_at"] is None


def test_fleet_uptime_no_sites_is_full(sf):
    with sf() as db:
        frac, n = fleet_uptime_fraction(db)
        assert frac == 1.0 and n == 0


def test_fleet_uptime_no_samples_is_full(sf):
    with sf() as db:
        _site(db)  # active + enabled, but never checked
        frac, n = fleet_uptime_fraction(db)
        assert frac == 1.0 and n == 0  # nothing measured yet -> compliant


def test_fleet_uptime_pools_samples(sf):
    with sf() as db:
        site = _site(db)
        _sample(db, site.id, up=True, hours_ago=1)
        _sample(db, site.id, up=True, hours_ago=2)
        _sample(db, site.id, up=False, hours_ago=3)  # 2/3 up
        frac, n = fleet_uptime_fraction(db)
        assert frac == pytest.approx(2 / 3)
        assert n == 1


def test_fleet_uptime_ignores_disabled_sites(sf):
    with sf() as db:
        site = _site(db)
        site.uptime_enabled = False
        db.commit()
        _sample(db, site.id, up=False, hours_ago=1)
        frac, n = fleet_uptime_fraction(db)
        # Disabled site excluded -> no active enabled sites -> full marks.
        assert frac == 1.0 and n == 0


# --------------------------------------------------------------------------- #
# API: series + config (RBAC, validation, audit)
# --------------------------------------------------------------------------- #


@pytest.fixture
def api_site(db_session):
    server = Server(name="vm-a", hostname="10.0.0.1")
    db_session.add(server)
    db_session.commit()
    bench = Bench(server_id=server.id, path=BENCH_PATH, name="b", webserver_port=8000)
    db_session.add(bench)
    db_session.commit()
    site = Site(bench_id=bench.id, name="test1.localhost")
    db_session.add(site)
    db_session.commit()
    db_session.add(UptimeSample(site_id=site.id, up=True, status_code=200, latency_ms=8.0))
    db_session.commit()
    return site.id


def test_uptime_series_endpoint(client, api_site):
    login(client, "readonly@example.com")
    resp = client.get(f"/api/sites/{api_site}/uptime?hours=24")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["site_id"] == api_site
    assert data["enabled"] is True
    assert len(data["samples"]) == 1
    assert data["summary"]["uptime_24h_pct"] == 100.0
    assert data["summary"]["last_latency_ms"] == 8.0


def test_uptime_config_toggle_and_audit(client, api_site, db_session):
    login(client, "developer@example.com")
    resp = client.post(
        f"/api/sites/{api_site}/uptime-config",
        json={"enabled": False},
        headers=csrf_headers(client),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["uptime_enabled"] is False
    assert db_session.get(Site, api_site).uptime_enabled is False
    # A non-job mutation still writes an audit row (rule 2).
    row = db_session.scalars(
        select(AuditLog).where(AuditLog.action == "site.uptime_config")
    ).first()
    assert row is not None
    assert row.params_masked.get("uptime_enabled") is False


def test_uptime_config_validates_check_url(client, api_site):
    login(client, "developer@example.com")
    resp = client.post(
        f"/api/sites/{api_site}/uptime-config",
        json={"check_url": "ftp://nope"},
        headers=csrf_headers(client),
    )
    assert resp.status_code == 422, resp.text


def test_uptime_config_accepts_and_clears_check_url(client, api_site, db_session):
    login(client, "developer@example.com")
    resp = client.post(
        f"/api/sites/{api_site}/uptime-config",
        json={"check_url": "https://erp.example.com/api/method/ping"},
        headers=csrf_headers(client),
    )
    assert resp.status_code == 200, resp.text
    assert db_session.get(Site, api_site).check_url == "https://erp.example.com/api/method/ping"
    # Empty string clears the override back to derived.
    resp = client.post(
        f"/api/sites/{api_site}/uptime-config",
        json={"check_url": ""},
        headers=csrf_headers(client),
    )
    assert resp.status_code == 200
    assert db_session.get(Site, api_site).check_url is None


def test_uptime_config_readonly_forbidden(client, api_site):
    login(client, "readonly@example.com")
    resp = client.post(
        f"/api/sites/{api_site}/uptime-config",
        json={"enabled": False},
        headers=csrf_headers(client),
    )
    assert resp.status_code == 403, resp.text


# --------------------------------------------------------------------------- #
# Fleet Health uptime component is now real
# --------------------------------------------------------------------------- #


def test_dashboard_fleet_health_drops_when_site_down(sf):
    with sf() as db:
        site = _site(db)
        # All-down history -> uptime fraction 0 -> lose the full 40-pt component.
        for h in range(1, 5):
            _sample(db, site.id, up=False, hours_ago=h)
        payload = build_dashboard(db)
        # backup(30, no sites backed up but also counts active sites...) — the
        # site exists and has no backup, so backup fraction is 0 too. What we
        # assert here is specifically that uptime pulled health below the old
        # placeholder floor of 70 (uptime was previously always +40).
        assert payload["kpis"]["uptime_30d_pct"] == 0
        assert payload["kpis"]["fleet_health_pct"] < 70


def test_dashboard_fleet_health_full_uptime(sf):
    with sf() as db:
        site = _site(db)
        for h in range(1, 5):
            _sample(db, site.id, up=True, hours_ago=h)
        payload = build_dashboard(db)
        assert payload["kpis"]["uptime_30d_pct"] == 100


# --------------------------------------------------------------------------- #
# End-to-end acceptance: green -> red flip through the real checker loop over a
# live localhost HTTP server (no bench needed — the check is external HTTP).
# --------------------------------------------------------------------------- #


def test_checker_flips_green_to_red_over_live_server(sf):
    import http.server
    import threading

    class _OK(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"pong")

        def log_message(self, *a):  # silence the test server
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), _OK)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    from app.core.uptime import UptimeChecker

    with sf() as db:
        site = _site(db)
        site.check_url = f"http://127.0.0.1:{port}/"  # point the checker here
        db.commit()
        site_id = site.id

    checker = UptimeChecker(sf, interval_seconds=15, retention_hours=720)
    try:
        # Server up -> the site checks green.
        _run(checker._tick())
        with sf() as db:
            assert db.get(Site, site_id).health == "ok"
            last = db.scalars(
                select(UptimeSample)
                .where(UptimeSample.site_id == site_id)
                .order_by(UptimeSample.ts.desc())
            ).first()
            assert last.up is True and last.status_code == 200
            assert last.latency_ms is not None
    finally:
        server.shutdown()
        server.server_close()

    # Server stopped -> the next check flips the dot red and drops uptime %.
    _run(checker._tick())
    with sf() as db:
        assert db.get(Site, site_id).health == "err"
        summary = rolling_uptime(db, site_id)
        assert summary["currently_up"] is False
        assert summary["uptime_24h_pct"] == 50.0  # 1 up, 1 down
