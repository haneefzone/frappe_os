"""Frappe app store (DOO-1194): registry reader/cache, compatibility filter,
transitive dependency resolution (cycle + version-conflict detection), and the
API surface (browse with per-bench compatibility, detail + plan, refresh, and
the install endpoint that reuses the get-app/install-app connector path).

No network is touched — a fixture catalog on disk stands in for the
`frappe/marketplace` repo, and the git operations of RegistryCache are exercised
through an injected runner (AC5: never hit the network in tests)."""

import base64
import json

import pytest

from app.api.routes.jobs import get_job_runner
from app.core.jobs import InMemoryJobBackend, JobRunner
from app.core.marketplace import (
    DependencyCycleError,
    IncompatibleError,
    RegistryCache,
    RegistryReader,
    RegistryUnavailableError,
    UnknownAppError,
    VersionConflictError,
    build_cache,
    compatible_releases,
    resolve_plan,
)
from app.core.security import get_secrets_service
from app.models import Server
from app.models.bench import Bench
from app.models.site import Site
from tests.conftest import csrf_headers, login

BENCH_PATH = "/home/frappe/frappe-bench"
FRAPPE_16 = "16.24.0"


# --------------------------------------------------------------------------- #
# A fixture catalog tree (a mini `frappe/marketplace`)
# --------------------------------------------------------------------------- #


def _rel(version, branch, frappe_core, *, deps=None, channel="stable"):
    return {
        "version": version,
        "branch": branch,
        "commit": version.replace(".", ""),
        "frappe_core": frappe_core,
        "dependencies": deps or {},
        "channel": channel,
    }


APPS_INDEX = [
    {
        "name": "erpnext",
        "title": "ERPNext",
        "description": "Open source ERP",
        "repo": "https://github.com/frappe/erpnext",
        "logo_url": "https://x/erpnext.png",
        "categories": ["Featured", "ERP"],
        "category": "Applications",
        "stars": 100,
        "releases": "apps/erpnext.json",
    },
    {
        "name": "hrms",
        "title": "Frappe HR",
        "description": "HR & Payroll (needs ERPNext)",
        "repo": "https://github.com/frappe/hrms",
        "categories": ["HR"],
        "stars": 50,
        "releases": "apps/hrms.json",
    },
    {
        "name": "legacy_only",
        "title": "Legacy Only",
        "description": "Only ships for Frappe 15",
        "repo": "https://github.com/acme/legacy_only",
        "categories": ["Misc"],
        "stars": 1,
        "releases": "apps/legacy_only.json",
    },
    {
        "name": "needs_old_erpnext",
        "title": "Needs Old ERPNext",
        "description": "Depends on an ERPNext that is not v16-compatible",
        "repo": "https://github.com/acme/needs_old_erpnext",
        "stars": 0,
        "releases": "apps/needs_old_erpnext.json",
    },
    {
        "name": "cyc_a",
        "title": "Cyclic A",
        "description": "Depends on cyc_b",
        "repo": "https://github.com/acme/cyc_a",
        "releases": "apps/cyc_a.json",
    },
    {
        "name": "cyc_b",
        "title": "Cyclic B",
        "description": "Depends on cyc_a",
        "repo": "https://github.com/acme/cyc_b",
        "releases": "apps/cyc_b.json",
    },
    {
        "name": "missing_dep",
        "title": "Missing Dep",
        "description": "Depends on an app not in the catalog",
        "repo": "https://github.com/acme/missing_dep",
        "releases": "apps/missing_dep.json",
    },
]

