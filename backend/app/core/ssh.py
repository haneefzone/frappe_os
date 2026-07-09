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

import hmac
import shlex
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import asyncssh

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
    ) -> None:
        self._secrets = secrets
        self._connector = connector
        self._pool: dict[int, asyncssh.SSHClientConnection] = {}

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
        result = await conn.run(command, check=False, timeout=timeout)
        return CommandOutput(
            exit_status=result.exit_status if result.exit_status is not None else -1,
            stdout=(result.stdout or "") if isinstance(result.stdout, str) else "",
            stderr=(result.stderr or "") if isinstance(result.stderr, str) else "",
        )

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
