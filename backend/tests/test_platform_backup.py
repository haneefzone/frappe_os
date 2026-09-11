"""Platform self-backup + master-key escrow (session 6.3).

Covers the pure engine (config-set filtering, passphrase-derived encryption,
archive build, create/verify with injected pg_dump/upload/download — including
the security invariant that FDM_SECRET_KEY never lands in the archive or a log),
the self-backup + verify *jobs* driven through the JobRunner over a
LocalRemoteExecutor with pg_dump/pg_restore/boto3 faked, the scheduler firing a
platform-targeted schedule, and the Admin-only API + escrow acknowledgement.
No live Postgres, MinIO or master key.
"""

import os

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.routes.jobs import get_job_runner
from app.core import platform_backup as pb
from app.core import storage as st
from app.core.jobs import InMemoryJobBackend, JobRunner
from app.core.scheduler import compute_next_run, tick
from app.core.security import get_secrets_service
from app.db import Base
from app.models import CommandJob
from app.models.platform_backup import PlatformBackup
from app.models.schedule import Schedule
from app.models.settings import PlatformSettings
from app.models.storage import StorageTarget
from tests.conftest import csrf_headers, login

SECRET_VALUE = "s3cr3t-master-key-value-do-not-leak"
PASSPHRASE = "correct-horse-backup-passphrase"


@pytest.fixture
def sf():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    yield factory
    engine.dispose()


# --------------------------------------------------------------------------- #
# Pure engine
# --------------------------------------------------------------------------- #


def test_filter_env_strips_master_key_and_passphrase():
    env = (
        "APP_NAME=FDM\n"
        f"FDM_SECRET_KEY={SECRET_VALUE}\n"
        "export FDM_BACKUP_PASSPHRASE=whatever\n"
        "DATABASE_URL=postgresql://x\n"
    )
    out = pb.filter_env_text(env)
    assert SECRET_VALUE not in out
    assert "whatever" not in out
    assert "APP_NAME=FDM" in out
    assert "DATABASE_URL=postgresql://x" in out
    # A breadcrumb marks the exclusion rather than silently dropping it.
    assert "FDM_SECRET_KEY excluded" in out


def test_derive_key_deterministic_and_salt_sensitive():
    salt = b"0" * 16
    assert pb.derive_backup_key(PASSPHRASE, salt) == pb.derive_backup_key(PASSPHRASE, salt)
    assert pb.derive_backup_key(PASSPHRASE, salt) != pb.derive_backup_key(PASSPHRASE, b"1" * 16)
    with pytest.raises(pb.SelfBackupError):
        pb.derive_backup_key("", salt)


def test_encrypt_decrypt_round_trip(tmp_path):
    from cryptography.fernet import Fernet

    salt = os.urandom(16)
    fernet = Fernet(pb.derive_backup_key(PASSPHRASE, salt))
    src = tmp_path / "plain.bin"
    src.write_bytes(b"hello world" * 1000)
    enc = tmp_path / "c.enc"
    dec = tmp_path / "d.bin"
    pb.encrypt_file(src, enc, fernet)
    assert enc.read_bytes() != src.read_bytes()
    pb.decrypt_file(enc, dec, fernet)
    assert dec.read_bytes() == src.read_bytes()
    # A wrong passphrase cannot decrypt.
    wrong = Fernet(pb.derive_backup_key("nope", salt))
    with pytest.raises(pb.SelfBackupError):
        pb.decrypt_file(enc, tmp_path / "x", wrong)


def _make_config_root(tmp_path):
    """A fake platform root with a secret-bearing .env and a deploy/ file."""
    root = tmp_path / "platform"
    root.mkdir()
    (root / ".env").write_text(
        f"APP_NAME=FDM\nFDM_SECRET_KEY={SECRET_VALUE}\nDATABASE_URL=postgresql://x\n"
    )
    (root / "deploy").mkdir()
    (root / "deploy" / "fdm.service").write_text("[Unit]\nDescription=FDM\n")
    (root / "deploy" / "nginx.conf").write_text("server { listen 80; }\n")
    return root


