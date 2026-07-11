"""Update advisor tables (session 3.2).

Read-only release detection: a scheduled poller reads the upstream git tags for
each installed app, computes how many releases the installed ref is *behind* on
its branch line, and records the result. No update is ever performed here — that
is the safe-update pipeline (3.3). Two tables:

- `UpstreamTagCache` — one row per upstream repo the platform tracks (keyed on a
  normalised `repo_key`, e.g. ``github.com/frappe/frappe``). Holds the last set
  of tags fetched with `git ls-remote --tags` and *when*, so a poll reuses a
  fresh cache instead of hammering the remote (the TTL cache the spec asks for).
  Shared across every site running that app.

- `AppVersionStatus` — one row per `InstalledApp` (the app×site cell): the ref
  the site is on, the latest upstream ref on its branch line, `behind_by`, an
  optional `security_update` flag, and when it was last checked. This is what
  the "behind by N" chips and the dashboard "updates available" count read.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class UpstreamTagCache(Base):
    """Cached `git ls-remote --tags` output for one upstream repo (TTL-guarded)."""

    __tablename__ = "upstream_tag_cache"
    __table_args__ = (
        UniqueConstraint("repo_key", name="uq_upstream_tag_cache_repo_key"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # Normalised repo identity, e.g. "github.com/frappe/frappe". One cache row per
    # repo regardless of how many sites install the app from it.
    repo_key: Mapped[str] = mapped_column(String(300))
    # The remote actually polled (https URL) — kept for the changelog links.
    remote_url: Mapped[str] = mapped_column(String(300))
    # JSON array of tag strings (e.g. ["v15.40.0", "v16.24.1", ...]).
    tags_json: Mapped[str] = mapped_column(Text, default="[]")
    # When the tags were last fetched; a poll within the TTL reuses this row.
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Last fetch error (network / bad host), surfaced so a stuck repo is visible.
    last_error: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class AppVersionStatus(Base):
    """The advisor verdict for one installed app (the behind-by-N chip's source)."""

    __tablename__ = "app_version_status"
    __table_args__ = (
        # Exactly one status row per installed-app cell; the poll upserts on it so
        # re-running the sweep never duplicates (idempotent, acceptance criterion).
        UniqueConstraint(
            "installed_app_id", name="uq_app_version_status_installed_app"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    installed_app_id: Mapped[int] = mapped_column(
        ForeignKey("installed_apps.id", ondelete="CASCADE"), index=True
    )
    # Denormalised so chips/summary queries don't need a join back through the
    # matrix; kept in sync by the poller.
    site_id: Mapped[int] = mapped_column(
        ForeignKey("sites.id", ondelete="CASCADE"), index=True
    )
    app_name: Mapped[str] = mapped_column(String(120))
    branch: Mapped[str | None] = mapped_column(String(100))
    repo_key: Mapped[str | None] = mapped_column(String(300))

    # The ref the site is on (from `bench version`, e.g. "16.24.1") and the latest
    # upstream tag on that branch line (e.g. "v16.25.0"). Either may be NULL when
    # unknown (app never versioned, or the repo could not be polled).
    installed_ref: Mapped[str | None] = mapped_column(String(80))
    latest_ref: Mapped[str | None] = mapped_column(String(80))
    # How many releases the installed ref is behind on its branch line. NULL =
    # not yet computable (no installed version, or the poll failed); 0 = current.
    behind_by: Mapped[int | None] = mapped_column(Integer)
    # Optional advisory flag (spec: "optional security-advisory flag"). No public
    # advisory feed is wired yet, so the poller leaves this False; the column and
    # the banner path exist so 3.x can light it up without a schema change.
    security_update: Mapped[bool] = mapped_column(Boolean, default=False)

    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Per-app poll error (e.g. remote unreachable) so a single bad repo is visible
    # without sinking the whole sweep.
    last_error: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
