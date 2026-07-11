"""Backup artifacts produced by the platform (session 1.11).

A `Backup` is one `bench --site X backup [--with-files]` run captured as a first-
class row: the artifact file paths on the server (database dump, public files,
private files, and the `site_config_backup.json` that carries the
`encryption_key`), each artifact's size + sha256, the total size, and the
lifecycle status. `restore_tested` flips true once a backup has been restored
into a scratch/other site and verified — the only real proof a backup works.

Artifacts are stored twice on purpose:
- the four convenience path columns (`db_path`, `public_files_path`,
  `private_files_path`, `config_path`) are what the guided restore flow hands to
  `bench restore` and the `encryption_key` copy step (gotcha #7), so they must be
  cheap to read and queryable;
- the `artifacts` JSON keeps the full per-artifact record (kind, path, size,
  checksum) so `backup.validate` can re-verify every checksum without a schema
  change per artifact kind.

`frappe_version` is the source bench's Frappe major at backup time; the restore
compatibility check blocks restoring a newer-major backup onto an older-major
target (gotcha #7: never allow downgrades).
"""

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

# JSONB on Postgres, plain JSON on the SQLite test fallback (mirrors job.py).
ArtifactsJSON = JSON().with_variant(JSONB(), "postgresql")

# db = database-only dump; with-files = database + public + private files.
BACKUP_TYPES = ("db", "with-files")
# pending = row created before the job finished; success/failed set by the job.
BACKUP_STATUSES = ("pending", "success", "failed")

# Offsite storage lifecycle (session 2.2):
#  local     — artifacts exist only on the source server (default; no target).
#  uploading — an upload step is in flight.
#  offsite   — every artifact is in the S3 target with its sha256 re-verified.
#  failed    — the upload or a checksum re-verify failed (artifacts stay local).
STORAGE_STATES = ("local", "uploading", "offsite", "failed")


class Backup(Base):
    """One captured backup of a site (its artifacts + integrity metadata)."""

    __tablename__ = "backups"

    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(
        ForeignKey("sites.id", ondelete="CASCADE"), index=True
    )
    # Denormalised so the Backups table can group/filter by bench without a join
    # through site (mirrors installed_apps.bench_id).
    bench_id: Mapped[int] = mapped_column(
        ForeignKey("benches.id", ondelete="CASCADE"), index=True
    )

    # db | with-files (see BACKUP_TYPES).
    type: Mapped[str] = mapped_column(String(20), default="db")

    # Convenience artifact paths (absolute, on the server). NULL when the artifact
    # kind isn't part of this backup (e.g. files paths on a db-only backup).
    db_path: Mapped[str | None] = mapped_column(String(500))
    public_files_path: Mapped[str | None] = mapped_column(String(500))
    private_files_path: Mapped[str | None] = mapped_column(String(500))
    # The site_config_backup.json — source of the encryption_key for a restore.
    config_path: Mapped[str | None] = mapped_column(String(500))

    # Total size across all artifacts, in bytes (NULL until the job records it).
    size_bytes: Mapped[int | None] = mapped_column(Integer)
    # Per-artifact records: [{kind, path, size_bytes, checksum_sha256}, ...].
    artifacts: Mapped[list] = mapped_column(ArtifactsJSON, default=list)

    # pending | success | failed (see BACKUP_STATUSES).
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)

    # The source bench's Frappe major (e.g. "16") at backup time; the restore
    # compatibility check uses it to block downgrades (gotcha #7).
    frappe_version: Mapped[str | None] = mapped_column(String(20))

    # The job that produced this backup (SET NULL so history survives a job purge).
    taken_by_job_id: Mapped[int | None] = mapped_column(
        ForeignKey("command_jobs.id", ondelete="SET NULL"), index=True
    )
    # True once this backup has been restored into a site and verified.
    restore_tested: Mapped[bool] = mapped_column(Boolean, default=False)

    # --- Offsite storage (session 2.2) ------------------------------------- #
    # local | uploading | offsite | failed (see STORAGE_STATES). Defaults to
    # local: a backup with no configured target is simply on-server only.
    storage_state: Mapped[str] = mapped_column(
        String(20), default="local", index=True
    )
    # The StorageTarget this backup was uploaded to (SET NULL if the target is
    # later deleted so the offsite record survives).
    storage_target_id: Mapped[int | None] = mapped_column(
        ForeignKey("storage_targets.id", ondelete="SET NULL"), index=True
    )
    # kind -> object key in the target bucket, for the presigned-download
    # endpoint ({"database": "prefix/backup-42/…-database.sql.gz", ...}).
    object_keys: Mapped[dict] = mapped_column(ArtifactsJSON, default=dict)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    site: Mapped["Site"] = relationship()  # noqa: F821
