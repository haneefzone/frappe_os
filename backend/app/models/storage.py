"""S3-compatible offsite storage targets (session 2.2).

A `StorageTarget` is one bucket on an S3-compatible service (AWS S3, MinIO,
Backblaze B2, Wasabi, …) the platform pushes backup artifacts to for offsite
retention. The access/secret keys are the only secrets on the row and live ONLY
as Fernet tokens in the `*_enc` columns (CLAUDE.md rule 6): they are decrypted
in memory to build a boto3 client and are never stored in the clear, logged, or
returned to the browser — the API exposes only a "keys are set" boolean.

`endpoint_url` is left NULL for real AWS (boto3 derives it from `region`); a
self-hosted MinIO/B2 target sets it explicitly (e.g. `https://minio.local:9000`).
`path_prefix` is an optional key prefix every uploaded object is placed under so
one bucket can hold several platforms/tenants without collisions.
"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

# The S3-compatible providers we render a friendly label for; any value is
# accepted (it only drives the UI badge), but these are the validated set.
STORAGE_PROVIDERS = ("aws", "minio", "backblaze", "wasabi", "other")


class StorageTarget(Base):
    """One S3-compatible bucket the platform can upload backup artifacts to."""

    __tablename__ = "storage_targets"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    # aws | minio | backblaze | wasabi | other (see STORAGE_PROVIDERS). Cosmetic.
    provider: Mapped[str] = mapped_column(String(20), default="aws")
    # Explicit endpoint for non-AWS (MinIO/B2/Wasabi). NULL => boto3 derives the
    # AWS endpoint from `region`.
    endpoint_url: Mapped[str | None] = mapped_column(String(255))
    region: Mapped[str | None] = mapped_column(String(64))
    bucket: Mapped[str] = mapped_column(String(255))
    # Optional key prefix every uploaded object is placed under (no leading/
    # trailing slash needed; the uploader normalises it).
    path_prefix: Mapped[str | None] = mapped_column(String(255))

    # The S3 credentials, Fernet-encrypted at rest (rule 6). NULL until set; the
    # plaintext never touches the DB, logs, or API responses.
    access_key_enc: Mapped[str | None] = mapped_column(Text)
    secret_key_enc: Mapped[str | None] = mapped_column(Text)

    # Whether TLS is used for the endpoint (MinIO in dev is often plain http).
    use_ssl: Mapped[bool] = mapped_column(Boolean, default=True)
    # Disabled targets are never auto-selected for an upload.
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    @property
    def keys_set(self) -> bool:
        """True when both credentials are configured (never exposes the values)."""
        return bool(self.access_key_enc and self.secret_key_enc)
