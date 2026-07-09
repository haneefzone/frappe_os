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


# --------------------------------------------------------------------------- #
# Sites (session 1.8)
# --------------------------------------------------------------------------- #

# Default dev-bench Redis ports (CLAUDE.md gotcha #3): queue :11000, cache
# :13000. Used for the shutdown-after only when the discovered bench row doesn't
# carry its own ports.
DEFAULT_REDIS_QUEUE_PORT = 11000
DEFAULT_REDIS_CACHE_PORT = 13000

# Fixed dev/prod probe: a production bench has supervisor/systemd unit files.
# No user input — the only variable is the cwd (the validated bench path).
_MODE_PROBE = (
    "if [ -f config/supervisor.conf ] || [ -f config/systemd/frappe-web.service ]; "
    "then echo PROD; else echo DEV; fi"
)


def _load_bench(ctx: JobContext, bench_path: str):
    """Fetch the discovered Bench row for this job's server + path, or None."""
    from sqlalchemy import select

    from app.models.bench import Bench

    return ctx.session.scalars(
        select(Bench).where(
            Bench.server_id == ctx.server_id, Bench.path == bench_path
        )
    ).first()


class CreateSiteAction(Action):
    """`site.create` — create a Frappe site, wrapping `bench new-site` (gotcha #4)
    in the dev-bench Redis dance (gotcha #3) as explicit steps in ONE job.

    Steps: (1) detect dev vs production from supervisor/systemd presence; (2) on
    a DEV bench, start the bench's own Redis (queue + cache) — else `new-site`
    fails `Error 111 connecting to 127.0.0.1:11000`; (3) run the non-interactive
    `bench new-site` (root + admin passwords supplied as flags so nothing ever
    prompts and hangs the job); (4) register the site row; (5) on a DEV bench,
    shut the Redis back down so a later `bench start` can bind — even if the
    create failed (the shutdown is in a `finally`).

    Non-idempotent: a determinate `new-site` failure is never auto-retried on top
    of a half-created site. Secrets (db_root_pw, admin_pw) are resolved from the
    job's secret sources at render time and redacted from every log line."""

    async def run(self, ctx: JobContext) -> None:
        from app.core import discovery
        from app.core.commands import get_template, render

        params = ctx.rendered.params_sanitized
        secrets = ctx.rendered.secret_map
        site = params["site"]
        bench_path = params["bench_path"]

        bench = _load_bench(ctx, bench_path)
        queue_port = (bench.redis_queue_port if bench else None) or DEFAULT_REDIS_QUEUE_PORT
        cache_port = (bench.redis_cache_port if bench else None) or DEFAULT_REDIS_CACHE_PORT

        # 1) Detect dev vs production (supervisor conf presence).
        is_dev = True
        with ctx.step("Detect bench mode"):
            res = await ctx.capture(["bash", "-c", _MODE_PROBE], cwd=bench_path)
            is_dev = "PROD" not in res.stdout
            await ctx.emit(
                f"Bench mode: {'development' if is_dev else 'production'} "
                f"({res.stdout.strip() or 'unknown'})."
            )

        # Build the real `bench new-site` argv through the safe registry using the
        # resolved secrets — never string interpolation, secrets redacted in logs.
        new_site = render(
            get_template("site.new"),
            {
                "site": site,
                "db_root_pw": secrets["db_root_pw"],
                "admin_pw": secrets["admin_pw"],
                "bench_path": bench_path,
            },
        )

        started_redis = False
        try:
            # 2) Dev bench: start bench-owned Redis before the site op (gotcha #3).
            if is_dev:
                with ctx.step("Start dev bench Redis (queue + cache)"):
                    for conf in ("config/redis_queue.conf", "config/redis_cache.conf"):
                        await ctx.emit(f"$ redis-server {conf} --daemonize yes")
                        # Best-effort: already-running Redis exits non-zero; that's
                        # fine, the port we need is up either way.
                        code = await ctx.stream(
                            ["redis-server", conf, "--daemonize", "yes"], cwd=bench_path
                        )
                        await ctx.emit(f"redis-server {conf} exited {code}.")
                    started_redis = True

            # 3) The actual, non-interactive site creation (gotcha #4).
            with ctx.step("Create site (bench new-site)"):
                await ctx.emit(f"$ {new_site.display}")
                code = await ctx.stream(new_site.argv, cwd=new_site.cwd)
                if code != 0:
                    raise RuntimeError(f"bench new-site exited with status {code}")

            # 4) Register the new site in the platform inventory.
            with ctx.step("Register site"):
                if bench is None:
                    await ctx.emit(
                        "Bench not in inventory yet — run a discovery to link this "
                        "site to its bench."
                    )
                else:
                    row = discovery.upsert_site_one(ctx.session, bench.id, site)
                    await ctx.emit(f"Registered site #{row.id} ({site}).")
        finally:
            # 5) Dev bench: shut the Redis we started back down so `bench start`
            #    can bind its ports later (gotcha #3). Best-effort (|| true).
            if started_redis:
                with ctx.step("Shut down dev bench Redis"):
                    for port in (queue_port, cache_port):
                        await ctx.emit(f"$ redis-cli -p {port} shutdown nosave")
                        code = await ctx.stream(
                            ["redis-cli", "-p", str(port), "shutdown", "nosave"]
                        )
                        await ctx.emit(f"redis-cli -p {port} shutdown exited {code}.")


class _SiteToggleAction(Action):
    """Shared base for the fast site toggles: run the single bench command, then
    record the resulting state on the Site row so the UI reflects it without a
    full re-discovery. `flag`/`value_from_state` say which column to set."""

    flag: str = ""

    def _new_value(self, state: str) -> bool:  # overridden per toggle
        raise NotImplementedError

    async def run(self, ctx: JobContext) -> None:
        params = ctx.rendered.params_sanitized
        site_name = params["site"]
        bench_path = params["bench_path"]
        state = params["state"]

        with ctx.step("Run command"):
            code = await ctx.stream(ctx.rendered.argv, cwd=ctx.rendered.cwd)
            if code != 0:
                raise RuntimeError(f"command exited with status {code}")

        with ctx.step("Update site state"):
            bench = _load_bench(ctx, bench_path)
            site = None
            if bench is not None:
                from sqlalchemy import select

                from app.models.site import Site

                site = ctx.session.scalars(
                    select(Site).where(
                        Site.bench_id == bench.id, Site.name == site_name
                    )
                ).first()
            if site is None:
                await ctx.emit(
                    f"Site {site_name!r} not in inventory — run a discovery to "
                    "track its state."
                )
                return
            setattr(site, self.flag, self._new_value(state))
            ctx.session.commit()
            await ctx.emit(f"{site_name}: {self.flag} = {getattr(site, self.flag)}.")


class SetSchedulerAction(_SiteToggleAction):
    """`site.set_scheduler` — `bench --site X scheduler enable|disable`, then set
    `Site.scheduler_enabled`."""

    flag = "scheduler_enabled"

    def _new_value(self, state: str) -> bool:
        return state == "enable"


class SetMaintenanceAction(_SiteToggleAction):
    """`site.set_maintenance` — `bench --site X set-maintenance-mode on|off`, then
    set `Site.maintenance_mode`."""

    flag = "maintenance_mode"

    def _new_value(self, state: str) -> bool:
        return state == "on"


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
