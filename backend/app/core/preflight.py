"""Bench-create pre-flight checks (session 1.7).

Before we spend minutes on `bench init`, verify the target can actually host the
selected Frappe version. Each check is a small read-only probe over SSH whose
result is a `CheckResult` (pass / warn / fail + human detail); a *blocking* fail
means `bench init` must not run.

The checks encode the validated gotchas from CLAUDE.md:
- **uv present** (gotcha #2 — the bench CLI runs `uv venv env --seed` even for
  v14/v15, so a missing `uv` is a hard stop for every version).
- **node major matches the matrix** for the chosen version (assets build with
  node; the wrong major is a hard stop).
- **MariaDB version + `innodb_snapshot_isolation`** when ≥ 11.6 (gotcha #5) —
  advisory: `bench init` itself needs no database, but site creation later will,
  so we surface it as a warning rather than blocking the bench.
- **wkhtmltopdf patched-Qt** (gotcha #6) — advisory: PDF printing needs it, the
  bench does not.
- **target ports free vs sibling benches** (gotcha #8) — advisory: a fresh dev
  bench tries to bind the standard ports, which collide if another bench on the
  host already claims them; the operator resolves it before `bench start`.
- **disk space ≥ 5 GB** — a hard stop; a half-cloned bench on a full disk is
  worse than not starting.

Safety (golden rule 1): every probe is a fixed argv of constants; the only
variable input is the bench *path*, validated against the discovery path
allowlist and passed as its own argv element (never spliced into a shell
string). `run_preflight` takes an injected async `capture`, so the whole check
suite is driven by a fake in tests with no SSH.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from app.core.commands.templates import has_dotdot_segment
from app.core.discovery import PATH_RE, DiscoveryError
from app.core.version_matrix import MatrixEntry, get_entry

# A fresh dev bench binds these by default (CLAUDE.md gotcha #3 pins the redis
# ports; the others are bench's conventional defaults). A conflict with a
# sibling bench means the new one won't `bench start` until ports are offset.
DEFAULT_BENCH_PORTS: dict[str, int] = {
    "webserver_port": 8000,
    "socketio_port": 9000,
    "redis_queue_port": 11000,
    "redis_socketio_port": 12000,
    "redis_cache_port": 13000,
    "file_watcher_port": 6787,
}

MIN_DISK_BYTES = 5 * 1024**3  # 5 GB

# MariaDB from this version on defaults `innodb_snapshot_isolation` ON, which
# Frappe needs OFF (gotcha #5).
MARIADB_SNAPSHOT_ISOLATION_FROM = (11, 6)


CheckStatus = str  # "pass" | "warn" | "fail" | "error"


@dataclass
class CheckResult:
    """One pre-flight probe's outcome, ready for the job log and the wizard grid."""

    key: str
    title: str
    status: CheckStatus  # pass | warn | fail | error
    detail: str
    # A blocking check that failed must stop `bench init`. Warnings and passes
    # never block; a non-blocking check never sets status "fail".
    blocking: bool = False

    @property
    def is_blocking_failure(self) -> bool:
        # Only a determinate "the host does not meet this requirement" (status
        # "fail") on a blocking check stops `bench init`. Status "error" means
        # the *probe itself* couldn't read a value (a platform-side read error,
        # not a host shortfall) — it is surfaced distinctly and never blocks, so
        # a fragile probe can't falsely gate a create the way it did on DOO-1189.
        return self.blocking and self.status == "fail"

    def as_dict(self) -> dict[str, object]:
        return {
            "key": self.key,
            "title": self.title,
            "status": self.status,
            "detail": self.detail,
            "blocking": self.blocking,
        }


# --------------------------------------------------------------------------- #
# Fixed probe commands (constants — never built from user input).
# --------------------------------------------------------------------------- #

UV_ARGV = ["uv", "--version"]
NODE_ARGV = ["node", "--version"]
MARIADB_ARGV = ["mariadb", "--version"]
MARIADB_SNAPSHOT_ARGV = [
    "mariadb",
    "-N",
    "-B",
    "-e",
    "SELECT @@global.innodb_snapshot_isolation",
]
WKHTMLTOPDF_ARGV = ["wkhtmltopdf", "--version"]


