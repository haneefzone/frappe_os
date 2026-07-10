"""Backup artifact parsing + `backups` row upkeep + restore compatibility
(session 1.11), kept out of the API/action layers so all three share one
implementation and are unit-testable without SSH or a DB session.

`bench --site X backup [--with-files]` writes its artifacts to
`sites/<site>/private/backups/` with a shared `<ts>-<site>-` filename prefix:

    20260710_101500-test1_localhost-database.sql.gz
    20260710_101500-test1_localhost-files.tar
    20260710_101500-test1_localhost-private-files.tar
    20260710_101500-test1_localhost-site_config_backup.json

Rather than scrape bench's chatty stdout, the platform runs a fixed inspect
script (`ARTIFACT_INSPECT_SCRIPT`) that finds the newest database dump, derives
the shared prefix, and prints `ART\t<kind>\t<abspath>\t<size>\t<sha256>` for
each artifact that exists — deterministic and injection-free (the site name is
passed as a positional `$1`, never interpolated into the shell string).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.backup import Backup

# The four artifact kinds, mapped to their bench-backup filename suffix and the
# Backup convenience-path column they populate.
ARTIFACT_SUFFIXES: dict[str, str] = {
    "database": "database.sql.gz",
    "public_files": "files.tar",
    "private_files": "private-files.tar",
    "config": "site_config_backup.json",
}
_SUFFIX_TO_COLUMN = {
    "database": "db_path",
    "public_files": "public_files_path",
    "private_files": "private_files_path",
    "config": "config_path",
}

# Fixed inspect script (golden rule 1: site name is $1, never shell-interpolated).
# cwd is the validated bench path; it descends into the site's backups dir, finds
# the newest DB dump, and reports every sibling artifact of the same prefix. If no
# backup exists it prints NO_BACKUP and exits 0 so the caller can raise a clean
# "backup produced no artifacts" instead of a shell error.
ARTIFACT_INSPECT_SCRIPT = r"""
set -eu
dir="sites/$1/private/backups"
cd "$dir"
db=$(ls -1t -- *-database.sql.gz 2>/dev/null | head -n1 || true)
if [ -z "${db:-}" ]; then echo NO_BACKUP; exit 0; fi
prefix=${db%-database.sql.gz}
for suffix in database.sql.gz files.tar private-files.tar site_config_backup.json; do
  f="$prefix-$suffix"
  if [ -f "$f" ]; then
    printf 'ART\t%s\t%s\t%s\t%s\n' \
      "$suffix" "$(pwd)/$f" "$(stat -c %s -- "$f")" "$(sha256sum -- "$f" | cut -d' ' -f1)"
  fi
