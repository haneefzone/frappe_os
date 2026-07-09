"""Bench discovery (session 1.6).

Walks a server over SSH looking for plain `bench init` installs — a directory
that holds `apps/frappe` and `sites/` — reads each one's port map from
`sites/common_site_config.json`, runs `bench version` inside it, and upserts a
`Bench` row per install. Benches that were known but whose directory has
vanished are marked `status="missing"` rather than deleted.

Safety (CLAUDE.md golden rule 1): every remote command is a fixed shell-script
*constant*; the only variable inputs are directory paths, which are passed as
separate argv elements (never interpolated into the script) and validated
against an absolute-path allowlist first. The scripts are strictly read-only —
`ls`, `cat`, `test`, and `bench version`.

The SSH/parse half (`gather`) and the DB half (`persist`) are split so each is
independently unit-testable; the parsing helpers are pure functions.
"""

from __future__ import annotations

import json
import posixpath
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.commands.templates import has_dotdot_segment
from app.models.bench import Bench

# Base paths scanned in addition to the SSH user's home (`$HOME`, resolved on
# the server). Callers may override per discovery request.
DEFAULT_BASE_PATHS: tuple[str, ...] = ("/home", "/opt", "/srv")

# A candidate directory path: absolute, and only characters that cannot carry
# shell metacharacters even though we never build a shell string from them.
PATH_RE = re.compile(r"^/[A-Za-z0-9._/-]{0,300}$")

# Marker lines used by the inspect script to delimit each captured section.
_SECTIONS = ("COMMON_SITE_CONFIG", "BENCH_VERSION", "PYTHON", "NODE")

# Lists candidate bench dirs. Roots arrive as "$@" (argv after the `_` sentinel),
# never spliced into the script. For each root we consider the root itself and
# its immediate children; a bench is a dir with both apps/frappe and sites/.
INVENTORY_SCRIPT = r"""
set -u
emit_bench() {
  d="$1"
  if [ -d "$d/apps/frappe" ] && [ -d "$d/sites" ]; then
    printf 'BENCH\t%s\n' "$d"
  fi
}
scan() {
  root="$1"
  [ -d "$root" ] || return 0
  emit_bench "$root"
  for d in "$root"/*; do
    [ -d "$d" ] && emit_bench "$d"
  done
}
scan "$HOME"
for r in "$@"; do scan "$r"; done
"""

# Inspects a single bench. Path arrives as "$1" (argv), never spliced in.
INSPECT_SCRIPT = r"""
set -u
b="$1"
if [ -f "$b/config/supervisor.conf" ] || [ -f "$b/config/systemd/frappe-web.service" ]; then
  echo "PROD=1"
else
  echo "PROD=0"
fi
echo "---COMMON_SITE_CONFIG---"
cat "$b/sites/common_site_config.json" 2>/dev/null || true
echo "---BENCH_VERSION---"
(cd "$b" && bench version --format json 2>/dev/null) || true
echo "---PYTHON---"
"$b/env/bin/python" --version 2>&1 || true
echo "---NODE---"
(cd "$b" && node --version 2>&1) || true
echo "---END---"
"""

# Fallback for older bench CLIs that don't accept `--format json`.
BENCH_VERSION_PLAIN_ARGV = ("bench", "version")


class DiscoveryError(ValueError):
    """A base path failed the absolute-path allowlist. Maps to HTTP 422."""


@dataclass
class BenchInfo:
    """Everything discovery learned about one bench, ready to upsert."""

    path: str
    name: str
    is_production: bool = False
    frappe_version: str | None = None
    python_version: str | None = None
    node_version: str | None = None
    ports: dict[str, int | None] = field(default_factory=dict)


@dataclass
class DiscoverySummary:
    """What a discovery run changed, for the job log + API response."""

    added: int = 0
    updated: int = 0
    missing: int = 0
    total_active: int = 0


# --------------------------------------------------------------------------- #
# Command building + validation
# --------------------------------------------------------------------------- #


def validate_base_paths(base_paths: list[str] | None) -> list[str]:
    """Return the validated, de-duplicated base paths (defaults if none given).

    Raises DiscoveryError on anything that isn't a clean absolute path so a bad
    value is rejected at the API boundary, never handed to the server."""
    paths = base_paths if base_paths else list(DEFAULT_BASE_PATHS)
    seen: list[str] = []
    for raw in paths:
        p = (raw or "").strip().rstrip("/") or "/"
        if not PATH_RE.match(p):
            raise DiscoveryError(f"base path {raw!r} is not a valid absolute path")
        if has_dotdot_segment(p):
            raise DiscoveryError(f"base path {raw!r} must not contain '..' segments")
        if p not in seen:
            seen.append(p)
    return seen