def login_shell(argv: list[str]) -> list[str]:
    """Wrap a fixed tool probe so it runs in the bench user's **login** shell.

    An SSH `exec` request (`conn.run`) runs a *non-login, non-interactive* shell,
    which never sources `~/.profile`/`~/.bash_profile`/`~/.bashrc`. nvm, pyenv and
    a user-local `uv`/`~/.local/bin` all put their binaries on `PATH` from those
    files, so a bare `node --version` over SSH reads the stale *system* Node
    instead of the nvm-managed one — the exact false "Node NN is outside the
    matrix" block that stopped `bench init` on DOO-1189.

    Safety (golden rule 1): `argv` is a developer-authored constant here (or a
    validated path already quoted by `shlex.join`), and the whole joined command
    is passed as a single `-c` argument, so no user input is interpolated and no
    metacharacter can escape its element.
    """
    return ["bash", "-lc", shlex.join(argv)]


def disk_argv(path: str) -> list[str]:
    """`df` on the bench's parent dir. Path is validated (absolute allowlist)
    and passed as its own argv element.

    Uses POSIX `df -Pk` (portable, one data row per filesystem, sizes in 1 KiB
    units) rather than the GNU-only `--output=avail`, so the probe reads on the
    widest range of hosts (DOO-1189: the old form returned nothing on the target
    and the read-failure was mis-rendered as a host shortfall)."""
    if not PATH_RE.match(path):
        raise DiscoveryError(f"path {path!r} is not a valid absolute path")
    if has_dotdot_segment(path):
        raise DiscoveryError(f"path {path!r} must not contain '..' segments")
    return ["df", "-Pk", path]


# --------------------------------------------------------------------------- #
# Pure parsers
# --------------------------------------------------------------------------- #


