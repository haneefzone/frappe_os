"""Request/response models for the platform self-backup + escrow API (6.3).

No secret material is ever exposed: the archive's checksums and the KDF salt are
safe to show (the salt is not secret), but the backup passphrase and master key
never appear here (rule 6). `sha256` is the encrypted archive's digest.
"""

from datetime import datetime

from pydantic import BaseModel

from app.models.platform_backup import PlatformBackup


class PlatformBackupOut(BaseModel):
    """One platform self-backup for the Settings panel."""

    id: int
    status: str
    size_bytes: int | None
    sha256: str | None
    plaintext_sha256: str | None
    encrypted: bool
    kdf_salt: str | None
    storage_target_id: int | None
    storage_target_name: str | None
    object_key: str | None
    verify_status: str
    verified_at: datetime | None
    restore_tested_at: datetime | None
    error: str | None
    taken_by_job_id: int | None
    verified_by_job_id: int | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_model(
        cls, row: PlatformBackup, *, storage_target_name: str | None = None
    ) -> "PlatformBackupOut":
        return cls(
            id=row.id,
            status=row.status,
            size_bytes=row.size_bytes,
            sha256=row.sha256,
            plaintext_sha256=row.plaintext_sha256,
            encrypted=row.encrypted,
            kdf_salt=row.kdf_salt,
            storage_target_id=row.storage_target_id,
            storage_target_name=storage_target_name,
            object_key=row.object_key,
            verify_status=row.verify_status,
            verified_at=row.verified_at,
            restore_tested_at=row.restore_tested_at,
            error=row.error,
            taken_by_job_id=row.taken_by_job_id,
            verified_by_job_id=row.verified_by_job_id,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )


class RunSelfBackupRequest(BaseModel):
    """Run a self-backup now. `storage_target_id` selects the S3 target; omitted
    = auto-select the single enabled target (422 if zero or several exist)."""

    storage_target_id: int | None = None
    priority: str = "default"


class EscrowStatusOut(BaseModel):
    """Master-key escrow acknowledgement state driving the persistent banner."""

    confirmed: bool
    confirmed_at: datetime | None
    confirmed_by: int | None
    confirmed_by_email: str | None


class EscrowConfirmRequest(BaseModel):
    """Admin confirms the master key has been escrowed per the runbook. The
    typed acknowledgement text must match so the tick is deliberate."""

    acknowledge: bool = True