RELEASES = {
    "erpnext": [
        _rel("17.0.0-dev", "develop", ">=17.0.0-dev,<18.0.0", channel="nightly"),
        _rel("16.30.0", "version-16", ">=16.21.0,<17.0.0"),
        _rel("15.118.3", "version-15", ">=15.111.0,<16.0.0"),
    ],
    "hrms": [
        _rel("16.15.0", "version-16", ">=16.0.0,<17.0.0", deps={"erpnext": ">=16.0.0,<17.0.0"}),
        _rel("15.63.2", "version-15", ">=15.0.0,<16.0.0", deps={"erpnext": ">=15.0.0,<16.0.0"}),
    ],
    "legacy_only": [
        _rel("15.1.0", "version-15", ">=15.0.0,<16.0.0"),
    ],
    "needs_old_erpnext": [
        # v16-compatible itself, but pins erpnext to <16 — no release satisfies
        # both Frappe 16 and that pin => version conflict.
        _rel("16.0.0", "version-16", ">=16.0.0,<17.0.0", deps={"erpnext": ">=15.0.0,<16.0.0"}),
    ],
    "cyc_a": [
        _rel("16.0.0", "version-16", ">=16.0.0,<17.0.0", deps={"cyc_b": ">=16.0.0,<17.0.0"}),
    ],
    "cyc_b": [
        _rel("16.0.0", "version-16", ">=16.0.0,<17.0.0", deps={"cyc_a": ">=16.0.0,<17.0.0"}),
    ],
    "missing_dep": [
        _rel("16.0.0", "version-16", ">=16.0.0,<17.0.0", deps={"ghost_app": ">=1.0.0"}),
    ],
}


def _write_catalog(root):
    (root / "apps").mkdir(parents=True, exist_ok=True)
    (root / "apps.json").write_text(json.dumps(APPS_INDEX))
    for name, rels in RELEASES.items():
        (root / "apps" / f"{name}.json").write_text(
            json.dumps({"name": name, "releases": rels})
        )


@pytest.fixture
def catalog_root(tmp_path):
    root = tmp_path / "catalog"
    _write_catalog(root)
    return root


@pytest.fixture
def reader(catalog_root):
    return RegistryReader(catalog_root)


# --------------------------------------------------------------------------- #
# Reader + compatibility (AC2)
# --------------------------------------------------------------------------- #


def test_reader_parses_index_and_releases(reader):
    apps = reader.apps()
    assert {a.name for a in apps} >= {"erpnext", "hrms", "legacy_only"}
    erp = reader.get_app("erpnext")
    assert erp.title == "ERPNext" and erp.stars == 100
    rels = reader.releases("erpnext")
    assert [r.version for r in rels][0] == "17.0.0-dev"  # newest first
    assert reader.releases("does_not_exist") == []


def test_compatible_releases_filters_by_frappe_version(reader):
    compat = compatible_releases(reader.releases("erpnext"), FRAPPE_16)
    assert [r.version for r in compat] == ["16.30.0"]
    # A Frappe 15 bench sees only the v15 release.
    compat15 = compatible_releases(reader.releases("erpnext"), "15.118.0")
    assert [r.version for r in compat15] == ["15.118.3"]


# --------------------------------------------------------------------------- #
# Dependency resolution (AC3)
# --------------------------------------------------------------------------- #


def test_resolve_plan_simple(reader):
    plan = resolve_plan(reader, "erpnext", FRAPPE_16)
    assert [s.app for s in plan] == ["erpnext"]
    assert plan[0].version == "16.30.0" and plan[0].branch == "version-16"
    assert plan[0].reason == "requested"


def test_resolve_plan_transitive_deps_first(reader):
    plan = resolve_plan(reader, "hrms", FRAPPE_16)
    # erpnext (dependency) must come before hrms.
    assert [s.app for s in plan] == ["erpnext", "hrms"]
    assert plan[0].reason == "dependency of hrms"
    assert plan[1].version == "16.15.0"


def test_resolve_plan_incompatible_raises(reader):
    with pytest.raises(IncompatibleError) as exc:
        resolve_plan(reader, "legacy_only", FRAPPE_16)
    assert "16.24.0" in str(exc.value) and "15.0.0" in str(exc.value)


def test_resolve_plan_unknown_app(reader):
    with pytest.raises(UnknownAppError):
        resolve_plan(reader, "nope", FRAPPE_16)


def test_resolve_plan_unknown_dependency(reader):
    with pytest.raises(UnknownAppError) as exc:
        resolve_plan(reader, "missing_dep", FRAPPE_16)
    assert "ghost_app" in str(exc.value)


