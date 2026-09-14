"""Guardrails for the local (localhost) execution backend (DOO-1199 / DOO-1196).

A `connection_type='local'` Server executes commands as the FDM service account
*on the FDM host itself* — no SSH. That collapses the usual blast-radius wall:
the machine being managed IS the control panel. Two invariants keep that safe:

- **run_as is honoured, root is refused.** Bench refuses to run as root
  (CLAUDE.md gotcha #1), so the local executor drops to `run_as` via
  `sudo -n -u <user>` exactly as the SSH path does (`core/ssh._wrap_command`).
  It never sudos to root — `deploy/fdm-elevate` (argument-validated, time-boxed)
  stays the ONLY privilege path (AC7).
- **FDM's own install dir is off-limits.** A local job must manage *other*
  benches/sites, never reach into the platform's own code, `.env`, deploy units
  or its own database (AC8). Any cwd / file path resolving under the platform
  root is refused before a process starts.

These helpers are import-light on purpose so both `core/jobs.py` (executor) and
`core/ssh.py` (local connection test / file stream) can use them without a cycle.
"""

from __future__ import annotations

import getpass
import os
from pathlib import Path


class LocalExecRefused(RuntimeError):
    """A local-backend command was refused for a safety reason (root escalation
    or an attempt to touch FDM's own install dir)."""


def platform_root() -> Path:
    """The platform's own install root — the tree the local backend must never
    manage. Mirrors `actions._platform_config_root`: PLATFORM_ROOT override, else
    the backend package's grandparent (…/backend/app/core -> repo root)."""
    override = os.environ.get("PLATFORM_ROOT", "")
    if not override:
        try:
            from app.config import get_settings

            override = getattr(get_settings(), "platform_root", "") or ""
        except Exception:  # pragma: no cover - settings must never break a guard
            override = ""
    if override:
        return Path(override).resolve()
    # app/core/local_guard.py -> parents: core, app, backend, repo root
    return Path(__file__).resolve().parents[3]


def assert_path_allowed(path: str | None) -> None:
    """Refuse a path that resolves inside the platform's own install root (AC8).

    `None`/empty is allowed (no path constraint). The check is prefix-based on the
    fully-resolved path so `..` traversal cannot escape it."""
    if not path:
        return
    root = platform_root()
    resolved = Path(path).resolve()
    if resolved == root or root in resolved.parents:
        raise LocalExecRefused(
            f"local backend refused to touch {resolved}: it is inside FDM's own "
            f"install root ({root}). The local server manages other benches, "
            "never the control panel itself."
        )


def current_user() -> str:
    """The OS user the FDM platform process runs as."""
    try:
        return getpass.getuser()
    except Exception:  # pragma: no cover - getpass can fail with no passwd entry
        return os.environ.get("USER", "") or str(os.getuid())


def wrap_local_argv(argv: list[str], run_as: str | None) -> list[str]:
    """Return the argv to actually exec locally, honouring `run_as` (AC7).

    - `run_as` unset, or already the platform user -> run argv as-is.
    - `run_as` set to another user -> `sudo -n -u <user> -- <argv>` (same shape
      as the SSH `_wrap_command`; `-n` never prompts, so a missing sudoers rule
      fails fast instead of hanging).
    - `run_as == "root"` -> refused. The job path never escalates to root;
      `deploy/fdm-elevate` is the only sanctioned privilege path.
    """
    if run_as == "root":
        raise LocalExecRefused(
            "local backend refused run_as=root: bench never runs as root and the "
            "job path never escalates. Use deploy/fdm-elevate for privileged ops."
        )
    if not run_as or run_as == current_user():
        return list(argv)
    return ["sudo", "-n", "-u", run_as, "--", *argv]


def local_env(run_as: str | None) -> dict[str, str]:
    """Environment for a local exec: the platform process env with the bench
    owner's `~/.local/bin` prepended to PATH. Non-interactive shells (and the
    platform's own systemd unit) omit it, but bench is always installed there —
    the SSH path does the same via `export PATH=$HOME/.local/bin` in
    `_wrap_command`. When `run_as` differs, `sudo` resets the environment and the
    target user's login PATH applies, so this only augments the same-user case."""
    env = dict(os.environ)
    if not run_as or run_as == current_user():
        home = os.path.expanduser("~")
        local_bin = os.path.join(home, ".local", "bin")
        env["PATH"] = local_bin + os.pathsep + env.get("PATH", "")
    return env
