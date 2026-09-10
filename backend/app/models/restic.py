"""Per-server restic OS/config backup repositories (session 4.1 — Full-system DR).

A `ResticRepo` is one restic repository that holds a managed server's *system
configuration* tier (nginx / supervisor / redis / mariadb configs plus the
`dpkg --get-selections` package manifest) — the bare-metal-DR complement to the
1.11 site backups. Each repo lives inside an existing 2.2 `StorageTarget`
bucket (we reuse its S3 endpoint + credentials — no second S3 config surface):
the repo occupies a per-server key `prefix` under that bucket, and restic
deduplicates + encrypts every snapshot client-side.

Secrets (CLAUDE.md rule 6): the ONLY secret on the row is the restic repo
password, held solely as a Fernet token in `password_enc` (master key from env
`FDM_SECRET_KEY`). It is decrypted in memory only to build the restic env file
on the target and is never stored in the clear, logged, put on an argv, or
returned to the browser — the API exposes only a `password_set` boolean. The S3
access/secret keys are NOT duplicated here; they stay on the `StorageTarget`.

`initialized` flips true once `restic init` has created the repo. `last_backup_at`
/ `last_check_at` and `last_snapshot_id` are evidence timestamps the §6 backup-
evidence view renders (4.2 adds the weekly snapshot + `restic check` schedule).
"""

from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class ResticRepo(Base):
    """One restic config-tier repository for a managed server (→ an S3 target)."""

    __tablename__ = "restic_repos"

    id: Mapped[int] = mapped_column(primary_key=True)
    # One repo per server (a server's config tier is a single restic repo).
    server_id: Mapped[int] = mapped_column(
        ForeignKey("servers.id", ondelete="CASCADE"), unique=True, index=True
    )
    # The 2.2 StorageTarget whose bucket + S3 credentials this repo reuses. SET
    # NULL if the target is later deleted so the repo record (and its evidence)
    # survives; a repo with no target can't run until one is reattached.
    storage_target_id: Mapped[int | None] = mapped_column(
        ForeignKey("storage_targets.id", ondelete="SET NULL"), index=True
    )
    # Key prefix inside the target bucket that holds this server's restic repo
    # (e.g. "restic/server-3"). Lets one bucket hold many servers' repos without
    # collisions. Normalised (no leading/trailing slash) before use.
    prefix: Mapped[str] = mapped_column(String(255))

    # The restic repository password, Fernet-encrypted at rest (rule 6). NULL
    # until set; the plaintext never touches the DB, logs, argv, or API responses.
    password_enc: Mapped[str | None] = mapped_column(Text)

    # True once `restic init` has created the repository in the bucket.
    initialized: Mapped[bool] = mapped_column(default=False)

    # Evidence timestamps (§6). last_backup_at set after a successful config
    # snapshot; last_check_at set by the 4.2 `restic check` integrity job.
    last_backup_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Short id of the most recent config snapshot (evidence / acceptance proof).
    last_snapshot_id: Mapped[str | None] = mapped_column(String(64))

    # --- 4.2 retention policy + integrity check ---------------------------- #
    # restic `forget --prune` retention (keep the last N daily/weekly/monthly
    # snapshots). NULL means that dimension is not applied; the forget action
    # REFUSES to run when all three are NULL (an unpolicied `forget` would remove
    # every snapshot — never let a destructive prune run with no keep policy).
    keep_daily: Mapped[int | None] = mapped_column()
    keep_weekly: Mapped[int | None] = mapped_column()
    keep_monthly: Mapped[int | None] = mapped_column()

    # `restic check --read-data-subset` selector for large repos (e.g. "5%",
    # "1/10", "50G"). NULL = structural check only (metadata, no data re-read).
    # Validated to restic's subset grammar before it reaches the argv.
    check_read_data_subset: Mapped[str | None] = mapped_column(String(20))

    # Result of the most recent `restic check` (evidence). last_check_ok is None
    # until the first check runs, then True/False; last_check_message is a short,
    # secret-free summary ("no errors were found" / the failure line).
    last_check_ok: Mapped[bool | None] = mapped_column()
    last_check_message: Mapped[str | None] = mapped_column(String(500))
    # When the last `restic forget --prune` completed (retention evidence).
    last_forget_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    server: Mapped["Server"] = relationship()  # noqa: F821
    storage_target: Mapped["StorageTarget | None"] = relationship()  # noqa: F821

    @property
    def password_set(self) -> bool:
        """True when a repo password is configured (never exposes the value)."""
        return bool(self.password_enc)
