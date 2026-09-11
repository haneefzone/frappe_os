"""AsyncSSH connection service: known-host pinning, a small connection pool,
safe fixed-argv command execution, and a structured connection test.

Security invariants (CLAUDE.md golden rule 1 + rule 6):
- Every remote command is a fixed argv list of constants — no user input is
  ever interpolated. Arguments are joined with shlex.quote before hitting the
  remote shell purely as belt-and-braces.
- Secret material (private key, passphrase, password) is decrypted from Fernet
  tokens only in memory, only to hand to AsyncSSH, and is never logged.
- Host keys are pinned trust-on-first-use: the key seen on the first connect is
  stored; any later connect presenting a different key is rejected before a
  single command runs.
"""

from __future__ import annotations

import asyncio
import hmac
import shlex
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import asyncssh

from app.config import get_settings
from app.core.security import SecretsService
from app.models import Server, SSHCredential

# Tool -> fixed argv used to detect presence + version. No user input, ever.
TOOL_COMMANDS: dict[str, list[str]] = {
    "git": ["git", "--version"],
    "python3": ["python3", "--version"],
    "uv": ["uv", "--version"],
    "node": ["node", "--version"],
    "mariadb": ["mariadb", "--version"],
    "redis-server": ["redis-server", "--version"],
    "wkhtmltopdf": ["wkhtmltopdf", "--version"],
    "bench": ["bench", "--version"],
}

Emit = Callable[[dict], Awaitable[None]]


class HostKeyMismatch(Exception):
    """The server presented a host key that differs from the pinned one."""


class SessionPoolTimeout(Exception):
    """No SSH session slot became free on a server within the configured wait.

    This is backpressure, not a crash: the host is at its concurrent-session cap
    and the caller waited (queued) up to the acquire timeout without a slot
    freeing. Raising here fails one operation cleanly instead of piling more
    channels onto an already-saturated host or hanging forever.
    """


@dataclass
class CommandOutput:
    exit_status: int
    stdout: str
    stderr: str


@dataclass
class ConnectionCheck:
    """Structured result of a server connection test. `host_key` carries the
    key seen on this connect so a first-time caller can persist it."""

    ssh_ok: bool = False
    whoami: str | None = None
    sudo_ok: bool = False
    lsb_release: str | None = None
    tools: dict[str, str | None] = field(default_factory=dict)
    host_key: str | None = None
    error: str | None = None


@asynccontextmanager
async def _null_slot():
    """A no-op session slot for connections opened outside server accounting."""
    yield


def normalize_host_key(openssh_line: str) -> str:
    """Reduce an OpenSSH public-key line to its comparable core: '<type> <base64>'
    (drops any trailing comment/whitespace) so pinning compares stably."""
    parts = openssh_line.strip().split()
    return " ".join(parts[:2]) if len(parts) >= 2 else openssh_line.strip()


def host_key_string(conn: asyncssh.SSHClientConnection) -> str:
    """OpenSSH one-line form of the server host key AsyncSSH negotiated."""
    key = conn.get_server_host_key()
    return normalize_host_key(key.export_public_key(format_name="openssh").decode())


