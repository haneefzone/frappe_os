"""Request/response models for the restic config-tier DR API (session 4.1).

The restic repo password is write-only: accepted on configure, Fernet-encrypted
immediately, and NEVER echoed back — the read model exposes only a
`password_set` boolean (CLAUDE.md rule 6). The S3 credentials are not part of
this surface at all; they stay on the reused 2.2 StorageTarget.
"""

from datetime import datetime

from pydantic import BaseModel, Field

from app.models.restic import ResticRepo


class ResticRepoOut(BaseModel):
    """One server's restic config-tier repo for the backup-evidence view — no
    secret material, plus the storage chip (target name) and evidence stamps."""

    # A stable kind so the Backups evidence table can chip config snapshots apart
    # from the 1.11 site backups ("config" vs "site").
    kind: str = "config"
    id: int
    server_id: int
    storage_target_id: int | None
    storage_target_name: str | None
    prefix: str
    password_set: bool
    initialized: bool
    last_backup_at: datetime | None
    last_check_at: datetime | None
    last_snapshot_id: str | None
    # --- 4.2 retention policy + integrity-check evidence -------------------- #
    keep_daily: int | None
    keep_weekly: int | None
    keep_monthly: int | None
    check_read_data_subset: str | None
    last_check_ok: bool | None
    last_check_message: str | None
    last_forget_at: datetime | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_model(
        cls, repo: ResticRepo, *, storage_target_name: str | None = None
    ) -> "ResticRepoOut":
        return cls(
            id=repo.id,
            server_id=repo.server_id,
            storage_target_id=repo.storage_target_id,
            storage_target_name=storage_target_name,
            prefix=repo.prefix,
            password_set=repo.password_set,
            initialized=repo.initialized,
            last_backup_at=repo.last_backup_at,
            last_check_at=repo.last_check_at,
            last_snapshot_id=repo.last_snapshot_id,
            keep_daily=repo.keep_daily,
            keep_weekly=repo.keep_weekly,
            keep_monthly=repo.keep_monthly,
            check_read_data_subset=repo.check_read_data_subset,
            last_check_ok=repo.last_check_ok,
            last_check_message=repo.last_check_message,
            last_forget_at=repo.last_forget_at,
            created_at=repo.created_at,
            updated_at=repo.updated_at,
        )


class ResticRepoConfigure(BaseModel):
    """Create/update a server's restic config-tier repo. `password` is write-only:
    a non-empty value (re-)encrypts it, omitting it on an existing repo leaves it
    unchanged. A repo needs both a storage target and a password before it runs.

    The 4.2 retention fields follow normal PUT-replace semantics like `prefix`:
    omitting a `keep_*` field (or sending `null`) clears that dimension.
    `forget --prune` itself still refuses to run while all three end up unset
    (see `restic.forget_keep_args`) — this schema only validates each *given*
    value is `>= 1`, not that at least one is set."""

    storage_target_id: int
    prefix: str = Field(default="", max_length=255)
    password: str | None = Field(default=None, min_length=1, max_length=255)
    keep_daily: int | None = Field(default=None, ge=1)
    keep_weekly: int | None = Field(default=None, ge=1)
    keep_monthly: int | None = Field(default=None, ge=1)
    check_read_data_subset: str | None = Field(default=None, max_length=20)
