"""S3-compatible offsite storage: boto3 client, connection test, artifact
upload with sha256 re-verification, and presigned downloads (session 2.2).

Design (CLAUDE.md golden rule 6):
- The access/secret keys live only as Fernet tokens on the `StorageTarget` row;
  they are decrypted here, in memory, only to build a boto3 client, and never
  logged, persisted in the clear, or returned to the browser.
- boto3 is imported lazily so the package is only needed when the platform
  actually talks to S3; unit tests inject a fake client via `client_factory`.
- The uploader streams each artifact's bytes from the source server (an async
  `read_chunks` provider backed by the job's SSH connection), recomputes the
  sha256 while spooling to a bounded temp file, and refuses to upload (or marks
  the artifact failed) if the recomputed digest doesn't match the one the 1.11
  backup recorded — integrity is verified end to end, offsite.

Presigned URLs are short-lived GET signatures: the signature is an HMAC derived
from the secret key, so the URL is safe to hand to the browser (the key itself
never travels), and it expires quickly.
"""

from __future__ import annotations

import hashlib
import posixpath
import tempfile
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass

from app.core.security import SecretsService, get_secrets_service
from app.models.storage import StorageTarget

# Default presigned-download TTL (seconds). Short-lived per the spec.
PRESIGN_TTL_SECONDS = 300
# Spool artifacts up to this many bytes in memory before the temp file spills to
# disk — bounds worker memory while streaming a large backup through boto3.
_SPOOL_MAX_BYTES = 32 * 1024 * 1024


class StorageError(RuntimeError):
    """A storage target is misconfigured or an S3 operation failed. Carries a
    browser-safe message (never any credential material)."""


@dataclass
class S3Config:
    """The resolved connection settings for one StorageTarget — plaintext keys
    held only in memory for the lifetime of a client build."""

    endpoint_url: str | None
    region: str | None
    bucket: str
    path_prefix: str
    access_key: str
    secret_key: str
    use_ssl: bool

    @classmethod
    def from_target(
        cls, target: StorageTarget, secrets: SecretsService | None = None
    ) -> S3Config:
        secrets = secrets or get_secrets_service()
        if not (target.access_key_enc and target.secret_key_enc):
            raise StorageError(
                f"Storage target {target.name!r} has no credentials configured."
            )
        return cls(
            endpoint_url=target.endpoint_url or None,
            region=target.region or None,
            bucket=target.bucket,
            path_prefix=(target.path_prefix or "").strip("/"),
            access_key=secrets.decrypt(target.access_key_enc),
            secret_key=secrets.decrypt(target.secret_key_enc),
            use_ssl=bool(target.use_ssl),
        )


# A client factory type so tests can inject a fake S3 client (no boto3/network).
ClientFactory = Callable[[S3Config], object]


def _boto3_client(cfg: S3Config):
    """Build a real boto3 S3 client from a resolved config. Imported lazily so
    boto3 is only required when the platform actually uploads/downloads."""
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=cfg.endpoint_url,
        region_name=cfg.region,
        aws_access_key_id=cfg.access_key,
        aws_secret_access_key=cfg.secret_key,
        use_ssl=cfg.use_ssl,
        config=Config(
            signature_version="s3v4",
            retries={"max_attempts": 3, "mode": "standard"},
            # Path-style addressing works for MinIO and any bucket name with dots;
            # AWS accepts it too. Avoids DNS/vhost-style surprises on self-hosted.
            s3={"addressing_style": "path"},
        ),
    )


def build_client(cfg: S3Config, client_factory: ClientFactory | None = None):
    return (client_factory or _boto3_client)(cfg)


def object_key(cfg: S3Config, backup_id: int, artifact_path: str) -> str:
    """Deterministic object key for one artifact: `<prefix>/backup-<id>/<file>`.
    The filename is the artifact's basename (already a safe backup filename)."""
    name = posixpath.basename(artifact_path)
    parts = [p for p in (cfg.path_prefix, f"backup-{backup_id}", name) if p]
    return "/".join(parts)


# --------------------------------------------------------------------------- #
# Connection test
# --------------------------------------------------------------------------- #


@dataclass
class ConnectionResult:
    """Structured result of `test_connection` for the Settings UI."""

    reachable: bool
    writable: bool
    latency_ms: int | None
    error: str | None = None


def test_connection(
    target: StorageTarget,
    *,
    secrets: SecretsService | None = None,
    client_factory: ClientFactory | None = None,
    clock: Callable[[], float] | None = None,
) -> ConnectionResult:
    """Probe a storage target: HEAD the bucket (reachable) and write+delete a
    tiny marker object (writable), timing the round trip. Never raises — every
    failure is captured in the result so the UI can render a clean red state."""
    import time as _time

    now = clock or _time.monotonic
    try:
        cfg = S3Config.from_target(target, secrets)
    except StorageError as exc:
        return ConnectionResult(False, False, None, str(exc))

    client = build_client(cfg, client_factory)
    start = now()
    try:
        client.head_bucket(Bucket=cfg.bucket)
    except Exception as exc:  # noqa: BLE001 — surface a clean message, never a trace.
        return ConnectionResult(
            False, False, None, _safe_error("Bucket not reachable", exc)
        )
    reachable_ms = int((now() - start) * 1000)

    writable = False
    error: str | None = None
    marker = object_key(cfg, 0, ".fdm-connection-test")
    try:
        client.put_object(Bucket=cfg.bucket, Key=marker, Body=b"fdm-ok")
        writable = True
        try:
            client.delete_object(Bucket=cfg.bucket, Key=marker)
        except Exception:  # noqa: BLE001 — a stray marker is harmless; still writable.
            pass
    except Exception as exc:  # noqa: BLE001
        error = _safe_error("Bucket not writable", exc)

    return ConnectionResult(True, writable, reachable_ms, error)


