"""S3-compatible offsite storage (session 2.2).

Covers the acceptance gates:
- object keys / config resolution and the Fernet key round-trip;
- test_connection reachable/writable/latency + clean red states;
- upload_artifact re-verifies sha256 (matches -> uploaded; mismatch -> refused;
  S3 error -> captured), never invalidating the local backup;
- the backup engine, given a storage target, uploads every artifact and flips
  storage_state local -> offsite (and -> failed on a tampered artifact), with the
  S3 secret key never appearing in any streamed log line;
- the storage-targets API encrypts keys, never echoes them, is Admin-only, and
  the presigned-download endpoint is Developer+ and refuses a local-only backup.

All S3 access goes through an injected fake client — no boto3/network.
"""

from __future__ import annotations

import asyncio
import hashlib

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core import storage as st
from app.core.jobs import InMemoryJobBackend, JobRunner
from app.core.security import get_secrets_service
from app.db import Base
from app.models import CommandJob, LogEntry, Server
from app.models.backup import Backup
from app.models.bench import Bench
from app.models.site import Site
from app.models.storage import StorageTarget
from tests.conftest import csrf_headers, login
from tests.test_backups import (
    BENCH_PATH,
    BackupExecutor,
    _setup,
    fake_factory,
)

# --------------------------------------------------------------------------- #
# A fake S3 client + factory injected in place of boto3.
# --------------------------------------------------------------------------- #


class FakeS3Client:
    """Records calls; head/put/delete configurable to raise for the red paths."""

    def __init__(self, *, head_ok=True, put_ok=True) -> None:
        self._head_ok = head_ok
        self._put_ok = put_ok
        self.uploaded: dict[str, bytes] = {}
        self.put_objects: dict[str, bytes] = {}
        self.deleted: list[str] = []
        self.presigned: list[dict] = []

    def head_bucket(self, Bucket):  # noqa: N803 — boto3 kwarg name
        if not self._head_ok:
            raise RuntimeError("NoSuchBucket")

    def put_object(self, Bucket, Key, Body):  # noqa: N803
        if not self._put_ok:
            raise RuntimeError("AccessDenied")
        self.put_objects[Key] = Body

    def delete_object(self, Bucket, Key):  # noqa: N803
        self.deleted.append(Key)

    def upload_fileobj(self, fileobj, Bucket, Key):  # noqa: N803
        if not self._put_ok:
            raise RuntimeError("AccessDenied")
        self.uploaded[Key] = fileobj.read()

    def generate_presigned_url(self, op, Params, ExpiresIn):  # noqa: N803
        self.presigned.append({"op": op, "params": Params, "ttl": ExpiresIn})
        return f"https://s3.example/{Params['Key']}?sig=abc&X-Expires={ExpiresIn}"


