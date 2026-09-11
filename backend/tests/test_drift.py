"""Config drift detection (session 6.7).

Covers the security-critical behaviour: secret masking before hashing, the
managed-change baseline hook (no false positive), drift detection on a manual
edit / missing artefact / reappearing elevation drop-in, the sanitised diff, and
the audited "accept as new baseline" flow. Root-owned artefacts are exercised
with a mocked SSH executor (the no-sudo test target can't read them for real —
reduced fidelity, see DOO-128)."""

import asyncio
from contextlib import contextmanager

from app.core import drift
from app.core.jobs import CaptureResult
from app.models import Bench, ConfigBaseline, Server, Site
from tests.conftest import csrf_headers, login

# --------------------------------------------------------------------------- #
# Fake job context (records reads; never writes to a real server)             #
# --------------------------------------------------------------------------- #


class _Rendered:
    def __init__(self, params):
        self.params_sanitized = params


class FakeCtx:
    """Drives app.core.drift against scripted file contents. `files` maps an
    absolute path to its content (str); a path absent from `files` reads as
    missing (exit 1). `dirs` maps a directory to a list of (basename, content)."""

    def __init__(self, db, *, server_id, job_id=1, params=None, files=None, dirs=None):
        self.session = db
        self.server_id = server_id
        self.job_id = job_id
        self.rendered = _Rendered(params or {})
        self.files = files or {}
        self.dirs = dirs or {}
        self.calls = []

    async def capture(self, argv, **kw):
        self.calls.append(list(argv))
        # Directory listing (find).
        if "find" in argv or "/usr/bin/find" in argv:
            path = argv[argv.index("find") + 1] if "find" in argv else argv[argv.index("/usr/bin/find") + 1]  # noqa: E501
            entries = self.dirs.get(path)
            if entries is None:
                return CaptureResult(exit_code=1, stdout="", stderr="not found")
            if "-printf" in argv:  # root conf.d: basenames only
                return CaptureResult(exit_code=0, stdout="".join(f"{n}\n" for n, _ in entries), stderr="")  # noqa: E501
            return CaptureResult(exit_code=0, stdout="".join(f"{path}/{n}\n" for n, _ in entries), stderr="")  # noqa: E501
        # cat: path is the last argv element.
        path = argv[-1]
        # a file inside a dir?
        for dpath, entries in self.dirs.items():
            for n, content in entries:
                if path == f"{dpath}/{n}":
                    return CaptureResult(exit_code=0, stdout=content, stderr="")
        if path in self.files:
            return CaptureResult(exit_code=0, stdout=self.files[path], stderr="")
        return CaptureResult(exit_code=1, stdout="", stderr="No such file or directory")

    async def emit(self, *a, **k):
        return None

    @contextmanager
    def step(self, name):
        yield object()


def _server(db):
    s = Server(name="vm-alpha", hostname="10.0.0.5", ssh_port=22)
    db.add(s)
    db.commit()
    return s


def _clean_files():
    """A full, clean tracked-artefact set for one server + bench + site."""
    return {
        "/etc/nginx/nginx.conf": "user www-data;\nworker_processes auto;\n",
        "/etc/supervisor/supervisord.conf": "[supervisord]\nnodaemon=false\n",
        "/etc/sudoers.d/fdm-platform": "frappe ALL=(root) NOPASSWD: /usr/sbin/nginx -t\n",
        # elevation drop-in absent (healthy)
        "/home/frappe/b/sites/common_site_config.json": '{"webserver_port": 8000, "redis_cache": "redis://localhost:13000"}',
        "/home/frappe/b/sites/s1.localhost/site_config.json": '{"db_name": "abc", "encryption_key": "TOPSECRET", "db_password": "hunter2"}',  # noqa: E501
    }


def _dirs():
    return {"/home/frappe/b/config/nginx-vhosts": [("s1.localhost.conf", "server { listen 80; }\n")],  # noqa: E501
            "/etc/supervisor/conf.d": [("frappe-b.conf", "[program:x]\n")]}


def _bench_site(db, server):
    bench = Bench(server_id=server.id, path="/home/frappe/b", name="b", status="active")
    db.add(bench)
    db.commit()
    site = Site(bench_id=bench.id, name="s1.localhost", status="active")
    db.add(site)
    db.commit()
    return bench, site


