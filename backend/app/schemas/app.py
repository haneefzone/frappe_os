"""Request/response models for the apps API (session 1.9).

The deploy key is WRITE-ONLY: it is accepted on create/update and stored Fernet-
encrypted, but never returned — responses expose only `has_deploy_key`
(CLAUDE.md rule 6, same pattern as the server's MariaDB root password).
"""

from datetime import datetime

from pydantic import BaseModel, Field

from app.models.app import AppSource, InstalledApp


class AppSourceOut(BaseModel):
    """One app source (repo or marketplace name). Deploy key never returned."""

    id: int
    name: str
    repo_url: str
    kind: str
    default_branch: str | None
    is_private: bool
    has_deploy_key: bool
    notes: str | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_model(cls, s: AppSource) -> "AppSourceOut":
        return cls(
            id=s.id,
            name=s.name,
            repo_url=s.repo_url,
            kind=s.kind,
            default_branch=s.default_branch,
            is_private=s.is_private,
            has_deploy_key=bool(s.deploy_key_enc),
            notes=s.notes,
            created_at=s.created_at,
            updated_at=s.updated_at,
        )


class CreateAppSourceRequest(BaseModel):
    """Register a source. `repo_url` is a marketplace name or an allowlisted repo
    URL (validated server-side). A private source should carry an SSH deploy
    key (git@host:… URL) so the fetch can authenticate."""

    name: str = Field(min_length=1, max_length=120)
    repo_url: str = Field(min_length=1, max_length=300)
    default_branch: str | None = Field(default=None, max_length=100)
    is_private: bool = False
    # PEM private key; write-only, stored encrypted.
    deploy_key: str | None = Field(default=None, max_length=10000)
    notes: str | None = Field(default=None, max_length=2000)


class UpdateAppSourceRequest(BaseModel):
    """Patch a source. Any omitted field is left unchanged; `deploy_key=""`
    clears the stored key, a non-empty value replaces it."""

    name: str | None = Field(default=None, min_length=1, max_length=120)
    repo_url: str | None = Field(default=None, min_length=1, max_length=300)
    default_branch: str | None = Field(default=None, max_length=100)
    is_private: bool | None = None
    deploy_key: str | None = Field(default=None, max_length=10000)
    notes: str | None = Field(default=None, max_length=2000)


class InstallAppRequest(BaseModel):
    """Install an app on a site. One source of install parameters:
    - `store_app` — a Frappe app-store name (DOO-1192); the platform resolves the
      compatible release (repo, pinned branch, dependencies) against the bench's
      Frappe version and reuses this same install path,
    - `app_source_id` — a saved source (its repo_url + branch + deploy key),
    - a raw `source` + `branch` for an ad-hoc public repo,
    - or neither for an already-fetched marketplace app.
    `app` is the module name to install (defaults to the source/store name)."""

    app: str | None = Field(default=None, max_length=120)
    store_app: str | None = Field(default=None, max_length=120)
    app_source_id: int | None = None
    source: str | None = Field(default=None, max_length=300)
    branch: str | None = Field(default=None, max_length=100)
    priority: str = "high"


class StoreCatalogAppOut(BaseModel):
    """One app-store catalog entry resolved against a bench's Frappe version.

    Field names are the AC2 interface contract shared with the frontend issue —
    do not rename without updating DOO-1192's frontend counterpart."""

    name: str
    title: str
    description: str
    repo: str
    logo_url: str | None
    website: str | None
    documentation: str | None
    categories: list[str]
    stars: int | None
    branch: str | None
    commit: str | None
    version: str | None
    channel: str | None
    required_version: str | None
    dependencies: dict
    is_installable: bool
    installed: bool
    incompatible_reason: str | None


class UninstallAppRequest(BaseModel):
    """Uninstall an app from a site. `confirm_name` must equal the app name
    (type-the-target-name-to-confirm, CLAUDE.md rule 5)."""

    confirm_name: str = Field(min_length=1, max_length=120)
    priority: str = "high"


class ListBranchesRequest(BaseModel):
    """Fetch remote branches for the picker. Runs `git ls-remote` on `server_id`
    against `repo_url` (or a saved `app_source_id`, whose deploy key is used)."""

    server_id: int
    repo_url: str | None = Field(default=None, max_length=300)
    app_source_id: int | None = None


class InstalledAppOut(BaseModel):
    """One app×site matrix cell."""

    id: int
    site_id: int
    site_name: str
    bench_id: int
    bench_name: str
    server_id: int
    app_source_id: int | None
    app_name: str
    branch: str | None
    version: str | None
    installed_at: datetime | None
    # Update advisor (session 3.2): "behind by N" chip data. NULL behind_by =
    # not yet checked or not trackable; 0 = up to date. Populated from the
    # AppVersionStatus row when present.
    behind_by: int | None = None
    latest_ref: str | None = None
    security_update: bool = False
    update_checked_at: datetime | None = None

    @classmethod
    def from_model(
        cls,
        ia: InstalledApp,
        *,
        site_name: str,
        bench_name: str,
        server_id: int,
        update_status: object | None = None,
    ) -> "InstalledAppOut":
        return cls(
            id=ia.id,
            site_id=ia.site_id,
            site_name=site_name,
            bench_id=ia.bench_id,
            bench_name=bench_name,
            server_id=server_id,
            app_source_id=ia.app_source_id,
            app_name=ia.app_name,
            branch=ia.branch,
            version=ia.version,
            installed_at=ia.installed_at,
            behind_by=getattr(update_status, "behind_by", None),
            latest_ref=getattr(update_status, "latest_ref", None),
            security_update=bool(getattr(update_status, "security_update", False)),
            update_checked_at=getattr(update_status, "checked_at", None),
        )
