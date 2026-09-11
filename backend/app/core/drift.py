"""Config drift detection (session 6.7, uiux-spec A2.16).

Two flows share this module:

1. **Baseline capture** (`capture_baselines`) — the JobRunner calls this after
   any job whose template declares `writes_config = [...]` succeeds, while the
   job's SSH session is still open. It re-reads the artefacts that job manages,
   hashes their secret-stripped canonical form, and upserts a `ConfigBaseline`
   row per artefact attributed to the job (status ``baseline``). A managed change
   therefore *moves* the baseline — no drift is raised for it.

2. **Drift check** (`run_drift_check`) — the `server.drift_check` action re-reads
   every tracked artefact for a server, hashes it the same way, and diffs the
   hash against the stored baseline. A mismatch (or a missing tracked file, or
   the 2.5 elevation drop-in reappearing) flips the row to ``drifted`` and is
   returned so the action can notify + surface it.

Golden rules honoured here:
- **Read-only** on the managed server. Every remote command is a `cat`/`find`
  (root ones via a fixed `sudo -n` line in the docs/implementation-plan.md
  allowlist). Nothing is ever written, reloaded, or reverted.
- **Secrets never leak** (rule 6). JSON artefacts are parsed, every secret value
  is masked to ``••••`` *before* hashing, and only that masked canonical text is
  stored/diffed/displayed. A file we expect to be JSON but can't parse is hashed
  raw for detection but its content is **not** stored (it might carry secrets).
- **A missing artefact is drift**, not a swallowed error.

The tracked set is deliberately small and explicit (`ARTIFACTS`).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

# Golden rule 6 mask — identical to app.core.commands.templates.MASK.
MASK = "••••"  # ••••

# The platform-managed nginx vhost directory, relative to a bench (session 2.4).
# Kept in sync with app.core.domains.VHOSTS_SUBDIR.
VHOSTS_SUBDIR = "config/nginx-vhosts"

# The 2.5 production-elevation sudoers drop-in. It is installed for the duration
# of a single `bench setup-production` run and ALWAYS revoked in a finally block
# (see docs/production-setup.md). It must therefore never persist at rest — its
# presence is itself drift, so we track its *absence*.
ELEVATION_DROPIN = "/etc/sudoers.d/fdm-prod-elevation"


# --------------------------------------------------------------------------- #
# Secret masking + canonicalisation                                           #
# --------------------------------------------------------------------------- #

# Substrings that mark a config key as secret-bearing. Matched case-insensitively
# against the key name. `_key` catches encryption_key / api_key / secret_key; the
# rest cover db/admin passwords, API tokens and private material. Deliberately
# conservative: a false positive only over-masks a value we would not display
# anyway; a false negative would leak a secret, so we bias toward masking.
_SECRET_MARKERS = (
    "password",
    "passwd",
    "secret",
    "token",
    "_key",
    "apikey",
    "api_key",
    "encryption_key",
    "private",
    "credential",
)


def is_secret_key(key: str) -> bool:
    k = key.lower()
    if k == "key":
        return True
    return any(marker in k for marker in _SECRET_MARKERS)


def mask_secrets(value):
    """Recursively replace every secret-keyed value with ``••••``.

    Only *values* under a secret key are masked (the key name is not a secret and
    must stay so the diff can show which keys exist). Non-secret scalars pass
    through unchanged so a legitimate managed edit still shows in the diff.
    """
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            out[k] = MASK if is_secret_key(str(k)) else mask_secrets(v)
        return out
    if isinstance(value, list):
        return [mask_secrets(v) for v in value]
    return value


def canonicalize_json(text: str) -> str | None:
    """Return a stable, secret-stripped JSON string, or None if unparseable.

    Sorted keys + fixed indent make the hash insensitive to key ordering and
    whitespace, so re-serialising the same logical config always hashes the same.
    Returning None signals the caller to fall back to a raw hash *without*
    storing content (the file may hold secrets we could not strip)."""
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return None
    return json.dumps(mask_secrets(data), sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# Tracked artefact set                                                        #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Artifact:
    """One tracked config artefact.

    `scope` decides how the path is resolved and which ids a baseline row carries
    ("server" → bench/site NULL, "bench" → bench set, "site" → bench+site set).
    `fmt` decides how content is canonicalised + whether it is safe to store:
      - "json"   parse + mask secrets; store the masked canonical text.
      - "text"   store raw text (non-secret config: nginx/supervisor/sudoers).
      - "dir"    concat the sorted files under a dir (bench-owned vhosts).
      - "absent" the file must NOT exist; presence is drift, content never stored.
    `needs_root` prepends the fixed `sudo -n` read line (reduced fidelity on a
    no-sudo target — covered by mocked-SSH tests, see DOO-128)."""

    key: str
    scope: str  # server | bench | site
    fmt: str  # json | text | dir | absent
    needs_root: bool = False
    # For a server-scoped artefact, its fixed absolute path. bench/site paths are
    # resolved at read time from the bench path + site name.
    server_path: str | None = None


ARTIFACTS: tuple[Artifact, ...] = (
    # -- server (root-owned; reduced fidelity on no-sudo targets) ----------- #
    Artifact("nginx.conf", "server", "text", needs_root=True, server_path="/etc/nginx/nginx.conf"),
    Artifact(
        "supervisor.conf",
        "server",
        "text",
        needs_root=True,
        server_path="/etc/supervisor/supervisord.conf",
    ),
    Artifact(
        "supervisor.confd",
        "server",
        "dir",
        needs_root=True,
        server_path="/etc/supervisor/conf.d",
    ),
    Artifact(
        "sudoers.fdm",
        "server",
        "text",
        needs_root=True,
        server_path="/etc/sudoers.d/fdm-platform",
    ),
    Artifact(
        "sudoers.elevation", "server", "absent", needs_root=True, server_path=ELEVATION_DROPIN
    ),
    # -- bench (bench-owner readable; full fidelity) ------------------------ #
    Artifact("common_site_config", "bench", "json"),
    Artifact("nginx_vhosts", "bench", "dir"),
    # -- site (bench-owner readable; secret-stripped; full fidelity) -------- #
    Artifact("site_config", "site", "json"),
)

ARTIFACTS_BY_KEY: dict[str, Artifact] = {a.key: a for a in ARTIFACTS}

# The artefact keys a config-writing template may declare in `writes_config`.
WRITABLE_ARTIFACT_KEYS = frozenset(a.key for a in ARTIFACTS)


def bench_artifact_path(art: Artifact, bench_path: str, site_name: str | None) -> str:
    """Resolve a bench/site-scoped artefact's absolute path."""
    if art.key == "common_site_config":
        return f"{bench_path}/sites/common_site_config.json"
    if art.key == "nginx_vhosts":
        return f"{bench_path}/{VHOSTS_SUBDIR}"
    if art.key == "site_config":
        return f"{bench_path}/sites/{site_name}/site_config.json"
    raise ValueError(f"no bench path for artefact {art.key!r}")


