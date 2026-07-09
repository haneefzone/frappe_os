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


def _sibling_ports(ctx: JobContext) -> set[int]:
    """Ports already claimed by other benches on this job's server, from the
    discovery inventory — for the pre-flight ports conflict check (gotcha #8)."""
    from sqlalchemy import select

    from app.core.preflight import sibling_ports_from_benches
    from app.models.bench import Bench

    benches = ctx.session.scalars(
        select(Bench).where(Bench.server_id == ctx.server_id)
    ).all()
    return sibling_ports_from_benches(benches)


async def _run_preflight_steps(ctx: JobContext):
    """Shared by the standalone pre-flight job and the create orchestration: run
    every check, then record each as its own step so the timeline shows the full
    grid. Steps only log here — a transient SSH error still propagates from
    `capture` (so the idempotent pre-flight job auto-retries), but a determinate
    pass/warn/fail is data, not an exception. The caller decides what a blocking
    failure means. Returns the PreflightReport."""
    from app.core.preflight import run_preflight

    params = ctx.rendered.params_sanitized
    report = await run_preflight(
        ctx.capture,
        frappe_major=params["frappe_version"],
        path=params["path"],
        sibling_ports=_sibling_ports(ctx),
    )
    for result in report.checks:
        icon = {"pass": "✓", "warn": "!", "fail": "✗"}.get(result.status, "·")
        with ctx.step(result.title):
            await ctx.emit(f"[{icon}] {result.title}: {result.detail}")
    return report


class BenchPreflightAction(Action):
    """`bench.preflight` — the wizard's live, re-runnable pre-flight (session 1.7).

    Runs every check for the chosen version against the target and records the
    structured verdict. The JOB always succeeds as long as the checks *ran*
    (idempotent: a transient SSH blip auto-retries); whether the target is
    *blocked* is a property of the results, not the job status — so a determinate
    "uv missing" doesn't get retried three times. The final `PREFLIGHT_RESULT`
    log line carries the machine-readable report the wizard renders and uses to
    enable/disable its Create button."""

    async def run(self, ctx: JobContext) -> None:
        import json

        report = await _run_preflight_steps(ctx)
        await ctx.emit("PREFLIGHT_RESULT " + json.dumps(report.as_dict()), stream="result")
        if report.blocked:
            verdict = "blocked"
        elif report.has_warnings:
            verdict = "passed with warnings"
        else:
            verdict = "all clear"
        await ctx.emit(f"Pre-flight {verdict}.")


class CreateBenchAction(Action):
    """`bench.create` — guided bench creation orchestrated as sequential steps in
    ONE job (session 1.7).

    Model choice (documented per the spec's "parent job with child jobs OR
    sequential steps — choose one"): a single non-idempotent `bench.create` job
    whose ordered steps are (1) the pre-flight checks, (2) `bench init`, (3)
    register the new bench. Chosen over parent+child jobs because the engine has
    no job-dependency scheduler, and the whole create is one lockable unit on
    `(server, bench path)` — the pre-flight, the long init, and the registration
    must not interleave with anything else touching that path.

    A *blocking* pre-flight failure raises before `bench init` runs, so the job
    fails cleanly and never starts a doomed install (acceptance: "preflight
    failure blocks init"). The job is non-idempotent, so this determinate
    failure is never auto-retried, and a partially-created bench dir is never
    re-attempted on top of itself."""

    async def run(self, ctx: JobContext) -> None:
        import posixpath

        from app.core import discovery
        from app.core.commands import get_template, render
        from app.core.version_matrix import branch_for

        params = ctx.rendered.params_sanitized
        frappe_major = params["frappe_version"]
        name = params["name"]
        path = params["path"]
        new_path = posixpath.join(path, name)

        # 1) Pre-flight — a blocking failure stops here; init never runs.
        report = await _run_preflight_steps(ctx)
        if report.blocked:
            failed = [c for c in report.checks if c.is_blocking_failure]
            summary = "; ".join(f"{c.title}: {c.detail}" for c in failed)
            with ctx.step("Pre-flight gate"):
                await ctx.emit(f"Blocking pre-flight failure — not running bench init: {summary}")
                raise RuntimeError(f"pre-flight failed: {summary}")

        # 2) bench init (long) — render the bench.init template so the argv is
        #    built through the safe registry, not string interpolation.
        branch = branch_for(frappe_major)
        init = render(
            get_template("bench.init"),
            {"branch": branch, "name": name, "path": path},
        )
        with ctx.step(f"Initialize bench ({branch})"):
            await ctx.emit(f"$ {init.display}")
            code = await ctx.stream(init.argv, cwd=init.cwd)
            if code != 0:
                raise RuntimeError(f"bench init exited with status {code}")

        # 3) Register the new bench in the platform inventory (single-bench
        #    upsert — never marks the siblings missing).
        with ctx.step("Register bench"):
            res = await ctx.capture(discovery.build_inspect_argv(new_path))
            info = discovery.parse_inspect(res.stdout, new_path)
            if info.frappe_version is None:
                plain = await ctx.capture(
                    list(discovery.BENCH_VERSION_PLAIN_ARGV), cwd=new_path
                )
                info.frappe_version = discovery.parse_bench_version_plain(plain.stdout)
            bench = discovery.upsert_one(ctx.session, ctx.server_id, info)
            await ctx.emit(
                f"Registered bench #{bench.id} at {new_path} "
                f"(frappe {info.frappe_version or 'unknown'})."
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