done
"""

# Reverse map: filename suffix -> artifact kind (for parsing the inspect output).
_SUFFIX_TO_KIND = {suffix: kind for kind, suffix in ARTIFACT_SUFFIXES.items()}


class BackupError(RuntimeError):
    """A backup produced no artifacts, or an artifact line was unparseable."""


@dataclass
class Artifact:
    """One captured backup file: its kind, absolute path, size and checksum."""

    kind: str
    path: str
    size_bytes: int
    checksum_sha256: str

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "path": self.path,
            "size_bytes": self.size_bytes,
            "checksum_sha256": self.checksum_sha256,
        }


@dataclass
class ParsedBackup:
    """The artifacts parsed out of one `ARTIFACT_INSPECT_SCRIPT` run."""

    artifacts: list[Artifact] = field(default_factory=list)

    @property
    def total_size(self) -> int:
        return sum(a.size_bytes for a in self.artifacts)

    def by_kind(self, kind: str) -> Artifact | None:
        return next((a for a in self.artifacts if a.kind == kind), None)

    @property
    def has_files(self) -> bool:
        return any(a.kind in ("public_files", "private_files") for a in self.artifacts)


def parse_artifacts(stdout: str) -> ParsedBackup:
    """Turn `ARTIFACT_INSPECT_SCRIPT` output into a ParsedBackup. Raises
    BackupError when the script reported NO_BACKUP (nothing was produced)."""
    parsed = ParsedBackup()
    saw_marker = False
    for line in stdout.splitlines():
        line = line.strip()
        if line == "NO_BACKUP":
            raise BackupError(
                "no backup artifacts were found in the site's backups directory"
            )
        if not line.startswith("ART\t"):
            continue
        saw_marker = True
        parts = line.split("\t")
        if len(parts) != 5:
            continue
        _, suffix, path, size, checksum = parts
        kind = _SUFFIX_TO_KIND.get(suffix)
        if kind is None:
            continue
        try:
            size_i = int(size)
        except ValueError:
            continue
        parsed.artifacts.append(
            Artifact(kind=kind, path=path, size_bytes=size_i, checksum_sha256=checksum)
        )
    if not saw_marker or not parsed.artifacts:
        raise BackupError("backup inspect produced no parseable artifact lines")
    return parsed


def record_backup(
    db: Session,
    *,
    backup: Backup,
    parsed: ParsedBackup,
    backup_type: str,
    frappe_version: str | None,
    now: datetime | None = None,
) -> Backup:
    """Fill a pending `Backup` row from parsed artifacts and mark it success.

    The convenience path columns are set from the matching artifact kinds; the
    full per-artifact records (with size + checksum) go into `artifacts`."""
    now = now or datetime.now(UTC)
    backup.type = backup_type
    backup.artifacts = [a.as_dict() for a in parsed.artifacts]
    backup.size_bytes = parsed.total_size
    backup.frappe_version = frappe_version
    for kind, artifact in ((k, parsed.by_kind(k)) for k in ARTIFACT_SUFFIXES):
        if artifact is not None:
            setattr(backup, _SUFFIX_TO_COLUMN[kind], artifact.path)
    backup.status = "success"
    backup.updated_at = now
    db.commit()
    db.refresh(backup)
    return backup


def create_pending_backup(
    db: Session,
    *,
    site_id: int,
    bench_id: int,
    backup_type: str,
    taken_by_job_id: int | None,
    now: datetime | None = None,
) -> Backup:
    """Insert a `pending` Backup row before the artifacts exist, so a failed
    backup still leaves a visible (failed) record instead of vanishing."""
    now = now or datetime.now(UTC)
    row = Backup(
        site_id=site_id,
        bench_id=bench_id,
        type=backup_type,
        status="pending",
        taken_by_job_id=taken_by_job_id,
        artifacts=[],
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


# --------------------------------------------------------------------------- #
# Checksum re-verification (backup.validate) + restore compatibility (gotcha #7)
# --------------------------------------------------------------------------- #

# Fixed script: recompute the sha256 of each path given on argv and print
# `SUM\t<path>\t<sha256>` (or MISSING). Paths are positional args ("$@"), never
# interpolated, so nothing shell-unsafe can pass.
CHECKSUM_VERIFY_SCRIPT = r"""
set -u
for f in "$@"; do
  if [ -f "$f" ]; then
    printf 'SUM\t%s\t%s\n' "$f" "$(sha256sum -- "$f" | cut -d' ' -f1)"
  else
    printf 'SUM\t%s\t%s\n' "$f" "MISSING"
  fi