# --------------------------------------------------------------------------- #
# Reading + hashing an artefact over SSH (read-only)                          #
# --------------------------------------------------------------------------- #


@dataclass
class Reading:
    """The result of reading one artefact: present?, its hash, and the
    secret-free content to store (None when content must not be stored)."""

    present: bool
    sha256: str | None
    size: int
    content: str | None


def _cat_argv(path: str, *, needs_root: bool) -> list[str]:
    # Fixed, absolute, single-command reads (rule 1). Root reads route through the
    # `sudo -n /bin/cat <abs path>` lines added to the implementation-plan
    # allowlist; `sudo -n` fails loudly rather than prompting if absent.
    if needs_root:
        return ["sudo", "-n", "/bin/cat", path]
    return ["cat", path]


async def read_artifact(ctx, art: Artifact, path: str) -> Reading:
    """Read + canonicalise + hash one artefact. Pure read; never writes."""
    if art.fmt == "absent":
        # Presence is drift. `sudo -n /bin/cat` exits non-zero when the file is
        # absent (the healthy state) and zero (with content) when it exists.
        res = await ctx.capture(["sudo", "-n", "/bin/cat", path])
        present = res.exit_code == 0
        if not present:
            return Reading(present=False, sha256=_sha256("<absent>"), size=0, content="<absent>")
        # It exists — that IS drift. Do not store the drop-in body.
        return Reading(
            present=True, sha256=_sha256("<present>"), size=len(res.stdout), content=None
        )

    if art.fmt == "dir":
        return await _read_dir(ctx, art, path)

    res = await ctx.capture(_cat_argv(path, needs_root=art.needs_root))
    if res.exit_code != 0:
        # Missing tracked file — a fact, not a swallowed error.
        return Reading(present=False, sha256=None, size=0, content=None)

    raw = res.stdout
    if art.fmt == "json":
        canonical = canonicalize_json(raw)
        if canonical is None:
            # Unparseable JSON: hash raw for detection but never store content.
            return Reading(present=True, sha256=_sha256(raw), size=len(raw), content=None)
        return Reading(
            present=True, sha256=_sha256(canonical), size=len(canonical), content=canonical
        )

    # Plain non-secret text (nginx/supervisor/sudoers).
    return Reading(present=True, sha256=_sha256(raw), size=len(raw), content=raw)


