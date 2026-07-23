"""Platform self-backup records (session 6.3).

The platform protects every managed site; a `PlatformBackup` is how the platform
protects *itself*. One row is one run of the `platform.self_backup` job: a
`pg_dump` (custom format) of the platform Postgres plus the platform config set
(`.env` **with `FDM_SECRET_KEY` excluded**, `deploy/` units, nginx conf), rolled
into a single archive, sha256'd, **encrypted at rest**, and uploaded through the
2.2 S3 storage service with the same verify-after-upload path site backups use.

**Key-handling decision (CLAUDE.md rule 6, made loud on purpose).** The archive
must never carry the master key, and it must not be encrypted *solely* with a
key that only lives inside that same archive — otherwise the backup could only
be opened by first restoring the thing it is meant to recover (a circular
dependency that makes the backup worthless in the one scenario it exists for:
`FDM_SECRET_KEY` is lost). So the archive is encrypted with a **separate,
operator-held backup passphrase** (`FDM_BACKUP_PASSPHRASE`, env only — never the
DB, never in the archive), from which a Fernet key is derived with PBKDF2 over a
per-backup random `kdf_salt` (the salt is not secret and is stored here so the
operator can decrypt with only the passphrase). The operator escrows the master
key AND the backup passphrase separately — see `docs/master-key-escrow.md`.

`sha256` is the digest of the **encrypted** archive (the object uploaded and
re-verified end to end); `plaintext_sha256` is the digest of the archive before
encryption (proof the *restored* plaintext is intact once decrypted).
`verify_status` reflects the `platform.self_backup_verify` job — the only real
proof the backup works: download → checksum → `pg_restore --list` structure
read. `restore_tested_at` is stamped when the out-of-band escrow restore drill
(runbook) has been executed against this backup.
"""

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

# Job lifecycle of the row: pending (created before the job finished),
# success/failed set by the job (mirrors Backup.status).
PLATFORM_BACKUP_STATUSES = ("pending", "success", "failed")

# Verify lifecycle (platform.self_backup_verify):
#  unverified — uploaded but the round-trip verify has not run yet.
#  verified   — downloaded, checksum matched, `pg_restore --list` was readable.
#  failed     — the download/checksum/structure check failed.
PLATFORM_VERIFY_STATUSES = ("unverified", "verified", "failed")


class PlatformBackup(Base):
    """One captured self-backup of the platform (its encrypted archive + metadata)."""

    __tablename__ = "platform_backups"

    id: Mapped[int] = mapped_column(primary_key=True)

    # pending | success | failed (see PLATFORM_BACKUP_STATUSES).
    status: Mapped[str] = mapped_column(
        String(20), default="pending", index=True
    )

    # Size of the uploaded (encrypted) archive in bytes; NULL until recorded.
    size_bytes: Mapped[int | None] = mapped_column(Integer)
    # sha256 of the ENCRYPTED archive — the object uploaded and re-verified.
    sha256: Mapped[str | None] = mapped_column(String(64))
    # sha256 of the archive BEFORE encryption — proves the decrypted plaintext is
    # intact once the operator supplies the backup passphrase on restore.
    plaintext_sha256: Mapped[str | None] = mapped_column(String(64))

    # True: the archive is encrypted at rest (always, for a real backup). The
    # column is explicit so the UI can render the loud "encrypted — you need the
    # backup passphrase to restore" dependency rather than inferring it.
    encrypted: Mapped[bool] = mapped_column(Boolean, default=True)
    # PBKDF2 salt (hex) the backup-passphrase-derived Fernet key was built over.
    # Not secret; required (with the passphrase) to decrypt on restore.
    kdf_salt: Mapped[str | None] = mapped_column(String(64))

    # The StorageTarget the archive was uploaded to (SET NULL if the target is
    # later deleted so the record survives) + the object key in its bucket.
    storage_target_id: Mapped[int | None] = mapped_column(
        ForeignKey("storage_targets.id", ondelete="SET NULL"), index=True
    )
    object_key: Mapped[str | None] = mapped_column(String(500))

    # unverified | verified | failed (see PLATFORM_VERIFY_STATUSES).
    verify_status: Mapped[str] = mapped_column(
        String(20), default="unverified", index=True
    )
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Stamped when the out-of-band escrow restore drill (runbook) has been run
    # against this backup — the strongest proof it is actually recoverable.
    restore_tested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Browser-safe failure message (never any secret material). NULL on success.
    error: Mapped[str | None] = mapped_column(Text)

    # The jobs that produced / verified this backup (SET NULL so history survives
    # a job purge).
    taken_by_job_id: Mapped[int | None] = mapped_column(
        ForeignKey("command_jobs.id", ondelete="SET NULL"), index=True
    )
    verified_by_job_id: Mapped[int | None] = mapped_column(
        ForeignKey("command_jobs.id", ondelete="SET NULL"), index=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
