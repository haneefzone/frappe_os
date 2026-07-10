"""App-source validation + installed-app bookkeeping (session 1.9).

Two concerns kept out of the API/action layers so both share one implementation
and both are unit-testable without SSH or a DB session:

- `validate_repo_source` — the host-allowlist gate (CLAUDE.md golden rule 1).
  A `bench get-app` argument is EITHER a bare marketplace name (`^[a-z0-9_]+$`)
  or an https/ssh repo URL whose host is on the configurable allowlist
  (github.com, gitlab.com by default). This is defence in depth on top of the
  template's shell-safe character whitelist: the char whitelist stops shell
  metacharacters; this stops a validly-shaped URL pointing at an unapproved
  host.

- `parse_app_version` / `upsert_installed_app` / `remove_installed_app` — parse
  an app's version out of `bench version` output and keep the `installed_apps`
  matrix rows in step with install/uninstall.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.app import InstalledApp

# A bare marketplace app / module name — the same shape Frappe uses for an app
# module. Matches `bench get-app <name>` and `install-app <name>`.
MARKETPLACE_NAME = re.compile(r"^[a-z0-9_]+$")

# https://host/owner/repo(.git)  or  git@host:owner/repo(.git)
_HTTPS_URL = re.compile(r"^https://(?P<host>[A-Za-z0-9.-]+)(?::\d+)?/(?P<path>[A-Za-z0-9._/-]+?)(?:\.git)?/?$")
_SSH_URL = re.compile(r"^git@(?P<host>[A-Za-z0-9.-]+):(?P<path>[A-Za-z0-9._/-]+?)(?:\.git)?/?$")


class RepoSourceError(ValueError):
    """The get-app source is neither a bare marketplace name nor an allowlisted
    repo URL. Maps to HTTP 422."""


@dataclass(frozen=True)
class ResolvedSource:
    """The classified, validated get-app argument."""

    # marketplace | github | gitlab (host mapped for known hosts, else the host)
    kind: str
    # The value to hand to `bench get-app` (unchanged from the input).
    argument: str
    # Whether the URL is an ssh (git@host:...) form — the only form a deploy key
    # can authenticate; https private repos would need a token, out of scope.
    is_ssh: bool


def _kind_for_host(host: str) -> str:
    host = host.lower()
    if host == "github.com":
        return "github"
    if host == "gitlab.com":
        return "gitlab"
    return host


def validate_repo_source(value: str, *, allowlist: set[str] | None = None) -> ResolvedSource:
    """Classify + host-check a get-app source. Raises RepoSourceError on an
    unapproved host or an unrecognised shape.

    `allowlist` defaults to the configured `repo_host_allowlist` (github.com,
    gitlab.com). Marketplace names bypass the host check entirely.
    """
    value = (value or "").strip()
    if not value:
        raise RepoSourceError("app source is empty")

    if MARKETPLACE_NAME.match(value):
        return ResolvedSource(kind="marketplace", argument=value, is_ssh=False)

    if allowlist is None:
        from app.config import get_settings

        allowlist = get_settings().repo_host_allowlist_set

    for pattern, is_ssh in ((_HTTPS_URL, False), (_SSH_URL, True)):
        m = pattern.match(value)
        if m:
            host = m.group("host").lower()
            if host not in allowlist:
                raise RepoSourceError(
                    f"repo host {host!r} is not on the allowlist "
                    f"({', '.join(sorted(allowlist)) or 'none configured'})"
                )
            return ResolvedSource(kind=_kind_for_host(host), argument=value, is_ssh=is_ssh)

    raise RepoSourceError(
        "app source must be a marketplace name (e.g. 'erpnext') or a "
        "github.com/gitlab.com repo URL (https:// or git@)"
    )


# --------------------------------------------------------------------------- #
# `bench version` parsing + installed-app matrix upkeep
# --------------------------------------------------------------------------- #

# `bench version` (plain) prints one "app x.y.z" line per installed app.
_VERSION_LINE = re.compile(r"^(?P<app>[a-z0-9_]+)\s+(?P<ver>\S+)\s*$")


def parse_app_version(text: str, app_name: str) -> str | None:
    """Pull one app's version out of plain `bench version` output, or None."""
    for line in text.splitlines():
        m = _VERSION_LINE.match(line.strip())
        if m and m.group("app") == app_name:
            return m.group("ver")
    return None


def upsert_installed_app(
    db: Session,
    *,
    site_id: int,
    bench_id: int,
    app_name: str,
    app_source_id: int | None = None,
    branch: str | None = None,
    version: str | None = None,
    now: datetime | None = None,
) -> InstalledApp:
    """Insert or refresh the (site, app) matrix cell. Keyed on (site_id,
    app_name) so re-installing the same app updates the row rather than
    duplicating it."""
    now = now or datetime.now(UTC)
    row = db.scalars(
        select(InstalledApp).where(
            InstalledApp.site_id == site_id, InstalledApp.app_name == app_name
        )
    ).first()
    if row is None:
        row = InstalledApp(site_id=site_id, bench_id=bench_id, app_name=app_name)
        db.add(row)
    row.bench_id = bench_id
    if app_source_id is not None:
        row.app_source_id = app_source_id
    if branch is not None:
        row.branch = branch
    if version is not None:
        row.version = version
    row.installed_at = now
    db.commit()
    db.refresh(row)
    return row


def remove_installed_app(db: Session, *, site_id: int, app_name: str) -> bool:
    """Drop the (site, app) matrix cell after a successful uninstall. Returns
    True if a row was removed."""
    row = db.scalars(
        select(InstalledApp).where(
            InstalledApp.site_id == site_id, InstalledApp.app_name == app_name
        )
    ).first()
    if row is None:
        return False
    db.delete(row)
    db.commit()
    return True