def test_build_archive_excludes_master_key(tmp_path):
    import tarfile

    root = _make_config_root(tmp_path)
    dump = tmp_path / "db.dump"
    dump.write_bytes(b"PGDMP-fake-dump")
    out = tmp_path / "a.tar.gz"
    pb.build_archive(dump, root, out)
    with tarfile.open(out, "r:gz") as tar:
        names = tar.getnames()
        assert "platform-db.dump" in names
        assert "config/.env" in names
        assert "config/deploy/nginx.conf" in names
        env_bytes = tar.extractfile("config/.env").read()
    # The security invariant: the master key value is nowhere in the archive.
    assert SECRET_VALUE.encode() not in out.read_bytes()
    assert SECRET_VALUE.encode() not in env_bytes


def test_create_and_verify_self_backup_end_to_end(tmp_path):
    """create_self_backup -> (fake) upload; verify_self_backup -> (fake) download.
    The encrypted object round-trips, the checksum matches, and the master key is
    absent from the encrypted object AND the decrypted archive."""
    root = _make_config_root(tmp_path)
    store: dict[str, bytes] = {}

    def fake_pg_dump(db_url, out_path):
        # A plausible custom-format dump header; contents are irrelevant here.
        with open(out_path, "wb") as fh:
            fh.write(b"PGDMP" + os.urandom(2048))

    def fake_upload(local_path, expected_sha):
        store["obj"] = open(local_path, "rb").read()
        # Prove the uploader is handed a file whose sha matches what it recorded.
        assert pb.sha256_file(local_path) == expected_sha
        return "prefix/platform-backup-1/archive.tar.gz.enc"

    work = tmp_path / "work"
    work.mkdir()
    res = pb.create_self_backup(
        database_url="postgresql://x",
        passphrase=PASSPHRASE,
        config_root=root,
        work_dir=work,
        backup_id=1,
        upload_fn=fake_upload,
        pg_dump_fn=fake_pg_dump,
    )
    assert res.object_key.endswith("archive.tar.gz.enc")
    assert res.sha256 and res.plaintext_sha256 and res.kdf_salt
    # The uploaded (encrypted) object never contains the master key.
    assert SECRET_VALUE.encode() not in store["obj"]

    vwork = tmp_path / "vwork"
    vwork.mkdir()

    def fake_download(key, dest):
        with open(dest, "wb") as fh:
            fh.write(store["obj"])

    verdict = pb.verify_self_backup(
        passphrase=PASSPHRASE,
        kdf_salt=res.kdf_salt,
        expected_sha256=res.sha256,
        object_key=res.object_key,
        work_dir=vwork,
        download_fn=fake_download,
        pg_restore_list_fn=lambda p: "; Archive created\n1; 2345 TABLE public users\n",
    )
    assert verdict.ok, verdict.detail


def test_verify_detects_checksum_mismatch(tmp_path):
    work = tmp_path / "w"
    work.mkdir()

    def bad_download(key, dest):
        with open(dest, "wb") as fh:
            fh.write(b"corrupted")

    verdict = pb.verify_self_backup(
        passphrase=PASSPHRASE,
        kdf_salt=(b"0" * 16).hex(),
        expected_sha256="a" * 64,
        object_key="k",
        work_dir=work,
        download_fn=bad_download,
        pg_restore_list_fn=lambda p: "x",
    )
    assert not verdict.ok
    assert "checksum" in verdict.detail


# --------------------------------------------------------------------------- #
# The jobs, through the JobRunner over a LocalRemoteExecutor
# --------------------------------------------------------------------------- #


class _FakeS3:
    """A tiny in-memory S3 client (upload_fileobj / download_file)."""

    store: dict[str, bytes] = {}

    def upload_fileobj(self, fileobj, bucket, key):
        type(self).store[f"{bucket}/{key}"] = fileobj.read()

    def download_file(self, bucket, key, dest):
        with open(dest, "wb") as fh:
            fh.write(type(self).store[f"{bucket}/{key}"])


