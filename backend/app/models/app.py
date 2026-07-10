"""App sources and installed apps (session 1.9).

Two tables:

- `AppSource` — a place the platform can `bench get-app` from: a marketplace
  bare name (`erpnext`), a public GitHub/GitLab repo, or a private repo reached
  with a Fernet-encrypted deploy key. The deploy key plaintext never leaves the
  `deploy_key_enc` column except, at execution time, onto a temp 0600 key file
  on the target for the duration of a single `get-app` (CLAUDE.md rule 6).

- `InstalledApp` — one row per (site, app): what the platform installed on a
  site (or discovered there), with the branch/version it is on. `app_source_id`
  is nullable so an app discovered on a site that the platform never fetched
  (e.g. `frappe` itself) can still appear in the app×site matrix.
"""

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

# How the source is reached (drives the get-app argument + the deploy-key dance).
APP_SOURCE_KINDS = ("marketplace", "github", "gitlab")


class AppSource(Base):
    """A repo (or marketplace name) the platform can fetch an app from."""

    __tablename__ = "app_sources"
    __table_args__ = (
        UniqueConstraint("name", name="uq_app_sources_name"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # Human label / the app's module name for a marketplace source (e.g.
    # "erpnext", "hrms", "my_custom_app"). Unique so the matrix can key on it.
    name: Mapped[str] = mapped_column(String(120))
    # The `bench get-app` argument: a bare marketplace name or an https/ssh repo
    # URL. Validated against the host allowlist + a shell-safe char whitelist
    # before it is ever stored (app.core.appsources.validate_repo_source).
    repo_url: Mapped[str] = mapped_column(String(300))
    # marketplace | github | gitlab (see APP_SOURCE_KINDS).
    kind: Mapped[str] = mapped_column(String(20), default="marketplace")
    default_branch: Mapped[str | None] = mapped_column(String(100))
    is_private: Mapped[bool] = mapped_column(Boolean, default=False)
    # A git deploy key (private key PEM), Fernet-encrypted at rest. NULL for
    # public/marketplace sources. Never returned in plaintext; the API exposes
    # only a "has a key" boolean.
    deploy_key_enc: Mapped[str | None] = mapped_column(Text)
    # Frappe compatibility is not known for arbitrary repos — the Add Source
    # flow warns on this. Free-text note the operator can set (e.g. "v15+").
    notes: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    installs: Mapped[list["InstalledApp"]] = relationship(
        back_populates="source", passive_deletes=True
    )


class InstalledApp(Base):
    """One app installed on one site (the app×site matrix cell)."""

    __tablename__ = "installed_apps"
    __table_args__ = (
        # One row per app per site; install/uninstall + discovery upsert on it.
        UniqueConstraint("site_id", "app_name", name="uq_installed_apps_site_app"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(
        ForeignKey("sites.id", ondelete="CASCADE"), index=True
    )
    # Denormalised so the matrix can group by bench without a join through site.
    bench_id: Mapped[int] = mapped_column(
        ForeignKey("benches.id", ondelete="CASCADE"), index=True
    )
    # NULL for an app discovered on the site that no known source produced
    # (e.g. `frappe`), or when the source row was later deleted.
    app_source_id: Mapped[int | None] = mapped_column(
        ForeignKey("app_sources.id", ondelete="SET NULL"), index=True
    )
    # The Frappe app module name (install-app/uninstall-app argument).
    app_name: Mapped[str] = mapped_column(String(120))
    branch: Mapped[str | None] = mapped_column(String(100))
    version: Mapped[str | None] = mapped_column(String(50))
    installed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    source: Mapped["AppSource | None"] = relationship(back_populates="installs")
