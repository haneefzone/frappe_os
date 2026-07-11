"""Update advisor core (session 3.2) — the testable logic behind "behind by N".

This module owns three read-only concerns, none of which touch a managed server
over SSH (upstream release detection is a control-plane→git-remote concern):

1. **Repo resolution** — map an installed app to the upstream repo it tracks
   (`frappe`/`erpnext`/… → github.com/frappe/<app>; anything else via its saved
   App Source repo URL). Marketplace-only apps with no git repo are un-pollable.
2. **Tag polling + versioning** — `git ls-remote --tags` the repo (argv, no shell;
   host-allowlist checked; cached with a TTL), parse the tags, and compute how
   many releases the installed ref is behind *on its own branch line* (v14/v15/
   v16-aware — a v15 site is never "behind" a v16 tag).
3. **Changelog preview** — the tag-range between installed and latest, with a
   per-release notes link and a compare URL (no blocking API call, no creds).

Everything here is pure functions + DB reads/writes given a session, so the whole
advisor is unit-testable against fixtures with an injected `tag_fetcher` — no
network, Redis or RQ needed. The scheduled poller (`app.workers.updates`) is thin
glue that calls `poll_updates` on the 2.1 scheduler's cadence.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.appsources import RepoSourceError, validate_repo_source
from app.models.app import AppSource, InstalledApp
from app.models.updates import AppVersionStatus, UpstreamTagCache

logger = logging.getLogger("app.updates")

# Apps that live under github.com/frappe and can be resolved without an App Source
# row (frappe itself + the common bundled apps). Anything else must carry a saved
# App Source whose repo_url points at an allowlisted git host.
FRAPPE_ORG_APPS: dict[str, str] = {
    app: f"https://github.com/frappe/{app}"
    for app in (
        "frappe",
        "erpnext",
        "hrms",
        "payments",
        "lms",
        "helpdesk",
        "insights",
        "wiki",
        "builder",
        "drive",
        "crm",
        "gameplan",
        "print_designer",
        "webshop",
    )
}

# A git tag we treat as a release: optional leading "v", then MAJOR.MINOR[.PATCH],
# optionally a pre-release suffix we keep out of the ordering.
_TAG_RE = re.compile(r"^v?(?P<maj>\d+)\.(?P<min>\d+)(?:\.(?P<pat>\d+))?(?P<pre>[-.].+)?$")
# A branch that pins a Frappe major, e.g. "version-16" / "v16" / "16".
_BRANCH_MAJOR_RE = re.compile(r"^(?:version-|v)?(?P<maj>\d+)$")

# `git ls-remote` line: "<sha>\trefs/tags/<tag>" (peeled tags carry a "^{}" suffix
# we strip so an annotated tag isn't double-counted).
_LS_REMOTE_RE = re.compile(r"^[0-9a-f]{40}\s+refs/tags/(?P<tag>.+?)(?:\^\{\})?$")


class UpdateAdvisorError(RuntimeError):
    """A repo could not be resolved or polled."""


# --------------------------------------------------------------------------- #
# Version parsing / comparison (branch-line aware)
# --------------------------------------------------------------------------- #

Version = tuple[int, int, int]


def parse_version(text: str | None) -> Version | None:
    """Parse a release tag/version into a (major, minor, patch) tuple, or None.

    Pre-release tags (``v16.0.0-beta.1``) are ignored for ordering — the advisor
    compares stable releases only, so a beta never shows a site as "behind".
    """
    if not text:
        return None
    m = _TAG_RE.match(text.strip())
    if not m or m.group("pre"):
        return None
    return (
        int(m.group("maj")),
        int(m.group("min")),
        int(m.group("pat") or 0),
    )


def major_of(branch: str | None, installed_ref: str | None) -> int | None:
    """The Frappe major line to compare on: the branch's pinned major if it names
    one (``version-16`` → 16), else the installed ref's own major."""
    if branch:
        m = _BRANCH_MAJOR_RE.match(branch.strip())
        if m:
            return int(m.group("maj"))
    v = parse_version(installed_ref)
    return v[0] if v else None