def parse_version_tuple(text: str) -> tuple[int, ...] | None:
    """Pull the first dotted-number token out of a `--version` line as ints."""
    m = re.search(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?", text)
    if not m:
        return None
    return tuple(int(g) for g in m.groups() if g is not None)


def parse_mariadb_server_version(text: str) -> tuple[int, ...] | None:
    """Extract the **server** version from `mariadb --version`.

    The client tool prints *its own* version first (`Ver 15.1` / `client 15.2`)
    and the server it was built for as a `Distrib <v>` or `from <v>` token — that
    token is the MariaDB *server* version the gotcha-#5 gate (`innodb_snapshot_
    isolation` on ≥ 11.6) must be evaluated against. Grabbing the first number
    instead reads the client version — a false green that let an 11.8 server pass
    unchecked on DOO-1189. Falls back to the first dotted token when neither
    marker is present (older/odd builds)."""
    m = re.search(r"(?:Distrib|from)\s+(\d+(?:\.\d+){0,2})", text)
    if m:
        return tuple(int(x) for x in m.group(1).split("."))
    return parse_version_tuple(text)


def parse_node_major(text: str) -> int | None:
    """Node prints `v24.1.0`; return the major, or None."""
    m = re.search(r"v?(\d+)\.\d+", text)
    return int(m.group(1)) if m else None


def wkhtmltopdf_is_patched(text: str) -> bool:
    """The Frappe-required build reports `(with patched qt)` (gotcha #6)."""
    return "with patched qt" in text.lower()


def parse_snapshot_isolation(text: str) -> bool | None:
    """`SELECT @@global.innodb_snapshot_isolation` prints ON/OFF or 1/0. Return
    True if ON, False if OFF, None if the value couldn't be read."""
    token = text.strip().splitlines()[0].strip().upper() if text.strip() else ""
    if token in ("ON", "1"):
        return True
    if token in ("OFF", "0"):
        return False
    return None


def parse_df_avail_bytes(text: str) -> int | None:
    """`df -Pk` prints a header row then one data row per filesystem; the
    Available column is POSIX field 4 (index 3), in 1 KiB units. Return it in
    bytes from the last data row, or None if nothing parseable is present (a
    read error → surfaced as status "error", never a host shortfall)."""
    for line in reversed(text.strip().splitlines()):
        parts = line.split()
        if len(parts) >= 4 and parts[3].isdigit():
            return int(parts[3]) * 1024
    return None


def _ge(version: tuple[int, ...], floor: tuple[int, ...]) -> bool:
    return version >= floor


# --------------------------------------------------------------------------- #
# Individual checks (each returns one CheckResult), given collected output.
# --------------------------------------------------------------------------- #


def evaluate_uv(exit_code: int, stdout: str, stderr: str) -> CheckResult:
    version = parse_version_tuple(stdout) if exit_code == 0 else None
    if exit_code == 0 and version is not None:
        return CheckResult(
            "uv", "uv installed", "pass",
            f"uv {'.'.join(map(str, version))} present.", blocking=True,
        )
    return CheckResult(
        "uv", "uv installed", "fail",
        "uv not found. The bench CLI runs `uv venv env --seed` during "
        "`bench init` for every version (gotcha #2) — install uv on the target "
        "before creating a bench.",
        blocking=True,
    )


def evaluate_node(exit_code: int, stdout: str, entry: MatrixEntry) -> CheckResult:
    major = parse_node_major(stdout) if exit_code == 0 else None
    want = entry.node_display
    if major is None:
        return CheckResult(
            "node", "Node.js version", "fail",
            f"node not found. Frappe v{entry.major} needs Node {want} to build "
            "assets during `bench init`.",
            blocking=True,
        )
    if entry.node_min_major <= major <= entry.node_max_major:
        return CheckResult(
            "node", "Node.js version", "pass",
            f"Node {major} matches the v{entry.major} matrix ({want}).",
            blocking=True,
        )
    return CheckResult(
        "node", "Node.js version", "fail",
        f"Node {major} is outside the v{entry.major} matrix ({want}). Install a "
        f"matching Node major before creating this bench.",
        blocking=True,
    )


def evaluate_mariadb(
    exit_code: int, stdout: str, snapshot: bool | None
) -> CheckResult:
    if exit_code != 0:
        return CheckResult(
            "mariadb", "MariaDB", "warn",
            "MariaDB client not found. Not needed for `bench init`, but site "
            "creation will need a running MariaDB ≥ 10.6.",
        )
    # The *server* version (Distrib/from token), never the client tool version.
    version = parse_mariadb_server_version(stdout)
    vtext = ".".join(map(str, version)) if version else "unknown"
    if version and _ge(version[:2], MARIADB_SNAPSHOT_ISOLATION_FROM):
        if snapshot is True:
            return CheckResult(
                "mariadb", "MariaDB", "warn",
                f"MariaDB {vtext} ≥ 11.6 with innodb_snapshot_isolation = ON. "
                "Frappe needs it OFF (gotcha #5) — set it before creating sites.",
            )
        if snapshot is False:
            return CheckResult(
                "mariadb", "MariaDB", "pass",
                f"MariaDB {vtext}, innodb_snapshot_isolation already OFF.",
            )
        return CheckResult(
            "mariadb", "MariaDB", "warn",
            f"MariaDB {vtext} ≥ 11.6. Could not read innodb_snapshot_isolation — "
            "verify it is OFF before creating sites (gotcha #5).",
        )
    return CheckResult(
        "mariadb", "MariaDB", "pass",
        f"MariaDB {vtext} (below 11.6 — no snapshot-isolation change needed).",
    )


def evaluate_wkhtmltopdf(exit_code: int, stdout: str) -> CheckResult:
    if exit_code != 0:
        return CheckResult(
            "wkhtmltopdf", "wkhtmltopdf (patched Qt)", "warn",
            "wkhtmltopdf not found. Needed for PDF printing (not for `bench "
            "init`) — install the patched-Qt 0.12.6.1 build (gotcha #6).",
        )
    if wkhtmltopdf_is_patched(stdout):
        return CheckResult(
            "wkhtmltopdf", "wkhtmltopdf (patched Qt)", "pass",
            "Patched-Qt wkhtmltopdf present.",
        )
    return CheckResult(
        "wkhtmltopdf", "wkhtmltopdf (patched Qt)", "warn",
        "wkhtmltopdf is installed but not the patched-Qt build. Frappe PDFs need "
        "the patched-Qt 0.12.6.1 build (gotcha #6).",
    )


def evaluate_ports(sibling_ports: set[int]) -> CheckResult:
    """Compare the default dev-bench port set against ports already claimed by
    sibling benches on this server (gotcha #8)."""
    conflicts = {
        name: port
        for name, port in DEFAULT_BENCH_PORTS.items()
        if port in sibling_ports
    }
    if not conflicts:
        return CheckResult(
            "ports", "Ports free", "pass",
            "Default bench ports (8000/9000/11000-13000/6787) are free on this "
            "server.",
        )
    listed = ", ".join(f"{name.replace('_', ' ')} {port}" for name, port in conflicts.items())
    return CheckResult(
        "ports", "Ports free", "warn",
        f"A sibling bench already uses: {listed}. The new bench will need its "
        "ports offset before `bench start` (gotcha #8).",
    )


def evaluate_disk(exit_code: int, stdout: str) -> CheckResult:
    avail = parse_df_avail_bytes(stdout) if exit_code == 0 else None
    if avail is None:
        # A probe read-error, NOT a host shortfall — status "error" so the UI
        # shows "couldn't check" rather than "FAIL · blocks init". This never
        # blocks `bench init` (DOO-1189: a fragile df probe was falsely gating
        # creates and sending operators to chase disk problems that didn't exist).
        return CheckResult(
            "disk", "Disk space", "error",
            "Could not read available disk space for the target path — this is a "
            "platform probe error, not a host shortfall. Check that `df` works on "
            "the path (or verify free space manually) and re-run pre-flight.",
            blocking=True,
        )
    gb = avail / 1024**3
    if avail >= MIN_DISK_BYTES:
        return CheckResult(
            "disk", "Disk space", "pass",
            f"{gb:.1f} GB free (≥ 5 GB required).", blocking=True,
        )
    return CheckResult(
        "disk", "Disk space", "fail",
        f"Only {gb:.1f} GB free — a bench needs at least 5 GB.", blocking=True,
    )


# --------------------------------------------------------------------------- #
# Orchestrator: run every probe over an injected capture.
# --------------------------------------------------------------------------- #

Capture = Callable[..., Awaitable["CaptureLike"]]


class CaptureLike:
    """Structural type: what `capture(argv)` resolves to (exit_code/stdout/stderr)."""

    exit_code: int
    stdout: str
    stderr: str


@dataclass
class PreflightReport:
    """The full result set plus a rolled-up verdict."""

    checks: list[CheckResult] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return any(c.is_blocking_failure for c in self.checks)

    @property
    def has_warnings(self) -> bool:
        return any(c.status == "warn" for c in self.checks)

    @property
    def has_errors(self) -> bool:
        """A probe couldn't read a value it needed (platform-side read error).
        Distinct from `blocked` (a host requirement is unmet) — surfaced so the
        wizard can say "couldn't check" instead of "your host failed"."""
        return any(c.status == "error" for c in self.checks)

    def as_dict(self) -> dict[str, object]:
        return {
            "blocked": self.blocked,
            "has_warnings": self.has_warnings,
            "has_errors": self.has_errors,
            "checks": [c.as_dict() for c in self.checks],
        }


async def run_preflight(
    capture: Capture,
    *,
    frappe_major: str,
    path: str,
    sibling_ports: set[int],
    on_progress: Callable[[CheckResult], Awaitable[None] | None] | None = None,
) -> PreflightReport:
    """Run every check for `frappe_major` against the target and return the
    report. `capture(argv)` runs a fixed argv and yields (exit_code, stdout,
    stderr); `sibling_ports` is the union of ports already used by other benches
    on the server (from the platform's discovery inventory)."""
    entry = get_entry(frappe_major)
    report = PreflightReport()

    async def record(result: CheckResult) -> None:
        report.checks.append(result)
        if on_progress is not None:
            maybe = on_progress(result)
            if maybe is not None:
                await maybe

    # Toolchain probes run through the bench user's login shell so nvm/pyenv/uv
    # PATH entries are loaded (DOO-1189); `df` is a system binary and runs direct.
    uv = await capture(login_shell(UV_ARGV))
    await record(evaluate_uv(uv.exit_code, uv.stdout, uv.stderr))

    node = await capture(login_shell(NODE_ARGV))
    await record(evaluate_node(node.exit_code, node.stdout, entry))

    maria = await capture(login_shell(MARIADB_ARGV))
    snapshot: bool | None = None
    version = (
        parse_mariadb_server_version(maria.stdout) if maria.exit_code == 0 else None
    )
    if version and _ge(version[:2], MARIADB_SNAPSHOT_ISOLATION_FROM):
        try:
            snap = await capture(login_shell(MARIADB_SNAPSHOT_ARGV), timeout=15.0)
            snapshot = parse_snapshot_isolation(snap.stdout) if snap.exit_code == 0 else None
        except Exception:
            snapshot = None  # best-effort; falls back to the advisory branch
    await record(evaluate_mariadb(maria.exit_code, maria.stdout, snapshot))

    wk = await capture(login_shell(WKHTMLTOPDF_ARGV))
    await record(evaluate_wkhtmltopdf(wk.exit_code, wk.stdout))

    await record(evaluate_ports(sibling_ports))

    disk = await capture(disk_argv(path))
    await record(evaluate_disk(disk.exit_code, disk.stdout))

    return report


def sibling_ports_from_benches(benches) -> set[int]:
    """Collect every non-null port across a server's discovered benches, for the
    ports conflict check. `benches` is an iterable of Bench rows."""
    ports: set[int] = set()
    for b in benches:
        for value in (
            b.webserver_port,
            b.socketio_port,
            b.redis_cache_port,
            b.redis_queue_port,
            b.redis_socketio_port,
            b.file_watcher_port,
        ):
            if value:
                ports.add(int(value))
    return ports