# --------------------------------------------------------------------------- #
# Secret masking + canonicalisation (golden rule 6)                           #
# --------------------------------------------------------------------------- #


def test_canonicalize_masks_secret_values_only():
    raw = '{"db_password":"hunter2","encryption_key":"k","webserver_port":8000,"api_token":"t"}'
    out = drift.canonicalize_json(raw)
    assert "hunter2" not in out and "\"k\"" not in out and "\"t\"" not in out
    assert drift.MASK in out
    assert "8000" in out  # non-secret preserved
    # sorted keys → stable hash regardless of input order
    assert drift.canonicalize_json(raw) == drift.canonicalize_json(
        '{"webserver_port":8000,"api_token":"t","encryption_key":"k","db_password":"hunter2"}'
    )


def test_is_secret_key():
    for k in ("db_password", "encryption_key", "api_key", "admin_secret", "auth_token", "key"):
        assert drift.is_secret_key(k)
    for k in ("webserver_port", "db_name", "host", "developer_mode"):
        assert not drift.is_secret_key(k)


def test_unparseable_json_is_hashed_but_content_withheld():
    ctx = FakeCtx(None, server_id=1, files={"/x/site_config.json": "{not json"})
    art = drift.Artifact("site_config", "site", "json")
    reading = asyncio.run(drift.read_artifact(ctx, art, "/x/site_config.json"))
    assert reading.present and reading.sha256 and reading.content is None


# --------------------------------------------------------------------------- #
# Drift check: clean, manual edit, missing, elevation present                 #
# --------------------------------------------------------------------------- #


def test_first_check_establishes_baseline_and_reports_clean(db_session):
    server = _server(db_session)
    _bench_site(db_session, server)
    ctx = FakeCtx(db_session, server_id=server.id, files=_clean_files(), dirs=_dirs())
    results = asyncio.run(drift.run_drift_check(ctx))
    assert all(not r.drifted for r in results)
    rows = db_session.query(ConfigBaseline).all()
    assert len(rows) == len(results) == 8  # 5 server + 2 bench + 1 site
    assert {r.status for r in rows} == {"baseline"}
    # site_config baseline stored its SECRET-STRIPPED content only
    sc = next(r for r in rows if r.artifact_key == "site_config")
    assert "hunter2" not in (sc.sanitized_content or "") and drift.MASK in sc.sanitized_content


def test_manual_edit_flags_drift_and_notifies(db_session, monkeypatch):
    server = _server(db_session)
    _bench_site(db_session, server)
    files = _clean_files()
    ctx = FakeCtx(db_session, server_id=server.id, files=files, dirs=_dirs())
    asyncio.run(drift.run_drift_check(ctx))  # establish baselines

    # Manual out-of-band edit to common_site_config.
    files["/home/frappe/b/sites/common_site_config.json"] = '{"webserver_port": 9999}'
    results = asyncio.run(drift.run_drift_check(ctx))
    drifted = [r for r in results if r.drifted]
    assert [r.artifact_key for r in drifted] == ["common_site_config"]
    assert drifted[0].reason == "content"
    row = db_session.query(ConfigBaseline).filter_by(artifact_key="common_site_config").one()
    assert row.status == "drifted" and row.drift_detected_at is not None
    assert row.current_content is not None  # snapshot for the drawer

    # DriftCheckAction dispatches config.drift for the drifted keys.
    sent = {}
    monkeypatch.setattr(
        "app.core.notifications.dispatch_config_drift",
        lambda db, **kw: sent.update(kw),
    )
    from app.core.commands.actions import DriftCheckAction

    files["/home/frappe/b/sites/common_site_config.json"] = '{"webserver_port": 7777}'
    asyncio.run(DriftCheckAction().run(ctx))
    assert sent.get("artifact_keys") == ["common_site_config"]
    assert sent.get("server_id") == server.id


