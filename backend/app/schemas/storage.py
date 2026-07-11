"""Request/response models for the S3-compatible storage-targets API (2.2).

The access/secret keys are write-only: accepted on create/update, Fernet-encrypted
immediately, and NEVER echoed back — the read model exposes only a `keys_set`
boolean (CLAUDE.md rule 6).
"""

from datetime import datetime

from pydantic import BaseModel, Field

from app.models.storage import STORAGE_PROVIDERS, StorageTarget


class StorageTargetOut(BaseModel):
    """One storage target for the Settings list (no secret material)."""

    id: int
    name: str
    provider: str
    endpoint_url: str | None
    region: str | None
    bucket: str
    path_prefix: str | None
    use_ssl: bool
    enabled: bool
    keys_set: bool
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_model(cls, target: StorageTarget) -> "StorageTargetOut":
        return cls(
            id=target.id,
            name=target.name,
            provider=target.provider,
            endpoint_url=target.endpoint_url,
            region=target.region,
            bucket=target.bucket,
            path_prefix=target.path_prefix,
            use_ssl=target.use_ssl,
            enabled=target.enabled,
            keys_set=target.keys_set,
            created_at=target.created_at,
            updated_at=target.updated_at,
        )


class StorageTargetCreate(BaseModel):
    """Register a storage target. Both keys are required to create a usable one."""

    name: str = Field(min_length=1, max_length=120)
    provider: str = "aws"
    endpoint_url: str | None = Field(default=None, max_length=255)
    region: str | None = Field(default=None, max_length=64)
    bucket: str = Field(min_length=1, max_length=255)
    path_prefix: str | None = Field(default=None, max_length=255)
    access_key: str = Field(min_length=1, max_length=255)
    secret_key: str = Field(min_length=1, max_length=255)
    use_ssl: bool = True
    enabled: bool = True

    def provider_normalized(self) -> str:
        return self.provider if self.provider in STORAGE_PROVIDERS else "other"


class StorageTargetUpdate(BaseModel):
    """Patch a storage target. Keys are optional; a non-empty value re-encrypts,
    an empty string clears them, and omitting them leaves them unchanged."""

    name: str | None = Field(default=None, min_length=1, max_length=120)
    provider: str | None = None
    endpoint_url: str | None = Field(default=None, max_length=255)
    region: str | None = Field(default=None, max_length=64)
    bucket: str | None = Field(default=None, min_length=1, max_length=255)
    path_prefix: str | None = Field(default=None, max_length=255)
    access_key: str | None = Field(default=None, max_length=255)
    secret_key: str | None = Field(default=None, max_length=255)
    use_ssl: bool | None = None
    enabled: bool | None = None


class TestConnectionOut(BaseModel):
    """Result of a storage target's test-connection probe."""

    reachable: bool
    writable: bool
    latency_ms: int | None
    error: str | None = None
