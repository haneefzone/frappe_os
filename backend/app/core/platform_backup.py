"""Platform self-backup engine (session 6.3): dump the platform Postgres + the
platform config set into one archive, sha256 it, encrypt it at rest with an
operator-held backup passphrase, and push it through the 2.2 S3 storage service
with the same verify-after-upload path site backups use.

Everything here is pure/injectable so the whole flow is unit-testable with no
live Postgres, MinIO or master key:

- `create_self_backup(...)` takes a `pg_dump_fn` and an `upload_fn`; the defaults
  shell out to `pg_dump` and stream to S3, but tests pass fakes.
- `verify_self_backup(...)` takes a `download_fn` and a `pg_restore_list_fn`.

**Key handling (CLAUDE.md rule 6 — deliberately loud).** The master key is never
dumped, never archived, never logged. The archive is encrypted with a Fernet key
*derived from a separate operator-held passphrase* (`FDM_BACKUP_PASSPHRASE`) over
a per-backup random salt, so the backup is NOT encrypted solely with a key that
lives inside itself — see `app/models/platform_backup.py` and
`docs/master-key-escrow.md`. `filter_env_text` strips `FDM_SECRET_KEY` (and the
backup passphrase) from the archived `.env` so neither secret is ever written to
an artifact.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import tarfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

# Secrets that must NEVER appear in an archived config file (rule 6). The master
# key is the whole point (its loss is what the backup protects against but its
# copy must not travel *in* the backup); the backup passphrase is what decrypts
# the archive, so shipping it inside would defeat the encryption.
ENV_SECRET_KEYS = ("FDM_SECRET_KEY", "FDM_BACKUP_PASSPHRASE")

# PBKDF2 work factor for deriving the archive key from the passphrase. Stored
# nowhere (fixed constant); the per-backup salt is stored on the row so the
# operator can re-derive with only the passphrase.
PBKDF2_ITERATIONS = 240_000

# Chunk size for hashing files without buffering them whole.
_HASH_CHUNK = 1024 * 1024

# The config paths (relative to the platform repo/deploy root) the self-backup
# captures. `.env` is filtered (ENV_SECRET_KEYS removed) before it is archived.
CONFIG_ENV_FILE = ".env"
CONFIG_DIRS = ("deploy",)


class SelfBackupError(RuntimeError):
    """A self-backup step failed. Carries a browser-safe message (no secrets)."""


# --------------------------------------------------------------------------- #
# Crypto: passphrase -> Fernet key, and whole-file encrypt/decrypt
# --------------------------------------------------------------------------- #


def derive_backup_key(
    passphrase: str, salt: bytes, *, iterations: int = PBKDF2_ITERATIONS
) -> bytes:
    """Derive a urlsafe-base64 Fernet key from the operator passphrase + salt.

    Deterministic in (passphrase, salt, iterations): the same three inputs
    reproduce the key, which is how the runbook restore drill decrypts an archive
    with only the escrowed passphrase and the row's stored `kdf_salt`."""
    import base64

    if not passphrase:
        raise SelfBackupError(
            "no backup passphrase configured (set FDM_BACKUP_PASSPHRASE)"
        )
    kdf = PBKDF2HMAC(algorithm=SHA256(), length=32, salt=salt, iterations=iterations)
    return base64.urlsafe_b64encode(kdf.derive(passphrase.encode()))


def encrypt_file(src: str | Path, dst: str | Path, fernet: Fernet) -> None:
    """Encrypt `src` -> `dst` with Fernet. Whole-file (the platform control-plane
    DB is small); a Fernet token is authenticated, so tampering is detected on
    decrypt."""
    Path(dst).write_bytes(fernet.encrypt(Path(src).read_bytes()))


