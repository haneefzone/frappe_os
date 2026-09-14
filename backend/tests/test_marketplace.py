"""App-store catalog + compatibility/dependency resolver + registry cache
(DOO-1192). No network is touched: the registry files are fixtured on disk and
git is driven through an injectable fake runner (AC7)."""

import json
import os

import pytest

from app.core import marketplace as m
from app.core.marketplace import (
    AppRecord,
    DependencyError,
    RegistryCache,
    RegistryError,
    SpecifierError,
    resolve_catalog,
    resolve_install_plan,
    select_release,
    spec_matches,
)

# --------------------------------------------------------------------------- #
# PEP-440 subset: version + specifier matching
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "spec,ver,ok",
    [
        (">=15.0.0,<16.0.0", "15.42.1", True),
        (">=15.0.0,<16.0.0", "16.0.0", False),
        (">=15.0.0,<16.0.0", "14.99.9", False),
        (">=16", "15.63.0", False),
        (">=16", "16.0.0", True),
        ("~=15.2", "15.9.0", True),
        ("~=15.2", "16.0.0", False),
        ("~=15.2.3", "15.2.9", True),
        ("~=15.2.3", "15.3.0", False),
        ("==15.*", "15.99.1", True),
        ("==15.*", "16.0.0", False),
        ("!=15.4", "15.5.0", True),
        ("==15.42.1", "15.42.1", True),
    ],
)
def test_spec_matches(spec, ver, ok):
    assert spec_matches(spec, ver) is ok


def test_version_tolerates_v_prefix_and_suffix():
    assert m.parse_release("v16.0.0-nightly") == (16, 0, 0)
    assert spec_matches(">=16.0.0", "v16.0.0-nightly") is True


@pytest.mark.parametrize("bad", ["garbage", ">=abc", "", "^1.2.3", "15.0.0"])
def test_unparseable_specifier_raises(bad):
    # A bare version with no operator, junk, or npm-style range are all rejected —
    # the resolver treats a raised SpecifierError as "not installable".
    with pytest.raises(SpecifierError):
        spec_matches(bad, "15.0.0")


def test_unparseable_version_raises():
    with pytest.raises(SpecifierError):
        spec_matches(">=15.0.0", "not-a-version")


# --------------------------------------------------------------------------- #
# Release selection
# --------------------------------------------------------------------------- #

_C15 = ">=15.0.0,<16.0.0"
_REL_STABLE_15 = {"version": "1.1.0", "branch": "v11", "frappe_core": _C15, "channel": "stable"}
_REL_STABLE_15_OLD = {"version": "1.0.0", "branch": "v1", "frappe_core": _C15, "channel": "stable"}
_NIGHTLY15 = {"version": "1.2.0", "branch": "v12", "frappe_core": _C15, "channel": "nightly"}
_REL_16 = {"version": "2.0.0", "branch": "v2", "frappe_core": ">=16.0.0", "channel": "stable"}
_REL_NO_CORE = {"version": "0.9.0", "branch": "old"}  # missing frappe_core
_REL_BAD_CORE = {"version": "0.8.0", "branch": "bad", "frappe_core": "not-a-range"}


def test_select_prefers_newest_compatible_stable_over_newer_nightly():
    chosen = select_release(
        [_REL_STABLE_15_OLD, _NIGHTLY15, _REL_STABLE_15, _REL_16], "15.42.1"
    )
    # 1.2.0 nightly is newer but stable is preferred → 1.1.0.
    assert chosen == _REL_STABLE_15


def test_select_falls_back_to_nightly_when_no_stable_is_compatible():
    chosen = select_release([_NIGHTLY15, _REL_16], "15.42.1")
    assert chosen == _NIGHTLY15


def test_select_excludes_missing_and_unparseable_frappe_core():
    # Only releases with a parseable, matching frappe_core are ever offered.
    assert select_release([_REL_NO_CORE, _REL_BAD_CORE], "15.42.1") is None


def test_select_returns_none_when_all_incompatible():
    assert select_release([_REL_16], "15.42.1") is None


# --------------------------------------------------------------------------- #
# Catalog resolution — incompatible apps surface with a reason, not hidden
# --------------------------------------------------------------------------- #


def _rec(name, releases, meta=None):
    return AppRecord(
        name=name,
        repo=f"https://github.com/frappe/{name}",
        releases=tuple(releases),
        meta={"name": name, "title": name.upper(), "description": "d", **(meta or {})},
    )


