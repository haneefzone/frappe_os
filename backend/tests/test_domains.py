"""Tests for Domains & SSL (session 2.4).

Covers the pure renderers/parsers, the DNS/nginx/certbot action orchestration
(vhost written under flock with a pre-change backup, `nginx -t` gate that
restores on failure, cert issue/renew/expiry bookkeeping), the API surface + RBAC,
the scheduler wiring, and the dashboard "SSL expiring ≤30d" KPI.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.routes.jobs import get_job_runner
from app.core import domains as dom
from app.core.commands.actions import (
    _FDM_CERTBOT,
    _NGINX_RELOAD_ARGV,
    _NGINX_TEST_ARGV,
    _PUBLIC_IP_SCRIPT,
    _VHOST_RESTORE_SCRIPT,
    _VHOST_WRITE_SCRIPT,
)
from app.core.dashboard import build_dashboard
from app.core.jobs import CaptureResult, InMemoryJobBackend, JobRunner
from app.core.scheduler import _build_fire
from app.db import Base
from app.models import CommandJob, Domain
from app.models.bench import Bench
from app.models.schedule import Schedule
from app.models.server import Server
from app.models.site import Site
from tests.conftest import csrf_headers, login

BENCH_PATH = "/home/frappe/frappe-bench"


def _utc(dt):
    """Normalise a datetime read back from the DB to aware UTC.

    SQLite (the test DB) drops tzinfo on a ``DateTime(timezone=True)`` column,
    while production Postgres preserves it; normalise so expiry assertions hold
    on both. The stored instant is UTC either way (the actions write aware UTC).
    """
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #


def test_render_vhost_http_only_has_no_tls_block():
    conf = dom.render_vhost(
        "erp.acme.com",
        upstream_host="127.0.0.1",
        upstream_port=8000,
        ssl_enabled=False,
        webroot="/home/frappe/frappe-bench/sites/erp/public",
        cert_dir="/etc/letsencrypt/live/erp.acme.com",
    )
    assert "server_name erp.acme.com;" in conf
    assert "listen 80;" in conf
    assert "listen 443 ssl;" not in conf
    # ACME challenge is always served so certbot --webroot can complete.
    assert "/.well-known/acme-challenge/" in conf
    assert "proxy_pass http://127.0.0.1:8000;" in conf


def test_render_vhost_ssl_adds_443_and_redirect():
    conf = dom.render_vhost(
        "erp.acme.com",
        upstream_host="127.0.0.1",
        upstream_port=8001,
        ssl_enabled=True,
        webroot="/x/public",
        cert_dir="/etc/letsencrypt/live/erp.acme.com",
    )
    assert "listen 443 ssl;" in conf
    assert "ssl_certificate /etc/letsencrypt/live/erp.acme.com/fullchain.pem;" in conf
    assert "ssl_certificate_key /etc/letsencrypt/live/erp.acme.com/privkey.pem;" in conf
    assert "return 301 https://$host$request_uri;" in conf


def test_parse_getent_ips():
    out = "93.184.216.34  STREAM  example.com\n93.184.216.34  DGRAM\n2606:2800::1 STREAM"
    assert dom.parse_getent_ips(out) == {"93.184.216.34", "2606:2800::1"}


def test_dns_ok_matches_and_mismatches():
    assert dom.dns_ok({"1.2.3.4"}, {"1.2.3.4", "5.6.7.8"}) is True
    assert dom.dns_ok({"9.9.9.9"}, {"1.2.3.4"}) is False
    assert dom.dns_ok(set(), {"1.2.3.4"}) is False  # NXDOMAIN


def test_parse_public_ips():
    assert dom.parse_public_ips("203.0.113.7\n10.0.0.1 192.168.1.5 ") == {
        "203.0.113.7",
        "10.0.0.1",
        "192.168.1.5",
    }


def test_parse_certbot_certificates():
    out = (
        "Found the following certs:\n"
        "  Certificate Name: erp.acme.com\n"
        "    Domains: erp.acme.com\n"
        "    Expiry Date: 2026-10-08 12:34:56+00:00 (VALID: 89 days)\n"
        "  Certificate Name: shop.acme.com\n"
        "    Expiry Date: 2026-11-01 00:00:00+00:00 (VALID: 113 days)\n"
    )
    parsed = dom.parse_certbot_certificates(out)
    assert parsed["erp.acme.com"] == datetime(2026, 10, 8, 12, 34, 56, tzinfo=UTC)
    assert parsed["shop.acme.com"].day == 1


# --------------------------------------------------------------------------- #
# Action orchestration
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


def _seed_site(db, *, hostname="203.0.113.7") -> tuple[int, int, int]:
    server = Server(name="vm", hostname=hostname)
    db.add(server)
    db.commit()
    bench = Bench(server_id=server.id, path=BENCH_PATH, name="frappe-bench",
                  webserver_port=8000)
    db.add(bench)
    db.commit()
    site = Site(bench_id=bench.id, name="erp.localhost")
    db.add(site)
    db.commit()
    return server.id, bench.id, site.id


def _add_domain(db, site_id, name="erp.acme.com", **kw) -> int:
    d = Domain(site_id=site_id, domain=name, **kw)
    db.add(d)
    db.commit()
    return d.id


class DomainExecutor:
    """Fake SSH executor scripted for the domain/SSL actions."""

    def __init__(self, *, resolved_ips="203.0.113.7", public_ip="203.0.113.7",
                 nginx_t_exit=0, certbot_exit=0,
                 certbot_certs="Certificate Name: erp.acme.com\n"
                               "  Expiry Date: 2026-10-08 12:00:00+00:00 (VALID: 89 days)\n"):
        self.resolved_ips = resolved_ips
        self.public_ip = public_ip
        self.nginx_t_exit = nginx_t_exit
        self.certbot_exit = certbot_exit
        self.certbot_certs = certbot_certs
        self.streamed: list[list[str]] = []
        self.wrote: list[str] = []
        self.restored = False

    async def capture(self, argv, *, cwd=None, timeout=120.0):
        if argv[0] == "getent":
            family = argv[1]
            if family == "ahostsv4":
                return CaptureResult(0, f"{self.resolved_ips}  STREAM  x", "")
            return CaptureResult(0, "", "")  # no v6
        if argv[:2] == ["bash", "-c"]:
            script = argv[2]
            if script == _PUBLIC_IP_SCRIPT:
                return CaptureResult(0, self.public_ip, "")
            if script == _VHOST_WRITE_SCRIPT:
                target = argv[5]
                self.wrote.append(target)
                return CaptureResult(0, f"WROTE {target}", "")
            if script == _VHOST_RESTORE_SCRIPT:
                self.restored = True
                return CaptureResult(0, "RESTORED", "")
        if argv[:4] == ["sudo", "-n", _FDM_CERTBOT, "certificates"]:
            return CaptureResult(0, self.certbot_certs, "")
        raise AssertionError(f"unexpected capture {argv}")

    async def run(self, argv, *, cwd, run_as, on_line, cancel_check):
        self.streamed.append(list(argv))
        if argv == _NGINX_TEST_ARGV:
            return self.nginx_t_exit
        if argv == _NGINX_RELOAD_ARGV:
            return 0
        if argv[:4] == ["sudo", "-n", _FDM_CERTBOT, "issue"]:
            return self.certbot_exit
        if argv[:4] == ["sudo", "-n", _FDM_CERTBOT, "renew"]:
            return 0
        return 0


def _factory(executor):
    class _CM:
        async def __aenter__(self):
            return executor

        async def __aexit__(self, *exc):
            return False

    return lambda server: _CM()


def _run(sf, server_id, action, params, executor, *, target_type, target_id):
    runner = JobRunner(sf, InMemoryJobBackend(), enqueue=lambda job: None)
    with sf() as db:
        job = runner.create(
            db,
            action_name=action,
            server_id=server_id,
            target_type=target_type,
            target_id=target_id,
            params=params,
            priority="default",
            created_by=None,
        )
        job_id = job.id
    runner.run_job(job_id, executor_factory=_factory(executor))
    return job_id


def test_dns_check_records_match(sf):
    with sf() as db:
        server_id, _, site_id = _seed_site(db)
        did = _add_domain(db, site_id)
    ex = DomainExecutor(resolved_ips="203.0.113.7", public_ip="203.0.113.7")
    _run(sf, server_id, "domain.dns_check",
         {"domain": "erp.acme.com", "domain_id": str(did),
          "site": "erp.localhost", "bench_path": BENCH_PATH},
         ex, target_type="site", target_id="x")
    with sf() as db:
        d = db.get(Domain, did)
        assert d.dns_ok is True
        assert d.last_checked is not None
        assert d.last_error is None


def test_dns_check_records_mismatch(sf):
    with sf() as db:
        server_id, _, site_id = _seed_site(db)
        did = _add_domain(db, site_id)
    ex = DomainExecutor(resolved_ips="198.51.100.9", public_ip="203.0.113.7")
    _run(sf, server_id, "domain.dns_check",
         {"domain": "erp.acme.com", "domain_id": str(did),
          "site": "erp.localhost", "bench_path": BENCH_PATH},
         ex, target_type="site", target_id="x")
    with sf() as db:
        d = db.get(Domain, did)
        assert d.dns_ok is False
        assert "does not resolve" in d.last_error


def test_render_vhost_writes_validates_and_reloads(sf):
    with sf() as db:
        server_id, _, site_id = _seed_site(db)
        did = _add_domain(db, site_id)
    ex = DomainExecutor(nginx_t_exit=0)
    jid = _run(sf, server_id, "nginx.render_vhost",
               {"domain": "erp.acme.com", "domain_id": str(did),
                "site": "erp.localhost", "bench_path": BENCH_PATH, "ssl": "off"},
               ex, target_type="server", target_id=None)
    assert ex.wrote == [f"{BENCH_PATH}/config/nginx-vhosts/erp.acme.com.conf"]
    assert _NGINX_TEST_ARGV in ex.streamed
    assert _NGINX_RELOAD_ARGV in ex.streamed
    assert ex.restored is False
    with sf() as db:
        assert db.get(CommandJob, jid).status == "success"


def test_render_vhost_restores_and_fails_when_nginx_t_rejects(sf):
    with sf() as db:
        server_id, _, site_id = _seed_site(db)
        did = _add_domain(db, site_id)
    ex = DomainExecutor(nginx_t_exit=1)
    jid = _run(sf, server_id, "nginx.render_vhost",
               {"domain": "erp.acme.com", "domain_id": str(did),
                "site": "erp.localhost", "bench_path": BENCH_PATH, "ssl": "off"},
               ex, target_type="server", target_id=None)
    # nginx -t failed → restore was called and nginx was NOT reloaded.
    assert ex.restored is True
    assert _NGINX_RELOAD_ARGV not in ex.streamed
    with sf() as db:
        assert db.get(CommandJob, jid).status == "failure"


def test_certbot_issue_records_cert_and_enables_ssl(sf):
    with sf() as db:
        server_id, _, site_id = _seed_site(db)
        did = _add_domain(db, site_id)
    ex = DomainExecutor(certbot_exit=0)
    jid = _run(sf, server_id, "ssl.certbot_issue",
               {"domain": "erp.acme.com", "domain_id": str(did),
                "site": "erp.localhost", "bench_path": BENCH_PATH,
                "email": "admin@acme.com"},
               ex, target_type="server", target_id=None)
    with sf() as db:
        d = db.get(Domain, did)
        assert d.cert_status == "issued"
        assert d.ssl_enabled is True
        assert _utc(d.cert_expires_at) == datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
        assert db.get(CommandJob, jid).status == "success"
    # The final vhost was re-rendered with TLS on (two writes: http then https).
    assert ex.wrote.count(f"{BENCH_PATH}/config/nginx-vhosts/erp.acme.com.conf") == 2


def test_certbot_issue_marks_error_on_failure(sf):
    with sf() as db:
        server_id, _, site_id = _seed_site(db)
        did = _add_domain(db, site_id)
    ex = DomainExecutor(certbot_exit=1)
    jid = _run(sf, server_id, "ssl.certbot_issue",
               {"domain": "erp.acme.com", "domain_id": str(did),
                "site": "erp.localhost", "bench_path": BENCH_PATH,
                "email": "admin@acme.com"},
               ex, target_type="server", target_id=None)
    with sf() as db:
        d = db.get(Domain, did)
        assert d.cert_status == "error"
        assert d.last_error
        assert db.get(CommandJob, jid).status == "failure"


def test_expiry_scan_updates_expiry(sf):
    with sf() as db:
        server_id, _, site_id = _seed_site(db)
        did = _add_domain(db, site_id, ssl_enabled=True, cert_status="issued")
    ex = DomainExecutor()
    _run(sf, server_id, "ssl.expiry_scan",
         {"site": "erp.localhost", "bench_path": BENCH_PATH},
         ex, target_type="site", target_id="x")
    with sf() as db:
        d = db.get(Domain, did)
        assert _utc(d.cert_expires_at) == datetime(2026, 10, 8, 12, 0, tzinfo=UTC)


def test_certbot_renew_renews_ssl_domains(sf):
    with sf() as db:
        server_id, _, site_id = _seed_site(db)
        _add_domain(db, site_id, name="erp.acme.com", ssl_enabled=True)
        _add_domain(db, site_id, name="plain.acme.com", ssl_enabled=False)
    ex = DomainExecutor()
    _run(sf, server_id, "ssl.certbot_renew",
         {"site": "erp.localhost", "bench_path": BENCH_PATH},
         ex, target_type="server", target_id=None)
    renews = [a for a in ex.streamed if a[:4] == ["sudo", "-n", _FDM_CERTBOT, "renew"]]
    assert len(renews) == 1  # only the SSL-enabled domain
    assert "erp.acme.com" in renews[0]


# --------------------------------------------------------------------------- #
# Scheduler wiring (2.1)
# --------------------------------------------------------------------------- #


def test_build_fire_supports_ssl_actions(sf):
    with sf() as db:
        _, _, site_id = _seed_site(db)
        for action in ("ssl.certbot_renew", "ssl.expiry_scan"):
            sched = Schedule(
                name=f"s-{action}", target_type="site", target_id=site_id,
                action_name=action, interval_seconds=86400,
            )
            db.add(sched)
            db.commit()
            server_id, target_id, params, side = _build_fire(db, sched)
            assert params == {"site": "erp.localhost", "bench_path": BENCH_PATH}
            assert side is None


# --------------------------------------------------------------------------- #
# Dashboard KPI
# --------------------------------------------------------------------------- #


def test_dashboard_counts_ssl_expiring_within_30d(sf):
    with sf() as db:
        _, _, site_id = _seed_site(db)
        now = datetime.now(UTC)
        _add_domain(db, site_id, name="soon.acme.com", ssl_enabled=True,
                    cert_status="issued", cert_expires_at=now + timedelta(days=10))
        _add_domain(db, site_id, name="later.acme.com", ssl_enabled=True,
                    cert_status="issued", cert_expires_at=now + timedelta(days=40))
        _add_domain(db, site_id, name="nossl.acme.com", ssl_enabled=False,
                    cert_expires_at=now + timedelta(days=5))
        payload = build_dashboard(db)
    assert payload["kpis"]["ssl_expiring_30d"] == 1


# --------------------------------------------------------------------------- #
# API surface + RBAC
# --------------------------------------------------------------------------- #


@pytest.fixture
def domains_client(client, db_session):
    runner = JobRunner(lambda: db_session, InMemoryJobBackend(), enqueue=lambda job: None)
    client.app.dependency_overrides[get_job_runner] = lambda: runner
    yield client
    client.app.dependency_overrides.pop(get_job_runner, None)


@pytest.fixture
def api_site(db_session):
    s = Server(name="vm-a", hostname="203.0.113.7")
    db_session.add(s)
    db_session.commit()
    bench = Bench(server_id=s.id, path=BENCH_PATH, name="frappe-bench", webserver_port=8000)
    db_session.add(bench)
    db_session.commit()
    site = Site(bench_id=bench.id, name="erp.localhost")
    db_session.add(site)
    db_session.commit()
    return site.id


def test_add_list_and_delete_domain(domains_client, api_site):
    login(domains_client, "developer@example.com")
    resp = domains_client.post(
        f"/api/sites/{api_site}/domains",
        json={"domain": "ERP.Acme.com", "is_primary": True},
        headers=csrf_headers(domains_client),
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["domain"] == "erp.acme.com"  # lowercased
    assert data["is_primary"] is True
    assert data["cert_status"] == "none"
    did = data["id"]

    listed = domains_client.get(f"/api/sites/{api_site}/domains")
    assert [d["domain"] for d in listed.json()] == ["erp.acme.com"]

    gone = domains_client.delete(
        f"/api/sites/{api_site}/domains/{did}", headers=csrf_headers(domains_client)
    )
    assert gone.status_code == 200, gone.text
    assert domains_client.get(f"/api/sites/{api_site}/domains").json() == []


def test_add_domain_rejects_invalid_and_duplicate(domains_client, api_site):
    login(domains_client, "developer@example.com")
    bad = domains_client.post(
        f"/api/sites/{api_site}/domains",
        json={"domain": "not a domain"},
        headers=csrf_headers(domains_client),
    )
    assert bad.status_code == 422, bad.text

    domains_client.post(
        f"/api/sites/{api_site}/domains",
        json={"domain": "erp.acme.com"},
        headers=csrf_headers(domains_client),
    )
    dup = domains_client.post(
        f"/api/sites/{api_site}/domains",
        json={"domain": "erp.acme.com"},
        headers=csrf_headers(domains_client),
    )
    assert dup.status_code == 409, dup.text


def test_setting_primary_clears_the_previous_one(domains_client, api_site, db_session):
    login(domains_client, "developer@example.com")
    for name in ("a.acme.com", "b.acme.com"):
        domains_client.post(
            f"/api/sites/{api_site}/domains",
            json={"domain": name, "is_primary": True},
            headers=csrf_headers(domains_client),
        )
    primaries = [
        d.domain
        for d in db_session.scalars(
            select(Domain).where(Domain.is_primary.is_(True))
        ).all()
    ]
    assert primaries == ["b.acme.com"]


def test_readonly_cannot_add_domain(domains_client, api_site):
    login(domains_client, "readonly@example.com")
    resp = domains_client.post(
        f"/api/sites/{api_site}/domains",
        json={"domain": "erp.acme.com"},
        headers=csrf_headers(domains_client),
    )
    assert resp.status_code == 403, resp.text


def test_domain_job_endpoints_launch_pending_jobs(domains_client, api_site, db_session):
    login(domains_client, "developer@example.com")
    did = domains_client.post(
        f"/api/sites/{api_site}/domains",
        json={"domain": "erp.acme.com"},
        headers=csrf_headers(domains_client),
    ).json()["id"]

    for path, body, action in (
        ("test-dns", {}, "domain.dns_check"),
        ("render-vhost", {}, "nginx.render_vhost"),
        ("issue-cert", {"email": "admin@acme.com"}, "ssl.certbot_issue"),
    ):
        resp = domains_client.post(
            f"/api/sites/{api_site}/domains/{did}/{path}",
            json=body,
            headers=csrf_headers(domains_client),
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["action_name"] == action
        assert resp.json()["status"] == "pending"
