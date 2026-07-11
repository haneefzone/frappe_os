"""Update advisor response schemas (session 3.2)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from app.models.updates import AppVersionStatus


class UpdateStatusOut(BaseModel):
    """One installed app's advisor verdict — the "behind by N" chip's data."""

    installed_app_id: int
    site_id: int
    site_name: str
    bench_id: int
    bench_name: str
    app_name: str
    branch: str | None
    installed_ref: str | None
    latest_ref: str | None
    behind_by: int | None
    security_update: bool
    checked_at: datetime | None
    last_error: str | None

    @classmethod
    def from_row(
        cls,
        status: AppVersionStatus,
        *,
        bench_id: int,
        site_name: str,
        bench_name: str,
    ) -> UpdateStatusOut:
        return cls(
            installed_app_id=status.installed_app_id,
            site_id=status.site_id,
            site_name=site_name,
            bench_id=bench_id,
            bench_name=bench_name,
            app_name=status.app_name,
            branch=status.branch,
            installed_ref=status.installed_ref,
            latest_ref=status.latest_ref,
            behind_by=status.behind_by,
            security_update=status.security_update,
            checked_at=status.checked_at,
            last_error=status.last_error,
        )


class UpdatesSummaryOut(BaseModel):
    """Fleet rollup for the dashboard "updates available" chip."""

    apps_behind: int
    sites_behind: int
    security_updates: int
    tracked: int
    up_to_date_fraction: float


class ChangelogReleaseOut(BaseModel):
    tag: str
    version: str
    notes_url: str | None


class ChangelogPreviewOut(BaseModel):
    """The release-range between installed and latest — the changelog preview."""

    installed_app_id: int
    app_name: str
    repo_key: str | None
    installed_ref: str | None
    latest_ref: str | None
    behind_by: int | None
    releases: list[ChangelogReleaseOut]
    compare_url: str | None
    releases_url: str | None