done
"""


@dataclass
class ChecksumVerdict:
    """Per-artifact re-verification result for `backup.validate`."""

    kind: str
    path: str
    expected: str
    actual: str | None  # None = the file is gone

    @property
    def ok(self) -> bool:
        return self.actual is not None and self.actual == self.expected


def parse_checksums(stdout: str) -> dict[str, str | None]:
    """path -> recomputed sha256 (or None for MISSING) from CHECKSUM_VERIFY."""
    out: dict[str, str | None] = {}
    for line in stdout.splitlines():
        if not line.startswith("SUM\t"):
            continue
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        _, path, checksum = parts
        out[path] = None if checksum == "MISSING" else checksum
    return out


def verify_backup(backup: Backup, recomputed: dict[str, str | None]) -> list[ChecksumVerdict]:
    """Compare each stored artifact checksum against the recomputed map."""
    verdicts: list[ChecksumVerdict] = []
    for art in backup.artifacts or []:
        verdicts.append(
            ChecksumVerdict(
                kind=art.get("kind", "?"),
                path=art.get("path", ""),
                expected=art.get("checksum_sha256", ""),
                actual=recomputed.get(art.get("path", "")),
            )
        )
    return verdicts


def _major(version: str | None) -> int | None:
    """Leading integer of a Frappe version string ("16.2.1" -> 16, "v15" -> 15)."""
    if not version:
        return None
    digits = ""
    for ch in version.lstrip("vV"):
        if ch.isdigit():
            digits += ch
        else:
            break
    return int(digits) if digits else None


@dataclass
class CompatResult:
    """Verdict of restoring a backup onto a target bench (gotcha #7)."""

    ok: bool
    source_major: int | None
    target_major: int | None
    reason: str


def check_restore_compatibility(
    source_version: str | None, target_version: str | None
) -> CompatResult:
    """Block restoring a newer-major backup onto an older-major bench.

    Restoring the same or a lower source major onto a target is allowed (Frappe
    migrates forward on the trailing `bench migrate`); restoring a HIGHER source
    major would be a downgrade of the data's schema onto older code — refused
    (gotcha #7: never allow version downgrades). Unknown majors don't block (we
    warn instead) so a missing version string can't wedge a legitimate restore."""
    src = _major(source_version)
    tgt = _major(target_version)
    if src is None or tgt is None:
        return CompatResult(
            ok=True,
            source_major=src,
            target_major=tgt,
            reason="Source or target Frappe major is unknown — proceeding, but "
            "confirm the versions match before restoring to production.",
        )
    if src > tgt:
        return CompatResult(
            ok=False,
            source_major=src,
            target_major=tgt,
            reason=f"Backup is from Frappe v{src}; the target bench is v{tgt}. "
            "Restoring a newer major onto older code is a downgrade and is blocked.",
        )
    return CompatResult(
        ok=True,
        source_major=src,
        target_major=tgt,
        reason=f"Compatible: backup v{src} restoring onto target v{tgt}.",
    )


def parse_encryption_key(config_json_text: str) -> str | None:
    """Pull `encryption_key` out of a site_config_backup.json (gotcha #7). The
    key is what makes encrypted fields (passwords, OAuth secrets) readable after
    a restore; without copying it into the target site_config the site boots but
    every encrypted value is undecryptable. Returns None if absent/unparseable."""
    import json

    try:
        data = json.loads(config_json_text)
    except (ValueError, TypeError):
        return None
    key = data.get("encryption_key") if isinstance(data, dict) else None
    return key if isinstance(key, str) and key else None


def latest_backup_for_site(db: Session, site_id: int) -> Backup | None:
    """The most recent successful backup of a site, or None."""
    return db.scalars(
        select(Backup)
        .where(Backup.site_id == site_id, Backup.status == "success")
        .order_by(Backup.created_at.desc())
    ).first()


# --------------------------------------------------------------------------- #
# Retention (session 2.1): decide which of a site's backups to prune
# --------------------------------------------------------------------------- #


def _as_utc(dt: datetime) -> datetime:
    """Treat a naive stored timestamp as UTC (SQLite drops tzinfo; Postgres keeps
    it). Retention compares instants, so everything is normalised to aware UTC."""
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def plan_retention(
    backups: list[Backup],
    *,
    keep_last: int | None,
    keep_days: int | None,
    now: datetime,
) -> tuple[list[Backup], list[Backup]]:
    """Split a site's backups into (keep, remove) under a retention policy.

    Only ``status == "success"`` backups are candidates (a pending/failed row has
    no complete artifact set to prune). Policy, newest-first:

    - ``keep_last`` — keep the N most recent backups;
    - ``keep_days`` — keep any backup newer than N days;
    - both set — keep the union (a backup survives if *either* rule keeps it);
    - neither set — keep everything (a no-op sweep).

    The newest backup is **always** kept regardless of policy, so a site is never
    left with zero backups (the "never delete the newest/only" safety floor).
    Returns (keep, remove) each newest-first.
    """
    from datetime import timedelta

    successful = sorted(
        (b for b in backups if b.status == "success"),
        key=lambda b: (_as_utc(b.created_at), b.id),
        reverse=True,
    )
    if not successful:
        return [], []

    keep_ids: set[int] = set()
    if keep_last is not None:
        for b in successful[: max(keep_last, 0)]:
            keep_ids.add(b.id)
    if keep_days is not None:
        cutoff = now - timedelta(days=keep_days)
        for b in successful:
            if _as_utc(b.created_at) >= cutoff:
                keep_ids.add(b.id)
    if keep_last is None and keep_days is None:
        keep_ids = {b.id for b in successful}

    # Safety floor: the newest backup is never pruned (never the only backup).
    keep_ids.add(successful[0].id)

    keep = [b for b in successful if b.id in keep_ids]
    remove = [b for b in successful if b.id not in keep_ids]
    return keep, remove


def retention_artifact_paths(backup: Backup) -> list[str]:
    """Every distinct on-server artifact path a retention delete must remove for
    one backup: the four convenience columns plus any path in the `artifacts`
    JSON, deduped and order-preserved."""
    paths: list[str] = []
    for col in ("db_path", "public_files_path", "private_files_path", "config_path"):
        p = getattr(backup, col, None)
        if p:
            paths.append(p)
    for art in backup.artifacts or []:
        p = art.get("path") if isinstance(art, dict) else None
        if p:
            paths.append(p)
    seen: set[str] = set()
    out: list[str] = []
    for p in paths:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out