@pytest.fixture
def platform_env(sf, tmp_path, monkeypatch):
    """Wire settings.fdm_backup_passphrase, a PLATFORM_ROOT config set, an enabled
    storage target, and fakes for pg_dump/pg_restore/boto3."""
    _FakeS3.store = {}
    root = _make_config_root(tmp_path)
    monkeypatch.setenv("PLATFORM_ROOT", str(root))
    monkeypatch.setenv("FDM_BACKUP_PASSPHRASE", PASSPHRASE)
    from app.config import get_settings

    get_settings.cache_clear()
    def _fake_dump(db, out):
        open(out, "wb").write(b"PGDMP" + os.urandom(1024))

    monkeypatch.setattr(pb, "pg_dump_to_file", _fake_dump)
    monkeypatch.setattr(pb, "pg_restore_list", lambda p, timeout=300: "; toc\n1; TABLE users\n")
    monkeypatch.setattr(st, "_boto3_client", lambda cfg: _FakeS3())
    with sf() as db:
        target = StorageTarget(
            name="minio", provider="minio", endpoint_url="http://127.0.0.1:9101",
            bucket="fdm-backups", use_ssl=False, enabled=True,
            access_key_enc=get_secrets_service().encrypt("ak"),
            secret_key_enc=get_secrets_service().encrypt("sk"),
        )
        db.add(target)
        db.commit()
        target_id = target.id
    yield {"target_id": target_id, "root": root}
    get_settings.cache_clear()


def _runner(sf):
    return JobRunner(
        sf, InMemoryJobBackend(), enqueue=lambda job: None, secrets=get_secrets_service()
    )


def test_self_backup_job_success_and_no_secret_leak(sf, platform_env):
    runner = _runner(sf)
    with sf() as db:
        row = PlatformBackup(status="pending")
        db.add(row)
        db.commit()
        backup_id = row.id
        job = runner.create(
            db, action_name="platform.self_backup", server_id=None,
            target_type="platform", target_id="platform",
            params={
                "backup_id": str(backup_id),
                "storage_target_id": str(platform_env["target_id"]),
            },
            priority="default", created_by=None,
        )
        job_id = job.id
    runner.run_job(job_id)  # no executor_factory -> LocalRemoteExecutor (template.local)

    with sf() as db:
        assert db.get(CommandJob, job_id).status == "success"
        b = db.get(PlatformBackup, backup_id)
        assert b.status == "success"
        assert b.encrypted and b.sha256 and b.kdf_salt and b.object_key
        assert b.verify_status == "unverified"
        # No job log line contains the master key value (rule 6).
        from app.models import LogEntry

        logs = db.scalars(select(LogEntry).where(LogEntry.job_id == job_id)).all()
        assert all(SECRET_VALUE not in ln.content for ln in logs)
    # The uploaded encrypted object exists and does not contain the master key.
    assert _FakeS3.store, "expected an uploaded object"
    assert all(SECRET_VALUE.encode() not in blob for blob in _FakeS3.store.values())


def test_verify_job_marks_verified(sf, platform_env):
    runner = _runner(sf)
    with sf() as db:
        row = PlatformBackup(status="pending")
        db.add(row)
        db.commit()
        backup_id = row.id
        job = runner.create(
            db, action_name="platform.self_backup", server_id=None,
            target_type="platform", target_id="platform",
            params={
                "backup_id": str(backup_id),
                "storage_target_id": str(platform_env["target_id"]),
            },
            priority="default", created_by=None,
        )
        jid = job.id
    runner.run_job(jid)

    with sf() as db:
        vjob = runner.create(
            db, action_name="platform.self_backup_verify", server_id=None,
            target_type="platform", target_id="platform",
            params={"backup_id": str(backup_id)}, priority="high", created_by=None,
        )
        vid = vjob.id
    runner.run_job(vid)
    with sf() as db:
        assert db.get(CommandJob, vid).status == "success"
        b = db.get(PlatformBackup, backup_id)
        assert b.verify_status == "verified"
        assert b.verified_at is not None


def test_self_backup_fails_without_passphrase(sf, platform_env, monkeypatch):
    from app.config import get_settings

    monkeypatch.delenv("FDM_BACKUP_PASSPHRASE", raising=False)
    get_settings.cache_clear()
    runner = _runner(sf)
    with sf() as db:
        row = PlatformBackup(status="pending")
        db.add(row)
        db.commit()
        backup_id = row.id
        job = runner.create(
            db, action_name="platform.self_backup", server_id=None,
            target_type="platform", target_id="platform",
            params={
                "backup_id": str(backup_id),
                "storage_target_id": str(platform_env["target_id"]),
            },
            priority="default", created_by=None,
        )
        jid = job.id
    runner.run_job(jid)
    with sf() as db:
        assert db.get(CommandJob, jid).status == "failure"
        b = db.get(PlatformBackup, backup_id)
        assert b.status == "failed"
        assert b.error and "passphrase" in b.error


