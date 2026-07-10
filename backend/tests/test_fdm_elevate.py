"""Unit tests for the hardened bench resolution in ``deploy/fdm-elevate`` (DOO-163).

The privileged helper's ``grant`` subcommand writes a NOPASSWD sudoers drop-in
permitting exactly ``<BENCH_BIN> setup production <user>``. That grant is only
safe if ``BENCH_BIN`` is (1) resolved deterministically — never from the caller's
``$PATH`` — and (2) not writable by any non-root user (else the grantee could
swap the binary and get an unconstrained root shell). These tests source the
bash helper (its tail is guarded by ``BASH_SOURCE == $0`` so sourcing does not
run ``main``) and exercise ``resolve_bench_bin`` / ``assert_secure_path``
directly. They run as a non-root user, so they can prove the *refusal* paths and
deterministic resolution; the accept-root-owned path is checked against a real
system binary (``/bin/true``).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

HELPER = (Path(__file__).resolve().parents[2] / "deploy" / "fdm-elevate").resolve()

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None, reason="bash required to test the elevate helper"
)


def _run(snippet: str) -> subprocess.CompletedProcess[str]:
    """Source the helper and run a bash snippet against its functions."""
    script = f'set -euo pipefail\nsource "{HELPER}"\n{snippet}\n'
    return subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, timeout=30
    )


def test_helper_parses_and_sourcing_does_not_run_main():
    # `bash -n` parse gate, and sourcing must not execute a subcommand.
    assert subprocess.run(["bash", "-n", str(HELPER)]).returncode == 0
    r = _run('echo SOURCED_OK')
    assert r.returncode == 0, r.stderr
    assert "SOURCED_OK" in r.stdout
    assert "unknown subcommand" not in (r.stdout + r.stderr)


def test_resolve_bench_bin_is_deterministic_ignoring_ambient_path(tmp_path: Path):
    # A fixture bench on the fixed SECURE_PATH is chosen even when a decoy `bench`
    # appears earlier on the caller-controlled ambient $PATH.
    good = tmp_path / "good" / "bin"
    good.mkdir(parents=True)
    (good / "bench").write_text("#!/bin/sh\necho good\n")
    (good / "bench").chmod(0o755)
    evil = tmp_path / "evil"
    evil.mkdir()
    (evil / "bench").write_text("#!/bin/sh\necho evil\n")
    (evil / "bench").chmod(0o755)

    r = _run(
        f'SECURE_PATH="{good}"\n'
        f'BENCH_BIN_PIN="{tmp_path}/no-such-pin"\n'
        f'export PATH="{evil}:$PATH"\n'
        f'resolve_bench_bin'
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == str(good / "bench")


def test_resolve_bench_bin_canonicalises_symlinks(tmp_path: Path):
    real = tmp_path / "real"
    real.mkdir()
    (real / "bench-real").write_text("#!/bin/sh\n")
    (real / "bench-real").chmod(0o755)
    link_dir = tmp_path / "link"
    link_dir.mkdir()
    (link_dir / "bench").symlink_to(real / "bench-real")

    r = _run(
        f'SECURE_PATH="{link_dir}"\n'
        f'BENCH_BIN_PIN="{tmp_path}/no-such-pin"\n'
        f'resolve_bench_bin'
    )
    assert r.returncode == 0, r.stderr
    # Follows the symlink to the canonical target — the drop-in names the real file.
    assert r.stdout.strip() == str(real / "bench-real")


def test_resolve_bench_bin_dies_when_absent(tmp_path: Path):
    r = _run(
        f'SECURE_PATH="{tmp_path}/empty"\n'
        f'BENCH_BIN_PIN="{tmp_path}/no-such-pin"\n'
        f'resolve_bench_bin'
    )
    assert r.returncode != 0
    assert "could not resolve bench" in r.stderr


def test_assert_secure_path_refuses_non_root_owned(tmp_path: Path):
    # A file the current (non-root) user owns is refused: the grantee could edit it.
    b = tmp_path / "bench"
    b.write_text("#!/bin/sh\n")
    b.chmod(0o755)
    r = _run(f'assert_secure_path "{b}"')
    assert r.returncode != 0
    assert "not root" in r.stderr


def test_assert_secure_path_refuses_world_writable_component():
    # /tmp is root-owned but 1777 (world-writable) — must be refused via the
    # write-bit branch, proving ancestor directories are checked, not just the file.
    r = _run('assert_secure_path "/tmp"')
    assert r.returncode != 0
    assert "writable" in r.stderr


def test_assert_secure_path_accepts_root_owned_system_binary():
    # A real root-owned binary on a root-owned path (e.g. /usr/bin/true) is accepted.
    r = _run('assert_secure_path "$(readlink -f /bin/true)"')
    assert r.returncode == 0, r.stderr


def test_grant_refuses_when_pin_is_not_root_owned(tmp_path: Path):
    # An operator pin the grantee can rewrite must not be trusted: resolving
    # through it fails on the pin's own permission check.
    pin = tmp_path / "bench-bin"
    pin.write_text("/usr/local/bin/bench\n")  # owned by the non-root test user
    r = _run(
        f'BENCH_BIN_PIN="{pin}"\n'
        f'resolve_bench_bin'
    )
    assert r.returncode != 0
    assert "not root" in r.stderr
