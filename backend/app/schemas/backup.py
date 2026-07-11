"""Request/response models for the backups + guided restore API (session 1.11)."""

from datetime import datetime

from pydantic import BaseModel, Field

from app.models import Server
from app.models.backup import Backup
from app.models.bench import Bench
from app.models.site import Site


class BackupArtifactOut(BaseModel):
    """One captured artifact (its integrity metadata; the server path is kept
    server-side and referenced only by the download endpoint's `artifact` key)."""

    kind: str
    size_bytes: int
    checksum_sha256: str


class BackupOut(BaseModel):
    """One backup, enriched with its site/bench/server for the Backups table."""

    id: int
    site_id: int
    site_name: str
    bench_id: int
    bench_name: str
    server_id: int
    type: str
    status: str
    size_bytes: int | None
    artifacts: list[BackupArtifactOut]
    # Which artifact kinds are downloadable (drives the download menu).
    available_artifacts: list[str]
    frappe_version: str | None
    restore_tested: bool
    taken_by_job_id: int | None
    # Offsite storage (session 2.2): drives the storage chip in the table (B4.6).
    storage_state: str
    storage_target_id: int | None
    # Artifact kinds present offsite (drive the presigned-download menu).
    offsite_artifacts: list[str]
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_model(
        cls, backup: Backup, site: Site, bench: Bench, server: Server
    ) -> "BackupOut":
        arts = [
            BackupArtifactOut(
                kind=a.get("kind", "?"),
                size_bytes=int(a.get("size_bytes") or 0),
                checksum_sha256=a.get("checksum_sha256", ""),
            )
            for a in (backup.artifacts or [])
        ]
        return cls(
            id=backup.id,
            site_id=backup.site_id,
            site_name=site.name,
            bench_id=backup.bench_id,
            bench_name=bench.name,
            server_id=server.id,
            type=backup.type,
            status=backup.status,
            size_bytes=backup.size_bytes,
            artifacts=arts,
            available_artifacts=[a.kind for a in arts],
            frappe_version=backup.frappe_version,
            restore_tested=backup.restore_tested,
            taken_by_job_id=backup.taken_by_job_id,
            storage_state=backup.storage_state or "local",
            storage_target_id=backup.storage_target_id,
            offsite_artifacts=sorted((backup.object_keys or {}).keys()),
            created_at=backup.created_at,
            updated_at=backup.updated_at,
        )


class CreateBackupRequest(BaseModel):
    """Back up a site now. `with_files` = full backup (db + public + private
    files); false = a faster db-only dump."""

    with_files: bool = True
    # bench backup is longer-running; default it to the high queue.
    priority: str = "high"
    # Push the artifacts to this S3 target after the backup (session 2.2). Omit
    # to let the API auto-select the single enabled target, or send 0/-1 to force
    # a local-only backup even when a target exists.
    storage_target_id: int | None = None


class RestoreRequest(BaseModel):
    """Launch a guided restore of `backup_id` onto a target.

    Modes:
    - same_site         — restore over the backup's own site (destructive).
    - new_site          — create a fresh site and restore into it.
    - different_bench   — restore onto an existing/new site on another bench
                          (compatibility-checked: no major downgrades).

    `confirm_name` must equal the target site name for a destructive restore
    (type-the-target-name-to-confirm, rule 5). `admin_password` is required for
    new_site (the fresh site's Administrator password); the MariaDB root password
    is pulled server-side, never sent."""

    mode: str
    backup_id: int
    # Target: for same_site the backup's own site is used; for new_site /
    # different_bench give the destination bench + site name.
    target_bench_id: int | None = None
    target_site_name: str | None = None
    admin_password: str | None = Field(default=None, max_length=128)
    # Destructive restores (over an existing site) require the typed site name.
    confirm_name: str | None = None
    priority: str = "high"


class CompatibilityOut(BaseModel):
    """Result of checking whether `backup_id` can restore onto a target bench."""

    ok: bool
    source_major: int | None
    target_major: int | None
    reason: str
