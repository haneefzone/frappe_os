"""Request/response models for the Frappe app store API (DOO-1194).

The catalog is served read-only from the cached `frappe/marketplace` checkout.
Compatibility (`is_installable` + a human-readable `reason`) is computed against a
target bench's installed Frappe version and surfaced, never silently hidden (AC2).
"""

from pydantic import BaseModel, Field

from app.core.marketplace import App, PlanStep, Release


class MarketplaceAppOut(BaseModel):
    """One catalog entry, optionally with compatibility for a target bench."""

    name: str
    title: str
    description: str
    repo: str
    logo_url: str | None = None
    website: str | None = None
    documentation: str | None = None
    categories: list[str] = []
    category: str | None = None
    stars: int = 0
    # Compatibility fields are populated only when a target bench is given.
    is_installable: bool | None = None
    reason: str | None = None
    latest_compatible_version: str | None = None

    @classmethod
    def from_app(
        cls,
        app: App,
        *,
        is_installable: bool | None = None,
        reason: str | None = None,
        latest_compatible_version: str | None = None,
    ) -> "MarketplaceAppOut":
        return cls(
            name=app.name,
            title=app.title,
            description=app.description,
            repo=app.repo,
            logo_url=app.logo_url,
            website=app.website,
            documentation=app.documentation,
            categories=list(app.categories),
            category=app.category,
            stars=app.stars,
            is_installable=is_installable,
            reason=reason,
            latest_compatible_version=latest_compatible_version,
        )


class MarketplaceReleaseOut(BaseModel):
    """One release of an app, with its compatibility against the target bench."""

    version: str
    branch: str
    commit: str
    frappe_core: str
    dependencies: dict[str, str] = {}
    channel: str
    is_compatible: bool | None = None

    @classmethod
    def from_release(
        cls, r: Release, *, is_compatible: bool | None = None
    ) -> "MarketplaceReleaseOut":
        return cls(
            version=r.version,
            branch=r.branch,
            commit=r.commit,
            frappe_core=r.frappe_core,
            dependencies=dict(r.dependencies),
            channel=r.channel,
            is_compatible=is_compatible,
        )


class PlanStepOut(BaseModel):
    """One fetch+install step of a resolved plan (dependencies first)."""

    app: str
    version: str
    branch: str
    commit: str
    repo: str
    channel: str
    reason: str

    @classmethod
    def from_step(cls, s: PlanStep) -> "PlanStepOut":
        return cls(
            app=s.app,
            version=s.version,
            branch=s.branch,
            commit=s.commit,
            repo=s.repo,
            channel=s.channel,
            reason=s.reason,
        )


class MarketplaceAppDetailOut(MarketplaceAppOut):
    """App detail: catalog entry + releases + the resolved install plan (or a
    legible error explaining why it cannot be installed)."""

    releases: list[MarketplaceReleaseOut] = []
    plan: list[PlanStepOut] | None = None
    plan_error: str | None = None


class MarketplaceInstallRequest(BaseModel):
    """Install a store app on a site. The full dependency plan is resolved and
    validated server-side before anything is enqueued (never a partial install)."""

    app: str = Field(min_length=1, max_length=120)
    priority: str = "high"


class MarketplaceRefreshOut(BaseModel):
    """Result of a forced catalog refresh."""

    refreshed: bool
    served_stale: bool
    app_count: int
