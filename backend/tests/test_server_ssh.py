"""SSHService: check_connection parsing + host-key pinning, with a fake SSH
connection so no network is touched."""

import asyncio

from cryptography.fernet import Fernet

from app.core.security import SecretsService, generate_ed25519_keypair
from app.core.ssh import SSHService, normalize_host_key
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
