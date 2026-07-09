"""Action handlers: the multi-step logic a template runs.

An Action structures its work with `ctx.step(...)` context managers and streams
command output through `ctx.stream(...)`. Actions never touch SSH, the DB or
Redis directly — they only talk to the `JobContext`, which the JobRunner
implements. That keeps actions trivially unit-testable with a fake context and
keeps all execution/persistence concerns in `app/core/jobs.py`.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Protocol

from app.core.commands.templates import RenderedCommand
from app.core.ssh import TOOL_COMMANDS


class JobContext(Protocol):
    """What an Action may use during execution. Implemented by the JobRunner."""

    rendered: RenderedCommand
    run_as: str | None
    # The server this job runs against; inventory actions (bench discovery) key
    # their upserts on it. Command actions can ignore it.
    server_id: int

    @property
    def session(self):
        """The worker DB session. Only inventory/discovery actions that produce
        rows use it; command actions stay DB-free."""
        ...

    def step(self, name: str) -> AbstractContextManager[object]:
        """Open an ordered step; the CommandStep row is written on enter and its
        terminal status/traceback on exit."""
        ...

    async def stream(
        self, argv: list[str], *, cwd: str | None = None, run_as: str | None = ...
    ) -> int:
        """Run a fixed argv on the target, streaming each output line to the log,
        and return the exit status."""
        ...

    async def capture(
        self, argv: list[str], *, cwd: str | None = None, timeout: float = ...
    ) -> object:
        """Run a fixed argv and return its collected result (exit_code, stdout,
        stderr) instead of streaming — for actions that must parse output."""
        ...

    async def emit(self, text: str, stream: str = "system") -> None:
        """Write a synthetic (runner-generated) log line."""
        ...


class Action:
    """Base action. The default behaviour runs the template's single rendered
    command as one step named after the action."""

    async def run(self, ctx: JobContext) -> None:
        with ctx.step("Run command"):
            code = await ctx.stream(ctx.rendered.argv, cwd=ctx.rendered.cwd)
            if code != 0:
                raise RuntimeError(f"command exited with status {code}")


class EchoDemoAction(Action):
    """`system.echo_demo` — a harmless three-step demo used to prove the engine
    end to end: pending -> running -> success with three steps and streamed
    logs. Every step's output is streamed to the DB and Redis pub/sub."""

    async def run(self, ctx: JobContext) -> None:
        with ctx.step("Prepare"):
            code = await ctx.stream(["echo", "Starting echo demo"])
            if code != 0:
                raise RuntimeError(f"prepare failed with status {code}")

        with ctx.step("Echo message"):
            code = await ctx.stream(ctx.rendered.argv, cwd=ctx.rendered.cwd)
            if code != 0:
                raise RuntimeError(f"echo failed with status {code}")

        with ctx.step("Finish"):
            code = await ctx.stream(["echo", "Echo demo complete"])
            if code != 0:
                raise RuntimeError(f"finish failed with status {code}")


class DiscoverBenchesAction(Action):
    """`bench.discover` — inventory the Frappe benches on a server (session 1.6).

    Read-only on the server (ls/cat/test + `bench version`), but it upserts the
    platform's `Bench` rows, so it takes a per-server lock and uses the context's
    DB session. Base paths come from the (optional) `base_paths` param — a
    comma-separated list of absolute dirs; empty falls back to the defaults plus
    the SSH user's home. Idempotent, so a transient SSH blip auto-retries."""

    async def run(self, ctx: JobContext) -> None:
        from app.core import discovery

        raw = (ctx.rendered.params_sanitized.get("base_paths") or "").strip()
        override = [p for p in (raw.split(",") if raw else []) if p]
        base_paths = discovery.validate_base_paths(override or None)

        infos: list = []
        with ctx.step("Scan server for benches"):
            await ctx.emit(f"Scanning: $HOME + {', '.join(base_paths)}")

            async def progress(msg: str) -> None:
                await ctx.emit(msg)

            infos = await discovery.gather(ctx.capture, base_paths, on_progress=progress)

        with ctx.step("Update bench inventory"):
            summary = discovery.persist(ctx.session, ctx.server_id, infos)
            await ctx.emit(
                f"Inventory updated: {summary.total_active} active "
                f"({summary.added} new, {summary.updated} refreshed), "
                f"{summary.missing} newly missing."
            )


class DetectToolsAction(Action):
    """`server.detect_tools` — inventory the Frappe toolchain on the target.

    Read-only, so it takes no lock. A missing tool (non-zero exit) is recorded
    in the logs but does not fail the job; only an unexpected error does."""

    async def run(self, ctx: JobContext) -> None:
        with ctx.step("Detect operating system"):
            await ctx.stream(["lsb_release", "-ds"])

        with ctx.step("Detect toolchain"):
            for tool, argv in TOOL_COMMANDS.items():
                await ctx.emit(f"$ {tool}")
                await ctx.stream(list(argv))