def build_inventory_argv(base_paths: list[str]) -> list[str]:
    # `_` is the conventional $0 placeholder; the roots become "$@" in the script.
    return ["bash", "-c", INVENTORY_SCRIPT, "_", *base_paths]


def build_inspect_argv(bench_path: str) -> list[str]:
    if not PATH_RE.match(bench_path):
        raise DiscoveryError(f"bench path {bench_path!r} is not a valid absolute path")
    if has_dotdot_segment(bench_path):
        raise DiscoveryError(f"bench path {bench_path!r} must not contain '..' segments")
    return ["bash", "-c", INSPECT_SCRIPT, "_", bench_path]


# --------------------------------------------------------------------------- #
# Parsing (pure)
# --------------------------------------------------------------------------- #


def parse_inventory(stdout: str) -> list[str]:
    """Pull the `BENCH\\t<path>` lines out of the inventory output, de-duped and
    order-stable (a path can surface under both $HOME and an explicit root)."""
    out: list[str] = []
    for line in stdout.splitlines():
        if line.startswith("BENCH\t"):
            path = line.split("\t", 1)[1].strip().rstrip("/")
            if path and path not in out:
                out.append(path)
    return out


def _split_sections(stdout: str) -> dict[str, str]:
    """Split the inspect output into its `---NAME---` delimited sections plus the
    leading PROD flag."""
    sections: dict[str, str] = {}
    current = "HEAD"
    buf: list[str] = []
    for line in stdout.splitlines():
        m = re.fullmatch(r"---([A-Z_]+)---", line.strip())
        if m:
            sections[current] = "\n".join(buf)
            current = m.group(1)
            buf = []
        else:
            buf.append(line)
    sections[current] = "\n".join(buf)
    return sections


def _port_from(value: object) -> int | None:
    """A common_site_config value may be a bare int port or a `redis://host:port`
    URL; return the port either way, or None."""
    if isinstance(value, bool):  # bool is an int subclass — exclude it
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        m = re.search(r":(\d{2,5})(?:/\d+)?/?$", value.strip())
        if m:
            return int(m.group(1))
        if value.strip().isdigit():
            return int(value.strip())
    return None


def parse_common_site_config(text: str) -> dict[str, int | None]:
    """Extract the six ports the Bench model tracks from common_site_config.json.
    Missing/invalid config yields all-None (still a valid discovery result)."""
    ports: dict[str, int | None] = {
        "webserver_port": None,
        "socketio_port": None,
        "redis_cache_port": None,
        "redis_queue_port": None,
        "redis_socketio_port": None,
        "file_watcher_port": None,
    }
    try:
        cfg = json.loads(text) if text.strip() else {}
    except (json.JSONDecodeError, ValueError):
        return ports
    if not isinstance(cfg, dict):
        return ports
    ports["webserver_port"] = _port_from(cfg.get("webserver_port"))
    ports["socketio_port"] = _port_from(cfg.get("socketio_port"))
    ports["file_watcher_port"] = _port_from(cfg.get("file_watcher_port"))
    ports["redis_cache_port"] = _port_from(cfg.get("redis_cache"))
    ports["redis_queue_port"] = _port_from(cfg.get("redis_queue"))
    # Frappe historically shares the queue Redis for socketio; prefer an explicit
    # redis_socketio if present, else fall back to redis_queue.
    ports["redis_socketio_port"] = _port_from(
        cfg.get("redis_socketio", cfg.get("redis_queue"))
    )
    return ports


def parse_bench_version(text: str) -> str | None:
    """Frappe's version from `bench version --format json`, or None.

    The JSON form is a list of `{"name","version"}` (newer bench) or a mapping
    of name->version; older CLIs ignore the flag and print plain lines, which
    `parse_bench_version_plain` handles as the fallback."""
    text = text.strip()
    if not text:
        return None
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return parse_bench_version_plain(text)
    if isinstance(data, dict):
        version = data.get("frappe")
        return str(version) if version else None
    if isinstance(data, list):
        for entry in data:
            if isinstance(entry, dict) and entry.get("name") == "frappe":
                version = entry.get("version")
                return str(version) if version else None
    return None


def parse_bench_version_plain(text: str) -> str | None:
    """`bench version` plain output: lines like `frappe 15.42.1`."""
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "frappe":
            return parts[1]
    return None


def _first_version_token(text: str) -> str | None:
    """Pull an `X.Y.Z`-ish token out of a `--version` line."""
    m = re.search(r"\d+\.\d+(?:\.\d+)?", text)
    return m.group(0) if m else None