def test_missing_tracked_artefact_is_drift_not_swallowed(db_session):
    server = _server(db_session)
    files = _clean_files()
    ctx = FakeCtx(db_session, server_id=server.id, files=files, dirs=_dirs())
    asyncio.run(drift.run_drift_check(ctx))
    del files["/etc/nginx/nginx.conf"]  # artefact vanishes
    results = asyncio.run(drift.run_drift_check(ctx))
    r = next(r for r in results if r.artifact_key == "nginx.conf")
    assert r.drifted and r.reason == "missing"


def test_elevation_dropin_presence_is_drift(db_session):
    server = _server(db_session)
    files = _clean_files()
    ctx = FakeCtx(db_session, server_id=server.id, files=files, dirs=_dirs())
    asyncio.run(drift.run_drift_check(ctx))  # absent → clean baseline
    files["/etc/sudoers.d/fdm-prod-elevation"] = "frappe ALL=(root) NOPASSWD: ALL\n"  # reappears!
    results = asyncio.run(drift.run_drift_check(ctx))
    r = next(r for r in results if r.artifact_key == "sudoers.elevation")
    assert r.drifted and r.reason == "present"
    # the drop-in body is NEVER stored
    row = db_session.query(ConfigBaseline).filter_by(artifact_key="sudoers.elevation").one()
    assert row.current_content is None


def test_first_sight_with_dropin_present_flags_drift(db_session):
    # DOO-405: if the very first drift check on a server catches the 2.5 elevation
    # drop-in present (a rare in-flight `bench setup-production`), it must NOT be
    # blessed as a clean baseline — the desired state of an absent-artefact is
    # always "<absent>", so present-on-first-sight is drift.
    server = _server(db_session)
    files = _clean_files()
    files["/etc/sudoers.d/fdm-prod-elevation"] = "frappe ALL=(root) NOPASSWD: ALL\n"  # present!
    ctx = FakeCtx(db_session, server_id=server.id, files=files, dirs=_dirs())
    results = asyncio.run(drift.run_drift_check(ctx))  # FIRST sight, drop-in present
    r = next(r for r in results if r.artifact_key == "sudoers.elevation")
    assert r.drifted and r.reason == "present"
    row = db_session.query(ConfigBaseline).filter_by(artifact_key="sudoers.elevation").one()
    assert row.status == "drifted" and row.drift_detected_at is not None
    # Baseline is pinned to the desired <absent> state, and the drop-in body is
    # never stored.
    assert row.sha256 == drift._sha256("<absent>")
    assert row.current_content is None

    # And once the drop-in is revoked, the next check self-heals against that
    # <absent> baseline (proving the baseline wasn't polluted with the presence).
    del files["/etc/sudoers.d/fdm-prod-elevation"]
    results = asyncio.run(drift.run_drift_check(ctx))
    r = next(r for r in results if r.artifact_key == "sudoers.elevation")
    assert not r.drifted and r.reason == "clean"


def test_reverted_edit_self_heals(db_session):
    server = _server(db_session)
    files = _clean_files()
    ctx = FakeCtx(db_session, server_id=server.id, files=files, dirs=_dirs())
    asyncio.run(drift.run_drift_check(ctx))
    original = files["/etc/nginx/nginx.conf"]
    files["/etc/nginx/nginx.conf"] = "tampered\n"
    asyncio.run(drift.run_drift_check(ctx))
    assert db_session.query(ConfigBaseline).filter_by(artifact_key="nginx.conf").one().status == "drifted"  # noqa: E501
    files["/etc/nginx/nginx.conf"] = original  # operator reverts
    asyncio.run(drift.run_drift_check(ctx))
    assert db_session.query(ConfigBaseline).filter_by(artifact_key="nginx.conf").one().status == "baseline"  # noqa: E501


# --------------------------------------------------------------------------- #
# Managed-change hook: baseline MOVES, no false-positive drift                #
# --------------------------------------------------------------------------- #