def test_resolve_plan_version_conflict(reader):
    with pytest.raises(VersionConflictError) as exc:
        resolve_plan(reader, "needs_old_erpnext", FRAPPE_16)
    assert "erpnext" in str(exc.value)


def test_resolve_plan_cycle_detected(reader):
    with pytest.raises(DependencyCycleError) as exc:
        resolve_plan(reader, "cyc_a", FRAPPE_16)
    assert "cyc_a" in str(exc.value) and "cyc_b" in str(exc.value)


# --------------------------------------------------------------------------- #
# Cache: shallow clone + serve-stale on failure (AC1)
# --------------------------------------------------------------------------- #


def test_cache_clone_then_read(tmp_path):
    calls = []

    def fake_git(argv, *, cwd):
        calls.append(argv[1])
        # Emulate a clone by materialising the catalog in the checkout dir.
        if argv[1] == "clone":
            _write_catalog(tmp_path / "cache" / "repo")

    cache = RegistryCache(tmp_path / "cache", "https://x/marketplace", git_runner=fake_git)
    assert cache.refresh() is True
    assert "clone" in calls
    assert cache.reader().get_app("erpnext").title == "ERPNext"


def test_cache_serves_stale_when_refresh_fails(tmp_path):
    _write_catalog(tmp_path / "cache" / "repo")  # a prior good cache exists

    def boom(argv, *, cwd):
        raise RuntimeError("network down")

    cache = RegistryCache(tmp_path / "cache", "https://x/marketplace", git_runner=boom)
    # Refresh fails but does NOT raise — stale cache is served.
    assert cache.refresh() is False
    assert cache.reader().get_app("hrms").title == "Frappe HR"


def test_cache_raises_when_no_cache_and_offline(tmp_path):
    def boom(argv, *, cwd):
        raise RuntimeError("network down")

    cache = RegistryCache(tmp_path / "cache", "https://x/marketplace", git_runner=boom)
    with pytest.raises(RegistryUnavailableError):
        cache.refresh()


# --------------------------------------------------------------------------- #
# API surface
# --------------------------------------------------------------------------- #


@pytest.fixture
def mk_client(client, db_session, catalog_root):
    """apps client with an in-memory runner and the catalog pointed at a fixture
    (checkout == the fixture root, git runner never invoked)."""
    runner = JobRunner(lambda: db_session, InMemoryJobBackend(), enqueue=lambda job: None)
    client.app.dependency_overrides[get_job_runner] = lambda: runner

    # A cache whose checkout points straight at the populated fixture root, so
    # `.reader()` reads it and no git op ever runs.
    class _FixtureCache(RegistryCache):
        @property
        def checkout(self):
            return catalog_root

    client.app.dependency_overrides[build_cache] = lambda: _FixtureCache(
        catalog_root.parent, "https://x/marketplace"
    )
    yield client
    client.app.dependency_overrides.pop(get_job_runner, None)
    client.app.dependency_overrides.pop(build_cache, None)


@pytest.fixture
def bench_env(db_session):
    s = Server(name="vm-a", hostname="10.0.0.1")
    s.mariadb_root_password_enc = get_secrets_service().encrypt("rootpw")
    db_session.add(s)
    db_session.commit()
    bench = Bench(
        server_id=s.id,
        path=BENCH_PATH,
        name="frappe-bench",
        webserver_port=8000,
        frappe_version=FRAPPE_16,
    )
    db_session.add(bench)
    db_session.commit()
    site = Site(bench_id=bench.id, name="test1.localhost")
    db_session.add(site)
    db_session.commit()
    return {"server_id": s.id, "bench_id": bench.id, "site_id": site.id}


def test_browse_without_bench_omits_compat(mk_client):
    login(mk_client, "readonly@example.com")
    resp = mk_client.get("/api/marketplace/apps")
    assert resp.status_code == 200, resp.text
    apps = {a["name"]: a for a in resp.json()}
    assert "erpnext" in apps
    assert apps["erpnext"]["is_installable"] is None