def _safe_error(prefix: str, exc: Exception) -> str:
    """A browser-safe one-liner for an S3 failure — the boto3 error code/message,
    never any credential or signing material."""
    code = getattr(getattr(exc, "response", None), "get", lambda *_: None)("Error")
    if isinstance(code, dict):
        detail = code.get("Code") or code.get("Message") or exc.__class__.__name__
    else:
        detail = exc.__class__.__name__
    return f"{prefix}: {detail}"


# --------------------------------------------------------------------------- #
# Upload one artifact (stream from source -> spool + re-verify sha256 -> S3)
# --------------------------------------------------------------------------- #


@dataclass
class UploadResult:
    """Outcome of uploading one artifact."""

    kind: str
    key: str
    size_bytes: int
    checksum_ok: bool
    expected_sha256: str
    actual_sha256: str
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.checksum_ok


async def upload_artifact(
    client,
    cfg: S3Config,
    *,
    kind: str,
    artifact_path: str,
    expected_sha256: str,
    backup_id: int,
    read_chunks: Callable[[str], AsyncIterator[bytes]],
) -> UploadResult:
    """Stream one artifact from the source server, recompute its sha256 while
    spooling to a bounded temp file, and — only if the digest matches what the
    1.11 backup recorded — put it to the S3 target. A mismatch is NOT uploaded
    (a corrupt/altered artifact must never masquerade as a verified offsite
    copy); the result carries checksum_ok=False so the caller marks it failed."""
    key = object_key(cfg, backup_id, artifact_path)
    digest = hashlib.sha256()
    size = 0
    spool = tempfile.SpooledTemporaryFile(max_size=_SPOOL_MAX_BYTES)
    try:
        async for chunk in read_chunks(artifact_path):
            digest.update(chunk)
            size += len(chunk)
            spool.write(chunk)
        actual = digest.hexdigest()
        if actual != expected_sha256:
            return UploadResult(
                kind=kind,
                key=key,
                size_bytes=size,
                checksum_ok=False,
                expected_sha256=expected_sha256,
                actual_sha256=actual,
                error="checksum mismatch — offsite upload refused",
            )
        spool.seek(0)
        try:
            client.upload_fileobj(spool, cfg.bucket, key)
        except Exception as exc:  # noqa: BLE001
            return UploadResult(
                kind=kind,
                key=key,
                size_bytes=size,
                checksum_ok=True,
                expected_sha256=expected_sha256,
                actual_sha256=actual,
                error=_safe_error("Upload failed", exc),
            )
        return UploadResult(
            kind=kind,
            key=key,
            size_bytes=size,
            checksum_ok=True,
            expected_sha256=expected_sha256,
            actual_sha256=actual,
        )
    finally:
        spool.close()


# --------------------------------------------------------------------------- #
# Streaming download (S3 -> caller), for the cross-server move (session 2.6)
# --------------------------------------------------------------------------- #


async def download_object(client, cfg: S3Config, key: str, *, chunk_size: int = 65536):
    """Yield the bytes of one object from the target bucket in chunks — the read
    side of a cross-server backup move (2.6): the artifact is streamed straight
    from S3 into the destination server's file over SSH, never buffered whole.

    boto3's StreamingBody is synchronous; reading it inside this async generator
    briefly blocks the loop, which is fine in the single-job-per-worker model
    (mirrors how `upload_artifact` calls `client.upload_fileobj` synchronously)."""
    obj = client.get_object(Bucket=cfg.bucket, Key=key)
    body = obj["Body"]
    try:
        while True:
            chunk = body.read(chunk_size)
            if not chunk:
                break
            yield chunk
    finally:
        body.close()


# --------------------------------------------------------------------------- #
# Presigned download
# --------------------------------------------------------------------------- #


def presign_get(
    target: StorageTarget,
    key: str,
    *,
    ttl_seconds: int = PRESIGN_TTL_SECONDS,
    filename: str | None = None,
    secrets: SecretsService | None = None,
    client_factory: ClientFactory | None = None,
) -> str:
    """A short-lived presigned GET URL for one object. The URL carries only an
    HMAC signature — never the secret key — so it is safe to return to the
    browser. `filename` sets a download Content-Disposition on the response."""
    cfg = S3Config.from_target(target, secrets)
    client = build_client(cfg, client_factory)
    params: dict = {"Bucket": cfg.bucket, "Key": key}
    if filename:
        params["ResponseContentDisposition"] = f'attachment; filename="{filename}"'
    try:
        return client.generate_presigned_url(
            "get_object", Params=params, ExpiresIn=ttl_seconds
        )
    except Exception as exc:  # noqa: BLE001
        raise StorageError(_safe_error("Could not sign download URL", exc)) from exc