def compute_behind(
    *, installed_ref: str | None, branch: str | None, tags: Iterable[str]
) -> tuple[int | None, str | None]:
    """Return (behind_by, latest_ref) for an installed ref against upstream tags.

    - Only tags on the *same major line* count (a v15 site is never behind v16).
    - `latest_ref` is the newest stable tag on that line (or None if the line has
      no tags upstream).
    - `behind_by` is how many distinct stable releases on the line are strictly
      newer than the installed ref. It is None when the major line can't be
      determined or the installed ref can't be parsed (unknown, not "0").
    """
    line = major_of(branch, installed_ref)
    if line is None:
        return None, None

    # Distinct parsed versions on the line, keyed by tuple → original tag string.
    on_line: dict[Version, str] = {}
    for tag in tags:
        v = parse_version(tag)
        if v is not None and v[0] == line:
            # Keep the first spelling we see for a given version tuple.
            on_line.setdefault(v, tag.strip())
    if not on_line:
        return None, None

    latest_v = max(on_line)
    latest_ref = on_line[latest_v]

    installed_v = parse_version(installed_ref)
    if installed_v is None or installed_v[0] != line:
        # Latest is known but we can't say how far behind without a parsed ref.
        return None, latest_ref

    behind = sum(1 for v in on_line if v > installed_v)
    return behind, latest_ref


# --------------------------------------------------------------------------- #
# Repo resolution + tag fetching
# --------------------------------------------------------------------------- #


def _repo_key(remote_url: str) -> str:
    """Normalise a remote URL to a stable cache key: "host/owner/repo"."""
    try:
        validate_repo_source(remote_url)
    except RepoSourceError:
        # Already-resolved frappe-org URLs are always allowlisted; keep raw.
        pass
    m = re.match(
        r"^https://(?P<host>[A-Za-z0-9.-]+)(?::\d+)?/(?P<path>[A-Za-z0-9._/-]+?)(?:\.git)?/?$",
        remote_url.strip(),
    )
    if m:
        return f"{m.group('host').lower()}/{m.group('path')}"
    m = re.match(
        r"^git@(?P<host>[A-Za-z0-9.-]+):(?P<path>[A-Za-z0-9._/-]+?)(?:\.git)?/?$",
        remote_url.strip(),
    )
    if m:
        return f"{m.group('host').lower()}/{m.group('path')}"
    return remote_url.strip()


def resolve_repo(app_name: str, source: AppSource | None) -> tuple[str, str] | None:
    """Return (repo_key, remote_url) for an installed app, or None if un-pollable.

    Preference: a saved App Source git repo (operator-declared) wins; otherwise a
    known frappe-org app resolves to its public GitHub repo. A marketplace-only
    App Source (bare name, no git URL) or an unknown custom app returns None — we
    have nothing read-only to poll.
    """
    if source is not None and source.repo_url:
        try:
            resolved = validate_repo_source(source.repo_url)
        except RepoSourceError:
            resolved = None
        if resolved is not None and resolved.kind != "marketplace":
            url = source.repo_url.strip()
            return _repo_key(url), url
    url = FRAPPE_ORG_APPS.get(app_name)
    if url:
        return _repo_key(url), url
    return None


def _git_ls_remote_tags(remote_url: str, *, timeout: int) -> list[str]:
    """Read-only `git ls-remote --tags` → list of tag names. Argv only (no shell),
    host-allowlist checked, no credentials (public repos)."""
    validate_repo_source(remote_url)  # raises RepoSourceError on a non-allowlisted host
    proc = subprocess.run(  # noqa: S603 — fixed argv, validated URL, no shell
        ["git", "ls-remote", "--tags", "--refs", remote_url],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        env={"GIT_TERMINAL_PROMPT": "0"},
    )
    if proc.returncode != 0:
        raise UpdateAdvisorError(
            f"git ls-remote failed for {remote_url}: {proc.stderr.strip()[:200]}"
        )
    tags: list[str] = []
    for line in proc.stdout.splitlines():
        m = _LS_REMOTE_RE.match(line.strip())
        if m:
            tags.append(m.group("tag"))
    return tags


# A tag fetcher: repo_key, remote_url -> tag list. Injected in tests; the default
# shells out to git. Raises UpdateAdvisorError on failure.
TagFetcher = Callable[[str, str], list[str]]


def _default_fetcher(timeout: int) -> TagFetcher:
    def fetch(repo_key: str, remote_url: str) -> list[str]:
        return _git_ls_remote_tags(remote_url, timeout=timeout)

    return fetch