def _target(db, *, name="offsite", enabled=True, keys=True) -> StorageTarget:
    secrets = get_secrets_service()
    t = StorageTarget(
        name=name,
        provider="minio",
        endpoint_url="https://minio.local:9000",
        region="us-east-1",
        bucket="fdm-backups",
        path_prefix="tenant-a",
        use_ssl=True,
        enabled=enabled,
        access_key_enc=secrets.encrypt("AKIAFAKE") if keys else None,
        secret_key_enc=secrets.encrypt("s3-secret-xyz") if keys else None,
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #


def test_object_key_uses_prefix_backup_and_basename(db_session):
    t = _target(db_session)
    cfg = st.S3Config.from_target(t)
    key = st.object_key(cfg, 42, "/a/b/20260710-database.sql.gz")
    assert key == "tenant-a/backup-42/20260710-database.sql.gz"


def test_s3config_decrypts_keys_and_requires_them(db_session):
    t = _target(db_session)
    cfg = st.S3Config.from_target(t)
    assert cfg.access_key == "AKIAFAKE"
    assert cfg.secret_key == "s3-secret-xyz"
    assert cfg.path_prefix == "tenant-a"

    unkeyed = _target(db_session, name="nokeys", keys=False)
    with pytest.raises(st.StorageError):
        st.S3Config.from_target(unkeyed)


def test_test_connection_reachable_and_writable(db_session):
    t = _target(db_session)
    fake = FakeS3Client()
    clock = iter([1.0, 1.05]).__next__
    result = st.test_connection(t, client_factory=lambda cfg: fake, clock=clock)
    assert result.reachable and result.writable
    assert result.latency_ms == 50 and result.error is None
    # The write probe cleaned up after itself.
    assert fake.deleted


def test_test_connection_not_reachable(db_session):
    t = _target(db_session)
    fake = FakeS3Client(head_ok=False)
    result = st.test_connection(t, client_factory=lambda cfg: fake)
    assert not result.reachable and not result.writable and result.error


def test_test_connection_reachable_not_writable(db_session):
    t = _target(db_session)
    fake = FakeS3Client(put_ok=False)
    result = st.test_connection(t, client_factory=lambda cfg: fake)
    assert result.reachable and not result.writable
    assert "not writable" in result.error.lower()


def test_test_connection_missing_keys(db_session):
    t = _target(db_session, keys=False)
    result = st.test_connection(t, client_factory=lambda cfg: FakeS3Client())
    assert not result.reachable and result.error


def test_upload_artifact_reverifies_and_uploads(db_session):
    t = _target(db_session)
    cfg = st.S3Config.from_target(t)
    fake = FakeS3Client()
    content = b"backup-bytes-123"
    digest = hashlib.sha256(content).hexdigest()

    async def read_chunks(path):
        yield content[:8]
        yield content[8:]

    res = asyncio.run(st.upload_artifact(
        fake, cfg, kind="database", artifact_path="/x/db.sql.gz",
        expected_sha256=digest, backup_id=7, read_chunks=read_chunks,
    ))
    assert res.ok and res.checksum_ok
    assert res.key == "tenant-a/backup-7/db.sql.gz"
    assert fake.uploaded[res.key] == content
    assert res.size_bytes == len(content)


def test_upload_artifact_refuses_on_checksum_mismatch(db_session):
    t = _target(db_session)
    cfg = st.S3Config.from_target(t)
    fake = FakeS3Client()

    async def read_chunks(path):
        yield b"tampered"

    res = asyncio.run(st.upload_artifact(
        fake, cfg, kind="database", artifact_path="/x/db.sql.gz",
        expected_sha256="0" * 64, backup_id=7, read_chunks=read_chunks,
    ))
    assert not res.ok and not res.checksum_ok
    assert "mismatch" in res.error
    assert fake.uploaded == {}  # nothing uploaded


def test_upload_artifact_captures_s3_error(db_session):
    t = _target(db_session)
    cfg = st.S3Config.from_target(t)
    fake = FakeS3Client(put_ok=False)
    content = b"good-bytes"
    digest = hashlib.sha256(content).hexdigest()

    async def read_chunks(path):
        yield content

    res = asyncio.run(st.upload_artifact(
        fake, cfg, kind="database", artifact_path="/x/db.sql.gz",
        expected_sha256=digest, backup_id=7, read_chunks=read_chunks,
    ))
    assert not res.ok and res.checksum_ok  # bytes were fine; the PUT failed
    assert "Upload failed" in res.error


def test_presign_get_returns_signed_url(db_session):
    t = _target(db_session)
    fake = FakeS3Client()
    url = st.presign_get(
        t, "tenant-a/backup-7/db.sql.gz", ttl_seconds=120,
        filename="db.sql.gz", client_factory=lambda cfg: fake,
    )
    assert url.startswith("https://s3.example/")
    call = fake.presigned[0]
    assert call["ttl"] == 120
    assert "attachment" in call["params"]["ResponseContentDisposition"]


# --------------------------------------------------------------------------- #
# Backup engine -> offsite upload
# --------------------------------------------------------------------------- #

# Build an inspect output whose checksums are the REAL sha256 of the bytes the
# fake read_file returns, so the offsite re-verify passes end to end.
_ART_BYTES = {
    "database.sql.gz": b"DBDATA-offsite",
    "files.tar": b"PUBFILES-offsite",
    "private-files.tar": b"PRIVFILES-offsite",
    "site_config_backup.json": b'{"encryption_key": "k"}',
}
_BK = f"{BENCH_PATH}/sites/test1.localhost/private/backups"
_PFX = "20260710_090000-test1_localhost"


def _inspect_real() -> str:
    lines = []
    for suffix, content in _ART_BYTES.items():
        path = f"{_BK}/{_PFX}-{suffix}"
        lines.append(
            f"ART\t{suffix}\t{path}\t{len(content)}\t{hashlib.sha256(content).hexdigest()}"
        )
    return "\n".join(lines) + "\n"


class StorageBackupExecutor(BackupExecutor):
    """BackupExecutor + a read_file that serves each artifact's known bytes (or
    tampered bytes when `tamper` is set, to exercise the failed-upload path)."""

    def __init__(self, *, tamper=False, **kw):
        super().__init__(inspect=_inspect_real(), **kw)
        self._tamper = tamper

    async def read_file(self, path, *, chunk_size=65536):
        for suffix, content in _ART_BYTES.items():
            if path == f"{_BK}/{_PFX}-{suffix}":
                yield (b"XX" if self._tamper else content)
                return
        raise AssertionError(f"unexpected read_file {path}")


@pytest.fixture
def sf():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    yield factory
    engine.dispose()


def _run_backup_job(sf, *, server_id, params, executor):
    runner = JobRunner(
        sf, InMemoryJobBackend(), enqueue=lambda job: None, secrets=get_secrets_service()
    )
    with sf() as db:
        job = runner.create(
            db, action_name="site.backup", server_id=server_id, target_type="site",
            target_id=f"{BENCH_PATH}::test1.localhost", params=params,
            priority="default", created_by=None,
        )
        job_id = job.id
    runner.run_job(job_id, executor_factory=fake_factory(executor))
    return job_id


def _patch_boto(monkeypatch, fake):
    monkeypatch.setattr(st, "_boto3_client", lambda cfg: fake)


def test_backup_engine_uploads_offsite(sf, monkeypatch):
    fake = FakeS3Client()
    _patch_boto(monkeypatch, fake)
    with sf() as db:
        server_id, bench_id, site_id = _setup(db)
        target = _target(db, name="prod-s3")
        target_id = target.id
        row = Backup(site_id=site_id, bench_id=bench_id, type="with-files", status="pending")
        db.add(row)
        db.commit()
        backup_id = row.id

    job_id = _run_backup_job(
        sf, server_id=server_id,
        params={
            "site": "test1.localhost", "bench_path": BENCH_PATH, "with_files": "1",
            "backup_id": str(backup_id), "storage_target_id": str(target_id),
        },
        executor=StorageBackupExecutor(),
    )

    with sf() as db:
        assert db.get(CommandJob, job_id).status == "success"
        b = db.get(Backup, backup_id)
        assert b.status == "success"
        assert b.storage_state == "offsite"
        assert b.storage_target_id == target_id
        assert set(b.object_keys.keys()) == {"database", "public_files", "private_files", "config"}
    # Every artifact really landed in the (fake) bucket.
    assert len(fake.uploaded) == 4


def test_backup_engine_marks_failed_on_tampered_artifact(sf, monkeypatch):
    fake = FakeS3Client()
    _patch_boto(monkeypatch, fake)
    with sf() as db:
        server_id, bench_id, site_id = _setup(db)
        target = _target(db, name="prod-s3")
        target_id = target.id
        row = Backup(site_id=site_id, bench_id=bench_id, type="with-files", status="pending")
        db.add(row)
        db.commit()
        backup_id = row.id

    job_id = _run_backup_job(
        sf, server_id=server_id,
        params={
            "site": "test1.localhost", "bench_path": BENCH_PATH, "with_files": "1",
            "backup_id": str(backup_id), "storage_target_id": str(target_id),
        },
        executor=StorageBackupExecutor(tamper=True),
    )

    with sf() as db:
        # The upload step failed the job, but the LOCAL backup stayed valid.
        assert db.get(CommandJob, job_id).status == "failure"
        b = db.get(Backup, backup_id)
        assert b.status == "success"
        assert b.storage_state == "failed"
    assert fake.uploaded == {}  # a mismatched artifact is never uploaded


def test_backup_engine_no_target_stays_local(sf, monkeypatch):
    fake = FakeS3Client()
    _patch_boto(monkeypatch, fake)
    with sf() as db:
        server_id, bench_id, site_id = _setup(db)
        row = Backup(site_id=site_id, bench_id=bench_id, type="with-files", status="pending")
        db.add(row)
        db.commit()
        backup_id = row.id

    _run_backup_job(
        sf, server_id=server_id,
        params={
            "site": "test1.localhost", "bench_path": BENCH_PATH, "with_files": "1",
            "backup_id": str(backup_id),
        },
        executor=StorageBackupExecutor(),
    )
    with sf() as db:
        assert db.get(Backup, backup_id).storage_state == "local"
    assert fake.uploaded == {}


def test_offsite_upload_never_logs_the_secret_key(sf, monkeypatch):
    fake = FakeS3Client()
    _patch_boto(monkeypatch, fake)
    with sf() as db:
        server_id, bench_id, site_id = _setup(db)
        target = _target(db, name="prod-s3")
        target_id = target.id
        row = Backup(site_id=site_id, bench_id=bench_id, type="with-files", status="pending")
        db.add(row)
        db.commit()
        backup_id = row.id

    job_id = _run_backup_job(
        sf, server_id=server_id,
        params={
            "site": "test1.localhost", "bench_path": BENCH_PATH, "with_files": "1",
            "backup_id": str(backup_id), "storage_target_id": str(target_id),
        },
        executor=StorageBackupExecutor(),
    )
    with sf() as db:
        logs = "\n".join(
            e.content for e in db.scalars(
                select(LogEntry).where(LogEntry.job_id == job_id)
            ).all()
        )
    assert "s3-secret-xyz" not in logs
    assert "AKIAFAKE" not in logs


# --------------------------------------------------------------------------- #
# Storage-targets API
# --------------------------------------------------------------------------- #


def test_create_target_encrypts_keys_and_hides_them(client, db_session):
    login(client, "admin@example.com")
    resp = client.post(
        "/api/storage-targets",
        json={
            "name": "b2", "provider": "backblaze", "bucket": "my-bucket",
            "endpoint_url": "https://s3.us.backblazeb2.com", "region": "us-west",
            "access_key": "AKIA-PLAINTEXT", "secret_key": "SECRET-PLAINTEXT",
        },
        headers=csrf_headers(client),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["keys_set"] is True
    assert "access_key" not in body and "secret_key" not in body
    # DB holds Fernet tokens, not plaintext.
    row = db_session.scalars(select(StorageTarget)).first()
    assert row.access_key_enc and "AKIA-PLAINTEXT" not in row.access_key_enc
    assert get_secrets_service().decrypt(row.secret_key_enc) == "SECRET-PLAINTEXT"


@pytest.mark.parametrize("email", ["developer@example.com", "readonly@example.com"])
def test_storage_config_is_admin_only(client, db_session, email):
    login(client, email)
    resp = client.post(
        "/api/storage-targets",
        json={"name": "x", "bucket": "b", "access_key": "a", "secret_key": "s"},
        headers=csrf_headers(client),
    )
    assert resp.status_code == 403


def test_update_target_sets_and_clears_keys(client, db_session):
    t = _target(db_session, name="edit-me")
    login(client, "admin@example.com")
    # Clear the keys with empty strings.
    resp = client.patch(
        f"/api/storage-targets/{t.id}",
        json={"access_key": "", "secret_key": ""},
        headers=csrf_headers(client),
    )
    assert resp.status_code == 200 and resp.json()["keys_set"] is False
    db_session.refresh(t)
    assert t.access_key_enc is None and t.secret_key_enc is None
    # Re-set just the access key; secret stays cleared.
    resp = client.patch(
        f"/api/storage-targets/{t.id}",
        json={"access_key": "NEWKEY", "bucket": "renamed"},
        headers=csrf_headers(client),
    )
    assert resp.status_code == 200
    db_session.refresh(t)
    assert get_secrets_service().decrypt(t.access_key_enc) == "NEWKEY"
    assert t.bucket == "renamed"


def test_delete_target(client, db_session):
    t = _target(db_session, name="gone")
    login(client, "admin@example.com")
    resp = client.delete(f"/api/storage-targets/{t.id}", headers=csrf_headers(client))
    assert resp.status_code == 204
    assert db_session.get(StorageTarget, t.id) is None


def test_test_connection_endpoint(client, db_session, monkeypatch):
    t = _target(db_session, name="probe")
    monkeypatch.setattr(st, "_boto3_client", lambda cfg: FakeS3Client())
    login(client, "admin@example.com")
    resp = client.post(
        f"/api/storage-targets/{t.id}/test-connection", headers=csrf_headers(client)
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["reachable"] and body["writable"]


# --------------------------------------------------------------------------- #
# Presigned offsite download endpoint
# --------------------------------------------------------------------------- #


@pytest.fixture
def offsite_backup(db_session):
    s = Server(name="vm-a", hostname="10.0.0.1")
    db_session.add(s)
    db_session.commit()
    bench = Bench(server_id=s.id, path=BENCH_PATH, name="frappe-bench", frappe_version="16.2.0")
    db_session.add(bench)
    db_session.commit()
    site = Site(bench_id=bench.id, name="test1.localhost", status="active")
    db_session.add(site)
    db_session.commit()
    target = _target(db_session, name="dl-target")
    row = Backup(
        site_id=site.id, bench_id=bench.id, type="with-files", status="success",
        storage_state="offsite", storage_target_id=target.id,
        object_keys={"database": "tenant-a/backup-1/db.sql.gz"},
    )
    db_session.add(row)
    db_session.commit()
    return {"backup_id": row.id, "target": target}


def test_presigned_download_returns_url(client, offsite_backup, monkeypatch):
    fake = FakeS3Client()
    monkeypatch.setattr(st, "_boto3_client", lambda cfg: fake)
    login(client, "developer@example.com")
    resp = client.get(
        f"/api/backups/{offsite_backup['backup_id']}/offsite-download?artifact=database"
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["url"].startswith("https://s3.example/")
    assert body["expires_in"] == st.PRESIGN_TTL_SECONDS


def test_presigned_download_requires_backup_restore(client, offsite_backup):
    login(client, "readonly@example.com")
    resp = client.get(
        f"/api/backups/{offsite_backup['backup_id']}/offsite-download?artifact=database"
    )
    assert resp.status_code == 403


def test_presigned_download_local_only_backup_409(client, db_session, monkeypatch):
    s = Server(name="vm-b", hostname="10.0.0.2")
    db_session.add(s)
    db_session.commit()
    bench = Bench(server_id=s.id, path=BENCH_PATH, name="fb", frappe_version="16.2.0")
    db_session.add(bench)
    db_session.commit()
    site = Site(bench_id=bench.id, name="local.localhost", status="active")
    db_session.add(site)
    db_session.commit()
    row = Backup(site_id=site.id, bench_id=bench.id, type="db", status="success")
    db_session.add(row)
    db_session.commit()
    login(client, "developer@example.com")
    resp = client.get(f"/api/backups/{row.id}/offsite-download?artifact=database")
    assert resp.status_code == 409
