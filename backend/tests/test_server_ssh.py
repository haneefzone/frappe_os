"""SSHService: check_connection parsing + host-key pinning, with a fake SSH
connection so no network is touched."""

import asyncio

from cryptography.fernet import Fernet

from app.core.preflight import NODE_ARGV, UV_ARGV
from app.core.security import SecretsService, generate_ed25519_keypair
from app.core.ssh import SSHService, login_shell_command, normalize_host_key
from app.models import Server, SSHCredential

STORED_KEY = "ssh-ed25519 AAAAStoredKeyBase64Value"
OTHER_KEY = "ssh-ed25519 AAAADifferentKeyBase64Value"

# Fixed diagnostic responses keyed by a substring of the command string.
HAPPY_RESPONSES = {
    "whoami": (0, "frappe\n"),
    "sudo -n true": (0, ""),
    "lsb_release": (0, '"Ubuntu 24.04.1 LTS"\n'),
    "git --version": (0, "git version 2.43.0\n"),
    "python3 --version": (0, "Python 3.12.3\n"),
    "uv --version": (0, "uv 0.5.11\n"),
    "node --version": (0, "v20.11.1\n"),
    "mariadb --version": (0, "mariadb from 11.4.2-MariaDB\n"),
    "redis-server --version": (0, "Redis server v=7.2.4\n"),
    "wkhtmltopdf --version": (127, ""),  # not installed
    "bench --version": (0, "6.1.0\n"),
}


class _FakeHostKey:
    def __init__(self, line: str) -> None:
        self._line = line

    def export_public_key(self, format_name: str = "openssh") -> bytes:
        return self._line.encode()


class _FakeResult:
    def __init__(self, exit_status: int, stdout: str) -> None:
        self.exit_status = exit_status
        self.stdout = stdout
        self.stderr = ""


class _FakeConn:
    def __init__(self, host_key_line: str, responses: dict[str, tuple[int, str]]) -> None:
        self._host_key = _FakeHostKey(host_key_line)
        self._responses = responses
        self.closed = False

    def get_server_host_key(self):
        return self._host_key

    def is_closed(self) -> bool:
        return self.closed

    def close(self) -> None:
        self.closed = True

    async def run(self, command, check=False, timeout=30):
        for needle, (exit_status, stdout) in self._responses.items():
            if needle in command:
                return _FakeResult(exit_status, stdout)
        return _FakeResult(127, "")


def _service_for(conn: _FakeConn):
    secrets = SecretsService(Fernet.generate_key())

    async def connector(**kwargs):
        return conn

    return SSHService(secrets, connector=connector), secrets


def _key_credential(secrets: SecretsService, known_host_key=None) -> SSHCredential:
    private_pem, _ = generate_ed25519_keypair()
    return SSHCredential(
        username="frappe",
        auth_type="key",
        private_key_enc=secrets.encrypt(private_pem),
        known_host_key=known_host_key,
    )


def _server() -> Server:
    return Server(id=1, name="test", hostname="10.0.0.5", ssh_port=22)


def test_check_connection_parses_tools_and_pins_host_key():
    conn = _FakeConn(STORED_KEY, HAPPY_RESPONSES)
    svc, secrets = _service_for(conn)
    cred = _key_credential(secrets, known_host_key=None)  # first connect

    result = asyncio.run(svc.check_connection(_server(), cred))

    assert result.ssh_ok is True
    assert result.whoami == "frappe"
    assert result.sudo_ok is True
    assert result.lsb_release == "Ubuntu 24.04.1 LTS"
    assert result.tools["git"] == "git version 2.43.0"
    assert result.tools["python3"] == "Python 3.12.3"
    assert result.tools["uv"] == "uv 0.5.11"
    assert result.tools["bench"] == "6.1.0"
    # Not installed -> None, not a crash.
    assert result.tools["wkhtmltopdf"] is None
    # First connect captures the host key for pinning.
    assert result.host_key == normalize_host_key(STORED_KEY)
    assert result.error is None


