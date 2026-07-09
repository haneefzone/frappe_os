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