def test_managed_change_moves_baseline_no_drift(db_session):
    server = _server(db_session)
    bench, site = _bench_site(db_session, server)
    files = _clean_files()
    ctx = FakeCtx(db_session, server_id=server.id, files=files, dirs=_dirs())
    asyncio.run(drift.run_drift_check(ctx))  # baselines established

    # A managed job rewrites site_config (e.g. site.set_scheduler), then the hook
    # re-captures. Params carry bench_path + site like every site action.
    files["/home/frappe/b/sites/s1.localhost/site_config.json"] = (
        '{"db_name": "abc", "encryption_key": "TOPSECRET", "db_password": "hunter2", "scheduler_enabled": 1}'  # noqa: E501
    )
    hook_ctx = FakeCtx(
        db_session, server_id=server.id, job_id=42,
        params={"bench_path": "/home/frappe/b", "site": "s1.localhost"},
        files=files, dirs=_dirs(),
    )
    moved = asyncio.run(drift.capture_baselines(hook_ctx, ["site_config"]))
    assert moved == ["site_config"]
    row = db_session.query(ConfigBaseline).filter_by(artifact_key="site_config").one()
    assert row.status == "baseline" and row.captured_by_job_id == 42

    # The very next drift check must NOT flag it (the whole point of the feature).
    results = asyncio.run(drift.run_drift_check(ctx))
    assert not any(r.drifted for r in results)


# --------------------------------------------------------------------------- #
# API: list / diff (secret-masked) / accept (audited)                         #
# --------------------------------------------------------------------------- #


def _make_drift_row(db, server_id, **kw):
    row = ConfigBaseline(server_id=server_id, artifact_key=kw.get("artifact_key", "site_config"),
                         path=kw.get("path", "/home/frappe/b/sites/s1/site_config.json"),
                         status="drifted", sha256="aaa", size=10,
                         sanitized_content='{\n  "db_password": "••••",\n  "db_name": "old"\n}\n',
                         current_sha256="bbb",
                         current_content='{\n  "db_password": "••••",\n  "db_name": "new"\n}\n')
    db.add(row)
    db.commit()
    return row


def test_drift_diff_masks_secrets(client, db_session):
    server = _server(db_session)
    row = _make_drift_row(db_session, server.id)
    login(client, "readonly@example.com")
    r = client.get(f"/api/drift/{row.id}")
    assert r.status_code == 200
    body = r.json()
    assert body["hash_only"] is False
    assert "db_name" in body["unified_diff"]
    assert "••••" in body["unified_diff"]  # secret shown masked
    assert "hunter2" not in body["unified_diff"]


def test_accept_baseline_admin_audited(client, db_session):
    server = _server(db_session)
    row = _make_drift_row(db_session, server.id)
    login(client, "admin@example.com")
    r = client.post(f"/api/drift/{row.id}/accept", json={"reason": "vetted vhost tweak"},
                    headers=csrf_headers(client))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "accepted"
    db_session.expire_all()
    fresh = db_session.get(ConfigBaseline, row.id)
    assert fresh.status == "accepted" and fresh.sha256 == "bbb" and fresh.drift_detected_at is None
    from app.models.audit import AuditLog
    audit = db_session.query(AuditLog).filter_by(action="drift.accept_baseline").one()
    assert "vetted vhost tweak" in (audit.params_masked or {}).get("reason", "")


def test_accept_requires_reason(client, db_session):
    server = _server(db_session)
    row = _make_drift_row(db_session, server.id)
    login(client, "admin@example.com")
    r = client.post(f"/api/drift/{row.id}/accept", json={"reason": ""}, headers=csrf_headers(client))  # noqa: E501
    assert r.status_code == 422


def test_accept_readonly_denied(client, db_session):
    server = _server(db_session)
    row = _make_drift_row(db_session, server.id)
    login(client, "readonly@example.com")
    r = client.post(f"/api/drift/{row.id}/accept", json={"reason": "x"}, headers=csrf_headers(client))  # noqa: E501
    assert r.status_code == 403


def test_list_drifted_only(client, db_session):
    server = _server(db_session)
    _make_drift_row(db_session, server.id, artifact_key="site_config")
    db_session.add(ConfigBaseline(server_id=server.id, artifact_key="nginx.conf",
                                  path="/etc/nginx/nginx.conf", status="baseline", sha256="c", size=1))  # noqa: E501
    db_session.commit()
    login(client, "readonly@example.com")
    r = client.get("/api/drift?drifted_only=true")
    assert r.status_code == 200
    keys = [b["artifact_key"] for b in r.json()]
    assert keys == ["site_config"]
