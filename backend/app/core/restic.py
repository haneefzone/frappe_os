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

import re
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


# --------------------------------------------------------------------------- #
# 4.2 retention + integrity helpers
# --------------------------------------------------------------------------- #

# Valid `restic check --read-data-subset` selectors: a percentage ("5%"), an
# "n/m" fraction ("1/10"), or a size with an optional unit ("50G"). Anchored so
# nothing outside restic's subset grammar can reach the argv element.
_SUBSET_RE = re.compile(r"(?:[0-9]{1,3}%|[0-9]+/[0-9]+|[0-9]+(?:\.[0-9]+)?[KMGT]?)\Z")


def validate_read_data_subset(subset: str | None) -> str | None:
    """Normalise + validate a restic `check --read-data-subset` selector.

    Returns the cleaned selector, or None for an empty value (a structural,
    metadata-only check). Raises ResticError for anything outside restic's subset
    grammar so an operator can never smuggle an argv fragment through this field.
    """
    if subset is None:
        return None
    cleaned = subset.strip()
    if not cleaned:
        return None
    if not _SUBSET_RE.match(cleaned):
        raise ResticError(
            "read-data-subset must be a percentage (e.g. '5%'), an 'n/m' fraction "
            "(e.g. '1/10'), or a size (e.g. '50G')"
        )
    return cleaned


def forget_keep_args(repo: ResticRepo) -> list[str]:
    """Build the `--keep-daily/weekly/monthly N` argv fragment from a repo's
    retention policy.

    Raises ResticError when NO keep dimension is set: an unpolicied
    `restic forget --prune` would delete *every* snapshot, so we refuse to launch
    one rather than let a destructive prune go out with no keep policy (rule 1 —
    never generate a footgun).
    """
    args: list[str] = []
    for flag, value in (
        ("--keep-daily", repo.keep_daily),
        ("--keep-weekly", repo.keep_weekly),
        ("--keep-monthly", repo.keep_monthly),
    ):
        if value is not None:
            if int(value) < 1:
                raise ResticError(f"{flag} must be >= 1")
            args += [flag, str(int(value))]
    if not args:
        raise ResticError(
            "no retention policy set — configure at least one of keep_daily / "
            "keep_weekly / keep_monthly before running forget --prune"
        )
    return args


def parse_check_result(output: str) -> tuple[bool, str]:
    """Interpret `restic check` output as (ok, short_message) evidence.

    restic prints 'no errors were found' on a clean repo; anything else (or an
    error/damage line) is a failed integrity check. The message is a short,
    credential-free summary the §6 evidence row renders."""
    lines = [ln.strip() for ln in output.splitlines() if ln.strip()]
    if any("no errors were found" in ln for ln in lines):
        return True, "no errors were found"
    for ln in lines:
        low = ln.lower()
        if "error" in low or "damaged" in low or "corrupt" in low:
            return False, ln[:500]
    return (False, lines[-1][:500] if lines else "check produced no output")


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