def test_browse_with_bench_surfaces_compat_and_reason(mk_client, bench_env):
    login(mk_client, "readonly@example.com")
    resp = mk_client.get(f"/api/marketplace/apps?bench={bench_env['bench_id']}")
    assert resp.status_code == 200, resp.text
    apps = {a["name"]: a for a in resp.json()}
    assert apps["erpnext"]["is_installable"] is True
    assert apps["erpnext"]["latest_compatible_version"] == "16.30.0"
    # Incompatible apps are surfaced, not hidden, WITH a human-readable reason.
    assert apps["legacy_only"]["is_installable"] is False
    assert "compatible" in apps["legacy_only"]["reason"].lower()
    # A dependency conflict is also surfaced as not-installable with its reason.
    assert apps["needs_old_erpnext"]["is_installable"] is False
    assert "erpnext" in apps["needs_old_erpnext"]["reason"]


def test_app_detail_includes_plan(mk_client, bench_env):
    login(mk_client, "readonly@example.com")
    resp = mk_client.get(f"/api/marketplace/apps/hrms?bench={bench_env['bench_id']}")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["is_installable"] is True
    assert [s["app"] for s in data["plan"]] == ["erpnext", "hrms"]
    compat = {r["version"]: r["is_compatible"] for r in data["releases"]}
    assert compat["16.15.0"] is True and compat["15.63.2"] is False


def test_app_detail_unknown_404(mk_client, bench_env):
    login(mk_client, "readonly@example.com")
    resp = mk_client.get("/api/marketplace/apps/nope")
    assert resp.status_code == 404


def test_install_marketplace_app_enqueues_plan(mk_client, bench_env, db_session):
    login(mk_client, "developer@example.com")
    resp = mk_client.post(
        f"/api/sites/{bench_env['site_id']}/marketplace-apps",
        json={"app": "hrms"},
        headers=csrf_headers(mk_client),
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["action_name"] == "site.install_marketplace_app"
    steps = json.loads(base64.b64decode(data["params_sanitized"]["plan_b64"]).decode())
    assert [s["app"] for s in steps] == ["erpnext", "hrms"]
    assert steps[0]["source"] == "https://github.com/frappe/erpnext"
    assert steps[0]["branch"] == "version-16"


def test_install_incompatible_is_422_and_enqueues_nothing(mk_client, bench_env):
    login(mk_client, "developer@example.com")
    resp = mk_client.post(
        f"/api/sites/{bench_env['site_id']}/marketplace-apps",
        json={"app": "needs_old_erpnext"},
        headers=csrf_headers(mk_client),
    )
    assert resp.status_code == 422, resp.text
    assert "erpnext" in resp.text


def test_install_requires_app_manage(mk_client, bench_env):
    login(mk_client, "readonly@example.com")
    resp = mk_client.post(
        f"/api/sites/{bench_env['site_id']}/marketplace-apps",
        json={"app": "erpnext"},
        headers=csrf_headers(mk_client),
    )
    assert resp.status_code == 403


def test_install_without_frappe_version_is_409(mk_client, db_session, catalog_root):
    s = Server(name="vm-b", hostname="10.0.0.2")
    s.mariadb_root_password_enc = get_secrets_service().encrypt("rootpw")
    db_session.add(s)
    db_session.commit()
    bench = Bench(server_id=s.id, path="/home/frappe/b2", name="b2", frappe_version=None)
    db_session.add(bench)
    db_session.commit()
    site = Site(bench_id=bench.id, name="s2.localhost")
    db_session.add(site)
    db_session.commit()
    login(mk_client, "developer@example.com")
    resp = mk_client.post(
        f"/api/sites/{site.id}/marketplace-apps",
        json={"app": "erpnext"},
        headers=csrf_headers(mk_client),
    )
    assert resp.status_code == 409
    assert "discovery" in resp.text.lower()


def test_refresh_endpoint_requires_app_manage(mk_client):
    login(mk_client, "readonly@example.com")
    resp = mk_client.post("/api/marketplace/refresh", headers=csrf_headers(mk_client))
    assert resp.status_code == 403
