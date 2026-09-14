"""LocalShellExecutor + local_guard + local connection check (DOO-1199).

The local (localhost) execution backend runs jobs as the FDM service account on
the FDM host itself instead of over SSH. These tests exercise the real executor
against real local subprocesses (echo/cat/sh) and a tmp dir — no DB, no network,
no migrations — so they stay hermetic and fast. Mirrors test_server_ssh.py.
"""

import asyncio

import pytest

from app.core.jobs import LocalShellExecutor
from app.core.local_guard import (
    LocalExecRefused,
    assert_path_allowed,
    current_user,
    local_env,
    platform_root,
    wrap_local_argv,
)
from app.core.ssh import run_local_connection_check


def _run(coro):
    return asyncio.run(coro)


# --- run(): streaming, cwd, exit code -------------------------------------


def test_run_streams_lines_and_returns_zero():
    lines: list[tuple[str, str]] = []

    async def on_line(stream: str, text: str):
        lines.append((stream, text))

    code = _run(
        LocalShellExecutor().run(
            ["sh", "-c", "echo out1; echo err1 1>&2; echo out2"],
            cwd=None,
            run_as=None,
            on_line=on_line,
            cancel_check=None,
        )
    )
    assert code == 0
    stdout = [t for s, t in lines if s == "stdout"]
    stderr = [t for s, t in lines if s == "stderr"]
    assert stdout == ["out1", "out2"]
    assert stderr == ["err1"]


def test_run_honours_cwd(tmp_path):
    seen: list[str] = []

    async def on_line(stream: str, text: str):
        seen.append(text)

    code = _run(
        LocalShellExecutor().run(
            ["pwd"], cwd=str(tmp_path), run_as=None, on_line=on_line, cancel_check=None
        )
    )
    assert code == 0
    # tmp_path may be a symlink (macOS /var); compare resolved.
    assert seen[0].rstrip("/") in (str(tmp_path), str(tmp_path.resolve()))


def test_run_returns_nonzero_exit():
    async def on_line(stream: str, text: str):
        return None

    code = _run(
        LocalShellExecutor().run(
            ["sh", "-c", "exit 3"], cwd=None, run_as=None, on_line=on_line, cancel_check=None
        )
    )
    assert code == 3


def test_run_cancel_check_terminates():
    async def on_line(stream: str, text: str):
        return None

    # cancel immediately on the first line; the long sleep must not run to completion.
    code = _run(
        LocalShellExecutor().run(
            ["sh", "-c", "echo go; sleep 30"],
            cwd=None,
            run_as=None,
            on_line=on_line,
            cancel_check=lambda: True,
        )
    )
    assert code != 0  # terminated, not a clean 0


# --- capture() -------------------------------------------------------------


def test_capture_returns_stdout_and_code():
    res = _run(LocalShellExecutor().capture(["echo", "hello"]))
    assert res.exit_code == 0
    assert res.stdout.strip() == "hello"


def test_capture_nonzero():
    res = _run(LocalShellExecutor().capture(["sh", "-c", "echo boom 1>&2; exit 7"]))
    assert res.exit_code == 7
    assert "boom" in res.stderr


# --- file IO round-trip ----------------------------------------------------


def test_write_then_read_file(tmp_path):
    target = tmp_path / "artifact.bin"
    payload = b"local-backend-bytes\n" * 1000

    async def _chunks():
        yield payload[:512]
        yield payload[512:]

    written = _run(LocalShellExecutor().write_file(str(target), _chunks()))
    assert written == 0
    assert target.read_bytes() == payload

    async def _collect():
        out = b""
        async for chunk in LocalShellExecutor().read_file(str(target), chunk_size=256):
            out += chunk
        return out

    assert _run(_collect()) == payload


# --- run_as / privilege (AC7) ---------------------------------------------


def test_wrap_local_argv_same_user_is_verbatim():
    assert wrap_local_argv(["bench", "version"], None) == ["bench", "version"]
    assert wrap_local_argv(["bench", "version"], current_user()) == ["bench", "version"]


def test_wrap_local_argv_other_user_uses_sudo():
    assert wrap_local_argv(["bench", "init"], "someoneelse") == [
        "sudo",
        "-n",
        "-u",
        "someoneelse",
        "--",
        "bench",
        "init",
    ]


def test_wrap_local_argv_refuses_root():
    with pytest.raises(LocalExecRefused):
        wrap_local_argv(["bench", "init"], "root")


def test_local_env_prepends_local_bin_same_user():
    env = local_env(None)
    assert env["PATH"].split(":")[0].endswith("/.local/bin")


# --- FDM-own-dir guard (AC8) ----------------------------------------------


def test_assert_path_allowed_permits_outside(tmp_path):
    assert_path_allowed(str(tmp_path))  # no raise
    assert_path_allowed(None)  # no raise
    assert_path_allowed("")  # no raise


def test_assert_path_allowed_refuses_platform_root():
    root = platform_root()
    with pytest.raises(LocalExecRefused):
        assert_path_allowed(str(root))
    with pytest.raises(LocalExecRefused):
        assert_path_allowed(str(root / "backend" / "app"))


def test_assert_path_allowed_blocks_traversal_into_root():
    root = platform_root()
    with pytest.raises(LocalExecRefused):
        assert_path_allowed(str(root / ".." / root.name / "backend"))


def test_run_refuses_cwd_inside_platform_root():
    async def on_line(stream: str, text: str):
        return None

    with pytest.raises(LocalExecRefused):
        _run(
            LocalShellExecutor().run(
                ["pwd"],
                cwd=str(platform_root()),
                run_as=None,
                on_line=on_line,
                cancel_check=None,
            )
        )


# --- local connection check (AC6) -----------------------------------------


def test_local_connection_check_reports_reachable_and_whoami():
    events: list[dict] = []

    async def emit(ev: dict):
        events.append(ev)

    result = _run(run_local_connection_check(emit=emit))
    # No SSH dial, but the host is always "reachable" locally.
    assert result.ssh_ok is True
    assert result.host_key is None
    assert result.whoami == current_user()
    # The probe set streamed the same event shape as the SSH check.
    checks = {e.get("check") for e in events}
    assert {"ssh", "whoami", "sudo", "os"} <= checks
    assert "tool" in checks