def test_host_key_mismatch_rejected_before_running_commands():
    # Server now presents a different key than the one we pinned earlier.
    conn = _FakeConn(OTHER_KEY, HAPPY_RESPONSES)
    svc, secrets = _service_for(conn)
    cred = _key_credential(secrets, known_host_key=STORED_KEY)

    result = asyncio.run(svc.check_connection(_server(), cred))

    assert result.ssh_ok is False
    assert result.whoami is None  # never got as far as running whoami
    assert "does not match" in (result.error or "")
    assert conn.closed is True  # the suspect connection was torn down


def test_check_connection_streams_events():
    conn = _FakeConn(STORED_KEY, HAPPY_RESPONSES)
    svc, secrets = _service_for(conn)
    cred = _key_credential(secrets)
    events: list[dict] = []

    async def run():
        async def emit(ev):
            events.append(ev)

        return await svc.check_connection(_server(), cred, emit=emit)

    asyncio.run(run())

    names = [e["check"] for e in events]
    assert names[0] == "ssh"
    assert "whoami" in names
    assert "sudo" in names
    assert "os" in names
    # one "tool" event per detected tool
    assert sum(1 for n in names if n == "tool") == 8


# --------------------------------------------------------------------------- #
# DOO-1208: the real exec path runs every command through the bench user's
# LOGIN shell — the same interpreter resolution the pre-flight probes clear a
# host with — so a green pre-flight is a real promise about the command that
# actually runs (`bench init`, `bench get-app`, the node/yarn asset build …).
# --------------------------------------------------------------------------- #


def test_wrap_command_runs_bench_through_login_shell():
    # A bench command with a cwd is wrapped in a bash LOGIN shell so nvm/pyenv/uv
    # PATH is sourced. Regression: before the fix this used a non-login
    # `export PATH=$HOME/.local/bin:$PATH && …` that never loaded the
    # nvm-managed node, so `bench init` ran without the node the green
    # pre-flight had promised (a live contributor to DOO-1187).
    cmd = SSHService._wrap_command(
        ["bench", "init", "frappe-bench"], cwd="/home/frappe", run_as=None
    )
    assert cmd == "bash -lc 'cd /home/frappe && bench init frappe-bench'"
    # The broken non-login mechanism is gone.
    assert "export PATH=$HOME/.local/bin" not in cmd


def test_exec_and_probe_resolve_interpreter_identically():
    # AC1: the wrapper the real command goes through is the SAME login shell the
    # pre-flight node/uv probe is resolved by. Prove it by construction — a bare
    # probe argv and a bench command both come out inside `bash -lc`, and the
    # probe's wrap equals `login_shell_command` (the one shared primitive).
    node_exec = SSHService._wrap_command(list(NODE_ARGV), cwd=None, run_as=None)
    assert node_exec == login_shell_command("node --version")
    assert node_exec == "bash -lc 'node --version'"

    uv_exec = SSHService._wrap_command(list(UV_ARGV), cwd=None, run_as=None)
    assert uv_exec == login_shell_command("uv --version")

    # The bench command the probe clears rides the identical login shell.
    bench_exec = SSHService._wrap_command(["bench", "--version"], cwd=None, run_as=None)
    assert bench_exec == "bash -lc 'bench --version'"


def test_wrap_command_run_as_sudos_the_login_shell():
    # When a command must run as another user, sudo wraps the login shell (so
    # bash sources *that* user's profile — the bench owner's nvm), never the
    # other way round.
    cmd = SSHService._wrap_command(
        ["bench", "build"], cwd="/home/frappe/bench", run_as="frappe"
    )
    assert cmd == "sudo -n -u frappe -- bash -lc 'cd /home/frappe/bench && bench build'"


def test_ssh_run_sends_login_shell_wrapped_command_to_the_channel():
    # End-to-end through SSHService.run: the string handed to the SSH channel is
    # the login-shell-wrapped form, not a bare `node --version`.
    seen: dict[str, str] = {}

    class _RecordConn(_FakeConn):
        async def run(self, command, check=False, timeout=30):
            seen["command"] = command
            return _FakeResult(0, "v20.11.1\n")

    conn = _RecordConn(STORED_KEY, HAPPY_RESPONSES)
    svc, _ = _service_for(conn)
    out = asyncio.run(svc.run(conn, ["node", "--version"]))
    assert out.exit_status == 0
    assert seen["command"] == "bash -lc 'node --version'"