def get_cached_tags(
    db: Session,
    *,
    repo_key: str,
    remote_url: str,
    fetcher: TagFetcher,
    ttl_seconds: int,
    now: datetime,
    force: bool = False,
) -> tuple[list[str], str | None]:
    """Return (tags, error) for a repo, refreshing the TTL cache when stale.

    A cache row fresher than ``ttl_seconds`` is reused verbatim (no network). On a
    fetch failure we keep the last good tags (so a transient outage doesn't wipe
    the advisor) and record `last_error`.
    """
    row = db.scalars(
        select(UpstreamTagCache).where(UpstreamTagCache.repo_key == repo_key)
    ).first()
    if row is None:
        row = UpstreamTagCache(repo_key=repo_key, remote_url=remote_url, tags_json="[]")
        db.add(row)

    fresh = (
        not force
        and row.fetched_at is not None
        and (now - _as_utc(row.fetched_at)) < timedelta(seconds=ttl_seconds)
        and row.tags_json not in (None, "[]")
    )
    if fresh:
        return json.loads(row.tags_json), None

    row.remote_url = remote_url
    try:
        tags = fetcher(repo_key, remote_url)
    except Exception as exc:  # noqa: BLE001 — one repo's failure must not sink the sweep
        row.last_error = str(exc)[:500]
        db.commit()
        logger.warning("tag fetch failed for %s: %s", repo_key, exc)
        return json.loads(row.tags_json or "[]"), row.last_error
    row.tags_json = json.dumps(tags)
    row.fetched_at = now
    row.last_error = None
    db.commit()
    return tags, None