async def _read_dir(ctx, art: Artifact, path: str) -> Reading:
    """Hash a directory of config files.

    Bench-owned dirs (nginx_vhosts) are read at full fidelity: list the files,
    cat each, concatenate in name order. The root-owned supervisor conf.d is
    reduced to a *listing* hash via one fixed `sudo -n find` (no wildcard, no
    per-file root cat) — it detects files added/removed, documented as reduced
    fidelity on the no-sudo target (DOO-128)."""
    if art.needs_root:
        res = await ctx.capture(
            ["sudo", "-n", "/usr/bin/find", path, "-maxdepth", "1",
             "-type", "f", "-printf", "%f\\n"]
        )
        if res.exit_code != 0:
            return Reading(present=False, sha256=None, size=0, content=None)
        names = sorted(n for n in res.stdout.splitlines() if n)
        listing = "\n".join(names) + "\n" if names else ""
        return Reading(present=True, sha256=_sha256(listing), size=len(listing), content=listing)

    # Bench-owned: full content.
    res = await ctx.capture(["find", path, "-maxdepth", "1", "-type", "f"])
    if res.exit_code != 0:
        return Reading(present=False, sha256=None, size=0, content=None)
    files = sorted(f for f in res.stdout.splitlines() if f)
    parts: list[str] = []
    for f in files:
        body = await ctx.capture(["cat", f])
        base = f.rsplit("/", 1)[-1]
        parts.append(f"### {base}\n{body.stdout if body.exit_code == 0 else ''}")
    canonical = "\n".join(parts) + ("\n" if parts else "")
    return Reading(present=True, sha256=_sha256(canonical), size=len(canonical), content=canonical)


def _now() -> datetime:
    return datetime.now(UTC)


# --------------------------------------------------------------------------- #
# DB-facing flows: baseline capture (managed change) + drift check            #
# --------------------------------------------------------------------------- #


@dataclass
class DriftResult:
    """One artefact's drift verdict, returned by `run_drift_check`."""

    artifact_key: str
    path: str
    drifted: bool
    reason: str  # "clean" | "content" | "missing" | "present" | "new-baseline"