def test_catalog_marks_incompatible_with_reason_and_keeps_it_visible():
    records = [
        _rec("erpnext", [_REL_STABLE_15]),
        _rec("hrms", [_REL_16]),  # needs Frappe >=16
    ]
    out = {r.name: r for r in resolve_catalog(records, "15.42.1", installed=set())}
    assert out["erpnext"].is_installable is True
    assert out["erpnext"].incompatible_reason is None
    assert out["erpnext"].branch == "v11" and out["erpnext"].version == "1.1.0"

    hrms = out["hrms"]
    assert hrms.is_installable is False
    assert hrms.incompatible_reason and ">=16.0.0" in hrms.incompatible_reason
    # Still carries display info so the UI can show what it would need.
    assert hrms.branch == "v2"


def test_catalog_sets_installed_flag():
    records = [_rec("erpnext", [_REL_STABLE_15])]
    out = resolve_catalog(records, "15.42.1", installed={"erpnext"})
    assert out[0].installed is True


def test_catalog_missing_core_reports_no_range_reason():
    out = resolve_catalog([_rec("x", [_REL_NO_CORE])], "15.42.1", installed=set())
    assert out[0].is_installable is False
    assert "compatibility range" in out[0].incompatible_reason


# --------------------------------------------------------------------------- #
# Dependency resolution — order, skip-installed, cycle, conflict
# --------------------------------------------------------------------------- #

_DB = {
    "hrms": [
        {
            "version": "1.0.0",
            "branch": "hv1",
            "commit": "aaa",
            "frappe_core": ">=15.0.0,<16.0.0",
            "channel": "stable",
            "dependencies": {"erpnext": ">=15.0.0,<16.0.0"},
        }
    ],
    "erpnext": [
        {
            "version": "15.5.0",
            "branch": "ev15",
            "commit": "bbb",
            "frappe_core": ">=15.0.0,<16.0.0",
            "channel": "stable",
        }
    ],
}


def _lookup(db):
    return lambda name: _rec(name, db[name]) if name in db else None


def test_dependencies_resolve_deps_first():
    plan = resolve_install_plan("hrms", "15.42.1", _lookup(_DB), installed={"frappe"})
    assert [p.app for p in plan] == ["erpnext", "hrms"]
    assert plan[0].branch == "ev15" and plan[0].commit == "bbb"
    assert plan[1].branch == "hv1"


def test_already_installed_dependency_is_skipped():
    plan = resolve_install_plan(
        "hrms", "15.42.1", _lookup(_DB), installed={"frappe", "erpnext"}
    )
    assert [p.app for p in plan] == ["hrms"]


def test_frappe_core_is_never_in_the_plan():
    db = {
        "a": [
            {
                "version": "1.0.0",
                "branch": "a1",
                "frappe_core": ">=15.0.0,<16.0.0",
                "dependencies": {"frappe": ">=15.0.0"},
            }
        ]
    }
    plan = resolve_install_plan("a", "15.42.1", _lookup(db), installed=set())
    assert [p.app for p in plan] == ["a"]


def test_dependency_version_conflict_fails_cleanly():
    db = dict(_DB)
    db["erpnext"] = [
        {"version": "14.0.0", "branch": "ev14", "frappe_core": ">=15.0.0,<16.0.0"}
    ]
    with pytest.raises(DependencyError) as exc:
        resolve_install_plan("hrms", "15.42.1", _lookup(db), installed={"frappe"})
    assert "requires" in str(exc.value)


def test_dependency_cycle_is_detected():
    def rel(branch, dep):
        return {"version": "1.0.0", "branch": branch, "frappe_core": _C15, "dependencies": dep}

    db = {"a": [rel("a", {"b": ">=1.0.0"})], "b": [rel("b", {"a": ">=1.0.0"})]}
    with pytest.raises(DependencyError) as exc:
        resolve_install_plan("a", "15.42.1", _lookup(db), installed=set())
    assert "cycle" in str(exc.value)


def test_unknown_app_fails_cleanly():
    with pytest.raises(DependencyError) as exc:
        resolve_install_plan("nope", "15.42.1", _lookup(_DB), installed=set())
    assert "catalog" in str(exc.value)


def test_incompatible_target_fails_cleanly():
    db = {"hrms": [_REL_16]}
    with pytest.raises(DependencyError) as exc:
        resolve_install_plan("hrms", "15.42.1", _lookup(db), installed=set())
    assert "compatible" in str(exc.value)


# --------------------------------------------------------------------------- #
# Registry cache — clone/refresh/graceful-degradation (no network)
# --------------------------------------------------------------------------- #