# --------------------------------------------------------------------------- #
# Scheduler fires a platform-targeted schedule
# --------------------------------------------------------------------------- #


def test_scheduler_fires_platform_self_backup(sf, platform_env):
    from datetime import UTC, datetime

    runner = _runner(sf)
    now = datetime(2026, 7, 23, 2, 0, tzinfo=UTC)
    with sf() as db:
        sched = Schedule(
            name="Nightly platform self-backup", target_type="platform", target_id=0,
            action_name="platform.self_backup", interval_seconds=86400,
            priority="default", enabled=True, next_run_at=now,
        )
        db.add(sched)
        db.commit()
        sched_id = sched.id
    fired = tick(sf, runner, now=now)
    assert sched_id in fired
    with sf() as db:
        # A pending PlatformBackup row + a CommandJob were created for the fire.
        rows = db.scalars(select(PlatformBackup)).all()
        assert len(rows) == 1
        assert rows[0].taken_by_job_id is not None
        sched = db.get(Schedule, sched_id)
        assert sched.last_run_job_id is not None
        # SQLite reads DateTime columns back tz-naive, so normalise before
        # comparing to the tz-aware computed value (same pattern as test_scheduler).
        expected = compute_next_run(sched, after=now)
        stored = sched.next_run_at
        assert stored.replace(tzinfo=None) == expected.replace(tzinfo=None)


# --------------------------------------------------------------------------- #
# API: Admin-only + escrow acknowledgement
# --------------------------------------------------------------------------- #


@pytest.fixture
def pb_client(client, db_session, monkeypatch):
    runner = JobRunner(
        lambda: db_session, InMemoryJobBackend(), enqueue=lambda job: None,
        secrets=get_secrets_service(),
    )
    client.app.dependency_overrides[get_job_runner] = lambda: runner
    yield client
    client.app.dependency_overrides.pop(get_job_runner, None)


def test_platform_backups_admin_only(pb_client):
    # Developer (not Admin) is refused every platform-backup route.
    login(pb_client, "developer@example.com")
    assert pb_client.get("/api/platform/backups").status_code == 403
    assert pb_client.get("/api/platform/escrow").status_code == 403
    # Admin can read.
    login(pb_client, "admin@example.com")
    assert pb_client.get("/api/platform/backups").status_code == 200
    assert pb_client.get("/api/platform/escrow").json()["confirmed"] is False


def test_run_backup_requires_a_storage_target(pb_client):
    login(pb_client, "admin@example.com")
    resp = pb_client.post(
        "/api/platform/backups", json={}, headers=csrf_headers(pb_client)
    )
    # No storage target configured -> 422 with a clear message.
    assert resp.status_code == 422
    assert "storage target" in resp.text.lower()


def test_run_backup_creates_pending_row_and_job(pb_client, db_session):
    target = StorageTarget(
        name="minio", provider="minio", bucket="fdm-backups", enabled=True,
        access_key_enc=get_secrets_service().encrypt("ak"),
        secret_key_enc=get_secrets_service().encrypt("sk"),
    )
    db_session.add(target)
    db_session.commit()
    login(pb_client, "admin@example.com")
    resp = pb_client.post(
        "/api/platform/backups", json={}, headers=csrf_headers(pb_client)
    )
    assert resp.status_code == 201, resp.text
    rows = db_session.scalars(select(PlatformBackup)).all()
    assert len(rows) == 1 and rows[0].status == "pending"
    assert rows[0].taken_by_job_id is not None


def test_escrow_confirm_writes_row_and_audit(pb_client, db_session):
    from app.models.audit import AuditLog

    login(pb_client, "admin@example.com")
    resp = pb_client.post(
        "/api/platform/escrow/confirm",
        json={"acknowledge": True},
        headers=csrf_headers(pb_client),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["confirmed"] is True
    row = db_session.get(PlatformSettings, 1)
    assert row.master_key_escrow_confirmed_at is not None
    assert row.master_key_escrow_confirmed_by is not None
    audits = db_session.scalars(
        select(AuditLog).where(AuditLog.action == "platform.escrow.confirm")
    ).all()
    assert len(audits) == 1
    # Status now reflects the acknowledgement.
    assert pb_client.get("/api/platform/escrow").json()["confirmed"] is True