def _upsert_baseline(
    db,
    *,
    server_id: int,
    bench_id: int | None,
    site_id: int | None,
    art: Artifact,
    path: str,
    reading: Reading,
    job_id: int | None,
):
    """Create or move a ConfigBaseline row to reflect a managed capture (status
    ``baseline``), clearing any prior drift. Idempotent on the identity tuple."""
    from app.models.drift import ConfigBaseline

    row = _find_baseline(db, server_id, art.key, path)
    if row is None:
        row = ConfigBaseline(server_id=server_id, artifact_key=art.key, path=path)
        db.add(row)
    row.bench_id = bench_id
    row.site_id = site_id
    row.sha256 = reading.sha256
    row.size = reading.size
    row.sanitized_content = reading.content
    row.status = "baseline"
    row.current_sha256 = None
    row.current_content = None
    row.drift_detected_at = None
    row.captured_at = _now()
    row.captured_by_job_id = job_id
    row.last_checked_at = _now()
    return row


def _find_baseline(db, server_id: int, artifact_key: str, path: str):
    from sqlalchemy import select

    from app.models.drift import ConfigBaseline

    return db.scalars(
        select(ConfigBaseline).where(
            ConfigBaseline.server_id == server_id,
            ConfigBaseline.artifact_key == artifact_key,
            ConfigBaseline.path == path,
        )
    ).first()


def _resolve_scope(db, server_id: int, bench_path: str | None, site_name: str | None):
    """Return (bench_row, site_row) for a bench/site artefact, or (None, None)."""
    from sqlalchemy import select

    from app.models.bench import Bench
    from app.models.site import Site

    bench = None
    site = None
    if bench_path:
        bench = db.scalars(
            select(Bench).where(Bench.server_id == server_id, Bench.path == bench_path)
        ).first()
        if bench is not None and site_name:
            site = db.scalars(
                select(Site).where(Site.bench_id == bench.id, Site.name == site_name)
            ).first()
    return bench, site


async def capture_baselines(ctx, artifact_keys: Iterable[str]) -> list[str]:
    """Re-hash the artefacts a just-succeeded managed job wrote and move their
    baselines forward (attributed to the job). Called by the JobRunner after any
    `writes_config` job succeeds, while its SSH session is still open.

    `bench_path` / `site` are read from the job's sanitised params (the same keys
    every bench/site action already carries); server artefacts need neither.
    Returns the artefact keys whose baseline was moved (for the job log)."""
    db = ctx.session
    params = dict(ctx.rendered.params_sanitized or {})
    bench_path = params.get("bench_path")
    site_name = params.get("site")
    bench, site = _resolve_scope(db, ctx.server_id, bench_path, site_name)

    moved: list[str] = []
    for key in artifact_keys:
        art = ARTIFACTS_BY_KEY.get(key)
        if art is None:
            continue
        if art.scope == "server":
            path = art.server_path or ""
            bench_id = site_id = None
        elif art.scope == "bench":
            if bench is None or not bench_path:
                continue
            path = bench_artifact_path(art, bench_path, None)
            bench_id, site_id = bench.id, None
        else:  # site
            if bench is None or site is None or not bench_path or not site_name:
                continue
            path = bench_artifact_path(art, bench_path, site_name)
            bench_id, site_id = bench.id, site.id

        reading = await read_artifact(ctx, art, path)
        _upsert_baseline(
            db,
            server_id=ctx.server_id,
            bench_id=bench_id,
            site_id=site_id,
            art=art,
            path=path,
            reading=reading,
            job_id=ctx.job_id,
        )
        moved.append(key)
    db.commit()
    return moved