def decrypt_file(src: str | Path, dst: str | Path, fernet: Fernet) -> None:
    """Decrypt `src` -> `dst`. Raises on a wrong passphrase or a tampered token."""
    from cryptography.fernet import InvalidToken

    try:
        Path(dst).write_bytes(fernet.decrypt(Path(src).read_bytes()))
    except InvalidToken as exc:
        raise SelfBackupError(
            "could not decrypt the archive — wrong backup passphrase or the "
            "archive was altered"
        ) from exc


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(_HASH_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


# --------------------------------------------------------------------------- #
# Config-set capture
# --------------------------------------------------------------------------- #


def filter_env_text(text: str, *, drop_keys: tuple[str, ...] = ENV_SECRET_KEYS) -> str:
    """Return `.env` text with every `KEY=...` line whose KEY is in `drop_keys`
    removed, so the master key / backup passphrase never land in the archive.

    A dropped key is replaced by a breadcrumb comment so a restoring operator
    sees *that* it was excluded (and must re-supply it from escrow), not silent
    absence. Matching is on the line's key token, case-insensitive, tolerating
    leading whitespace and an optional `export `."""
    drop = {k.upper() for k in drop_keys}
    out: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        key = stripped
        if key.lower().startswith("export "):
            key = key[len("export ") :].strip()
        key = key.split("=", 1)[0].strip()
        if key.upper() in drop:
            out.append(f"# {key} excluded from platform self-backup — restore from escrow")
            continue
        out.append(line)
    return "\n".join(out) + ("\n" if text.endswith("\n") else "")


def gather_config_files(config_root: str | Path) -> list[tuple[str, Path]]:
    """(arcname, absolute path) for every config file the self-backup captures:
    the `deploy/` tree (systemd units, nginx conf) that exists under the root.
    `.env` is handled separately (it is filtered, not copied verbatim)."""
    root = Path(config_root)
    found: list[tuple[str, Path]] = []
    for d in CONFIG_DIRS:
        base = root / d
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if path.is_file():
                found.append((str(path.relative_to(root)), path))
    return found


def build_archive(dump_path: str | Path, config_root: str | Path, out_path: str | Path) -> None:
    """Roll the pg_dump + the (filtered) config set into one gzip tar at
    `out_path`. `.env` is added as `config/.env` with ENV_SECRET_KEYS stripped;
    `deploy/` files keep their relative path under `config/`."""
    root = Path(config_root)
    with tarfile.open(out_path, "w:gz") as tar:
        tar.add(dump_path, arcname="platform-db.dump")
        env_file = root / CONFIG_ENV_FILE
        if env_file.is_file():
            filtered = filter_env_text(env_file.read_text()).encode()
            info = tarfile.TarInfo(name="config/.env")
            info.size = len(filtered)
            import io

            tar.addfile(info, io.BytesIO(filtered))
        for arcname, path in gather_config_files(root):
            tar.add(path, arcname=f"config/{arcname}")


# --------------------------------------------------------------------------- #
# pg_dump / pg_restore (default real implementations; injectable in tests)
# --------------------------------------------------------------------------- #


def _pg_env(database_url: str) -> tuple[dict[str, str], str]:
    """Split a SQLAlchemy/psycopg URL into (env-with-PGPASSWORD, password-free
    connection URL). The password travels in the environment (PGPASSWORD), never
    on argv or in a log line (rule 6)."""
    from sqlalchemy.engine import make_url

    url = make_url(database_url)
    env = dict(os.environ)
    if url.password:
        env["PGPASSWORD"] = url.password
    safe = url.set(password=None)
    # Render a libpq-style URL pg_dump accepts; drop the SQLAlchemy driver tag.
    safe = safe.set(drivername="postgresql")
    return env, safe.render_as_string(hide_password=False)


def pg_dump_to_file(database_url: str, out_path: str | Path, *, timeout: float = 3600) -> None:
    """`pg_dump --format=custom` the platform DB to `out_path`. Custom format is
    what `pg_restore --list` reads back in the verify step."""
    env, conn = _pg_env(database_url)
    proc = subprocess.run(  # noqa: S603 — fixed argv, no shell; conn has no password.
        ["pg_dump", "--format=custom", "--file", str(out_path), "--dbname", conn],
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        stderr = proc.stderr.strip()[:300]
        raise SelfBackupError(f"pg_dump failed (exit {proc.returncode}): {stderr}")


def pg_restore_list(dump_path: str | Path, *, timeout: float = 300) -> str:
    """`pg_restore --list` the custom-format dump — reads the archive's table of
    contents without touching any database. Non-empty output = the dump's
    structure is readable (the verify proof)."""
    proc = subprocess.run(  # noqa: S603 — fixed argv, no shell.
        ["pg_restore", "--list", str(dump_path)],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise SelfBackupError(
            f"pg_restore --list failed (exit {proc.returncode}): {proc.stderr.strip()[:300]}"
        )
    return proc.stdout


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #


@dataclass
class SelfBackupResult:
    """Everything the caller records on the PlatformBackup row after a run."""

    archive_size: int
    sha256: str  # of the ENCRYPTED archive (the uploaded object)
    plaintext_sha256: str  # of the archive before encryption
    kdf_salt: str  # hex, stored so restore can re-derive the key
    object_key: str


# upload_fn(local_path, expected_sha256) -> object key in the bucket.
UploadFn = Callable[[str, str], str]


def create_self_backup(
    *,
    database_url: str,
    passphrase: str,
    config_root: str | Path,
    work_dir: str | Path,
    backup_id: int,
    upload_fn: UploadFn,
    pg_dump_fn: Callable[[str, str], None] | None = None,
) -> SelfBackupResult:
    """Run one self-backup end to end into `work_dir` and hand the encrypted
    archive to `upload_fn`. Returns the metadata to persist.

    Steps: pg_dump -> build archive (dump + filtered config) -> plaintext sha256
    -> derive key from passphrase+salt -> encrypt -> encrypted sha256 -> upload
    (upload_fn re-verifies the encrypted sha256 end to end). `upload_fn` returns
    the object key it wrote."""
    work = Path(work_dir)
    dump_path = work / "platform-db.dump"
    archive_path = work / f"platform-backup-{backup_id}.tar.gz"
    enc_path = work / f"platform-backup-{backup_id}.tar.gz.enc"

    dump = pg_dump_fn or (lambda db, out: pg_dump_to_file(db, out))
    dump(database_url, str(dump_path))
    if not dump_path.exists():
        raise SelfBackupError("pg_dump produced no output file")

    build_archive(dump_path, config_root, archive_path)
    plaintext_sha = sha256_file(archive_path)

    salt = os.urandom(16)
    fernet = Fernet(derive_backup_key(passphrase, salt))
    encrypt_file(archive_path, enc_path, fernet)
    enc_sha = sha256_file(enc_path)
    size = enc_path.stat().st_size

    object_key = upload_fn(str(enc_path), enc_sha)

    return SelfBackupResult(
        archive_size=size,
        sha256=enc_sha,
        plaintext_sha256=plaintext_sha,
        kdf_salt=salt.hex(),
        object_key=object_key,
    )


@dataclass
class VerifyResult:
    """Outcome of the download -> checksum -> pg_restore --list verify."""

    ok: bool
    detail: str


def verify_self_backup(
    *,
    passphrase: str,
    kdf_salt: str,
    expected_sha256: str,
    object_key: str,
    work_dir: str | Path,
    download_fn: Callable[[str, str], None],
    pg_restore_list_fn: Callable[[str], str] | None = None,
) -> VerifyResult:
    """Prove a self-backup is recoverable: download the encrypted archive,
    re-checksum it against `expected_sha256`, decrypt it with the passphrase +
    stored salt, unpack the pg_dump, and `pg_restore --list` it (structure
    readable). This is the ONLY real proof the self-backup works.

    `download_fn(object_key, dest_path)` fetches the object; injectable so tests
    run without S3."""
    work = Path(work_dir)
    enc_path = work / "verify.tar.gz.enc"
    archive_path = work / "verify.tar.gz"

    download_fn(object_key, str(enc_path))
    actual_sha = sha256_file(enc_path)
    if actual_sha != expected_sha256:
        return VerifyResult(
            ok=False,
            detail=(
                f"checksum mismatch: expected {expected_sha256[:12]}…, "
                f"downloaded {actual_sha[:12]}…"
            ),
        )

    fernet = Fernet(derive_backup_key(passphrase, bytes.fromhex(kdf_salt)))
    decrypt_file(enc_path, archive_path, fernet)

    with tarfile.open(archive_path, "r:gz") as tar:
        member = tar.getmember("platform-db.dump")
        tar.extract(member, path=work, filter="data")
    dump_path = work / "platform-db.dump"

    lister = pg_restore_list_fn or (lambda p: pg_restore_list(p))
    listing = lister(str(dump_path))
    entries = [ln for ln in listing.splitlines() if ln.strip() and not ln.startswith(";")]
    if not entries:
        return VerifyResult(ok=False, detail="pg_restore --list returned no entries")
    return VerifyResult(
        ok=True,
        detail=(
            f"checksum matched; archive decrypted; pg_restore --list read "
            f"{len(entries)} entrie(s) — structure intact"
        ),
    )