class SSHService:
    """Pooled AsyncSSH access to managed servers.

    `connector` is injectable so tests can drive the whole flow with a fake
    connection; production uses asyncssh.connect.
    """

    def __init__(
        self,
        secrets: SecretsService,
        connector: Callable[..., Awaitable[asyncssh.SSHClientConnection]] = asyncssh.connect,
        *,
        max_sessions_per_server: int | None = None,
        acquire_timeout: float | None = None,
    ) -> None:
        self._secrets = secrets
        self._connector = connector
        self._pool: dict[int, asyncssh.SSHClientConnection] = {}
        settings = get_settings()
        self._default_limit = (
            max_sessions_per_server
            if max_sessions_per_server is not None
            else settings.ssh_max_sessions_per_server
        )
        self._acquire_timeout = (
            acquire_timeout
            if acquire_timeout is not None
            else settings.ssh_session_acquire_timeout_seconds
        )
        # Per-server session accounting (session 2.6). The semaphore caps how many
        # AsyncSSH channels run at once on one host; `_active`/`_peak` expose the
        # live/high-water counts for the per-server dashboard + tests.
        self._sems: dict[int, asyncio.Semaphore] = {}
        self._limits: dict[int, int] = {}
        self._active: dict[int, int] = {}
        self._peak: dict[int, int] = {}

    # -- connection-pool limits (session 2.6) --------------------------------

    def pool_stats(self, server_id: int) -> dict[str, int]:
        """Live pool accounting for one server: the cap, the number of sessions
        in flight right now, and the high-water mark seen. Used by the per-server
        dashboard rollup and asserted in the pool-limit tests."""
        return {
            "limit": self._limits.get(server_id, self._default_limit),
            "active": self._active.get(server_id, 0),
            "peak": self._peak.get(server_id, 0),
        }

    @asynccontextmanager
    async def limit_sessions(self, server_id: int, limit: int | None = None):
        """Acquire one session slot on `server_id`, queuing (waiting) if the host
        is at its cap and raising SessionPoolTimeout only if no slot frees within
        the acquire timeout. The semaphore is created lazily on first use and its
        size is fixed for the service's lifetime (a later per-server override
        takes effect on the next fresh SSHService)."""
        sem = self._sems.get(server_id)
        if sem is None:
            effective = self._default_limit if limit is None else limit
            effective = max(1, int(effective))
            sem = asyncio.Semaphore(effective)
            self._sems[server_id] = sem
            self._limits[server_id] = effective
        try:
            await asyncio.wait_for(sem.acquire(), timeout=self._acquire_timeout)
        except TimeoutError as exc:
            raise SessionPoolTimeout(
                f"No free SSH session slot on server {server_id} within "
                f"{self._acquire_timeout:.0f}s (cap {self._limits[server_id]}). "
                "The host is at its concurrent-session limit; the operation was "
                "not run — retry once current work drains."
            ) from exc
        self._active[server_id] = self._active.get(server_id, 0) + 1
        self._peak[server_id] = max(self._peak.get(server_id, 0), self._active[server_id])
        try:
            yield
        finally:
            self._active[server_id] -= 1
            sem.release()

    def _session_slot(self, conn: asyncssh.SSHClientConnection):
        """Session-slot context for a pooled connection, keyed on the server id
        stashed at open time. A bare connection with no id (e.g. a hand-built
        test conn) runs unmetered."""
        server_id = getattr(conn, "_fdm_server_id", None)
        if server_id is None:
            return _null_slot()
        limit = getattr(conn, "_fdm_session_limit", None)
        return self.limit_sessions(server_id, limit)

    # -- connection management ------------------------------------------------

    def _connect_options(self, server: Server, cred: SSHCredential) -> dict:
        # known_hosts=None: we do our own trust-on-first-use pinning below,
        # comparing the presented key against the stored one and rejecting the
        # connection before running anything if it differs.
        options: dict = {
            "host": server.hostname,
            "port": server.ssh_port,
            "username": cred.username,
            "known_hosts": None,
        }
        if cred.auth_type == "password":
            options["password"] = self._secrets.decrypt(cred.password_enc or "")
        else:
            passphrase = self._secrets.decrypt(cred.passphrase_enc) if cred.passphrase_enc else None
            private_key = asyncssh.import_private_key(
                self._secrets.decrypt(cred.private_key_enc or ""), passphrase
            )
            options["client_keys"] = [private_key]
        return options

    async def _open(self, server: Server, cred: SSHCredential) -> asyncssh.SSHClientConnection:
        """Open a fresh connection and enforce host-key pinning."""
        conn = await self._connector(**self._connect_options(server, cred))
        presented = host_key_string(conn)
        if cred.known_host_key:
            if not hmac.compare_digest(
                normalize_host_key(cred.known_host_key), presented
            ):
                conn.close()
                raise HostKeyMismatch(
                    f"Host key for {server.hostname} does not match the pinned key. "
                    "If you did not rebuild this server, stop and investigate a "
                    "possible man-in-the-middle."
                )
        # Stash what we saw so the caller can pin it on first connect.
        conn._fdm_host_key = presented  # type: ignore[attr-defined]
        # Stash the server identity + its session cap so every session opened on
        # this connection is metered against the right per-server semaphore (2.6).
        conn._fdm_server_id = server.id  # type: ignore[attr-defined]
        conn._fdm_session_limit = server.ssh_pool_limit or self._default_limit  # type: ignore[attr-defined]
        return conn

    async def connect(self, server: Server, cred: SSHCredential) -> asyncssh.SSHClientConnection:
        """Return a pooled live connection, opening one if needed."""
        existing = self._pool.get(server.id)
        if existing is not None and not existing.is_closed():
            return existing
        conn = await self._open(server, cred)
        self._pool[server.id] = conn
        return conn

    async def run(
        self,
        conn: asyncssh.SSHClientConnection,
        argv: list[str],
        cwd: str | None = None,
        timeout: float = 30.0,
    ) -> CommandOutput:
        """Run a fixed argv on an open connection. Never raises on a non-zero
        exit; the caller inspects exit_status."""
        command = shlex.join(argv)
        if cwd is not None:
            command = f"cd {shlex.quote(cwd)} && {command}"
        async with self._session_slot(conn):
            result = await conn.run(command, check=False, timeout=timeout)
        return CommandOutput(
            exit_status=result.exit_status if result.exit_status is not None else -1,
            stdout=(result.stdout or "") if isinstance(result.stdout, str) else "",
            stderr=(result.stderr or "") if isinstance(result.stderr, str) else "",
        )

    async def stream_file(
        self,
        server: Server,
        cred: SSHCredential,
        path: str,
        *,
        chunk_size: int = 65536,
    ):
        """Yield the raw bytes of a remote file over SSH (`cat`), in chunks — for
        streaming a backup artifact to the browser without buffering the whole
        file (session 1.11 download endpoint). `path` is an absolute artifact path
        the platform itself recorded; it is still shell-quoted as its own argv.
        Binary-safe: the process uses no text decoding."""
        conn = await self._open(server, cred)
        self._pool[server.id] = conn
        command = f"cat -- {shlex.quote(path)}"
        async with self._session_slot(conn):
            async with conn.create_process(command, encoding=None) as process:
                while True:
                    chunk = await process.stdout.read(chunk_size)
                    if not chunk:
                        break
                    yield chunk
                await process.wait()

    @staticmethod
    def _wrap_command(argv: list[str], cwd: str | None, run_as: str | None) -> str:
        """Build the remote shell command line from a fixed argv (rule 1): argv is
        shlex-joined; an optional `run_as` runs it as another user via `sudo -n`;
        an optional cwd `cd`s first. No user input is ever interpolated — argv
        elements are already validated + quoted."""
        command = shlex.join(argv)
        if run_as:
            command = f"sudo -n -u {shlex.quote(run_as)} -- {command}"
        if cwd is not None:
            command = f"cd {shlex.quote(cwd)} && {command}"
        # Non-interactive SSH sessions omit ~/.local/bin; bench is always installed there
        command = "export PATH=$HOME/.local/bin:$PATH && " + command
        return command

    async def stream(
        self,
        conn: asyncssh.SSHClientConnection,
        argv: list[str],
        *,
        cwd: str | None = None,
        run_as: str | None = None,
        on_line: Callable[[str, str], Awaitable[None] | None],
        cancel_check: Callable[[], bool] | None = None,
        timeout: float = 4 * 3600,
    ) -> int:
        """Run a fixed argv and stream each stdout/stderr line to `on_line`
        (stream name, text) as it arrives. Returns the exit status.

        Cancellation is best-effort: if `cancel_check()` turns true the remote
        process is signalled (terminate) and we stop reading.
        """
        command = self._wrap_command(argv, cwd, run_as)

        async def pump(reader, stream_name: str) -> None:
            async for line in reader:
                text = line.rstrip("\n")
                result = on_line(stream_name, text)
                if result is not None:
                    await result
                if cancel_check is not None and cancel_check():
                    process.terminate()
                    return

        async with self._session_slot(conn):
            async with conn.create_process(command) as process:
                await asyncio.wait_for(
                    asyncio.gather(
                        pump(process.stdout, "stdout"), pump(process.stderr, "stderr")
                    ),
                    timeout=timeout,
                )
                await process.wait()
                return process.exit_status if process.exit_status is not None else -1

    async def read_file(
        self, conn: asyncssh.SSHClientConnection, path: str, *, chunk_size: int = 65536
    ) -> AsyncIterator[bytes]:
        """Yield a remote file's raw bytes over an already-open pooled connection
        (`cat`, binary-safe), metered against the server's session cap. Used by
        the offsite upload (2.2) and cross-server move (2.6) read paths."""
        command = f"cat -- {shlex.quote(path)}"
        async with self._session_slot(conn):
            async with conn.create_process(command, encoding=None) as process:
                while True:
                    chunk = await process.stdout.read(chunk_size)
                    if not chunk:
                        break
                    yield chunk
                await process.wait()

    async def write_file(
        self,
        conn: asyncssh.SSHClientConnection,
        path: str,
        chunks: AsyncIterator[bytes],
    ) -> int:
        """Stream `chunks` into a remote file (`cat > path`, binary-safe) over an
        open pooled connection, metered against the server's session cap. The
        destination is shell-quoted as its own argument (rule 1); the platform
        chooses it, never the user's shell. Returns the process exit status; a
        non-zero status means the write failed (e.g. permission denied) and the
        caller must refuse to register the transfer (session 2.6)."""
        command = f"cat > {shlex.quote(path)}"
        async with self._session_slot(conn):
            async with conn.create_process(command, encoding=None) as process:
                async for chunk in chunks:
                    process.stdin.write(chunk)
                    await process.stdin.drain()
                process.stdin.write_eof()
                await process.wait()
                return process.exit_status if process.exit_status is not None else -1

    async def check_connection(
        self, server: Server, cred: SSHCredential, emit: Emit | None = None
    ) -> ConnectionCheck:
        """Connect and run the fixed diagnostic suite, optionally streaming each
        result via `emit` as it completes (for the SSE test endpoint)."""
        result = ConnectionCheck()

        async def send(event: str, **data) -> None:
            if emit is not None:
                await emit({"check": event, **data})

        try:
            conn = await self._open(server, cred)
        except HostKeyMismatch as exc:
            result.error = str(exc)
            await send("ssh", ok=False, error="Host key mismatch")
            return result
        except Exception as exc:  # report any connection failure to the caller
            result.error = f"SSH connection failed: {exc}"
            await send("ssh", ok=False, error=str(exc))
            return result

        result.ssh_ok = True
        result.host_key = getattr(conn, "_fdm_host_key", None)
        self._pool[server.id] = conn
        await send("ssh", ok=True)

        try:
            whoami = await self.run(conn, ["whoami"])
            result.whoami = whoami.stdout.strip() or None
            await send("whoami", ok=whoami.exit_status == 0, value=result.whoami)

            sudo = await self.run(conn, ["sudo", "-n", "true"])
            result.sudo_ok = sudo.exit_status == 0
            await send("sudo", ok=result.sudo_ok)

            lsb = await self.run(conn, ["lsb_release", "-ds"])
            if lsb.exit_status == 0:
                result.lsb_release = lsb.stdout.strip().strip('"') or None
            await send("os", ok=lsb.exit_status == 0, value=result.lsb_release)

            for tool, argv in TOOL_COMMANDS.items():
                out = await self.run(conn, argv)
                version = out.stdout.strip().splitlines()[0].strip() if out.stdout.strip() else None
                detected = version if out.exit_status == 0 else None
                result.tools[tool] = detected
                await send("tool", name=tool, ok=out.exit_status == 0, value=detected)
        except Exception as exc:  # surface, don't crash the stream
            result.error = f"Diagnostics failed after connect: {exc}"
            await send("error", error=str(exc))

        return result

    async def close_all(self) -> None:
        for conn in self._pool.values():
            conn.close()
        self._pool.clear()


def get_ssh_service() -> SSHService:
    """FastAPI dependency: an SSHService keyed by the platform secrets. Overridable
    in tests to inject a fake connector."""
    from app.core.security import get_secrets_service

    return SSHService(get_secrets_service())