def _enumerate_server_artifacts(db, server_id: int):
    """Yield (artifact, path, bench_id, site_id) for every tracked artefact on a
    server: the 5 server-scoped ones, plus each bench's + each site's."""
    from sqlalchemy import select

    from app.models.bench import Bench
    from app.models.site import Site

    for art in ARTIFACTS:
        if art.scope == "server":
            yield art, (art.server_path or ""), None, None

    benches = db.scalars(select(Bench).where(Bench.server_id == server_id)).all()
    for bench in benches:
        for art in ARTIFACTS:
            if art.scope == "bench":
                yield art, bench_artifact_path(art, bench.path, None), bench.id, None
        sites = db.scalars(select(Site).where(Site.bench_id == bench.id)).all()
        for site in sites:
            for art in ARTIFACTS:
                if art.scope == "site":
                    yield art, bench_artifact_path(art, bench.path, site.name), bench.id, site.id


async def run_drift_check(ctx) -> list[DriftResult]:
    """Re-hash every tracked artefact on the job's server and diff against the
    stored baseline. First sighting of an artefact with no baseline *establishes*
    one (reports clean — we cannot know prior state); an existing baseline whose
    hash no longer matches flips to ``drifted``; a previously drifted artefact
    that matches its baseline again self-heals back to ``baseline``.

    Returns one DriftResult per artefact; the action notifies on the drifted
    ones. Pure read on the managed server (rule: drift detection never writes)."""
    from app.models.drift import ConfigBaseline

    db = ctx.session
    results: list[DriftResult] = []
    for art, path, bench_id, site_id in _enumerate_server_artifacts(db, ctx.server_id):
        reading = await read_artifact(ctx, art, path)
        row = _find_baseline(db, ctx.server_id, art.key, path)

        if row is None:
            # First sighting — establish the baseline.
            row = ConfigBaseline(server_id=ctx.server_id, artifact_key=art.key, path=path)
            db.add(row)
            row.bench_id = bench_id
            row.site_id = site_id
            row.captured_at = _now()
            row.captured_by_job_id = ctx.job_id
            row.last_checked_at = _now()

            if art.fmt == "absent":
                # The *desired* state of an absent-artefact is fixed ("<absent>"),
                # so we must never bless an observed presence as the baseline
                # (DOO-405). Pin the baseline to <absent> and, if the file is
                # present right now (e.g. the 2.5 elevation drop-in caught during
                # a rare in-flight `bench setup-production`), flag it as drift on
                # first sight rather than recording a clean baseline that would
                # mask every future reappearance.
                row.sha256 = _sha256("<absent>")
                row.size = 0
                row.sanitized_content = "<absent>"
                if reading.present:
                    row.status = "drifted"
                    row.current_sha256 = reading.sha256
                    row.current_content = reading.content  # None: body never stored
                    row.drift_detected_at = _now()
                    results.append(DriftResult(art.key, path, drifted=True, reason="present"))
                    continue
                row.status = "baseline"
                results.append(DriftResult(art.key, path, drifted=False, reason="new-baseline"))
                continue

            # Every other artefact: baseline whatever we observe (we cannot know
            # the prior intended state), report clean.
            row.sha256 = reading.sha256
            row.size = reading.size
            row.sanitized_content = reading.content
            row.status = "baseline"
            results.append(DriftResult(art.key, path, drifted=False, reason="new-baseline"))
            continue

        row.last_checked_at = _now()
        matches = reading.sha256 == row.sha256
        if matches:
            # Self-heal: a reverted manual edit clears a prior drift flag.
            if row.status == "drifted":
                row.status = "baseline"
                row.current_sha256 = None
                row.current_content = None
                row.drift_detected_at = None
            results.append(DriftResult(art.key, path, drifted=False, reason="clean"))
            continue

        # Mismatch → drift. Snapshot what we found for the drawer.
        row.status = "drifted"
        row.current_sha256 = reading.sha256
        row.current_content = reading.content
        row.drift_detected_at = _now()
        if art.fmt == "absent":
            reason = "present"  # the elevation drop-in reappeared (security)
        elif not reading.present:
            reason = "missing"
        else:
            reason = "content"
        results.append(DriftResult(art.key, path, drifted=True, reason=reason))

    db.commit()
    return results
