"""Update advisor (session 3.2): branch-line-aware behind-by-N computation, repo
resolution, the TTL tag cache, the idempotent poll sweep, changelog preview, and
the full read-only API (list / summary / changelog / refresh-RBAC). No network,
Redis, RQ or SSH touched — the poll is driven with an injected tag fetcher."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.core.updates import (
    changelog_preview,
    compute_behind,
    get_cached_tags,
    major_of,
    parse_version,
    poll_updates,
    resolve_repo,
    updates_summary,
)
from app.models.app import AppSource, InstalledApp
from app.models.bench import Bench
from app.models.server import Server
from app.models.site import Site
from app.models.updates import AppVersionStatus
from tests.conftest import csrf_headers, login

BENCH_PATH = "/home/frappe/frappe-bench"

# A realistic frappe tag list spanning v14/v15/v16 (stable + a beta to ignore).
FRAPPE_TAGS = [
    "v14.70.0",
    "v15.40.0",
    "v15.41.0",
    "v16.20.0",
    "v16.24.1",
    "v16.25.0",
    "v16.26.0",
    "v16.27.0-beta.1",  # pre-release: must never count
]


# --------------------------------------------------------------------------- #
# Version parsing / branch line
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "text,expected",
    [
        ("v16.25.0", (16, 25, 0)),
        ("16.24.1", (16, 24, 1)),
        ("v15.40", (15, 40, 0)),
        ("v16.27.0-beta.1", None),  # pre-release ignored
        ("not-a-tag", None),
        (None, None),
        ("", None),
    ],
)
def test_parse_version(text, expected):
    assert parse_version(text) == expected


@pytest.mark.parametrize(
    "branch,ref,expected",
    [
        ("version-16", None, 16),
        ("v15", None, 15),
        ("16", None, 16),
        (None, "16.24.1", 16),
        ("develop", "16.24.1", 16),  # unparseable branch → fall back to ref
        (None, None, None),
    ],
)
def test_major_of(branch, ref, expected):
    assert major_of(branch, ref) == expected


# --------------------------------------------------------------------------- #
# behind-by (branch-line aware)
# --------------------------------------------------------------------------- #


def test_behind_by_counts_only_same_major_line():
    behind, latest = compute_behind(
        installed_ref="16.24.1", branch="version-16", tags=FRAPPE_TAGS
    )
    # v16.25.0 and v16.26.0 are newer stable v16 releases; the beta is ignored;
    # v15/v14 tags never count against a v16 site.
    assert behind == 2
    assert latest == "v16.26.0"


def test_behind_by_zero_when_current():
    behind, latest = compute_behind(
        installed_ref="16.26.0", branch="version-16", tags=FRAPPE_TAGS
    )
    assert behind == 0 and latest == "v16.26.0"


def test_behind_by_unknown_installed_ref_still_reports_latest():
    behind, latest = compute_behind(
        installed_ref=None, branch="version-15", tags=FRAPPE_TAGS
    )
    assert behind is None and latest == "v15.41.0"


def test_behind_by_none_when_no_line_tags():
    behind, latest = compute_behind(
        installed_ref="13.1.0", branch="version-13", tags=FRAPPE_TAGS
    )
    assert behind is None and latest is None


# --------------------------------------------------------------------------- #
# Repo resolution
# --------------------------------------------------------------------------- #


def test_resolve_frappe_org_app():
    key, url = resolve_repo("erpnext", None)
    assert key == "github.com/frappe/erpnext"
    assert url == "https://github.com/frappe/erpnext"


def test_resolve_prefers_git_source_over_frappe_org():
    src = AppSource(
        name="erpnext", repo_url="https://github.com/acme/erpnext", kind="github"
    )
    key, url = resolve_repo("erpnext", src)
    assert key == "github.com/acme/erpnext"


def test_resolve_marketplace_only_source_is_unpollable():
    src = AppSource(name="widgets", repo_url="widgets", kind="marketplace")
    assert resolve_repo("widgets", src) is None


def test_resolve_unknown_custom_app_is_unpollable():
    assert resolve_repo("my_secret_app", None) is None


# --------------------------------------------------------------------------- #
# TTL tag cache
# --------------------------------------------------------------------------- #


def test_tag_cache_reuses_fresh_and_refetches_stale(db_session):
    calls = {"n": 0}

    def fetcher(repo_key, remote_url):
        calls["n"] += 1
        return list(FRAPPE_TAGS)

    now = datetime(2026, 7, 10, 12, 0, tzinfo=UTC)
    key = "github.com/frappe/frappe"
    url = "https://github.com/frappe/frappe"

    tags, err = get_cached_tags(
        db_session, repo_key=key, remote_url=url, fetcher=fetcher,
        ttl_seconds=1800, now=now,
    )
    assert err is None and tags == FRAPPE_TAGS and calls["n"] == 1

    # Within TTL → served from cache, fetcher NOT called again.
    get_cached_tags(
        db_session, repo_key=key, remote_url=url, fetcher=fetcher,
        ttl_seconds=1800, now=now + timedelta(minutes=10),
    )
    assert calls["n"] == 1

    # Past TTL → refetch.
    get_cached_tags(
        db_session, repo_key=key, remote_url=url, fetcher=fetcher,
        ttl_seconds=1800, now=now + timedelta(hours=1),
    )
    assert calls["n"] == 2

    # force=True → refetch even within TTL.
    get_cached_tags(
        db_session, repo_key=key, remote_url=url, fetcher=fetcher,
        ttl_seconds=1800, now=now + timedelta(hours=1), force=True,
    )
    assert calls["n"] == 3


def test_tag_cache_keeps_last_good_on_fetch_error(db_session):
    now = datetime(2026, 7, 10, 12, 0, tzinfo=UTC)
    key, url = "github.com/frappe/frappe", "https://github.com/frappe/frappe"
    get_cached_tags(
        db_session, repo_key=key, remote_url=url,
        fetcher=lambda k, u: list(FRAPPE_TAGS), ttl_seconds=0, now=now,
    )

    def boom(k, u):
        raise RuntimeError("network down")

    tags, err = get_cached_tags(
        db_session, repo_key=key, remote_url=url, fetcher=boom,
        ttl_seconds=0, now=now + timedelta(hours=1),
    )
    assert tags == FRAPPE_TAGS  # last good preserved
    assert "network down" in err


# --------------------------------------------------------------------------- #
# Poll sweep (idempotent) + summary
# --------------------------------------------------------------------------- #


def _seed_site_with_apps(db, *, apps):
    """apps = list of (app_name, branch, version). Returns (site, {name: ia})."""
    s = Server(name="vm", hostname="10.0.0.1")
    db.add(s)
    db.commit()
    bench = Bench(server_id=s.id, path=BENCH_PATH, name="frappe-bench", webserver_port=8000)
    db.add(bench)
    db.commit()
    site = Site(bench_id=bench.id, name="test1.localhost", status="active", health="ok")
    db.add(site)
    db.commit()
    ias = {}
    for app_name, branch, version in apps:
        ia = InstalledApp(
            site_id=site.id, bench_id=bench.id, app_name=app_name,
            branch=branch, version=version,
        )
        db.add(ia)
        db.commit()
        ias[app_name] = ia
    return site, ias


def test_poll_computes_behind_and_is_idempotent(db_session):
    _seed_site_with_apps(
        db_session,
        apps=[
            ("frappe", "version-16", "16.24.1"),
            ("erpnext", "version-16", "16.26.0"),  # current
            ("my_secret_app", "main", "1.0.0"),    # un-pollable
        ],
    )
    fetcher = lambda k, u: list(FRAPPE_TAGS)  # noqa: E731

    now = datetime(2026, 7, 10, 12, 0, tzinfo=UTC)
    summary = poll_updates(db_session, now=now, fetcher=fetcher, ttl_seconds=1800)
    assert summary == {"checked": 3, "updated": 3, "behind": 1, "errors": 1}

    rows = {r.app_name: r for r in db_session.scalars(
        select(AppVersionStatus)).all()}
    assert rows["frappe"].behind_by == 2 and rows["frappe"].latest_ref == "v16.26.0"
    assert rows["erpnext"].behind_by == 0
    assert rows["my_secret_app"].behind_by is None
    assert rows["my_secret_app"].last_error

    # Re-run: no duplicate rows (unique on installed_app_id), values stable.
    poll_updates(db_session, now=now + timedelta(hours=2), fetcher=fetcher, ttl_seconds=1800)
    all_rows = db_session.scalars(
        select(AppVersionStatus)).all()
    assert len(all_rows) == 3


def test_updates_summary_rollup(db_session):
    _seed_site_with_apps(
        db_session,
        apps=[
            ("frappe", "version-16", "16.24.1"),   # behind 2
            ("erpnext", "version-16", "16.26.0"),  # current
        ],
    )
    poll_updates(
        db_session, now=datetime(2026, 7, 10, tzinfo=UTC),
        fetcher=lambda k, u: list(FRAPPE_TAGS), ttl_seconds=1800,
    )
    summary = updates_summary(db_session)
    assert summary["apps_behind"] == 1
    assert summary["sites_behind"] == 1
    assert summary["tracked"] == 2
    assert summary["up_to_date_fraction"] == 0.5


# --------------------------------------------------------------------------- #
# Changelog preview
# --------------------------------------------------------------------------- #


def test_changelog_preview_lists_range_with_links():
    preview = changelog_preview(
        repo_key="github.com/frappe/frappe",
        installed_ref="16.24.1",
        branch="version-16",
        tags=FRAPPE_TAGS,
    )
    assert preview["latest_ref"] == "v16.26.0"
    assert preview["behind_by"] == 2
    assert [r["tag"] for r in preview["releases"]] == ["v16.26.0", "v16.25.0"]
    assert preview["releases"][0]["notes_url"].endswith("/releases/tag/v16.26.0")
    # Compare URL uses the real upstream tag spelling for the installed ref.
    assert preview["compare_url"] == (
        "https://github.com/frappe/frappe/compare/v16.24.1...v16.26.0"
    )


def test_changelog_preview_empty_when_current():
    preview = changelog_preview(
        repo_key="github.com/frappe/frappe",
        installed_ref="16.26.0",
        branch="version-16",
        tags=FRAPPE_TAGS,
    )
    assert preview["releases"] == [] and preview["behind_by"] == 0


# --------------------------------------------------------------------------- #
# API surface
# --------------------------------------------------------------------------- #


@pytest.fixture
def updates_env(db_session):
    """Seed a site with two apps, poll them, so the API has advisor rows."""
    site, ias = _seed_site_with_apps(
        db_session,
        apps=[
            ("frappe", "version-16", "16.24.1"),   # behind 2
            ("erpnext", "version-16", "16.26.0"),  # current
        ],
    )
    poll_updates(
        db_session, now=datetime(2026, 7, 10, tzinfo=UTC),
        fetcher=lambda k, u: list(FRAPPE_TAGS), ttl_seconds=1800,
    )
    return {"site_id": site.id, "frappe_ia": ias["frappe"].id, "erpnext_ia": ias["erpnext"].id}


def test_api_list_updates_and_behind_only(client, updates_env):
    login(client, "readonly@example.com")
    r = client.get("/api/updates")
    assert r.status_code == 200, r.text
    rows = {x["app_name"]: x for x in r.json()}
    assert rows["frappe"]["behind_by"] == 2
    assert rows["frappe"]["latest_ref"] == "v16.26.0"
    assert rows["erpnext"]["behind_by"] == 0

    r2 = client.get("/api/updates", params={"behind_only": True})
    names = {x["app_name"] for x in r2.json()}
    assert names == {"frappe"}


def test_api_updates_summary(client, updates_env):
    login(client, "readonly@example.com")
    r = client.get("/api/updates/summary")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["apps_behind"] == 1 and body["sites_behind"] == 1
    assert body["up_to_date_fraction"] == 0.5


def test_api_changelog(client, updates_env):
    login(client, "readonly@example.com")
    r = client.get(f"/api/updates/{updates_env['frappe_ia']}/changelog")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["app_name"] == "frappe"
    assert [x["tag"] for x in body["releases"]] == ["v16.26.0", "v16.25.0"]
    assert body["compare_url"].endswith("/compare/v16.24.1...v16.26.0")


def test_api_matrix_carries_behind_by_chip(client, updates_env):
    login(client, "readonly@example.com")
    r = client.get("/api/installed-apps")
    assert r.status_code == 200, r.text
    rows = {x["app_name"]: x for x in r.json()}
    assert rows["frappe"]["behind_by"] == 2
    assert rows["frappe"]["latest_ref"] == "v16.26.0"
    assert rows["erpnext"]["behind_by"] == 0


def test_api_refresh_requires_app_manage(client, updates_env, monkeypatch):
    import app.workers.updates as workers_updates

    monkeypatch.setattr(workers_updates, "enqueue_poll", lambda force=True: "rq-fake-1")

    # Read-only cannot trigger a poll.
    login(client, "readonly@example.com")
    denied = client.post("/api/updates/refresh", headers=csrf_headers(client))
    assert denied.status_code == 403

    # Developer (app:manage) can.
    login(client, "developer@example.com")
    ok = client.post("/api/updates/refresh", headers=csrf_headers(client))
    assert ok.status_code == 202, ok.text
    assert ok.json()["rq_job_id"] == "rq-fake-1"


def test_api_updates_requires_auth(client, updates_env):
    assert client.get("/api/updates").status_code == 401