def _as_utc(dt: datetime) -> datetime:
    """SQLite (tests) hands back naive UTC; Postgres tz-aware. Normalise to aware."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


# --------------------------------------------------------------------------- #
# The poll sweep (idempotent)
# --------------------------------------------------------------------------- #


def poll_updates(
    db: Session,
    *,
    now: datetime | None = None,
    ttl_seconds: int = 1800,
    git_timeout: int = 20,
    fetcher: TagFetcher | None = None,
    force: bool = False,
) -> dict:
    """Sweep every installed app: resolve its repo, (TTL-)fetch tags, compute
    behind-by, and upsert its `AppVersionStatus`. Read-only against upstream.

    Idempotent: status rows are keyed on `installed_app_id`, so re-running the
    sweep updates in place — never duplicates (acceptance criterion). Un-pollable
    apps (no git repo) still get a row with `behind_by=None` so the UI can say
    "not tracked" rather than silently omitting them.

    Returns a summary dict (checked/updated/behind/errors) for the RQ result and
    the run-now endpoint.
    """
    now = now or datetime.now(UTC)
    fetcher = fetcher or _default_fetcher(git_timeout)

    installed = list(db.scalars(select(InstalledApp)).all())
    sources = {s.id: s for s in db.scalars(select(AppSource)).all()}

    checked = updated = behind_count = errors = 0
    # Fetch each repo's tags at most once per sweep (cache handles cross-repo).
    for ia in installed:
        checked += 1
        source = sources.get(ia.app_source_id) if ia.app_source_id else None
        resolved = resolve_repo(ia.app_name, source)

        tags: list[str] = []
        err: str | None = None
        repo_key: str | None = None
        if resolved is None:
            err = "no upstream git repo to poll (marketplace/unknown app)"
        else:
            repo_key, remote_url = resolved
            tags, err = get_cached_tags(
                db,
                repo_key=repo_key,
                remote_url=remote_url,
                fetcher=fetcher,
                ttl_seconds=ttl_seconds,
                now=now,
                force=force,
            )

        behind_by, latest_ref = (None, None)
        if tags:
            behind_by, latest_ref = compute_behind(
                installed_ref=ia.version, branch=ia.branch, tags=tags
            )
        if behind_by:
            behind_count += 1
        if err:
            errors += 1

        _upsert_status(
            db,
            ia=ia,
            repo_key=repo_key,
            latest_ref=latest_ref,
            behind_by=behind_by,
            checked_at=now,
            last_error=err,
        )
        updated += 1

    db.commit()
    summary = {
        "checked": checked,
        "updated": updated,
        "behind": behind_count,
        "errors": errors,
    }
    logger.info("update advisor poll: %s", summary)
    return summary


def _upsert_status(
    db: Session,
    *,
    ia: InstalledApp,
    repo_key: str | None,
    latest_ref: str | None,
    behind_by: int | None,
    checked_at: datetime,
    last_error: str | None,
) -> AppVersionStatus:
    row = db.scalars(
        select(AppVersionStatus).where(
            AppVersionStatus.installed_app_id == ia.id
        )
    ).first()
    if row is None:
        row = AppVersionStatus(installed_app_id=ia.id, site_id=ia.site_id)
        db.add(row)
    row.site_id = ia.site_id
    row.app_name = ia.app_name
    row.branch = ia.branch
    row.repo_key = repo_key
    row.installed_ref = ia.version
    row.latest_ref = latest_ref
    row.behind_by = behind_by
    row.checked_at = checked_at
    row.last_error = last_error
    return row


# --------------------------------------------------------------------------- #
# Changelog preview
# --------------------------------------------------------------------------- #


def _github_repo(repo_key: str | None) -> str | None:
    """The "owner/repo" for a github.com repo key, else None (no notes links)."""
    if repo_key and repo_key.lower().startswith("github.com/"):
        return repo_key.split("/", 1)[1]
    return None


def changelog_preview(
    *,
    repo_key: str | None,
    installed_ref: str | None,
    branch: str | None,
    tags: Iterable[str],
) -> dict:
    """Build the release-range preview between the installed ref and the latest.

    Returns the intervening stable releases newest-first, each with a per-release
    notes URL (GitHub releases page for the tag) plus a compare URL — so the UI
    "renders release notes between current and latest" without a blocking API call
    or credentials. When the installed ref is unknown we still list the line's
    releases up to latest so the operator sees what is available.
    """
    line = major_of(branch, installed_ref)
    tag_list = list(tags)
    installed_v = parse_version(installed_ref)

    on_line: list[tuple[Version, str]] = []
    for tag in tag_list:
        v = parse_version(tag)
        if v is not None and (line is None or v[0] == line):
            on_line.append((v, tag.strip()))
    on_line.sort(reverse=True)  # newest first

    gh = _github_repo(repo_key)
    latest_ref = on_line[0][1] if on_line else None
    # The upstream tag spelling for the installed version ("16.24.1" → "v16.24.1"),
    # so the compare URL uses a ref GitHub actually knows.
    installed_tag = next(
        (tag for v, tag in on_line if installed_v is not None and v == installed_v),
        installed_ref,
    )

    releases: list[dict] = []
    for v, tag in on_line:
        if installed_v is not None and v <= installed_v:
            break  # only releases newer than what's installed
        releases.append(
            {
                "tag": tag,
                "version": ".".join(str(p) for p in v),
                "notes_url": f"https://github.com/{gh}/releases/tag/{tag}" if gh else None,
            }
        )

    compare_url = None
    if gh and installed_tag and latest_ref and installed_tag != latest_ref:
        compare_url = f"https://github.com/{gh}/compare/{installed_tag}...{latest_ref}"

    return {
        "repo_key": repo_key,
        "installed_ref": installed_ref,
        "latest_ref": latest_ref,
        "behind_by": len(releases) if installed_v is not None else None,
        "releases": releases,
        "compare_url": compare_url,
        "releases_url": f"https://github.com/{gh}/releases" if gh else None,
    }


# --------------------------------------------------------------------------- #
# Dashboard / needs-attention summary
# --------------------------------------------------------------------------- #


def updates_summary(db: Session) -> dict:
    """Fleet-wide advisor rollup for the dashboard "Needs attention" row and the
    real Fleet-Health `updates` component.

    - `apps_behind` / `sites_behind` — distinct counts of what has an update.
    - `security_updates` — apps flagged with a security advisory (0 until wired).
    - `up_to_date_fraction` — of apps we could actually check (behind_by not
      NULL), the fraction that are current. 1.0 when nothing is tracked yet (a
      fresh/empty fleet is not penalised — mirrors the uptime/backup convention).
    """
    rows = list(db.scalars(select(AppVersionStatus)).all())
    tracked = [r for r in rows if r.behind_by is not None]
    apps_behind = sum(1 for r in tracked if r.behind_by and r.behind_by > 0)
    sites_behind = len(
        {r.site_id for r in tracked if r.behind_by and r.behind_by > 0}
    )
    security_updates = sum(1 for r in rows if r.security_update)
    up_to_date_fraction = (
        1.0 if not tracked else (len(tracked) - apps_behind) / len(tracked)
    )
    return {
        "apps_behind": apps_behind,
        "sites_behind": sites_behind,
        "security_updates": security_updates,
        "tracked": len(tracked),
        "up_to_date_fraction": up_to_date_fraction,
    }
