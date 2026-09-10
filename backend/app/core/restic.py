"""restic config-tier backup helpers (session 4.1 — Full-system DR).

This module holds the pure, side-effect-free plumbing the restic actions use:
- how a `ResticRepo` + its 2.2 `StorageTarget` resolve to a restic S3
  repository URI and the env-var set restic needs;
- the fixed config paths that make up the OS/config tier;
- the pinned restic version + the shell snippets the actions run on the target.

Golden rule 6: the restic repo password and the S3 access/secret keys are
secrets. They are passed to restic ONLY through its process environment (a 0600
env file staged on the target and sourced for the single command), never on an
argv element, never logged, never persisted in the clear. This module builds the
env-file *content* (with every value shlex-quoted so sourcing is safe); the
action stages it via base64 and removes it in a `finally`.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from urllib.parse import urlparse

from app.core.security import SecretsService, get_secrets_service
from app.models.restic import ResticRepo
from app.models.storage import StorageTarget

# The restic release we pin + install when the target has no `restic` on PATH.
# A fixed constant (never user input) so the download URL is fully deterministic.
RESTIC_VERSION = "0.16.4"

# The restic tag every config-tier snapshot carries, so the evidence listing can
# filter this platform's OS/config snapshots out of any other snapshots in the
# same repo. A fixed constant baked into the backup/snapshots templates.
CONFIG_TAG = "fdm-config-tier"

# The OS/config tier: the directories + files a bare-metal restore needs to
# reconstruct a managed server's service configuration. Only paths that exist on
# the target are actually handed to restic (the action probes first), so a server
# without e.g. supervisor doesn't fail the snapshot.
CONFIG_PATHS: tuple[str, ...] = (
    "/etc/nginx",
    "/etc/supervisor",
    "/etc/redis",
    "/etc/mysql",  # MariaDB ships its config under /etc/mysql on Debian/Ubuntu.
)

# Where the `dpkg --get-selections` package manifest is staged so it is captured
# *inside* the same snapshot as the configs (a bare-metal restore reinstalls the
# exact package set first, then drops the configs back). Under the SSH user's
# home so no root write is needed.
STAGING_DIRNAME = ".fdm-restic-stage"
DPKG_MANIFEST_NAME = "dpkg-selections.txt"

# Default fraction of pack data `restic check` re-reads and verifies (session 4.2).
# `--read-data-subset` keeps the integrity check affordable on a large repo while
# still exercising real data (not just structure/metadata). A callable override
# can pass a different subset; this is the value the schedule + API launch use.
DEFAULT_CHECK_SUBSET = "5%"


class ResticError(RuntimeError):
    """A restic repo is misconfigured (no target / no password / bad bucket).
    Carries a browser-safe message — never any credential material."""


@dataclass
class ResticEnv:
    """The resolved restic connection for one repo — the repository URI plus the
    secret env-var set. Plaintext secrets are held only in memory for the
    lifetime of one command and are shlex-quoted into the staged 0600 env file.
    """

    repository: str
    password: str
    access_key: str
    secret_key: str
    region: str | None

    @property
    def secret_values(self) -> tuple[str, ...]:
        """The plaintext secrets to register with the log redactor (rule 6)."""
        return (self.password, self.access_key, self.secret_key)

    def env_file_content(self) -> str:
        """The body of the 0600 env file sourced (`set -a; . file`) before restic
        runs. Each value is shlex-quoted so a password with spaces/quotes sources
        safely; RESTIC_REPOSITORY is NOT written here (it is a non-secret argv
        `-r` element the operator can see in the job log)."""
        lines = [
            f"RESTIC_PASSWORD={shlex.quote(self.password)}",
            f"AWS_ACCESS_KEY_ID={shlex.quote(self.access_key)}",
            f"AWS_SECRET_ACCESS_KEY={shlex.quote(self.secret_key)}",
        ]
        if self.region:
            lines.append(f"AWS_DEFAULT_REGION={shlex.quote(self.region)}")
        return "\n".join(lines) + "\n"


def normalize_prefix(prefix: str | None) -> str:
    """A repo prefix with no leading/trailing slashes and no `..` segments."""
    cleaned = (prefix or "").strip().strip("/")
    if any(seg == ".." for seg in cleaned.split("/")):
        raise ResticError("repo prefix must not contain '..' segments")
    return cleaned


def repository_uri(target: StorageTarget, prefix: str) -> str:
    """Build the restic S3 repository URI for a target + prefix.

    restic addresses S3 as `s3:<host-or-endpoint>/<bucket>[/<prefix>]`:
    - a self-hosted MinIO/B2/Wasabi target has an explicit `endpoint_url`
      (scheme kept, e.g. `s3:http://minio.local:9000/bucket/prefix`);
    - real AWS leaves `endpoint_url` NULL, so we derive the regional host
      (`s3:s3.<region>.amazonaws.com/bucket/prefix`, or `s3.amazonaws.com` when
      no region is set).
    """
    if not target.bucket:
        raise ResticError("storage target has no bucket configured")
    prefix = normalize_prefix(prefix)
    if target.endpoint_url:
        endpoint = target.endpoint_url.rstrip("/")
    elif target.region:
        endpoint = f"s3.{target.region}.amazonaws.com"
    else:
        endpoint = "s3.amazonaws.com"
    parts = [endpoint, target.bucket]
    if prefix:
        parts.append(prefix)
    return "s3:" + "/".join(parts)


def resolve_env(
    repo: ResticRepo,
    target: StorageTarget | None,
    *,
    secrets: SecretsService | None = None,
) -> ResticEnv:
    """Decrypt the repo password + the target's S3 keys (in memory only) and
    resolve the repository URI. Raises ResticError if anything required is
    missing so the action fails loud before touching the target."""
    secrets = secrets or get_secrets_service()
    if target is None:
        raise ResticError("this server's restic repo has no storage target attached")
    if not repo.password_enc:
        raise ResticError("this server's restic repo has no password configured")
    if not (target.access_key_enc and target.secret_key_enc):
        raise ResticError(
            f"storage target {target.name!r} has no S3 credentials configured"
        )
    return ResticEnv(
        repository=repository_uri(target, repo.prefix),
        password=secrets.decrypt(repo.password_enc),
        access_key=secrets.decrypt(target.access_key_enc),
        secret_key=secrets.decrypt(target.secret_key_enc),
        region=target.region or None,
    )


def install_url(arch: str) -> str:
    """The pinned restic release download URL for a `uname -m` architecture.

    Fully deterministic (fixed version + a small arch allowlist) — no user input
    reaches the URL. Unknown arches raise so we never fetch an arbitrary URL.
    """
    goarch = {
        "x86_64": "amd64",
        "amd64": "amd64",
        "aarch64": "arm64",
        "arm64": "arm64",
    }.get(arch.strip())
    if goarch is None:
        raise ResticError(f"unsupported architecture for restic install: {arch!r}")
    return (
        f"https://github.com/restic/restic/releases/download/v{RESTIC_VERSION}"
        f"/restic_{RESTIC_VERSION}_linux_{goarch}.bz2"
    )


def parse_installed_version(output: str) -> str | None:
    """Pull the version out of `restic version` output ("restic 0.16.4 compiled
    with ...") or return None if restic isn't present / the line is unexpected."""
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("restic ") and len(line.split()) >= 2:
            return line.split()[1]
    return None


def parse_snapshot_id(output: str) -> str | None:
    """Pull the short snapshot id restic prints after a backup ("snapshot 1a2b3c4d
    saved"). Returns the id or None if the line isn't present."""
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("snapshot ") and line.endswith(" saved"):
            parts = line.split()
            if len(parts) >= 3:
                return parts[1]
    return None


def build_forget_keep_args(repo: ResticRepo) -> list[str]:
    """Turn a repo's retention policy into restic ``--keep-*`` argv elements.

    Each configured dimension maps to its restic flag as two argv elements (flag
    + count) so nothing is interpolated into a shell string. Raises ResticError
    when NO dimension is set: `restic forget --prune` with no keep flags would
    delete *every* snapshot, so an empty policy must never reach restic (this is
    the destructive-safety floor for the retention sweep). Counts must be >= 1.
    """
    mapping = (
        ("--keep-last", repo.retention_keep_last),
        ("--keep-daily", repo.retention_keep_daily),
        ("--keep-weekly", repo.retention_keep_weekly),
        ("--keep-monthly", repo.retention_keep_monthly),
    )
    args: list[str] = []
    for flag, value in mapping:
        if value is None:
            continue
        if int(value) < 1:
            raise ResticError(f"retention {flag} must be >= 1 (got {value})")
        args += [flag, str(int(value))]
    if not args:
        raise ResticError(
            "no retention policy configured — refusing to prune (an empty policy "
            "would delete every snapshot)"
        )
    return args


def is_restic_lock_error(output: str) -> bool:
    """True when a restic run failed because the repository was already locked by
    another operation (e.g. a concurrent snapshot or prune) rather than because of
    a genuine problem. The per-server job lock only serialises jobs of the *same*
    action (the lock key includes the action name), so a `restic check` can still
    overlap a `restic backup`/`forget` on the same repo; restic's own exclusive
    repository lock then rejects the loser. That is an operational retry condition,
    NOT an integrity failure — the check action uses this to avoid recording a
    false `last_check_ok=False` and raising a spurious breach alert."""
    low = output.lower()
    markers = (
        "unable to create lock",
        "repository is already locked",
        "already locked exclusively",
    )
    return any(marker in low for marker in markers)


def summarize_check(exit_code: int, output: str) -> tuple[bool, str]:
    """Interpret a `restic check` run into (ok, one-line summary).

    restic exits 0 and prints "no errors were found" when the repository is
    intact; any other exit code means the check found problems. The summary is a
    single restic verdict line (or a generic fallback) — it is a status line
    only and carries no repo URI, password, or S3 key (golden rule 6).
    """
    ok = exit_code == 0
    verdict = ""
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        low = line.lower()
        if ok and "no errors were found" in low:
            verdict = line
            break
        if not ok and ("error" in low or "fatal" in low or "damaged" in low):
            verdict = line  # keep the last error line as the summary
    if not verdict:
        verdict = (
            "restic check reported no errors"
            if ok
            else f"restic check failed (exit {exit_code})"
        )
    return ok, verdict[:500]


def redacted_repository(uri: str) -> str:
    """A repository URI safe to show even if an endpoint ever embedded userinfo
    (it never should) — strips any `user:pass@` before the host."""
    try:
        # s3:<endpoint>/... — only the endpoint portion could theoretically carry
        # userinfo; parse just that defensively.
        scheme, _, rest = uri.partition(":")
        parsed = urlparse(rest if "//" in rest else f"//{rest}")
        if parsed.username or parsed.password:
            netloc = parsed.hostname or ""
            if parsed.port:
                netloc += f":{parsed.port}"
            return f"{scheme}:{netloc}{parsed.path}"
    except Exception:  # noqa: BLE001 — never let a display helper raise.
        pass
    return uri