def parse_inspect(stdout: str, bench_path: str) -> BenchInfo:
    """Turn one bench's inspect output into a BenchInfo."""
    sections = _split_sections(stdout)
    head = sections.get("HEAD", "")
    info = BenchInfo(
        path=bench_path,
        name=posixpath.basename(bench_path.rstrip("/")) or bench_path,
        is_production="PROD=1" in head,
    )
    info.ports = parse_common_site_config(sections.get("COMMON_SITE_CONFIG", ""))
    info.frappe_version = parse_bench_version(sections.get("BENCH_VERSION", ""))
    info.python_version = _first_version_token(sections.get("PYTHON", ""))
    info.node_version = _first_version_token(sections.get("NODE", ""))
    return info


# --------------------------------------------------------------------------- #
# Gather (SSH) — uses an injected async `capture` so it's fake-driven in tests.
# --------------------------------------------------------------------------- #

Capture = Callable[..., Awaitable["CaptureLike"]]


class CaptureLike:
    """Structural type for a capture result (exit_code, stdout, stderr)."""

    exit_code: int
    stdout: str
    stderr: str


async def gather(
    capture: Capture,
    base_paths: list[str],
    *,
    on_progress: Callable[[str], Awaitable[None] | None] | None = None,
) -> list[BenchInfo]:
    """Run the inventory + per-bench inspection over an injected `capture`
    (argv -> CaptureResult). Returns one BenchInfo per bench found."""

    async def note(msg: str) -> None:
        if on_progress is not None:
            result = on_progress(msg)
            if result is not None:
                await result

    inv = await capture(build_inventory_argv(base_paths))
    paths = parse_inventory(inv.stdout)
    await note(f"Found {len(paths)} candidate bench(es).")

    infos: list[BenchInfo] = []
    for path in paths:
        res = await capture(build_inspect_argv(path))
        info = parse_inspect(res.stdout, path)
        # Fallback: older bench CLIs reject --format json, so retry plain.
        if info.frappe_version is None:
            plain = await capture(list(BENCH_VERSION_PLAIN_ARGV), cwd=path)
            info.frappe_version = parse_bench_version_plain(plain.stdout)
        infos.append(info)
        await note(
            f"{info.name}: frappe {info.frappe_version or 'unknown'}"
            f"{' (production)' if info.is_production else ''}"
        )
    return infos


# --------------------------------------------------------------------------- #
# Persist (DB) — pure upsert + vanish marking.
# --------------------------------------------------------------------------- #


def _apply_info(bench: Bench, info: BenchInfo, now: datetime) -> None:
    """Copy a discovered BenchInfo onto a Bench row (shared by the full-scan
    `persist` and the single-bench `upsert_one`)."""
    bench.name = info.name
    bench.is_production = info.is_production
    bench.frappe_version = info.frappe_version
    bench.python_version = info.python_version
    bench.node_version = info.node_version
    bench.webserver_port = info.ports.get("webserver_port")
    bench.socketio_port = info.ports.get("socketio_port")
    bench.redis_cache_port = info.ports.get("redis_cache_port")
    bench.redis_queue_port = info.ports.get("redis_queue_port")
    bench.redis_socketio_port = info.ports.get("redis_socketio_port")
    bench.file_watcher_port = info.ports.get("file_watcher_port")
    bench.status = "active"
    bench.discovered_at = now


def upsert_one(
    db: Session, server_id: int, info: BenchInfo, *, now: datetime | None = None
) -> Bench:
    """Insert or refresh a single bench row keyed on (server_id, path) WITHOUT
    the vanish pass — used to register a bench the platform just created
    (session 1.7). Unlike `persist`, it never touches sibling benches, so
    registering one newly-created bench cannot mark the others missing."""
    now = now or datetime.now(UTC)
    bench = db.scalars(
        select(Bench).where(Bench.server_id == server_id, Bench.path == info.path)
    ).first()
    if bench is None:
        bench = Bench(server_id=server_id, path=info.path)
        db.add(bench)
    _apply_info(bench, info, now)
    db.commit()
    db.refresh(bench)
    return bench


def persist(
    db: Session,
    server_id: int,
    infos: list[BenchInfo],
    *,
    now: datetime | None = None,
) -> DiscoverySummary:
    """Upsert one row per discovered bench (keyed on server_id+path) and mark any
    previously-active bench that was NOT rediscovered as `missing`."""
    now = now or datetime.now(UTC)
    summary = DiscoverySummary()

    existing = {
        b.path: b
        for b in db.scalars(select(Bench).where(Bench.server_id == server_id)).all()
    }
    seen_paths: set[str] = set()

    for info in infos:
        seen_paths.add(info.path)
        bench = existing.get(info.path)
        if bench is None:
            bench = Bench(server_id=server_id, path=info.path)
            db.add(bench)
            summary.added += 1
        else:
            summary.updated += 1
        _apply_info(bench, info, now)

    for path, bench in existing.items():
        if path not in seen_paths and bench.status != "missing":
            bench.status = "missing"
            summary.missing += 1

    summary.total_active = len(seen_paths)
    db.commit()
    return summary
