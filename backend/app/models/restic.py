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
    # snapshot; last_check_at set after every `restic check` (pass or fail).
    last_backup_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Short id of the most recent config snapshot (evidence / acceptance proof).
    last_snapshot_id: Mapped[str | None] = mapped_column(String(64))

    # --- Session 4.2: retention + integrity-check evidence ------------------- #
    # Result of the most recent `restic check` (True=pass, False=fail, NULL=never
    # run). A False here is what raises the breach alert; the §6 evidence view
    # renders it as the repo's integrity state.
    last_check_ok: Mapped[bool | None] = mapped_column()
    # A short, credential-free one-line summary of the last check outcome ("no
    # errors were found" / "repository contains errors"). Never carries a repo
    # URI, password, or S3 key (rule 6) — only restic's own verdict line.
    last_check_summary: Mapped[str | None] = mapped_column(String(500))
    # When the last `restic forget --prune` retention sweep ran (evidence).
    last_forget_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Retention policy for `restic forget --prune` (per repo, configurable). Each
    # maps to the matching restic `--keep-*` flag; NULL means that dimension is
    # not applied. The forget action REFUSES to prune when every dimension is
    # NULL (an empty policy would delete every snapshot — a destructive footgun),
    # so a repo with no policy set simply never prunes.
    retention_keep_last: Mapped[int | None] = mapped_column()
    retention_keep_daily: Mapped[int | None] = mapped_column()
    retention_keep_weekly: Mapped[int | None] = mapped_column()
    retention_keep_monthly: Mapped[int | None] = mapped_column()

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

    @property
    def retention_configured(self) -> bool:
        """True when at least one retention dimension is set. The forget action
        refuses to prune when this is False (an empty policy would delete every
        snapshot), so this gates whether a retention sweep can run at all."""
        return any(
            v is not None
            for v in (
                self.retention_keep_last,
                self.retention_keep_daily,
                self.retention_keep_weekly,
                self.retention_keep_monthly,
            )
        )

    @property
    def retention_summary(self) -> str | None:
        """A compact, human-readable policy string for the §6 evidence view
        ("last 3, 7 daily, 4 weekly"), or None when no policy is set."""
        parts: list[str] = []
        if self.retention_keep_last is not None:
            parts.append(f"last {self.retention_keep_last}")
        if self.retention_keep_daily is not None:
            parts.append(f"{self.retention_keep_daily} daily")
        if self.retention_keep_weekly is not None:
            parts.append(f"{self.retention_keep_weekly} weekly")
        if self.retention_keep_monthly is not None:
            parts.append(f"{self.retention_keep_monthly} monthly")
        return ", ".join(parts) if parts else None
