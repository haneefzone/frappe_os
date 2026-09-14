"""Local PTY terminal backend (DOO-1203).

The WS terminal gains a local branch for `connection_type='local'` servers: an
interactive PTY on the FDM host as the service account instead of an SSH shell.
These tests exercise the real `open_local_pty` against real local subprocesses
(no SSH, no Redis, no DB) plus the security invariants from the trust model
(docs/local-pty-terminal-trust-model.md). Mirrors test_server_local.py.
"""

import asyncio

import pytest

from app.core.local_guard import LocalExecRefused
from app.core import local_pty
from app.core.local_pty import (
    LocalPtyProcess,
    local_shell_argv,
    open_local_pty,
)


def _run(coro):
    return asyncio.run(coro)


# --- open_local_pty: it really runs a shell on a PTY -----------------------


def test_open_local_pty_runs_and_streams(monkeypatch):
    """A PTY-backed subprocess streams its stdout through the async-iterable
    `stdout` the WS bridge loops over, then ends (EOF) when the shell exits."""
    monkeypatch.setattr(
        local_pty, "local_shell_argv", lambda: ["/bin/sh", "-c", "printf 'READY\\n'"]
    )

    async def scenario():
        proc = await open_local_pty()
        assert isinstance(proc, LocalPtyProcess)
        chunks: list[bytes] = []
        try:
            async for chunk in proc.stdout:
                chunks.append(chunk)
        finally:
            proc.terminate()
        return b"".join(chunks)

    out = _run(scenario())
    assert b"READY" in out


def test_open_local_pty_echoes_input_and_resizes(monkeypatch):
    """stdin.write reaches the shell and change_terminal_size does not raise —
    the two write-side operations the bridge performs."""
    monkeypatch.setattr(
        local_pty,
        "local_shell_argv",
        lambda: ["/bin/sh", "-c", "read line; printf 'GOT:%s\\n' \"$line\""],
    )

    async def scenario():
        proc = await open_local_pty()
        # Resize must be a no-crash op on the master fd.
        proc.change_terminal_size(120, 40)
        proc.stdin.write(b"hello\n")
        chunks: list[bytes] = []
        try:
            async for chunk in proc.stdout:
                chunks.append(chunk)
                if b"GOT:" in b"".join(chunks):
                    break
        finally:
            proc.terminate()
        return b"".join(chunks)

    out = _run(scenario())
    assert b"GOT:hello" in out


# --- security invariants ---------------------------------------------------


def test_open_local_pty_refuses_root(monkeypatch):
    """A local PTY must never be an unrestricted root shell: refuse to open when
    the platform process runs as root (trust model invariant #1)."""
    monkeypatch.setattr(local_pty, "current_user", lambda: "root")

    with pytest.raises(LocalExecRefused):
        _run(open_local_pty())


def test_run_as_root_is_refused_via_guard(monkeypatch):
    """run_as='root' is refused through the shared local_guard path, not a second
    copy — the same refusal DOO-1199's job executor enforces."""
    # Not running as root, so the open-time guard passes; the root refusal must
    # then come from wrap_local_argv when a run_as is supplied.
    monkeypatch.setattr(local_pty, "current_user", lambda: "fdm")
    with pytest.raises(LocalExecRefused):
        _run(open_local_pty(run_as="root"))


def test_local_shell_argv_is_interactive_login():
    argv = local_shell_argv()
    assert argv[1:] == ["-i", "-l"]
    assert argv[0]  # a shell path