class FakeGit:
    """A scriptable git runner. `fetch_rc` / `clone_rc` / `status_dirty` drive the
    outcomes; a clone writes the fixture registry files into the target path."""

    def __init__(self, *, fixture=None, clone_rc=0, fetch_rc=0, status_dirty=False):
        self.fixture = fixture or {}
        self.clone_rc = clone_rc
        self.fetch_rc = fetch_rc
        self.status_dirty = status_dirty
        self.calls: list[list[str]] = []

    def __call__(self, args, *, cwd=None, timeout=120):
        self.calls.append(list(args))
        cmd = args[0]
        if cmd == "clone":
            path = args[-1]
            if self.clone_rc == 0:
                _write_registry(path, self.fixture)
            return _Proc(self.clone_rc, "", "boom" if self.clone_rc else "")
        if cmd == "fetch":
            return _Proc(self.fetch_rc, "", "no network" if self.fetch_rc else "")
        if cmd == "reset":
            return _Proc(0, "", "")
        if cmd == "status":
            return _Proc(0, " M apps.json\n" if self.status_dirty else "", "")
        return _Proc(0, "", "")  # pragma: no cover


class _Proc:
    def __init__(self, returncode, stdout, stderr):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


_FIXTURE = {
    "apps.json": [
        {"name": "erpnext", "title": "ERPNext", "description": "ERP", "repo": "https://github.com/frappe/erpnext",
         "categories": ["ERP"], "stars": 100, "releases": "apps/erpnext.json"},
        {"name": "hrms", "title": "HR", "description": "HR", "repo": "https://github.com/frappe/hrms",
         "categories": ["HR"], "stars": 50, "releases": "apps/hrms.json"},
    ],
    "apps/erpnext.json": {"name": "erpnext", "releases": [dict(_REL_STABLE_15)]},
    "apps/hrms.json": {"name": "hrms", "releases": [dict(_REL_16)]},
}


def _write_registry(path, fixture):
    os.makedirs(os.path.join(path, ".git"), exist_ok=True)
    os.makedirs(os.path.join(path, "apps"), exist_ok=True)
    for rel, data in fixture.items():
        full = os.path.join(path, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as fh:
            json.dump(data, fh)


def _cache(tmp_path, git, ttl=3600, now=1000.0):
    clock = {"t": now}
    c = RegistryCache(
        path=str(tmp_path / "reg"),
        url="https://github.com/frappe/marketplace",
        ttl_seconds=ttl,
        git=git,
        clock=lambda: clock["t"],
    )
    c._clock_state = clock  # test handle to advance time
    return c


def test_never_cloned_and_clone_fails_is_hard_error(tmp_path):
    c = _cache(tmp_path, FakeGit(clone_rc=1))
    with pytest.raises(RegistryError):
        c.ensure_fresh()
    with pytest.raises(RegistryError):
        c.load_index()


def test_clone_then_records_parse(tmp_path):
    git = FakeGit(fixture=_FIXTURE)
    c = _cache(tmp_path, git)
    c.ensure_fresh()
    assert git.calls[0][0] == "clone"
    names = {r.name for r in c.records()}
    assert names == {"erpnext", "hrms"}
    erp = c.record("erpnext")
    assert erp.repo == "https://github.com/frappe/erpnext"
    assert erp.releases[0]["frappe_core"] == ">=15.0.0,<16.0.0"


def test_fresh_clone_is_not_refetched(tmp_path):
    git = FakeGit(fixture=_FIXTURE)
    c = _cache(tmp_path, git)
    c.ensure_fresh()  # clones + marks fresh
    git.calls.clear()
    c.ensure_fresh()  # still fresh → no git at all
    assert git.calls == []


def test_stale_clone_refreshes(tmp_path):
    git = FakeGit(fixture=_FIXTURE)
    c = _cache(tmp_path, git, ttl=3600)
    c.ensure_fresh()
    git.calls.clear()
    c._clock_state["t"] += 4000  # now stale
    c.ensure_fresh()
    kinds = [call[0] for call in git.calls]
    assert "fetch" in kinds and "reset" in kinds


def test_fetch_failure_serves_last_good_clone(tmp_path):
    # Clone succeeds; a later refresh's fetch fails → the existing clone is still
    # served, not an error (AC1 graceful degradation).
    git = FakeGit(fixture=_FIXTURE)
    c = _cache(tmp_path, git)
    c.ensure_fresh()
    git.fetch_rc = 1
    c._clock_state["t"] += 4000
    c.ensure_fresh()  # must not raise
    assert {r.name for r in c.records()} == {"erpnext", "hrms"}


def test_tampered_clone_is_still_served(tmp_path):
    git = FakeGit(fixture=_FIXTURE, status_dirty=True)
    c = _cache(tmp_path, git)
    c.ensure_fresh()
    # Even with a dirty tree, the index still parses and is served.
    assert c.load_index()
    assert c._is_tampered() is True
