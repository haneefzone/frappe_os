"""Local interactive PTY backend for the WS terminal (DOO-1203).

A `connection_type='local'` Server (DOO-1199) is the machine FDM itself runs on,
so its terminal is not an SSH shell but a local subprocess attached to a
pseudo-terminal. This module launches that shell and wraps it in an object that
duck-types the slice of the AsyncSSH process interface the terminal bridge
uses — ``stdin.write`` / async-iterable ``stdout`` / ``change_terminal_size`` /
``terminate`` — so the WS route branches only on *how a session is opened*, not
on the whole byte-bridge loop.

Trust model: ``docs/local-pty-terminal-trust-model.md``. In short — runs as the
FDM service account, **never root**; adds no escalation beyond
``deploy/fdm-elevate``; the argv is built through
``local_guard.wrap_local_argv`` so ``run_as`` (root refusal + ``sudo -n -u``
drop) shares exactly one code path with the DOO-1199 job executor.
"""

from __future__ import annotations

import asyncio
import fcntl
import os
import pty
import struct
import termios

from app.core.local_guard import (
    LocalExecRefused,
    current_user,
    local_env,
    wrap_local_argv,
)


def _default_shell() -> str:
    """The service account's login shell: the passwd entry first, then ``$SHELL``,
    then ``/bin/bash``. Interactive/login flags are added by ``local_shell_argv``."""
    try:
        import pwd

        shell = pwd.getpwuid(os.getuid()).pw_shell
        if shell:
            return shell
    except Exception:  # pragma: no cover - no passwd entry for the uid
        pass
    return os.environ.get("SHELL") or "/bin/bash"


def local_shell_argv() -> list[str]:
    """argv for an interactive login shell of the service account. Login (`-l`)
    so the operator gets the same PATH/profile an SSH shell would; interactive
    (`-i`) so it behaves like a real terminal (prompt, job control, history)."""
    return [_default_shell(), "-i", "-l"]


class _PtyWriter:
    """Write side of the PTY master, shaped like ``asyncssh.SSHWriter`` (a plain
    non-awaited ``write(bytes)``). Interactive keystroke volumes never fill the
    master buffer, so a direct ``os.write`` matches AsyncSSH's fire-and-forget
    semantics without an await."""

    def __init__(self, master_fd: int) -> None:
        self._fd = master_fd

    def write(self, data: bytes) -> None:
        try:
            os.write(self._fd, data)
        except OSError:
            # Slave gone (shell exited) — the reader's EOF drives close_reason.
            pass


class _PtyReader:
    """Async-iterable read side of the PTY master, shaped like the AsyncSSH
    ``process.stdout`` the bridge loops over (``async for chunk in stdout``).

    A ``loop.add_reader`` callback drains the master fd into a queue; ``None`` is
    the EOF sentinel so ``async for`` ends exactly as it does on an SSH stream."""

    def __init__(self, master_fd: int, loop: asyncio.AbstractEventLoop) -> None:
        self._fd = master_fd
        self._loop = loop
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._detached = False
        loop.add_reader(master_fd, self._on_readable)

    def _on_readable(self) -> None:
        try:
            data = os.read(self._fd, 65536)
        except OSError:
            # EIO on Linux when the slave side closes == EOF.
            data = b""
        if not data:
            self._detach()
            self._queue.put_nowait(None)
            return
        self._queue.put_nowait(data)

    def _detach(self) -> None:
        if not self._detached:
            self._detached = True
            try:
                self._loop.remove_reader(self._fd)
            except Exception:  # pragma: no cover - loop already tearing down
                pass

    def __aiter__(self) -> _PtyReader:
        return self

    async def __anext__(self) -> bytes:
        chunk = await self._queue.get()
        if chunk is None:
            raise StopAsyncIteration
        return chunk


class LocalPtyProcess:
    """A local shell on a PTY, presenting the subset of the AsyncSSH process
    interface the WS terminal bridge uses. Lets the bridge loop stay shared
    between the SSH and local backends (DOO-1203 scope pt.2/4)."""

    def __init__(
        self,
        proc: asyncio.subprocess.Process,
        master_fd: int,
        reader: _PtyReader,
    ) -> None:
        self._proc = proc
        self._master_fd = master_fd
        self.stdin = _PtyWriter(master_fd)
        self.stdout = reader

    def change_terminal_size(self, cols: int, rows: int) -> None:
        """Mirror ``asyncssh`` PTY resize via ``TIOCSWINSZ`` on the master fd."""
        winsize = struct.pack("HHHH", rows, cols, 0, 0)
        try:
            fcntl.ioctl(self._master_fd, termios.TIOCSWINSZ, winsize)
        except OSError:
            pass

    def terminate(self) -> None:
        try:
            self._proc.terminate()
        except ProcessLookupError:
            pass
        self.stdout._detach()
        try:
            os.close(self._master_fd)
        except OSError:
            pass


def _set_controlling_tty() -> None:
    """Child-side (post-fork, pre-exec): become a session leader and claim the
    slave (fd 0) as the controlling terminal, so the shell gets job control and
    signal delivery (Ctrl-C) exactly like an SSH PTY."""
    os.setsid()
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)


async def open_local_pty(
    *, cwd: str | None = None, run_as: str | None = None
) -> LocalPtyProcess:
    """Launch an interactive login shell on a fresh PTY as the FDM service
    account and return it wrapped for the terminal bridge.

    Security (see trust model): refuses outright if the platform runs as root; a
    local terminal must never be an unrestricted root shell. The argv passes
    through ``wrap_local_argv`` so any ``run_as`` shares the executor's root
    refusal + ``sudo -n -u`` drop. Defaults the cwd to the service account home
    rather than FDM's own tree.
    """
    if current_user() == "root":
        raise LocalExecRefused(
            "local terminal refused: the FDM platform is running as root, and a "
            "local PTY must never be an unrestricted root shell. Run FDM under an "
            "unprivileged service account."
        )

    argv = wrap_local_argv(local_shell_argv(), run_as)
    master_fd, slave_fd = pty.openpty()
    loop = asyncio.get_running_loop()
    reader = _PtyReader(master_fd, loop)
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            cwd=cwd or os.path.expanduser("~"),
            env=local_env(run_as),
            preexec_fn=_set_controlling_tty,
            close_fds=True,
        )
    except Exception:
        reader._detach()
        for fd in (master_fd, slave_fd):
            try:
                os.close(fd)
            except OSError:
                pass
        raise

    # The child holds its own dup of the slave; the parent never needs it.
    os.close(slave_fd)
    return LocalPtyProcess(proc, master_fd, reader)
