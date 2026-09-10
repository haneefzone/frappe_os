"""Action handlers: the multi-step logic a template runs.

An Action structures its work with `ctx.step(...)` context managers and streams
command output through `ctx.stream(...)`. Actions never touch SSH, the DB or
Redis directly — they only talk to the `JobContext`, which the JobRunner
implements. That keeps actions trivially unit-testable with a fake context and
keeps all execution/persistence concerns in `app/core/jobs.py`.
"""

from __future__ import annotations

import re
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
    # This job's id — actions that write rows referencing the job (e.g. a Backup
    # row's `taken_by_job_id`) read it; command actions can ignore it.
    job_id: int

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

    def register_secret(self, value: str) -> None:
        """Register a plaintext secret resolved during the run (e.g. a restic
        repo password) so the log redactor masks it in every line (4.1)."""
        ...

    def read_file(self, path: str, *, chunk_size: int = ...):
        """Yield a remote file's raw bytes over SSH in chunks — for streaming a
        backup artifact to offsite storage without buffering it whole (2.2)."""
        ...

    async def write_file(self, path: str, chunks) -> int:
        """Stream bytes into a remote file over SSH (binary-safe), returning the
        exit status — the write side of a cross-server backup move (2.6)."""
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


def _load_site(ctx: JobContext, bench, site_name: str):
    """Fetch the Site row for a bench + name, or None (bench may be None)."""
    if bench is None:
        return None
    from sqlalchemy import select

    from app.models.site import Site

    return ctx.session.scalars(
        select(Site).where(Site.bench_id == bench.id, Site.name == site_name)
    ).first()


def _redis_ports(bench) -> tuple[int, int]:
    """The dev bench's own Redis (queue, cache) ports for the shutdown-after —
    from the discovered row, falling back to the gotcha #3 defaults."""
    queue = (bench.redis_queue_port if bench else None) or DEFAULT_REDIS_QUEUE_PORT
    cache = (bench.redis_cache_port if bench else None) or DEFAULT_REDIS_CACHE_PORT
    return queue, cache


async def _detect_bench_mode(ctx: JobContext, bench_path: str) -> bool:
    """One step: probe supervisor/systemd presence → True on a DEV bench (the
    one that needs the manual Redis dance before a site op)."""
    with ctx.step("Detect bench mode"):
        res = await ctx.capture(["bash", "-c", _MODE_PROBE], cwd=bench_path)
        is_dev = "PROD" not in res.stdout
        await ctx.emit(
            f"Bench mode: {'development' if is_dev else 'production'} "
            f"({res.stdout.strip() or 'unknown'})."
        )
    return is_dev


async def _start_dev_redis(ctx: JobContext, bench_path: str) -> None:
    """Start the dev bench's own Redis (queue + cache) so a site op that
    enqueues background jobs doesn't fail `Error 111` (gotcha #3). Best-effort:
    an already-running Redis exits non-zero and that's fine."""
    with ctx.step("Start dev bench Redis (queue + cache)"):
        for conf in ("config/redis_queue.conf", "config/redis_cache.conf"):
            await ctx.emit(f"$ redis-server {conf} --daemonize yes")
            code = await ctx.stream(
                ["redis-server", conf, "--daemonize", "yes"], cwd=bench_path
            )
            await ctx.emit(f"redis-server {conf} exited {code}.")


async def _stop_dev_redis(ctx: JobContext, queue_port: int, cache_port: int) -> None:
    """Shut the dev bench Redis back down so a later `bench start` can bind its
    ports (gotcha #3). Best-effort. Meant to run in a `finally`."""
    with ctx.step("Shut down dev bench Redis"):
        for port in (queue_port, cache_port):
            await ctx.emit(f"$ redis-cli -p {port} shutdown nosave")
            code = await ctx.stream(
                ["redis-cli", "-p", str(port), "shutdown", "nosave"]
            )
            await ctx.emit(f"redis-cli -p {port} shutdown exited {code}.")


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
        queue_port, cache_port = _redis_ports(bench)

        # 1) Detect dev vs production (supervisor conf presence).
        is_dev = await _detect_bench_mode(ctx, bench_path)

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
                await _start_dev_redis(ctx, bench_path)
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
                await _stop_dev_redis(ctx, queue_port, cache_port)


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


# --------------------------------------------------------------------------- #
# Apps (session 1.9)
# --------------------------------------------------------------------------- #

# GIT_SSH_COMMAND template for a private-repo fetch: use only the staged deploy
# key, auto-accept the host key (a fresh deploy op), and don't touch the user's
# known_hosts. The {key_path} is a platform-generated temp path (never user
# input) and is passed as one argv element, so nothing can be injected.
_GIT_SSH_TEMPLATE = (
    "ssh -i {key_path} -o IdentitiesOnly=yes "
    "-o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=/dev/null"
)

# Fixed script to stage a deploy key on the target: 0600, base64-decoded from an
# argv element. Run via `capture` (NOT streamed), so the key material never
# reaches a persisted/publish log line (rule 6). $1=b64 key, $2=dest path.
_WRITE_KEY_SCRIPT = 'umask 077; printf %s "$1" | base64 -d > "$2" && chmod 600 "$2"'


async def _write_deploy_key(ctx: JobContext, key_path: str, key_pem: str) -> None:
    """Stage a private deploy key at `key_path` (0600) without logging it."""
    import base64

    b64 = base64.b64encode(key_pem.encode()).decode()
    res = await ctx.capture(["bash", "-c", _WRITE_KEY_SCRIPT, "_", b64, key_path])
    if res.exit_code != 0:
        raise RuntimeError("failed to stage deploy key on the target")


async def _get_app_steps(
    ctx: JobContext,
    *,
    source: str,
    branch: str,
    bench_path: str,
    deploy_key: str | None,
) -> None:
    """Run `bench get-app` on the bench as ordered steps. For a private repo,
    stage the deploy key to a temp 0600 file, point GIT_SSH_COMMAND at it for the
    single fetch, and remove it in a `finally` — the key is never streamed to a
    log line."""
    from secrets import token_hex

    from app.core.commands import get_template, render

    get = render(
        get_template("app.get"),
        {"branch": branch, "source": source, "bench_path": bench_path},
    )

    argv = list(get.argv)
    key_path: str | None = None
    try:
        if deploy_key:
            key_path = f"/tmp/fdm-deploykey-{token_hex(8)}"
            with ctx.step("Stage deploy key"):
                await _write_deploy_key(ctx, key_path, deploy_key)
                await ctx.emit(
                    "Deploy key staged at a temp 0600 file; removed after fetch."
                )
            git_ssh = _GIT_SSH_TEMPLATE.format(key_path=key_path)
            argv = ["env", f"GIT_SSH_COMMAND={git_ssh}", *get.argv]

        with ctx.step(f"Fetch app ({branch})"):
            await ctx.emit(f"$ {get.display}")
            code = await ctx.stream(argv, cwd=get.cwd)
            if code != 0:
                raise RuntimeError(f"bench get-app exited with status {code}")
    finally:
        if key_path is not None:
            with ctx.step("Remove deploy key"):
                await ctx.capture(["rm", "-f", key_path])
                await ctx.emit("Deploy key removed from the target.")


async def _install_app_step(
    ctx: JobContext, *, site: str, app: str, bench_path: str
) -> None:
    """One step: the actual `bench --site X install-app APP`."""
    from app.core.commands import get_template, render

    inst = render(
        get_template("app.install"),
        {"site": site, "app": app, "bench_path": bench_path},
    )
    with ctx.step(f"Install {app} on {site}"):
        await ctx.emit(f"$ {inst.display}")
        code = await ctx.stream(inst.argv, cwd=inst.cwd)
        if code != 0:
            raise RuntimeError(f"bench install-app exited with status {code}")


async def _app_version(ctx: JobContext, bench_path: str, app: str) -> str | None:
    """Read one app's version from plain `bench version` (best-effort)."""
    from app.core.appsources import parse_app_version

    try:
        res = await ctx.capture(["bench", "version"], cwd=bench_path)
    except Exception:
        return None
    return parse_app_version(res.stdout, app)


def _register_installed_app(
    ctx: JobContext,
    *,
    bench,
    site_name: str,
    app: str,
    branch: str | None,
    version: str | None,
) -> None:
    """Upsert the app×site matrix row after a successful install."""
    from sqlalchemy import select

    from app.core.appsources import upsert_installed_app
    from app.models.app import AppSource
    from app.models.site import Site

    site = ctx.session.scalars(
        select(Site).where(Site.bench_id == bench.id, Site.name == site_name)
    ).first()
    if site is None:
        # Register the site row first so the matrix has something to hang off.
        from app.core import discovery

        site = discovery.upsert_site_one(ctx.session, bench.id, site_name)

    source_id: int | None = None
    source_name = ctx.rendered.params_sanitized.get("source_name")
    if source_name:
        src = ctx.session.scalars(
            select(AppSource).where(AppSource.name == source_name)
        ).first()
        source_id = src.id if src else None

    row = upsert_installed_app(
        ctx.session,
        site_id=site.id,
        bench_id=bench.id,
        app_name=app,
        app_source_id=source_id,
        branch=branch,
        version=version,
    )
    return row


class GetAppAction(Action):
    """`app.get` — fetch an app onto a bench (deploy-key dance for private
    repos). Registered so the orchestrator renders `bench get-app` through the
    safe registry; runnable standalone too."""

    async def run(self, ctx: JobContext) -> None:
        params = ctx.rendered.params_sanitized
        deploy_key = ctx.rendered.secret_map.get("deploy_key")
        await _get_app_steps(
            ctx,
            source=params["source"],
            branch=params["branch"],
            bench_path=params["bench_path"],
            deploy_key=deploy_key,
        )


class InstallAppOnSiteAction(Action):
    """`site.install_app` — the orchestrator POST /api/sites/{id}/apps launches:
    get the app onto the bench if it isn't present yet, then `install-app` on the
    site, all as chained steps in ONE non-idempotent job locked on the site.

    Reuses the 1.8 dev-bench Redis dance (gotcha #3) around the install (install
    enqueues background jobs), and the shared get-app deploy-key dance for
    private sources. Secrets (the deploy key) are resolved at render time and
    redacted from every log line."""

    async def run(self, ctx: JobContext) -> None:
        params = ctx.rendered.params_sanitized
        site = params["site"]
        app = params["app"]
        bench_path = params["bench_path"]
        source = params.get("source")
        branch = params.get("branch")
        deploy_key = ctx.rendered.secret_map.get("deploy_key")

        bench = _load_bench(ctx, bench_path)
        queue_port, cache_port = _redis_ports(bench)

        # 1) Fetch the app onto the bench first if a source was given (a
        #    marketplace name already resolvable by bench also flows through
        #    get-app; already-present apps are a no-op get that bench short-circuits).
        if source:
            await _get_app_steps(
                ctx,
                source=source,
                branch=branch or "",
                bench_path=bench_path,
                deploy_key=deploy_key,
            )

        # 2) Detect dev/prod, run the install wrapped in the Redis dance.
        is_dev = await _detect_bench_mode(ctx, bench_path)
        started_redis = False
        try:
            if is_dev:
                await _start_dev_redis(ctx, bench_path)
                started_redis = True

            await _install_app_step(ctx, site=site, app=app, bench_path=bench_path)

            # 3) Register the app×site matrix cell.
            with ctx.step("Register installed app"):
                version = await _app_version(ctx, bench_path, app)
                if bench is None:
                    await ctx.emit(
                        "Bench not in inventory yet — run a discovery to link this "
                        "install to its site."
                    )
                else:
                    _register_installed_app(
                        ctx,
                        bench=bench,
                        site_name=site,
                        app=app,
                        branch=branch,
                        version=version,
                    )
                    await ctx.emit(
                        f"Registered {app} on {site} "
                        f"(version {version or 'unknown'})."
                    )
        finally:
            if started_redis:
                await _stop_dev_redis(ctx, queue_port, cache_port)


class UninstallAppAction(Action):
    """`app.uninstall` — DESTRUCTIVE (`danger` permission): `bench --site X
    uninstall-app APP --yes`, wrapped in the dev-bench Redis dance, then drop the
    matrix row. The type-the-app-name confirm is enforced in the UI + API.

    Per CLAUDE.md rule 5, an AUTOMATIC pre-op backup (with files) runs first,
    recorded as its own Backup row and visible in the timeline; a failed pre-op
    backup aborts the uninstall (same posture as `RestoreAction`'s pre-restore
    backup). Non-idempotent: never auto-retried."""

    async def run(self, ctx: JobContext) -> None:
        params = ctx.rendered.params_sanitized
        site = params["site"]
        app = params["app"]
        bench_path = params["bench_path"]

        bench = _load_bench(ctx, bench_path)
        queue_port, cache_port = _redis_ports(bench)

        is_dev = await _detect_bench_mode(ctx, bench_path)
        started_redis = False
        try:
            if is_dev:
                await _start_dev_redis(ctx, bench_path)
                started_redis = True

            # Rule 5: automatic pre-op backup FIRST (with files), inside the dev
            # Redis dance (redis is already up). Uninstalling an app is destructive
            # (drops the app's DocTypes/data) — a failed backup aborts the uninstall.
            await ctx.emit(
                "Taking an automatic pre-uninstall backup before removing the app."
            )
            try:
                await _run_backup(
                    ctx,
                    site=site,
                    bench_path=bench_path,
                    bench=bench,
                    with_files=True,
                    step_label="Pre-uninstall backup (with files)",
                )
            except Exception as exc:  # noqa: BLE001 — no uninstall without a safety net
                raise RuntimeError(
                    f"pre-uninstall backup failed ({exc}); not uninstalling {app}"
                ) from exc

            with ctx.step(f"Uninstall {app} from {site}"):
                await ctx.emit(f"$ {ctx.rendered.display}")
                code = await ctx.stream(ctx.rendered.argv, cwd=ctx.rendered.cwd)
                if code != 0:
                    raise RuntimeError(f"bench uninstall-app exited with status {code}")

            with ctx.step("Update installed-app matrix"):
                from sqlalchemy import select

                from app.core.appsources import remove_installed_app
                from app.models.site import Site

                site_row = None
                if bench is not None:
                    site_row = ctx.session.scalars(
                        select(Site).where(
                            Site.bench_id == bench.id, Site.name == site
                        )
                    ).first()
                if site_row is not None and remove_installed_app(
                    ctx.session, site_id=site_row.id, app_name=app
                ):
                    await ctx.emit(f"Removed {app} from the {site} matrix row.")
                else:
                    await ctx.emit(f"No matrix row for {app} on {site} to remove.")
        finally:
            if started_redis:
                await _stop_dev_redis(ctx, queue_port, cache_port)


class ListBranchesAction(Action):
    """`app.list_branches` — `git ls-remote --heads {url}` for the branch picker.
    Read-only; emits a machine-readable `BRANCHES_RESULT <json>` line the wizard
    tails for. Private sources use the same deploy-key dance."""

    async def run(self, ctx: JobContext) -> None:
        import json
        from secrets import token_hex

        params = ctx.rendered.params_sanitized
        url = params["url"]
        deploy_key = ctx.rendered.secret_map.get("deploy_key")

        argv = list(ctx.rendered.argv)
        key_path: str | None = None
        branches: list[str] = []
        try:
            if deploy_key:
                key_path = f"/tmp/fdm-deploykey-{token_hex(8)}"
                with ctx.step("Stage deploy key"):
                    await _write_deploy_key(ctx, key_path, deploy_key)
                git_ssh = _GIT_SSH_TEMPLATE.format(key_path=key_path)
                argv = ["env", f"GIT_SSH_COMMAND={git_ssh}", *ctx.rendered.argv]

            with ctx.step("List remote branches"):
                await ctx.emit(f"$ git ls-remote --heads {url}")
                res = await ctx.capture(argv)
                if res.exit_code != 0:
                    raise RuntimeError(
                        f"git ls-remote exited with status {res.exit_code}"
                    )
                for line in res.stdout.splitlines():
                    parts = line.split("\trefs/heads/")
                    if len(parts) == 2:
                        branches.append(parts[1].strip())
                await ctx.emit(f"Found {len(branches)} branch(es).")
        finally:
            if key_path is not None:
                with ctx.step("Remove deploy key"):
                    await ctx.capture(["rm", "-f", key_path])

        await ctx.emit("BRANCHES_RESULT " + json.dumps(sorted(branches)), stream="result")


# --------------------------------------------------------------------------- #
# Maintenance actions (session 1.10)
# --------------------------------------------------------------------------- #


def _active_sites(ctx: JobContext, bench):
    """The bench's active (non-missing) sites from the platform inventory, for
    the bulk migrate / update safety-backup fan-outs."""
    from sqlalchemy import select

    from app.models.site import Site

    return list(
        ctx.session.scalars(
            select(Site)
            .where(Site.bench_id == bench.id, Site.status == "active")
            .order_by(Site.name)
        ).all()
    )


class SiteMaintenanceAction(Action):
    """Shared orchestrator for the single-command site maintenance ops
    (`site.migrate`, `site.clear_cache`, `site.clear_website_cache`).

    Each template renders its own fixed `bench --site X <verb>` argv; this action
    detects dev vs production and, on a DEV bench, runs the bench-owned Redis
    dance (gotcha #3) around the command — `migrate` and `clear-cache` touch the
    bench's redis, which isn't up when `bench start` isn't running. The Redis is
    shut back down in a `finally` even if the command failed, so a later `bench
    start` can bind its ports."""

    async def run(self, ctx: JobContext) -> None:
        params = ctx.rendered.params_sanitized
        site = params["site"]
        bench_path = params["bench_path"]
        # argv is ("bench", "--site", "{site}", "<verb>"); the verb is the label.
        verb = ctx.rendered.argv[3] if len(ctx.rendered.argv) > 3 else "run"

        bench = _load_bench(ctx, bench_path)
        queue_port, cache_port = _redis_ports(bench)
        is_dev = await _detect_bench_mode(ctx, bench_path)

        started_redis = False
        try:
            if is_dev:
                await _start_dev_redis(ctx, bench_path)
                started_redis = True

            with ctx.step(f"bench {verb} ({site})"):
                await ctx.emit(f"$ {ctx.rendered.display}")
                code = await ctx.stream(ctx.rendered.argv, cwd=ctx.rendered.cwd)
                if code != 0:
                    raise RuntimeError(f"bench {verb} exited with status {code}")
        finally:
            if started_redis:
                await _stop_dev_redis(ctx, queue_port, cache_port)


class SiteBackupAction(Action):
    """`site.backup_db` / `site.backup_files` — the raw `bench backup` command.

    Registered so the bench.update safety pre-step and the 1.11 BackupAction
    engine render the real backup command through the safe registry. Runs the
    single command as one step (no artifact parsing / Backup row — that is the
    engine's job); not launched on its own path."""

    async def run(self, ctx: JobContext) -> None:
        site = ctx.rendered.params_sanitized["site"]
        with ctx.step(f"Backup {site}"):
            await ctx.emit(f"$ {ctx.rendered.display}")
            code = await ctx.stream(ctx.rendered.argv, cwd=ctx.rendered.cwd)
            if code != 0:
                raise RuntimeError(f"bench backup exited with status {code}")


class BenchBuildAction(Action):
    """`bench.build` — (re)compile the bench's JS/CSS assets. One step, no site,
    no redis; safely repeatable (idempotent)."""

    async def run(self, ctx: JobContext) -> None:
        with ctx.step("Build assets (bench build)"):
            await ctx.emit(f"$ {ctx.rendered.display}")
            code = await ctx.stream(ctx.rendered.argv, cwd=ctx.rendered.cwd)
            if code != 0:
                raise RuntimeError(f"bench build exited with status {code}")


class BenchRestartAction(Action):
    """`bench.restart` — restart the bench's services, mode-aware (session 1.10).

    Detects dev vs production from supervisor/systemd presence. On a PRODUCTION
    bench it runs `sudo -n supervisorctl restart <bench-basename>:*` (rendered
    through the `bench.supervisor_restart` template; `sudo -n` fails loudly if the
    ratified sudoers allowlist isn't installed). On a DEV bench there is nothing
    to restart under supervisor — dev benches run via a manual `bench start` — so
    it fails with an informative message rather than pretending to succeed."""

    async def run(self, ctx: JobContext) -> None:
        import posixpath

        from app.core.commands import RenderError, get_template, render

        bench_path = ctx.rendered.params_sanitized["bench_path"]
        is_dev = await _detect_bench_mode(ctx, bench_path)

        if is_dev:
            with ctx.step("Restart bench"):
                await ctx.emit(
                    "This is a development bench (no supervisor/systemd config). "
                    "Dev benches are not managed by supervisor — they run in the "
                    "foreground via `bench start`, which the platform cannot "
                    "restart for you. Open a terminal to the server and run "
                    f"`bench start` in {bench_path} (or stop and re-run it)."
                )
                raise RuntimeError(
                    "development bench restart is manual (bench start)"
                )

        group = f"{posixpath.basename(bench_path.rstrip('/'))}:*"
        try:
            restart = render(get_template("bench.supervisor_restart"), {"group": group})
        except RenderError as exc:
            with ctx.step("Restart bench (supervisorctl)"):
                await ctx.emit(
                    f"Could not build a supervisor group target from {bench_path!r}: "
                    f"{exc}. Restart the bench's services manually."
                )
                raise RuntimeError("could not derive supervisor group target") from exc

        with ctx.step("Restart bench (supervisorctl)"):
            await ctx.emit(f"$ {restart.display}")
            code = await ctx.stream(restart.argv, cwd=restart.cwd)
            if code != 0:
                raise RuntimeError(
                    f"supervisorctl restart exited with status {code} "
                    "(is the fdm-platform sudoers allowlist installed?)"
                )


class MigrateAllSitesAction(Action):
    """`bench.migrate_all` — migrate every known active site on the bench, each as
    its own ordered step in ONE job (session 1.10).

    Loads the bench's active sites from the platform inventory, then runs `bench
    --site X migrate` for each — wrapped once in the dev-bench Redis dance so the
    per-site migrations don't each start/stop redis. A per-site failure is
    recorded and the run continues to the next site; the job fails at the end if
    any site failed, so a partial run is never reported as a clean success."""

    async def run(self, ctx: JobContext) -> None:
        from app.core.commands import get_template, render

        bench_path = ctx.rendered.params_sanitized["bench_path"]
        bench = _load_bench(ctx, bench_path)
        if bench is None:
            with ctx.step("Load sites"):
                await ctx.emit(
                    "Bench not in inventory — run a discovery so the platform "
                    "knows which sites live on it."
                )
                raise RuntimeError("bench not found in inventory")

        sites = _active_sites(ctx, bench)
        with ctx.step("Plan migration"):
            names = ", ".join(s.name for s in sites) or "(none)"
            await ctx.emit(f"Migrating {len(sites)} active site(s) on {bench_path}: {names}")
        if not sites:
            return

        queue_port, cache_port = _redis_ports(bench)
        is_dev = await _detect_bench_mode(ctx, bench_path)

        started_redis = False
        failures: list[str] = []
        try:
            if is_dev:
                await _start_dev_redis(ctx, bench_path)
                started_redis = True

            for site in sites:
                mig = render(
                    get_template("site.migrate"),
                    {"site": site.name, "bench_path": bench_path},
                )
                try:
                    with ctx.step(f"Migrate {site.name}"):
                        await ctx.emit(f"$ {mig.display}")
                        code = await ctx.stream(mig.argv, cwd=mig.cwd)
                        if code != 0:
                            raise RuntimeError(
                                f"bench migrate exited with status {code}"
                            )
                except Exception as exc:  # noqa: BLE001 — one bad site must not sink the batch
                    failures.append(site.name)
                    await ctx.emit(f"Migration failed for {site.name}: {exc}")
        finally:
            if started_redis:
                await _stop_dev_redis(ctx, queue_port, cache_port)

        if failures:
            raise RuntimeError(f"migration failed for: {', '.join(failures)}")


class BenchUpdateAction(Action):
    """`bench.update` — update the whole bench (git pull + deps + patches + build
    + restart), preceded by an automatic lightweight safety backup (session 1.10).

    Runs (1) a db-only `bench --site X backup` for every known active site as the
    FIRST step — a safety net before a potentially breaking update — then (2) the
    long-running `bench update`. Both are wrapped once in the dev-bench Redis
    dance (update runs migrate/build which touch redis). Non-idempotent: a
    determinate update failure is never auto-retried on top of a half-update."""

    async def run(self, ctx: JobContext) -> None:
        from app.core.commands import get_template, render

        bench_path = ctx.rendered.params_sanitized["bench_path"]
        bench = _load_bench(ctx, bench_path)
        sites = _active_sites(ctx, bench) if bench is not None else []
        queue_port, cache_port = _redis_ports(bench)
        is_dev = await _detect_bench_mode(ctx, bench_path)

        started_redis = False
        try:
            if is_dev:
                await _start_dev_redis(ctx, bench_path)
                started_redis = True

            # 1) Safety backup FIRST (acceptance: the backup step is visible
            #    before the update runs). db-only, best-effort per site but a
            #    failed backup aborts the update — we don't update without one.
            with ctx.step("Safety backup (db-only, all sites)"):
                if not sites:
                    await ctx.emit(
                        "No known sites on this bench to back up — run a discovery "
                        "to inventory them. Proceeding with the update."
                    )
                for site in sites:
                    bkp = render(
                        get_template("site.backup_db"),
                        {"site": site.name, "bench_path": bench_path},
                    )
                    await ctx.emit(f"$ {bkp.display}")
                    code = await ctx.stream(bkp.argv, cwd=bkp.cwd)
                    if code != 0:
                        raise RuntimeError(
                            f"safety backup failed for {site.name} (status {code}); "
                            "not running the update"
                        )

            # 2) The long-running update itself.
            with ctx.step("Update bench (bench update)"):
                await ctx.emit(f"$ {ctx.rendered.display}")
                code = await ctx.stream(ctx.rendered.argv, cwd=ctx.rendered.cwd)
                if code != 0:
                    raise RuntimeError(f"bench update exited with status {code}")
        finally:
            if started_redis:
                await _stop_dev_redis(ctx, queue_port, cache_port)


# --------------------------------------------------------------------------- #
# Backup & guided restore (session 1.11)
# --------------------------------------------------------------------------- #


async def _run_backup(
    ctx: JobContext,
    *,
    site: str,
    bench_path: str,
    bench,
    with_files: bool,
    backup_id: str | None = None,
    step_label: str | None = None,
):
    """Run `bench backup [--with-files]` and record a `Backup` row from the
    parsed artifacts. Shared by `BackupAction` (the standalone backup engine) and
    `RestoreAction` (its automatic pre-restore backup). Assumes the dev-bench
    Redis dance is already handled by the caller.

    Returns the (updated) Backup row, or None if the site/bench isn't in
    inventory yet (the artifacts still get captured and logged, just not stored).
    """
    from app.core import backups as bk
    from app.core.commands import get_template, render
    from app.models.backup import Backup

    backup_type = "with-files" if with_files else "db"
    row = ctx.session.get(Backup, int(backup_id)) if backup_id else None
    site_row = _load_site(ctx, bench, site)
    if row is None and site_row is not None and bench is not None:
        row = bk.create_pending_backup(
            ctx.session,
            site_id=site_row.id,
            bench_id=bench.id,
            backup_type=backup_type,
            taken_by_job_id=ctx.job_id,
        )

    tmpl = "site.backup_files" if with_files else "site.backup_db"
    cmd = render(get_template(tmpl), {"site": site, "bench_path": bench_path})
    label = step_label or f"Back up {site} ({'with files' if with_files else 'db-only'})"
    try:
        with ctx.step(label):
            await ctx.emit(f"$ {cmd.display}")
            code = await ctx.stream(cmd.argv, cwd=cmd.cwd)
            if code != 0:
                raise RuntimeError(f"bench backup exited with status {code}")

        with ctx.step("Capture backup artifacts"):
            res = await ctx.capture(
                ["bash", "-c", bk.ARTIFACT_INSPECT_SCRIPT, "_", site], cwd=bench_path
            )
            parsed = bk.parse_artifacts(res.stdout)
            frappe_version = getattr(bench, "frappe_version", None) if bench else None
            produced_type = "with-files" if parsed.has_files else "db"
            if row is not None:
                bk.record_backup(
                    ctx.session,
                    backup=row,
                    parsed=parsed,
                    backup_type=produced_type,
                    frappe_version=frappe_version,
                )
            total_mb = parsed.total_size / (1024 * 1024)
            await ctx.emit(
                f"Captured {len(parsed.artifacts)} artifact(s), "
                f"{total_mb:.1f} MB total; sha256 recorded per artifact."
            )
            for art in parsed.artifacts:
                await ctx.emit(
                    f"  {art.kind}: {art.path} "
                    f"({art.size_bytes} B, sha256 {art.checksum_sha256[:12]}…)"
                )
    except Exception:
        # Leave a visible failed record instead of a vanished backup.
        if row is not None:
            row.status = "failed"
            ctx.session.commit()
        raise
    return row


async def _upload_backup_offsite(ctx: JobContext, row, storage_target_id: int) -> None:
    """Push a recorded backup's artifacts to an S3-compatible target, re-verifying
    each artifact's sha256 against what 1.11 recorded (session 2.2).

    The backup itself already succeeded (artifacts are on-server); this is the
    offsite copy. `storage_state` tracks the outcome (uploading -> offsite/failed)
    while the Backup's own `status` stays success — a failed upload never
    invalidates a good local backup. An upload/verify failure raises so the job
    (and the step) surface it; the operator re-runs once the target is fixed.
    """
    from app.core import storage as st
    from app.models.storage import StorageTarget

    target = ctx.session.get(StorageTarget, storage_target_id)
    if target is None or not target.enabled:
        await ctx.emit(
            f"Storage target #{storage_target_id} is not available (deleted or "
            "disabled) — the backup stays local. Configure a target and re-run "
            "to push it offsite."
        )
        return

    with ctx.step(f"Upload to offsite storage ({target.name})"):
        cfg = st.S3Config.from_target(target)  # decrypts keys in memory only
        client = st.build_client(cfg)
        row.storage_state = "uploading"
        ctx.session.commit()

        object_keys: dict[str, str] = {}
        failures: list[str] = []
        for art in row.artifacts or []:
            kind = art.get("kind", "?")
            path = art.get("path", "")
            expected = art.get("checksum_sha256", "")
            if not path or not expected:
                continue
            await ctx.emit(f"Uploading {kind} → s3://{cfg.bucket}/…")
            result = await st.upload_artifact(
                client,
                cfg,
                kind=kind,
                artifact_path=path,
                expected_sha256=expected,
                backup_id=row.id,
                read_chunks=ctx.read_file,
            )
            if result.ok:
                object_keys[kind] = result.key
                size_mb = result.size_bytes / (1024 * 1024)
                await ctx.emit(
                    f"  ✓ {kind}: {size_mb:.1f} MB, sha256 re-verified "
                    f"({result.actual_sha256[:12]}…) → {result.key}"
                )
            else:
                failures.append(f"{kind} ({result.error})")
                await ctx.emit(f"  ✗ {kind}: {result.error}")

        if failures:
            row.storage_state = "failed"
            row.object_keys = object_keys
            ctx.session.commit()
            raise RuntimeError(
                "offsite upload failed for: " + ", ".join(failures)
            )

        row.object_keys = object_keys
        row.storage_target_id = target.id
        row.storage_state = "offsite"
        ctx.session.commit()
        await ctx.emit(
            f"All {len(object_keys)} artifact(s) offsite in "
            f"{target.name!r}; checksums re-verified end to end."
        )


async def _download_with_digest(client, cfg, key: str, digest):
    """Stream one S3 object, folding each chunk into `digest` as it passes so the
    caller learns the artifact's sha256 the moment the write finishes (2.6)."""
    from app.core import storage as st

    async for chunk in st.download_object(client, cfg, key):
        digest.update(chunk)
        yield chunk


def _resolve_target_site_id(ctx: JobContext, bench_id: int, site_name: str) -> int:
    """The Site row for `site_name` on the destination bench — the moved copy is
    registered against it so it shows up under that site's Backups and is ready
    to restore. The API validates existence; this re-checks under the job."""
    from sqlalchemy import select

    from app.models.site import Site

    site = ctx.session.scalars(
        select(Site).where(Site.bench_id == bench_id, Site.name == site_name)
    ).first()
    if site is None:
        raise RuntimeError(
            f"site {site_name!r} does not exist on the destination bench "
            f"(#{bench_id}); create it there before moving a backup onto it"
        )
    return site.id


def _source_server_id(ctx: JobContext, source_backup) -> int | None:
    """The server the source backup's artifacts came off (via its bench), for the
    moved copy's provenance. None if the source bench is gone."""
    from app.models.bench import Bench

    bench = ctx.session.get(Bench, source_backup.bench_id)
    return bench.server_id if bench is not None else None


class MoveBackupAction(Action):
    """`backup.move_across_servers` — copy a backup's artifacts from their offsite
    S3 target down onto another managed server, re-verifying each artifact's
    sha256 on arrival, then registering the moved copy as a first-class Backup on
    the destination (session 2.6).

    S3 is the transit medium on purpose: an agentless control plane has no direct
    A→B trust, so a cross-server move streams each object out of the shared
    StorageTarget straight into the destination server's file over SSH (never
    buffered whole), computing the sha256 in flight AND re-running `sha256sum` on
    the landed file. A mismatch on either check refuses the move — a corrupt or
    truncated copy must never register as a good backup (rule 5 spirit). The
    source backup must already be offsite (session 2.2); the API enforces it and
    passes the source's own storage target so the keys are read server-side.
    """

    async def run(self, ctx: JobContext) -> None:
        import hashlib
        import posixpath

        from app.core import storage as st
        from app.models.backup import Backup
        from app.models.storage import StorageTarget

        params = ctx.rendered.params_sanitized
        source_backup_id = int(params["backup_id"])
        storage_target_id = int(params["storage_target_id"])
        dest_dir = params["dest_dir"]
        target_site = params["target_site"]
        target_bench_id = int(params["target_bench_id"])

        source = ctx.session.get(Backup, source_backup_id)
        if source is None:
            raise RuntimeError(f"source backup #{source_backup_id} no longer exists")
        target = ctx.session.get(StorageTarget, storage_target_id)
        if target is None or not target.enabled:
            raise RuntimeError(
                f"storage target #{storage_target_id} is unavailable (deleted or "
                "disabled); the artifacts live there in transit — cannot move"
            )

        cfg = st.S3Config.from_target(target)  # decrypts keys in memory only
        client = st.build_client(cfg)
        object_keys = source.object_keys or {}
        await ctx.emit(
            f"Moving backup #{source.id} from {target.name!r} onto "
            f"{target_site} (bench #{target_bench_id}) at {dest_dir}"
        )

        moved_artifacts: list[dict] = []
        by_kind_path: dict[str, str] = {}
        total = 0
        for art in source.artifacts or []:
            kind = art.get("kind", "?")
            src_path = art.get("path", "")
            expected = art.get("checksum_sha256", "")
            key = object_keys.get(kind)
            if not src_path or not expected or not key:
                await ctx.emit(f"Skipping {kind}: no offsite object recorded.")
                continue
            dest = posixpath.join(dest_dir, posixpath.basename(src_path))

            with ctx.step(f"Transfer {kind} → {dest}"):
                digest = hashlib.sha256()
                code = await ctx.write_file(
                    dest, _download_with_digest(client, cfg, key, digest)
                )
                if code != 0:
                    raise RuntimeError(
                        f"writing {kind} to {dest} failed (exit {code}); check the "
                        "destination directory exists and is writable by the bench user"
                    )
                in_flight = digest.hexdigest()
                if in_flight != expected:
                    raise RuntimeError(
                        f"{kind} checksum changed in transit (expected "
                        f"{expected[:12]}…, got {in_flight[:12]}…) — move refused"
                    )

            with ctx.step(f"Verify {kind} on arrival"):
                res = await ctx.capture(["sha256sum", "--", dest])
                if res.exit_code != 0:
                    raise RuntimeError(
                        f"could not read back {dest} to verify (exit {res.exit_code})"
                    )
                on_disk = (res.stdout.split() or [""])[0]
                if on_disk != expected:
                    raise RuntimeError(
                        f"{kind} checksum on the destination does not match "
                        f"(expected {expected[:12]}…, got {on_disk[:12]}…) — move refused"
                    )
                size = int(art.get("size_bytes") or 0)
                total += size
                moved_artifacts.append(
                    {
                        "kind": kind,
                        "path": dest,
                        "size_bytes": size,
                        "checksum_sha256": expected,
                    }
                )
                by_kind_path[kind] = dest
                await ctx.emit(
                    f"  ✓ {kind}: {size / (1024 * 1024):.1f} MB, sha256 re-verified "
                    f"on arrival ({expected[:12]}…)"
                )

        if not moved_artifacts:
            raise RuntimeError(
                "no offsite artifacts to move — the source backup has no recorded "
                "object keys (push it offsite first, session 2.2)"
            )

        with ctx.step("Register moved copy"):
            moved = Backup(
                site_id=_resolve_target_site_id(ctx, target_bench_id, target_site),
                bench_id=target_bench_id,
                type=source.type,
                db_path=by_kind_path.get("database"),
                public_files_path=by_kind_path.get("public_files"),
                private_files_path=by_kind_path.get("private_files"),
                config_path=by_kind_path.get("config"),
                size_bytes=total,
                artifacts=moved_artifacts,
                status="success",
                frappe_version=source.frappe_version,
                taken_by_job_id=ctx.job_id,
                storage_state="local",
                moved_from_backup_id=source.id,
                source_server_id=_source_server_id(ctx, source),
            )
            ctx.session.add(moved)
            ctx.session.commit()
            await ctx.emit(
                f"Registered moved backup #{moved.id}: {len(moved_artifacts)} "
                f"artifact(s), {total / (1024 * 1024):.1f} MB, all checksums verified."
            )


class BackupAction(Action):
    """`site.backup` — the full backup engine (session 1.11) + offsite upload (2.2).

    Detects dev vs production, runs the dev-bench Redis dance (gotcha #3) around
    `bench backup [--with-files]`, then inspects the site's backups dir to capture
    each artifact's absolute path, size and sha256 and record a `Backup` row. The
    API pre-creates a `pending` Backup row and passes its id so a failed backup
    still leaves a visible failed record. Non-idempotent: one row per run.

    When a `storage_target_id` is supplied, each recorded artifact is then
    streamed to that S3-compatible target with its sha256 re-verified offsite."""

    async def run(self, ctx: JobContext) -> None:
        params = ctx.rendered.params_sanitized
        site = params["site"]
        bench_path = params["bench_path"]
        with_files = params.get("with_files") == "1"
        backup_id = params.get("backup_id")
        raw_target = params.get("storage_target_id")
        storage_target_id = int(raw_target) if raw_target else None

        bench = _load_bench(ctx, bench_path)
        queue_port, cache_port = _redis_ports(bench)
        is_dev = await _detect_bench_mode(ctx, bench_path)

        started_redis = False
        try:
            if is_dev:
                await _start_dev_redis(ctx, bench_path)
                started_redis = True
            row = await _run_backup(
                ctx,
                site=site,
                bench_path=bench_path,
                bench=bench,
                with_files=with_files,
                backup_id=backup_id,
            )
            if storage_target_id and row is not None and row.status == "success":
                await _upload_backup_offsite(ctx, row, storage_target_id)
        finally:
            if started_redis:
                await _stop_dev_redis(ctx, queue_port, cache_port)


class ValidateBackupAction(Action):
    """`backup.validate` — re-verify a backup's integrity (session 1.11).

    Loads the Backup row, recomputes the sha256 of every stored artifact on the
    server, compares it to what was recorded, and reports the type/version.
    Read-only (no server writes, no Redis); emits a machine-readable
    `VALIDATE_RESULT <json>` line the restore wizard tails before letting the
    operator continue. Idempotent: a transient SSH blip auto-retries."""

    async def run(self, ctx: JobContext) -> None:
        import json

        from app.core import backups as bk
        from app.models.backup import Backup

        params = ctx.rendered.params_sanitized
        backup_id = int(params["backup_id"])
        backup = ctx.session.get(Backup, backup_id)
        if backup is None:
            with ctx.step("Load backup"):
                await ctx.emit(f"Backup #{backup_id} not found.")
                raise RuntimeError(f"backup {backup_id} not found")

        paths = [a.get("path", "") for a in (backup.artifacts or []) if a.get("path")]
        verdicts: list = []
        with ctx.step("Verify artifact checksums"):
            if not paths:
                await ctx.emit("This backup has no recorded artifacts to verify.")
            else:
                res = await ctx.capture(
                    ["bash", "-c", bk.CHECKSUM_VERIFY_SCRIPT, "_", *paths]
                )
                recomputed = bk.parse_checksums(res.stdout)
                verdicts = bk.verify_backup(backup, recomputed)
                for v in verdicts:
                    icon = "✓" if v.ok else ("✗" if v.actual is not None else "?")
                    state = (
                        "match" if v.ok else ("MISSING" if v.actual is None else "MISMATCH")
                    )
                    await ctx.emit(f"[{icon}] {v.kind}: {state} ({v.path})")

        all_ok = bool(verdicts) and all(v.ok for v in verdicts)
        result = {
            "backup_id": backup_id,
            "type": backup.type,
            "frappe_version": backup.frappe_version,
            "size_bytes": backup.size_bytes,
            "artifact_count": len(paths),
            "all_ok": all_ok,
            "artifacts": [
                {"kind": v.kind, "ok": v.ok, "missing": v.actual is None}
                for v in verdicts
            ],
        }
        await ctx.emit("VALIDATE_RESULT " + json.dumps(result), stream="result")
        await ctx.emit(
            "Backup verified — all checksums match."
            if all_ok
            else "Backup verification found problems — see the artifact lines above."
        )


class RestoreAction(Action):
    """`site.restore` — the guided restore orchestrator (session 1.11, gotcha #7).

    One non-idempotent job locked on the target site. Ordered steps:
      1. (new_site mode) create the target site with `bench new-site` first;
      2. (target exists) an AUTOMATIC pre-restore backup, with files, recorded as
         a Backup row so it is visible in the timeline and recoverable;
      3. `bench --site X --force restore <db> [--with-*-files]`;
      4. copy the source `encryption_key` from the backup's config artifact into
         the target site_config (`bench set-config encryption_key`);
      5. `bench --site X migrate` — gotcha #7 exactly;
      6. mark the source backup `restore_tested` and register the target site.

    All wrapped once in the dev-bench Redis dance. Non-idempotent: a restore is
    destructive and is never auto-retried on top of a half-restored site."""

    async def run(self, ctx: JobContext) -> None:
        from app.core import backups as bk
        from app.core.commands import get_template, render
        from app.models.backup import Backup

        params = ctx.rendered.params_sanitized
        secrets = ctx.rendered.secret_map
        site = params["site"]
        bench_path = params["bench_path"]
        mode = params["mode"]
        with_files = params.get("with_files") == "1"
        backup_id = params.get("backup_id")
        db_path = params["db_path"]
        public_files = params.get("public_files")
        private_files = params.get("private_files")
        config_path = params.get("config_path")

        bench = _load_bench(ctx, bench_path)
        queue_port, cache_port = _redis_ports(bench)
        is_dev = await _detect_bench_mode(ctx, bench_path)

        started_redis = False
        try:
            if is_dev:
                await _start_dev_redis(ctx, bench_path)
                started_redis = True

            # 1) new_site: create the target first (gotcha #4), then restore into it.
            if mode == "new_site":
                admin_pw = secrets.get("admin_pw")
                db_root_pw = secrets.get("db_root_pw")
                if not admin_pw or not db_root_pw:
                    raise RuntimeError(
                        "restoring into a new site needs an admin password and the "
                        "server's MariaDB root password"
                    )
                new_site = render(
                    get_template("site.new"),
                    {
                        "site": site,
                        "db_root_pw": db_root_pw,
                        "admin_pw": admin_pw,
                        "bench_path": bench_path,
                    },
                )
                with ctx.step("Create target site (bench new-site)"):
                    await ctx.emit(f"$ {new_site.display}")
                    code = await ctx.stream(new_site.argv, cwd=new_site.cwd)
                    if code != 0:
                        raise RuntimeError(f"bench new-site exited with status {code}")
            else:
                # 2) Target exists → automatic pre-restore backup FIRST (with files),
                #    recorded as its own Backup row and visible in the timeline.
                await ctx.emit(
                    "Target site already exists — taking an automatic pre-restore "
                    "backup before overwriting it."
                )
                try:
                    await _run_backup(
                        ctx,
                        site=site,
                        bench_path=bench_path,
                        bench=bench,
                        with_files=True,
                        step_label="Pre-restore backup (with files)",
                    )
                except Exception as exc:  # noqa: BLE001 — no restore without a safety net
                    raise RuntimeError(
                        f"pre-restore backup failed ({exc}); not restoring over the site"
                    ) from exc

            # 3) The restore itself (db, plus files when the backup carried them).
            if with_files and public_files and private_files:
                rst = render(
                    get_template("site.restore_files"),
                    {
                        "site": site,
                        "bench_path": bench_path,
                        "db_path": db_path,
                        "public_files": public_files,
                        "private_files": private_files,
                    },
                )
                restore_label = "Restore database + files"
            else:
                rst = render(
                    get_template("site.restore_db"),
                    {"site": site, "bench_path": bench_path, "db_path": db_path},
                )
                restore_label = "Restore database"
            with ctx.step(restore_label):
                await ctx.emit(f"$ {rst.display}")
                code = await ctx.stream(rst.argv, cwd=rst.cwd)
                if code != 0:
                    raise RuntimeError(f"bench restore exited with status {code}")

            # 4) gotcha #7: copy the source encryption_key into the target config.
            with ctx.step("Copy encryption_key into target site_config (gotcha #7)"):
                if not config_path:
                    await ctx.emit(
                        "No config artifact in this backup — the source site had no "
                        "site_config backup; skipping the encryption_key copy."
                    )
                else:
                    res = await ctx.capture(["cat", config_path])
                    key = bk.parse_encryption_key(res.stdout)
                    if not key:
                        await ctx.emit(
                            "Source config backup carried no encryption_key — "
                            "nothing to copy (the site had none)."
                        )
                    else:
                        setcfg = render(
                            get_template("site.set_encryption_key"),
                            {"site": site, "bench_path": bench_path, "key": key},
                        )
                        await ctx.emit(f"$ {setcfg.display}")
                        code = await ctx.stream(setcfg.argv, cwd=setcfg.cwd)
                        if code != 0:
                            raise RuntimeError(
                                f"set-config encryption_key exited with status {code}"
                            )
                        await ctx.emit(
                            "encryption_key copied — encrypted fields will decrypt "
                            "after migrate."
                        )

            # 5) gotcha #7: migrate the restored site.
            mig = render(
                get_template("site.migrate"),
                {"site": site, "bench_path": bench_path},
            )
            with ctx.step("Migrate restored site (bench migrate)"):
                await ctx.emit(f"$ {mig.display}")
                code = await ctx.stream(mig.argv, cwd=mig.cwd)
                if code != 0:
                    raise RuntimeError(f"bench migrate exited with status {code}")

            # 6) Mark the source backup restore-tested + register the target site.
            with ctx.step("Finalize restore"):
                if backup_id:
                    backup = ctx.session.get(Backup, int(backup_id))
                    if backup is not None:
                        backup.restore_tested = True
                        ctx.session.commit()
                        await ctx.emit(f"Backup #{backup.id} marked restore-tested.")
                if bench is not None:
                    from app.core import discovery

                    row = discovery.upsert_site_one(ctx.session, bench.id, site)
                    await ctx.emit(f"Target site #{row.id} ({site}) is ready.")
                else:
                    await ctx.emit(
                        "Bench not in inventory — run a discovery to track the "
                        "restored site."
                    )
        finally:
            if started_redis:
                await _stop_dev_redis(ctx, queue_port, cache_port)


# --------------------------------------------------------------------------- #
# Safe update pipeline (session 3.3): clone -> staging -> verify -> promote.
# --------------------------------------------------------------------------- #


def _load_pipeline(ctx: JobContext, pipeline_id):
    """Load the UpdatePipeline row a 3.3 job threads its progress onto, or None
    when the job runs without one (e.g. a standalone verify)."""
    if not pipeline_id:
        return None
    from app.models.update_pipeline import UpdatePipeline

    return ctx.session.get(UpdatePipeline, int(pipeline_id))


async def _restore_artifacts_into_site(
    ctx: JobContext,
    *,
    site: str,
    bench_path: str,
    bench,
    db_path: str,
    public_files: str | None,
    private_files: str | None,
    config_path: str | None,
    create: bool,
    admin_pw: str | None = None,
    db_root_pw: str | None = None,
) -> None:
    """Restore a set of backup artifacts into ``site`` on ``bench``, reusing the
    exact 1.11 restore sequence (gotcha #7): (optionally) create the target site,
    `bench --force restore` (db [+ files]), copy the source encryption_key into
    the target site_config, then `bench migrate`. Shared by the clone-to-staging
    job (create=True) and the promote job's rollback (create=False, restore over
    the existing prod site). The caller owns the dev-bench Redis dance."""
    from app.core import backups as bk
    from app.core.commands import get_template, render
    from app.models.backup import Backup  # noqa: F401  (kept parallel to RestoreAction)

    if create:
        if not admin_pw or not db_root_pw:
            raise RuntimeError(
                "creating the target site needs an admin password and the "
                "server's MariaDB root password"
            )
        new_site = render(
            get_template("site.new"),
            {
                "site": site,
                "db_root_pw": db_root_pw,
                "admin_pw": admin_pw,
                "bench_path": bench_path,
            },
        )
        with ctx.step("Create target site (bench new-site)"):
            await ctx.emit(f"$ {new_site.display}")
            code = await ctx.stream(new_site.argv, cwd=new_site.cwd)
            if code != 0:
                raise RuntimeError(f"bench new-site exited with status {code}")

    with_files = bool(public_files and private_files)
    if with_files:
        rst = render(
            get_template("site.restore_files"),
            {
                "site": site,
                "bench_path": bench_path,
                "db_path": db_path,
                "public_files": public_files,
                "private_files": private_files,
            },
        )
        restore_label = "Restore database + files"
    else:
        rst = render(
            get_template("site.restore_db"),
            {"site": site, "bench_path": bench_path, "db_path": db_path},
        )
        restore_label = "Restore database"
    with ctx.step(restore_label):
        await ctx.emit(f"$ {rst.display}")
        code = await ctx.stream(rst.argv, cwd=rst.cwd)
        if code != 0:
            raise RuntimeError(f"bench restore exited with status {code}")

    with ctx.step("Copy encryption_key into target site_config (gotcha #7)"):
        if not config_path:
            await ctx.emit(
                "No config artifact in this backup — skipping the encryption_key "
                "copy (the source site had none)."
            )
        else:
            res = await ctx.capture(["cat", config_path])
            key = bk.parse_encryption_key(res.stdout)
            if not key:
                await ctx.emit("Source config backup carried no encryption_key.")
            else:
                setcfg = render(
                    get_template("site.set_encryption_key"),
                    {"site": site, "bench_path": bench_path, "key": key},
                )
                await ctx.emit(f"$ {setcfg.display}")
                code = await ctx.stream(setcfg.argv, cwd=setcfg.cwd)
                if code != 0:
                    raise RuntimeError(
                        f"set-config encryption_key exited with status {code}"
                    )

    mig = render(get_template("site.migrate"), {"site": site, "bench_path": bench_path})
    with ctx.step("Migrate restored site (bench migrate)"):
        await ctx.emit(f"$ {mig.display}")
        code = await ctx.stream(mig.argv, cwd=mig.cwd)
        if code != 0:
            raise RuntimeError(f"bench migrate exited with status {code}")


class CloneToStagingAction(Action):
    """`site.clone_to_staging` — clone a (prod) site onto a staging bench (3.3).

    Reuses the 1.11 backup + restore machinery end to end: (1) take a with-files
    backup of the source site, (2) create a fresh staging site and restore the
    backup into it (db + files + encryption_key + migrate), (3) run an optional
    data-scrub hook to mask PII for prod→dev copies (uiux §8), (4) register the
    staging site tagged `environment="staging"`. One non-idempotent job locked on
    the staging target — a clone is never auto-retried on top of a half-clone."""

    async def run(self, ctx: JobContext) -> None:
        from app.core import discovery
        from app.core.commands import get_template, render

        params = ctx.rendered.params_sanitized
        secrets = ctx.rendered.secret_map
        source_site = params["source_site"]
        source_bench_path = params["source_bench_path"]
        staging_site = params["site"]
        staging_bench_path = params["bench_path"]
        scrub_method = params.get("scrub_method")
        pipeline = _load_pipeline(ctx, params.get("pipeline_id"))

        source_bench = _load_bench(ctx, source_bench_path)
        staging_bench = _load_bench(ctx, staging_bench_path)

        # 1) Back up the source site (with files) — the artifacts we clone from.
        s_queue, s_cache = _redis_ports(source_bench)
        src_is_dev = await _detect_bench_mode(ctx, source_bench_path)
        started = False
        try:
            if src_is_dev:
                await _start_dev_redis(ctx, source_bench_path)
                started = True
            row = await _run_backup(
                ctx,
                site=source_site,
                bench_path=source_bench_path,
                bench=source_bench,
                with_files=True,
                step_label=f"Back up source site {source_site} (with files)",
            )
        finally:
            if started:
                await _stop_dev_redis(ctx, s_queue, s_cache)
        if row is None or row.status != "success" or not row.db_path:
            raise RuntimeError(
                "source backup did not produce a usable database artifact; "
                "not cloning"
            )

        # 2) Restore into a fresh staging site, then (3) optional PII scrub.
        t_queue, t_cache = _redis_ports(staging_bench)
        tgt_is_dev = await _detect_bench_mode(ctx, staging_bench_path)
        started = False
        try:
            if tgt_is_dev:
                await _start_dev_redis(ctx, staging_bench_path)
                started = True
            await _restore_artifacts_into_site(
                ctx,
                site=staging_site,
                bench_path=staging_bench_path,
                bench=staging_bench,
                db_path=row.db_path,
                public_files=row.public_files_path,
                private_files=row.private_files_path,
                config_path=row.config_path,
                create=True,
                admin_pw=secrets.get("admin_pw"),
                db_root_pw=secrets.get("db_root_pw"),
            )
            if scrub_method:
                scrub = render(
                    get_template("site.scrub"),
                    {
                        "site": staging_site,
                        "bench_path": staging_bench_path,
                        "method": scrub_method,
                    },
                )
                with ctx.step(f"Scrub PII on the clone ({scrub_method})"):
                    await ctx.emit(f"$ {scrub.display}")
                    code = await ctx.stream(scrub.argv, cwd=scrub.cwd)
                    if code != 0:
                        raise RuntimeError(f"data scrub exited with status {code}")
        finally:
            if started:
                await _stop_dev_redis(ctx, t_queue, t_cache)

        # 4) Register the staging site tagged as a staging environment.
        with ctx.step("Register staging site"):
            if staging_bench is not None:
                site_row = discovery.upsert_site_one(
                    ctx.session, staging_bench.id, staging_site
                )
                site_row.environment = "staging"
                if pipeline is not None:
                    pipeline.staging_site_id = site_row.id
                    pipeline.phase = "cloned"
                ctx.session.commit()
                await ctx.emit(
                    f"Staging site #{site_row.id} ({staging_site}) is ready "
                    "(environment=staging)."
                )
            else:
                await ctx.emit(
                    "Staging bench not in inventory — run a discovery to track "
                    "the clone."
                )


_INT_TOKEN = re.compile(r"-?\d+")


def _parse_row_count(stdout: str | None) -> int | None:
    """Extract the doctype row count from `bench execute get_count` stdout.

    `bench execute` prints the return value (an int) on its own line, but log /
    deprecation lines and version strings can precede it. Scan lines bottom-up
    and return the integer from the last line that carries one, so stray digits
    earlier in the stream (e.g. "frappe 16.24") can't be concatenated in.
    """
    for line in reversed((stdout or "").splitlines()):
        tokens = _INT_TOKEN.findall(line)
        if tokens:
            return int(tokens[-1])
    return None


class VerifyChecklistAction(Action):
    """`site.verify_checklist` — the pre-promote verification gate (uiux §5, 3.3).

    Runs four read-mostly probes against the (staging) site and emits a
    machine-readable `CHECKLIST_RESULT <json>` line the UI renders as the
    all-green gate: (1) the site boots (`frappe.ping`), (2) migrations are clean
    (`bench migrate` is a no-op exit 0), (3) the scheduler is enabled, (4) a
    row-count sanity check vs the source site shows no gross data loss. The
    verdict + all-green flag are persisted onto the UpdatePipeline row so the
    promote endpoint can enforce "verified before promote" server-side."""

    async def run(self, ctx: JobContext) -> None:
        import json

        from app.core.commands import get_template, render

        params = ctx.rendered.params_sanitized
        site = params["site"]
        bench_path = params["bench_path"]
        source_site = params.get("source_site")
        source_bench_path = params.get("source_bench_path")
        pipeline = _load_pipeline(ctx, params.get("pipeline_id"))

        bench = _load_bench(ctx, bench_path)
        queue_port, cache_port = _redis_ports(bench)
        is_dev = await _detect_bench_mode(ctx, bench_path)

        checks: list[dict] = []

        async def _count(target_site: str, target_bench: str) -> int | None:
            cmd = render(
                get_template("site.count_doctype"),
                {"site": target_site, "bench_path": target_bench, "doctype": "User"},
            )
            res = await ctx.capture(cmd.argv, cwd=cmd.cwd)
            return _parse_row_count(res.stdout)

        started = False
        try:
            if is_dev:
                await _start_dev_redis(ctx, bench_path)
                started = True

            # (1) Site boots.
            with ctx.step("Check: site boots (frappe.ping)"):
                ping = render(
                    get_template("site.ping"), {"site": site, "bench_path": bench_path}
                )
                res = await ctx.capture(ping.argv, cwd=ping.cwd)
                boots = res.exit_code == 0 and "pong" in (res.stdout or "").lower()
                checks.append(
                    {"key": "boots", "label": "Site boots", "ok": boots,
                     "detail": "frappe.ping returned pong" if boots else "site did not boot"}
                )

            # (2) Migrations clean (idempotent bench migrate exits 0).
            with ctx.step("Check: migrations clean (bench migrate)"):
                mig = render(
                    get_template("site.migrate"), {"site": site, "bench_path": bench_path}
                )
                await ctx.emit(f"$ {mig.display}")
                code = await ctx.stream(mig.argv, cwd=mig.cwd)
                clean = code == 0
                checks.append(
                    {"key": "migrations", "label": "Migrations clean", "ok": clean,
                     "detail": "bench migrate exited 0" if clean else f"migrate exited {code}"}
                )

            # (3) Scheduler / workers up.
            with ctx.step("Check: scheduler enabled"):
                sched = render(
                    get_template("site.scheduler_status"),
                    {"site": site, "bench_path": bench_path},
                )
                res = await ctx.capture(sched.argv, cwd=sched.cwd)
                up = res.exit_code == 0 and "enabled" in (res.stdout or "").lower()
                checks.append(
                    {"key": "scheduler", "label": "Scheduler/workers up", "ok": up,
                     "detail": "scheduler enabled" if up else "scheduler not enabled"}
                )

            # (4) Row-count sanity vs the source site (no gross data loss).
            with ctx.step("Check: row-count sanity vs source"):
                staging_n = await _count(site, bench_path)
                source_n = (
                    await _count(source_site, source_bench_path)
                    if source_site and source_bench_path
                    else None
                )
                if staging_n is None:
                    ok = False
                    detail = "could not read a row count on the clone"
                elif source_n is None:
                    ok = staging_n >= 1
                    detail = f"clone has {staging_n} User rows (no source baseline)"
                else:
                    # Clone should carry ~all of source; allow growth from migrate,
                    # flag a gross loss (more than half the rows gone).
                    ok = staging_n * 2 >= source_n
                    detail = f"clone {staging_n} vs source {source_n} User rows"
                checks.append(
                    {"key": "row_count", "label": "Row-count sanity", "ok": ok,
                     "detail": detail}
                )
        finally:
            if started:
                await _stop_dev_redis(ctx, queue_port, cache_port)

        all_ok = all(c["ok"] for c in checks)
        result = {"all_ok": all_ok, "checks": checks}
        await ctx.emit("CHECKLIST_RESULT " + json.dumps(result), stream="result")
        if pipeline is not None:
            pipeline.checklist = result
            pipeline.checklist_ok = all_ok
            pipeline.phase = "verified" if all_ok else "verify_failed"
            ctx.session.commit()
        await ctx.emit(
            "Verification checklist all-green — safe to promote."
            if all_ok
            else "Verification checklist has RED items — promote is blocked."
        )


class PromoteUpdateAction(Action):
    """`site.promote_update` — apply the update to production, safely (3.3).

    Ordered, non-idempotent, never auto-retried:
      1. MANDATORY pre-update backup of prod (with files) FIRST — the gate. If it
         fails the job aborts and prod is never touched.
      2. `bench update` on the production bench.
      3. Post-update check (`frappe.ping`).
    If step 2 or 3 fails, an in-job ROLLBACK restores the pre-update backup over
    prod (db + files + encryption_key + migrate) and the pipeline is marked
    `rolled_back`; the job then fails loudly so the operator sees the update did
    not land. The rollback path is exercised by test_updates.py."""

    async def run(self, ctx: JobContext) -> None:
        from app.core.commands import get_template, render

        params = ctx.rendered.params_sanitized
        site = params["site"]
        bench_path = params["bench_path"]
        pipeline = _load_pipeline(ctx, params.get("pipeline_id"))

        bench = _load_bench(ctx, bench_path)
        queue_port, cache_port = _redis_ports(bench)
        is_dev = await _detect_bench_mode(ctx, bench_path)
        if pipeline is not None:
            pipeline.phase = "promoting"
            ctx.session.commit()

        started = False
        try:
            if is_dev:
                await _start_dev_redis(ctx, bench_path)
                started = True

            # 1) The pre-update backup GATE — before any prod mutation.
            try:
                pre = await _run_backup(
                    ctx,
                    site=site,
                    bench_path=bench_path,
                    bench=bench,
                    with_files=True,
                    step_label="Pre-update backup of production (mandatory gate)",
                )
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(
                    f"pre-update backup failed ({exc}); production was NOT touched"
                ) from exc
            if pre is None or pre.status != "success" or not pre.db_path:
                raise RuntimeError(
                    "pre-update backup produced no usable artifact; production "
                    "was NOT touched"
                )
            if pipeline is not None:
                pipeline.pre_backup_id = pre.id
                ctx.session.commit()
            await ctx.emit(
                f"Pre-update backup #{pre.id} captured — prod is now recoverable; "
                "proceeding with the update."
            )

            # 2) Apply the update to production.
            update_failed: Exception | None = None
            update = render(get_template("bench.update"), {"bench_path": bench_path})
            try:
                with ctx.step("Apply update to production (bench update)"):
                    await ctx.emit(f"$ {update.display}")
                    code = await ctx.stream(update.argv, cwd=update.cwd)
                    if code != 0:
                        raise RuntimeError(f"bench update exited with status {code}")

                # 3) Post-update check.
                with ctx.step("Post-update check (frappe.ping)"):
                    ping = render(
                        get_template("site.ping"),
                        {"site": site, "bench_path": bench_path},
                    )
                    res = await ctx.capture(ping.argv, cwd=ping.cwd)
                    if res.exit_code != 0 or "pong" not in (res.stdout or "").lower():
                        raise RuntimeError(
                            "post-update check failed: site did not boot after update"
                        )
            except Exception as exc:  # noqa: BLE001
                update_failed = exc

            if update_failed is not None:
                # ROLLBACK: restore the pre-update backup over prod.
                await ctx.emit(
                    f"Update failed ({update_failed}) — rolling back by restoring "
                    f"pre-update backup #{pre.id}."
                )
                await _restore_artifacts_into_site(
                    ctx,
                    site=site,
                    bench_path=bench_path,
                    bench=bench,
                    db_path=pre.db_path,
                    public_files=pre.public_files_path,
                    private_files=pre.private_files_path,
                    config_path=pre.config_path,
                    create=False,
                )
                if pipeline is not None:
                    pipeline.phase = "rolled_back"
                    pipeline.rollback_job_id = ctx.job_id
                    pipeline.note = f"promote failed, rolled back: {update_failed}"
                    ctx.session.commit()
                raise RuntimeError(
                    f"update failed and was rolled back to pre-update backup "
                    f"#{pre.id}: {update_failed}"
                )

            if pipeline is not None:
                pipeline.phase = "promoted"
                ctx.session.commit()
            await ctx.emit("Production update promoted and verified.")
        finally:
            if started:
                await _stop_dev_redis(ctx, queue_port, cache_port)


class RestartServiceAction(Action):
    """`server.restart_service` — restart one managed system service from the
    monitoring services grid (session 1.12).

    Runs `sudo -n systemctl restart <service>` where <service> is one of the four
    allowlisted names (nginx, mariadb, redis-server, supervisor). `sudo -n` never
    prompts: it fails loudly if the ratified `/etc/sudoers.d/fdm-platform`
    allowlist line (`systemctl restart <svc>`) isn't installed, so a
    mis-provisioned server surfaces a clear error rather than hanging."""

    async def run(self, ctx: JobContext) -> None:
        service = ctx.rendered.params_sanitized["service"]
        with ctx.step(f"Restart {service}"):
            await ctx.emit(f"$ {ctx.rendered.display}")
            code = await ctx.stream(ctx.rendered.argv, cwd=ctx.rendered.cwd)
            if code != 0:
                raise RuntimeError(
                    f"systemctl restart {service} exited with status {code} "
                    "(is the fdm-platform sudoers allowlist installed on this server?)"
                )


# --------------------------------------------------------------------------- #
# Production setup (session 2.5)
# --------------------------------------------------------------------------- #

# The one-shot pre-backup tar path. Under the fixed prefix the fdm-elevate helper
# confines to (never /etc or /root); the job id keeps concurrent-server runs
# distinct. Rollback = extract this tar back over /etc and restart services.
_PREBACKUP_PREFIX = "/var/backups/fdm"

# How many changed/added/removed config paths to spell out in the timeline before
# collapsing to a count (the full lists are in the POSTCONFIG_RESULT json line).
_DIFF_PREVIEW = 40


def _set_bench_production(ctx: JobContext, bench_path: str) -> None:
    """Flip the discovered Bench row to production so the UI reflects the new
    mode immediately (without waiting for the next discovery)."""
    bench = _load_bench(ctx, bench_path)
    if bench is not None and not bench.is_production:
        bench.is_production = True
        ctx.session.commit()


class SetupProductionAction(Action):
    """`bench.setup_production` — convert a DEV bench to production (session 2.5).

    Runs `bench setup production <user>`, which rewrites the server's nginx +
    supervisor config to serve the bench's sites under supervisor/nginx instead
    of the dev `bench start`. Highest-sensitivity Phase 2 item: it needs root.

    Sudo decision point (implementation-plan.md 2.5): the platform does NOT hold
    a permanent `bench setup production` sudo grant. The only standing grant is
    the fixed `fdm-elevate` helper, which (1) captures + tars the nginx/supervisor
    config for a pre-backup and (2) installs a TIME-BOXED, single-command sudoers
    drop-in permitting exactly one `bench setup production <user>` run — then this
    action removes it again in a `finally`, so at rest no sudo path to setup
    production exists (drift-baseline-clean, Phase 6.7).

    Ordered steps:
      1. detect mode — refuse if the bench is already production;
      2. pre-op config capture: tar /etc/nginx + /etc/supervisor (rollback) and
         record the before-state manifest (sha256 per file);
      3. grant temporary elevation (install the drop-in), capturing the exact
         bench binary the drop-in allows;
      4. run `sudo -n <bench> setup production <user>`;
      5. toggle Bench.is_production = True;
      6. post-op config capture + before→after diff (emitted + POSTCONFIG_RESULT);
      7. `sudo -n nginx -t` gate;
      finally: revoke the temporary elevation (always).

    Non-idempotent: a determinate failure is never auto-retried on top of a
    half-converted bench.
    """

    async def run(self, ctx: JobContext) -> None:
        import json

        from app.core import production as prod
        from app.core.commands import get_template, render

        params = ctx.rendered.params_sanitized
        bench_path = params["bench_path"]
        user = params["production_user"]

        # 1) Refuse to "convert" a bench that is already production.
        is_dev = await _detect_bench_mode(ctx, bench_path)
        if not is_dev:
            with ctx.step("Production setup gate"):
                await ctx.emit(
                    "This bench already has supervisor/systemd config — it is "
                    "already set up for production. Nothing to convert."
                )
                raise RuntimeError("bench is already in production mode")

        # 2) Pre-op config capture: tar the config for rollback + before-manifest.
        prebackup_tar = f"{_PREBACKUP_PREFIX}/pre-production-job{ctx.job_id}.tar.gz"
        before: dict[str, str] = {}
        with ctx.step("Capture nginx/supervisor config (pre-backup)"):
            await ctx.emit(
                f"Backing up {', '.join(prod.CAPTURED_CONFIG_ROOTS)} to {prebackup_tar} "
                "before conversion (rollback: extract this tar back over /etc and "
                "restart nginx + supervisor)."
            )
            res = await ctx.capture(
                ["sudo", "-n", "/usr/local/sbin/fdm-elevate", "backup", prebackup_tar]
            )
            if res.exit_code != 0:
                raise RuntimeError(
                    "config pre-backup failed "
                    f"(status {res.exit_code}); is the fdm-elevate helper + its "
                    "sudoers line installed on this server? Not converting."
                )
            before = prod.parse_manifest(res.stdout)
            await ctx.emit(
                f"Pre-backup complete: {len(before)} config file(s) hashed "
                f"under {', '.join(prod.CAPTURED_CONFIG_ROOTS)}."
            )

        # 3) Grant the time-boxed elevation and learn the exact bench binary.
        with ctx.step("Grant temporary elevation (single-command sudoers drop-in)"):
            res = await ctx.capture(
                ["sudo", "-n", "/usr/local/sbin/fdm-elevate", "grant", user]
            )
            if res.exit_code != 0:
                # The hardened helper `die`s with actionable guidance on stderr
                # (e.g. "install a root-owned bench at … or set the pin …").
                # Surface that tail so the timeline shows *why* it refused and how
                # to fix it, not just the bare status code.
                hint = _elevate_stderr_tail(res.stderr)
                detail = f": {hint}" if hint else ""
                raise RuntimeError(
                    f"could not install the temporary elevation (status "
                    f"{res.exit_code}){detail}; not running setup production"
                )
            bench_bin = _parse_bench_bin(res.stdout)
            if bench_bin is None:
                # Best-effort revoke before bailing — never leave a dangling grant.
                await ctx.capture(
                    ["sudo", "-n", "/usr/local/sbin/fdm-elevate", "revoke"]
                )
                raise RuntimeError(
                    "elevation helper did not report the bench binary path; aborted"
                )
            await ctx.emit(
                "Temporary elevation installed: a single-command drop-in permitting "
                f"only `{bench_bin} setup production {user}`. It is removed again "
                "when this job finishes."
            )

        try:
            # 4) The actual conversion, permitted only by the temporary drop-in.
            run = render(
                get_template("bench.setup_production_run"),
                {"bench_bin": bench_bin, "production_user": user, "bench_path": bench_path},
            )
            with ctx.step("Set up production (bench setup production)"):
                await ctx.emit(f"$ {run.display}")
                code = await ctx.stream(run.argv, cwd=run.cwd)
                if code != 0:
                    raise RuntimeError(
                        f"bench setup production exited with status {code}"
                    )

            # 5) Toggle the bench to production so the UI reflects the new mode.
            with ctx.step("Mark bench as production"):
                _set_bench_production(ctx, bench_path)
                await ctx.emit(f"{bench_path} is now a production bench.")

            # 6) Post-op capture + before→after diff of the config trees.
            with ctx.step("Diff nginx/supervisor config (before vs after)"):
                res = await ctx.capture(
                    ["sudo", "-n", "/usr/local/sbin/fdm-elevate", "manifest"]
                )
                after = prod.parse_manifest(res.stdout) if res.exit_code == 0 else {}
                diff = prod.diff_manifests(before, after)
                await ctx.emit(
                    f"Config changes: {len(diff.added)} added, "
                    f"{len(diff.changed)} changed, {len(diff.removed)} removed "
                    f"({diff.unchanged} unchanged)."
                )
                for label, paths in (
                    ("+", diff.added),
                    ("~", diff.changed),
                    ("-", diff.removed),
                ):
                    for path in paths[:_DIFF_PREVIEW]:
                        await ctx.emit(f"  [{label}] {path}")
                    if len(paths) > _DIFF_PREVIEW:
                        await ctx.emit(f"  … and {len(paths) - _DIFF_PREVIEW} more")
                await ctx.emit(
                    "POSTCONFIG_RESULT " + json.dumps(diff.as_dict()), stream="result"
                )

            # 7) nginx -t gate — the generated vhosts must parse.
            nginx = render(get_template("bench.nginx_test"), {})
            with ctx.step("Validate nginx config (nginx -t)"):
                await ctx.emit(f"$ {nginx.display}")
                code = await ctx.stream(nginx.argv, cwd=nginx.cwd)
                if code != 0:
                    raise RuntimeError(
                        f"nginx -t failed (status {code}) after setup production — the "
                        "generated config did not validate; investigate or roll back "
                        f"from {prebackup_tar}"
                    )
                await ctx.emit("nginx config valid — sites are served by nginx/supervisor.")
        finally:
            # Always revoke the temporary elevation, success or failure — no
            # standing sudo path to setup production is left behind.
            with ctx.step("Revoke temporary elevation"):
                res = await ctx.capture(
                    ["sudo", "-n", "/usr/local/sbin/fdm-elevate", "revoke"]
                )
                if res.exit_code == 0:
                    await ctx.emit(
                        "Temporary elevation revoked — the single-command drop-in is "
                        "removed; no standing grant for setup production remains "
                        "(drift baseline clean)."
                    )
                else:
                    await ctx.emit(
                        "WARNING: could not confirm removal of the temporary elevation "
                        f"drop-in (status {res.exit_code}) — remove "
                        "/etc/sudoers.d/fdm-prod-elevation manually and verify."
                    )


def _elevate_stderr_tail(stderr: str, *, max_len: int = 400) -> str:
    """Trim the fdm-elevate helper's refusal guidance for embedding in an error
    message / job-timeline line. Keeps the last few non-blank lines (that is where
    the helper's `die` prints the actionable fix), collapsed onto one line and
    capped so a runaway stderr can't bloat the RuntimeError. Returns "" when there
    is nothing useful to show."""
    lines = [ln.strip() for ln in stderr.splitlines() if ln.strip()]
    if not lines:
        return ""
    tail = " ".join(lines[-4:])
    if len(tail) > max_len:
        tail = "…" + tail[-(max_len - 1) :]
    return tail


def _parse_bench_bin(stdout: str) -> str | None:
    """Read the `BENCH_BIN=<abs path>` line the elevate helper prints on grant,
    so the run step invokes the exact binary the drop-in permits. Returns None if
    absent or not an absolute path."""
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith("BENCH_BIN="):
            path = line[len("BENCH_BIN=") :].strip()
            if path.startswith("/"):
                return path
    return None


# --------------------------------------------------------------------------- #
# Scheduled maintenance (session 2.1)
# --------------------------------------------------------------------------- #


def _safe_backup_path(path: str) -> bool:
    """A path is safe to `rm` only if it is absolute, has no `..` segment, and
    lives under a site's `private/backups/` directory. The paths come from our
    own inspect script (server-controlled, already absolute), so this is
    defence-in-depth: a retention sweep can only ever delete backup artifacts,
    never live data — even if a Backup row were somehow tampered."""
    from app.core.commands.templates import has_dotdot_segment

    return (
        path.startswith("/")
        and not has_dotdot_segment(path)
        and "/private/backups/" in path
    )


class RetentionSweepAction(Action):
    """`backup.retention_sweep` — prune a site's backups down to its policy
    (session 2.1). Reuses the 1.11 backup inventory: it reads the site's `Backup`
    rows, decides which fall outside the retention window (`keep_last` /
    `keep_days`), and removes the excess — files on the server AND their rows.

    Destructive-class, so it is guarded, not blind:
      1. it NEVER deletes the newest/only backup (safety floor in `plan_retention`);
      2. it logs a dry-run summary (kept vs to-remove, each listed) BEFORE any
         delete, so the timeline records exactly what was pruned and why;
      3. every artifact path is re-validated to live under `private/backups/`
         with no `..` before `rm` (so a sweep can only touch backup files).

    Non-idempotent (deletes), so it is never auto-retried. The paths are absolute
    argv elements to `rm -f` (execve, no shell) — nothing is interpolated."""

    async def run(self, ctx: JobContext) -> None:
        from datetime import UTC, datetime

        from sqlalchemy import select

        from app.core import backups as bk
        from app.models.backup import Backup

        params = ctx.rendered.params_sanitized
        site_name = params["site"]
        bench_path = params["bench_path"]
        keep_last = int(params["keep_last"]) if params.get("keep_last") else None
        keep_days = int(params["keep_days"]) if params.get("keep_days") else None
        now = datetime.now(UTC)

        bench = _load_bench(ctx, bench_path)
        site = _load_site(ctx, bench, site_name)

        with ctx.step("Evaluate retention policy"):
            if site is None:
                await ctx.emit(
                    f"Site {site_name!r} is not in inventory; nothing to sweep."
                )
                return
            rows = list(
                ctx.session.scalars(select(Backup).where(Backup.site_id == site.id)).all()
            )
            keep, remove = bk.plan_retention(
                rows, keep_last=keep_last, keep_days=keep_days, now=now
            )
            policy = (
                ", ".join(
                    p
                    for p in (
                        f"keep last {keep_last}" if keep_last is not None else None,
                        f"keep {keep_days} day(s)" if keep_days is not None else None,
                    )
                    if p
                )
                or "keep all"
            )
            await ctx.emit(
                f"Retention policy [{policy}] for {site_name}: "
                f"{len(keep) + len(remove)} successful backup(s), "
                f"{len(keep)} to keep, {len(remove)} to remove (dry-run)."
            )
            for b in remove:
                created = b.created_at.isoformat() if b.created_at else "?"
                mb = (b.size_bytes or 0) / (1024 * 1024)
                await ctx.emit(
                    f"  will remove backup #{b.id} ({b.type}, {created}, {mb:.1f} MB)"
                )

        if not remove:
            with ctx.step("No backups beyond retention"):
                await ctx.emit("Nothing to prune; the newest backup is always kept.")
            return

        removed_ids: list[int] = []
        with ctx.step(f"Remove {len(remove)} expired backup(s)"):
            for b in remove:
                all_paths = bk.retention_artifact_paths(b)
                paths = [p for p in all_paths if _safe_backup_path(p)]
                for p in (p for p in all_paths if not _safe_backup_path(p)):
                    await ctx.emit(f"  SKIP unsafe path (not under private/backups): {p}")
                if paths:
                    await ctx.emit(f"$ rm -f {' '.join(paths)}")
                    code = await ctx.stream(["rm", "-f", *paths], cwd=bench_path)
                    if code != 0:
                        raise RuntimeError(f"rm of backup #{b.id} artifacts exited {code}")
                ctx.session.delete(b)
                removed_ids.append(b.id)
            ctx.session.commit()
            kept_ids = ", ".join(str(k.id) for k in keep) or "none"
            await ctx.emit(
                f"Pruned {len(removed_ids)} backup(s); {len(keep)} kept (ids {kept_ids})."
            )


# --------------------------------------------------------------------------- #
# Domains & SSL (session 2.4)
#
# Managing a site's custom domains and TLS. See app/core/domains.py for the pure
# renderers/parsers and the documented nginx-write policy (no root file writes;
# vhosts live in the bench-user-writable config/nginx-vhosts/ include dir; the
# only sudo is the ratified `nginx -t` gate + `systemctl reload nginx`).
# --------------------------------------------------------------------------- #

# Best-effort discovery of the server's own public IP(s): try the public
# reflector, then fall back to the host's configured addresses. Read-only.
_PUBLIC_IP_SCRIPT = (
    "curl -fsS -4 --max-time 5 https://api.ipify.org 2>/dev/null; echo; "
    "hostname -I 2>/dev/null || true"
)

# Atomically write a generated vhost under a flock, after a pre-change tar backup
# of the vhosts dir (golden rule 5). All values arrive as their own argv
# elements ($1..$4) — no user text is interpolated into the script text.
_VHOST_WRITE_SCRIPT = r"""
set -eu
VHOSTS="$1"; TARGET="$2"; B64="$3"; BACKUP="$4"
mkdir -p "$VHOSTS"
# Pre-change backup of the whole vhosts include dir so a bad write can be rolled
# back (best-effort; empty dir is fine).
tar czf "$BACKUP" -C "$VHOSTS" . 2>/dev/null || true
exec 9>"$VHOSTS/.fdm-vhosts.lock"
flock 9
TMP="$(mktemp "$VHOSTS/.tmp.XXXXXX")"
printf '%s' "$B64" | base64 -d > "$TMP"
mv -f "$TMP" "$TARGET"
echo "WROTE $TARGET"
"""

# Restore the vhosts dir from a pre-change backup tar (used when `nginx -t`
# rejects the new config, so the live config is never left broken).
_VHOST_RESTORE_SCRIPT = r"""
set -eu
VHOSTS="$1"; BACKUP="$2"; TARGET="$3"
exec 9>"$VHOSTS/.fdm-vhosts.lock"
flock 9
rm -f "$TARGET"
if [ -s "$BACKUP" ]; then
  tar xzf "$BACKUP" -C "$VHOSTS" 2>/dev/null || true
fi
echo "RESTORED $VHOSTS"
"""

_NGINX_TEST_ARGV = ["sudo", "-n", "/usr/sbin/nginx", "-t"]
_NGINX_RELOAD_ARGV = ["sudo", "-n", "/usr/bin/systemctl", "reload", "nginx"]

# certbot is invoked ONLY through the fixed root-owned wrapper (deploy/fdm-certbot,
# installed at this path). The wrapper pins the exact argv shape and refuses any
# flag-shaped argument, so the NOPASSWD sudoers line targets the wrapper — never a
# `certbot certonly *` / `renew *` wildcard, which would let a `--deploy-hook`
# run arbitrary commands as root (DOO-220). Mirrors the fdm-elevate pattern (2.5).
_FDM_CERTBOT = "/usr/local/sbin/fdm-certbot"


def _load_domain(ctx: JobContext, domain_id: int):
    """Fetch the Domain row this job acts on, or None."""
    from app.models.domain import Domain

    return ctx.session.get(Domain, int(domain_id))


def _server_public_ips(ctx: JobContext, probe_stdout: str) -> set[str]:
    """The server's public IP set: whatever the probe found, plus the Server
    row's hostname when it is itself a literal IP."""
    from app.core import domains as dom
    from app.models.server import Server

    ips = dom.parse_public_ips(probe_stdout)
    server = ctx.session.get(Server, ctx.server_id)
    if server is not None and server.hostname:
        ips |= dom.parse_public_ips(server.hostname)
    return ips


class DnsCheckAction(Action):
    """`domain.dns_check` — resolve a domain's A/AAAA records on the managed
    server and compare them to the server's public IP, recording dns_ok on the
    Domain row. Read-only on the server (getent/curl), idempotent."""

    async def run(self, ctx: JobContext) -> None:
        from datetime import UTC, datetime

        from app.core import domains as dom

        params = ctx.rendered.params_sanitized
        domain = params["domain"]
        domain_id = int(params["domain_id"])

        resolved: set[str] = set()
        with ctx.step(f"Resolve DNS for {domain}"):
            for family in ("ahostsv4", "ahostsv6"):
                res = await ctx.capture(["getent", family, domain])
                resolved |= dom.parse_getent_ips(res.stdout)
            await ctx.emit(
                f"{domain} resolves to: "
                + (", ".join(sorted(resolved)) or "no records")
            )

        with ctx.step("Determine server public IP"):
            res = await ctx.capture(["bash", "-c", _PUBLIC_IP_SCRIPT])
            server_ips = _server_public_ips(ctx, res.stdout)
            await ctx.emit(
                "Server public IP(s): "
                + (", ".join(sorted(server_ips)) or "unknown")
            )

        ok = dom.dns_ok(resolved, server_ips)
        row = _load_domain(ctx, domain_id)
        with ctx.step("Record DNS result"):
            if row is not None:
                row.dns_ok = ok
                row.last_checked = datetime.now(UTC)
                row.last_error = (
                    None
                    if ok
                    else f"{domain} does not resolve to the server's public IP"
                )
                ctx.session.commit()
            await ctx.emit(
                "DNS OK — points at this server."
                if ok
                else "DNS MISMATCH — the A/AAAA record does not point here yet."
            )


def _vhost_paths(bench_path: str, domain: str, site: str) -> tuple[str, str, str, str]:
    """Return (vhosts_dir, target_file, webroot, cert_dir) for a domain."""
    from app.core.domains import VHOSTS_SUBDIR

    vhosts_dir = f"{bench_path}/{VHOSTS_SUBDIR}"
    target = f"{vhosts_dir}/{domain}.conf"
    webroot = f"{bench_path}/sites/{site}/public"
    cert_dir = f"/etc/letsencrypt/live/{domain}"
    return vhosts_dir, target, webroot, cert_dir


async def _write_vhost_and_reload(ctx: JobContext, *, domain: str, bench_path: str,
                                  site: str, ssl_enabled: bool) -> None:
    """Render the domain's vhost, write it under a flock with a pre-change
    backup, gate on `nginx -t`, and reload. If `nginx -t` fails, restore the
    pre-change backup and raise — the live config is never left broken."""
    import base64

    from app.core import domains as dom

    bench = _load_bench(ctx, bench_path)
    port = (bench.webserver_port if bench else None) or 8000
    vhosts_dir, target, webroot, cert_dir = _vhost_paths(bench_path, domain, site)
    backup = f"{vhosts_dir}/.fdm-backup-{domain}.tgz"

    content = dom.render_vhost(
        domain,
        upstream_host="127.0.0.1",
        upstream_port=port,
        ssl_enabled=ssl_enabled,
        webroot=webroot,
        cert_dir=cert_dir,
    )
    b64 = base64.b64encode(content.encode()).decode()

    with ctx.step(f"Write nginx vhost for {domain}"):
        await ctx.emit(f"Backing up {vhosts_dir} then writing {target} (under flock).")
        res = await ctx.capture(
            ["bash", "-c", _VHOST_WRITE_SCRIPT, "_", vhosts_dir, target, b64, backup]
        )
        if res.exit_code != 0:
            raise RuntimeError(f"vhost write failed: {res.stderr.strip()}")
        await ctx.emit(res.stdout.strip())

    with ctx.step("Validate nginx config (nginx -t)"):
        await ctx.emit("$ sudo -n /usr/sbin/nginx -t")
        code = await ctx.stream(_NGINX_TEST_ARGV)
        if code != 0:
            await ctx.emit("nginx -t FAILED — restoring the previous vhosts config.")
            await ctx.capture(
                ["bash", "-c", _VHOST_RESTORE_SCRIPT, "_", vhosts_dir, backup, target]
            )
            raise RuntimeError(
                "nginx -t rejected the generated vhost; the live config was "
                "restored and left untouched (is the fdm-platform sudoers "
                "allowlist installed?)"
            )

    with ctx.step("Reload nginx"):
        await ctx.emit("$ sudo -n /usr/bin/systemctl reload nginx")
        code = await ctx.stream(_NGINX_RELOAD_ARGV)
        if code != 0:
            raise RuntimeError(
                f"systemctl reload nginx exited {code} (allowlist installed?)"
            )


class RenderVhostAction(Action):
    """`nginx.render_vhost` — (re)generate a domain's nginx vhost, validate with
    `nginx -t` under a filelock with a config pre-backup, and reload. Dangerous
    class (touches nginx), so it takes a per-server lock."""

    async def run(self, ctx: JobContext) -> None:
        params = ctx.rendered.params_sanitized
        await _write_vhost_and_reload(
            ctx,
            domain=params["domain"],
            bench_path=params["bench_path"],
            site=params["site"],
            ssl_enabled=params.get("ssl") == "on",
        )


def _certbot_issue_argv(domain: str, email: str, webroot: str) -> list[str]:
    """The webroot HTTP-01 issue argv, routed through the fixed fdm-certbot
    wrapper. The wrapper builds the certbot command line itself from these three
    validated positionals and rejects any flag-shaped argument, so no
    `--deploy-hook` can reach certbot. Every value is its own argv element
    (execve, no shell) and pre-validated by the ParamSpecs (DOO-220)."""
    return [
        "sudo", "-n", _FDM_CERTBOT, "issue",
        domain, email, webroot,
    ]


async def _refresh_cert_expiry(ctx: JobContext, domain_ids: list[int]) -> None:
    """Read `certbot certificates` and update cert_expires_at for the given
    Domain rows (matched by certificate name == domain)."""
    from datetime import UTC, datetime

    from app.core import domains as dom

    res = await ctx.capture(["sudo", "-n", _FDM_CERTBOT, "certificates"])
    by_name = dom.parse_certbot_certificates(res.stdout)
    for did in domain_ids:
        row = _load_domain(ctx, did)
        if row is None:
            continue
        expiry = by_name.get(row.domain)
        if expiry is not None:
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=UTC)
            row.cert_expires_at = expiry
            row.cert_status = "issued"
            row.ssl_enabled = True
        row.last_checked = datetime.now(UTC)
    ctx.session.commit()


class CertbotIssueAction(Action):
    """`ssl.certbot_issue` — obtain a Let's Encrypt certificate for a domain via
    the webroot plugin, then re-render the vhost with TLS on, validate and
    reload. Records cert_status/expiry on the Domain row. Per-server lock (shares
    nginx with render_vhost)."""

    async def run(self, ctx: JobContext) -> None:
        from datetime import UTC, datetime

        params = ctx.rendered.params_sanitized
        domain = params["domain"]
        bench_path = params["bench_path"]
        site = params["site"]
        email = params["email"]
        domain_id = int(params["domain_id"])
        _, _, webroot, _ = _vhost_paths(bench_path, domain, site)

        # Ensure an HTTP vhost serving the ACME challenge exists first.
        await _write_vhost_and_reload(
            ctx, domain=domain, bench_path=bench_path, site=site, ssl_enabled=False
        )

        try:
            with ctx.step(f"Issue certificate for {domain} (certbot)"):
                await ctx.emit(f"$ sudo -n fdm-certbot issue {domain}")
                code = await ctx.stream(_certbot_issue_argv(domain, email, webroot))
                if code != 0:
                    raise RuntimeError(
                        f"certbot exited {code}; check that {domain} resolves here "
                        "and ports 80/443 are reachable from the internet"
                    )
        except Exception as exc:
            row = _load_domain(ctx, domain_id)
            if row is not None:
                row.cert_status = "error"
                row.last_error = str(exc)[:500]
                row.last_checked = datetime.now(UTC)
                ctx.session.commit()
            raise

        # Cert now on disk — re-render the vhost with TLS on and reload.
        await _write_vhost_and_reload(
            ctx, domain=domain, bench_path=bench_path, site=site, ssl_enabled=True
        )
        with ctx.step("Record certificate expiry"):
            await _refresh_cert_expiry(ctx, [domain_id])
            row = _load_domain(ctx, domain_id)
            when = row.cert_expires_at.isoformat() if row and row.cert_expires_at else "?"
            await ctx.emit(f"Certificate issued for {domain}; expires {when}.")


class CertbotRenewAction(Action):
    """`ssl.certbot_renew` — renew the SSL-enabled domains of a site (scheduled
    via 2.1) and reload nginx, then refresh recorded expiry. `certbot renew`
    is a no-op for certs not near expiry, so this is safe to run on a cadence."""

    async def run(self, ctx: JobContext) -> None:
        from sqlalchemy import select

        from app.models.domain import Domain

        params = ctx.rendered.params_sanitized
        site = params["site"]
        bench_path = params["bench_path"]

        bench = _load_bench(ctx, bench_path)
        site_row = _load_site(ctx, bench, site)
        rows = []
        if site_row is not None:
            rows = list(
                ctx.session.scalars(
                    select(Domain).where(
                        Domain.site_id == site_row.id, Domain.ssl_enabled.is_(True)
                    )
                ).all()
            )

        if not rows:
            with ctx.step("Nothing to renew"):
                await ctx.emit(f"No SSL-enabled domains on {site}.")
            return

        with ctx.step(f"Renew {len(rows)} certificate(s)"):
            for row in rows:
                await ctx.emit(f"$ sudo -n fdm-certbot renew {row.domain}")
                code = await ctx.stream(
                    ["sudo", "-n", _FDM_CERTBOT, "renew", row.domain]
                )
                if code != 0:
                    await ctx.emit(f"certbot renew for {row.domain} exited {code}.")

        with ctx.step("Reload nginx"):
            await ctx.stream(_NGINX_RELOAD_ARGV)

        with ctx.step("Refresh recorded expiry"):
            await _refresh_cert_expiry(ctx, [r.id for r in rows])
            await ctx.emit("Renewal pass complete.")


class SslExpiryScanAction(Action):
    """`ssl.expiry_scan` — read `certbot certificates` and update cert_expires_at
    for a site's domains (scheduled via 2.1). Feeds the dashboard "SSL expiring
    ≤30d" KPI. Read-only on the server, idempotent."""

    async def run(self, ctx: JobContext) -> None:
        from sqlalchemy import select

        from app.models.domain import Domain

        params = ctx.rendered.params_sanitized
        site = params["site"]
        bench_path = params["bench_path"]

        bench = _load_bench(ctx, bench_path)
        site_row = _load_site(ctx, bench, site)
        rows = []
        if site_row is not None:
            rows = list(
                ctx.session.scalars(
                    select(Domain).where(Domain.site_id == site_row.id)
                ).all()
            )

        with ctx.step(f"Scan certificate expiry for {site}"):
            if not rows:
                await ctx.emit(f"No domains on {site}.")
                return
            await _refresh_cert_expiry(ctx, [r.id for r in rows])
            for row in rows:
                when = row.cert_expires_at.isoformat() if row.cert_expires_at else "no cert"
                await ctx.emit(f"  {row.domain}: {when}")


class DriftCheckAction(Action):
    """`server.drift_check` — re-hash every tracked config artefact on the server
    and diff each against its stored baseline (session 6.7, uiux-spec A2.16).

    Strictly **read-only** on the managed server: it only `cat`s/`find`s files
    (root ones via the fixed `sudo -n` allowlist lines). It never writes,
    reloads, or reverts config. Drifted artefacts flip their baseline row to
    `drifted`, fire one `config.drift` notification, and surface on the Dashboard
    "Needs attention" row. No auto-remediation."""

    async def run(self, ctx: JobContext) -> None:
        from app.core import drift

        with ctx.step("Read + hash tracked config artefacts"):
            results = await drift.run_drift_check(ctx)

        drifted = [r for r in results if r.drifted]
        with ctx.step("Evaluate drift vs baseline"):
            await ctx.emit(
                f"checked {len(results)} artefact(s); {len(drifted)} drifted"
            )
            for r in drifted:
                # Names + reason only — never artefact content (rule 6).
                await ctx.emit(
                    f"DRIFT [{r.reason}] {r.artifact_key} at {r.path}", stream="stderr"
                )
            if drifted:
                from app.core.notifications import dispatch_config_drift
                from app.models.server import Server

                server = ctx.session.get(Server, ctx.server_id)
                dispatch_config_drift(
                    ctx.session,
                    server_id=ctx.server_id,
                    server_name=server.name if server else str(ctx.server_id),
                    artifact_keys=[r.artifact_key for r in drifted],
                )
# --------------------------------------------------------------------------- #
# restic config-tier DR backups (session 4.1)
#
# Each managed server has one `ResticRepo` (its OS/config tier), stored inside an
# existing 2.2 StorageTarget bucket. restic dedups + encrypts client-side. The
# repo password + the target's S3 keys are secrets: they reach restic ONLY via
# its process environment — a 0600 env file staged on the target and sourced for
# the single command (`set -a; . file; exec restic …`). They are NEVER placed on
# an argv element, logged, or persisted in the clear (golden rule 6); the action
# also registers them with the log redactor as belt-and-suspenders.
# --------------------------------------------------------------------------- #

# Source the staged 0600 env file (RESTIC_PASSWORD, AWS_*) then exec the restic
# argv passed as positional parameters. $1 is the env-file path (shifted away);
# $@ afterward is the fixed restic argv — nothing is interpolated into the script.
_RESTIC_ENV_WRAP = 'set -a; . "$1"; shift; exec "$@"'

# Detect the tier's readable config dirs (only existing paths are backed up so a
# server without e.g. supervisor doesn't fail the snapshot) and stage the
# `dpkg --get-selections` manifest inside the same tree. Emits one existing path
# per line on stdout so the action can parse the real source set.
_RESTIC_STAGE_SCRIPT = r'''
set -e
stage="$HOME/.fdm-restic-stage"
rm -rf "$stage"
mkdir -p "$stage"
dpkg --get-selections > "$stage/dpkg-selections.txt"
echo "$stage/dpkg-selections.txt"
for p in "$@"; do
  if [ -e "$p" ]; then echo "$p"; fi
done
'''

# Install a pinned restic to the SSH user's ~/.local/bin when none is on PATH.
# $1 is the fixed release URL (a deterministic constant — never user input).
_RESTIC_INSTALL_SCRIPT = r'''
set -e
url="$1"
dest="$HOME/.local/bin/restic"
mkdir -p "$HOME/.local/bin"
tmp="$(mktemp)"
curl -fsSL "$url" -o "$tmp.bz2"
bunzip2 -f "$tmp.bz2"
mv "$tmp" "$dest"
chmod 755 "$dest"
"$dest" version
'''


async def _restic_prepare(ctx: JobContext):
    """Load this server's ResticRepo + its StorageTarget, resolve the restic env
    (repo URI + decrypted secrets), and register the secrets with the log
    redactor. Returns (repo_row, ResticEnv). Raises ResticError if unconfigured."""
    from sqlalchemy import select

    from app.core import restic as rst
    from app.models.restic import ResticRepo
    from app.models.storage import StorageTarget

    repo = ctx.session.scalars(
        select(ResticRepo).where(ResticRepo.server_id == ctx.server_id)
    ).first()
    if repo is None:
        raise rst.ResticError("this server has no restic repo configured")
    target = (
        ctx.session.get(StorageTarget, repo.storage_target_id)
        if repo.storage_target_id
        else None
    )
    env = rst.resolve_env(repo, target)
    # Redact the decrypted secrets from every subsequent log line (rule 6).
    for secret in env.secret_values:
        ctx.register_secret(secret)
    return repo, env


async def _stage_restic_env(ctx: JobContext, env) -> str:
    """Write the restic env file (RESTIC_PASSWORD, AWS_*) to a 0600 temp file on
    the target via base64 (never streamed), returning its path. The plaintext is
    only ever inside this 0600 file; it is removed by the caller's `finally`."""
    import base64
    from secrets import token_hex

    env_path = f"/tmp/fdm-restic-env-{token_hex(8)}"
    b64 = base64.b64encode(env.env_file_content().encode()).decode()
    res = await ctx.capture(["bash", "-c", _WRITE_KEY_SCRIPT, "_", b64, env_path])
    if res.exit_code != 0:
        raise RuntimeError("failed to stage restic env file on the target")
    return env_path


def _restic_wrap(env_path: str, restic_argv: list[str]) -> list[str]:
    """Wrap a rendered restic argv so it runs with the staged env file sourced."""
    return ["bash", "-c", _RESTIC_ENV_WRAP, "_", env_path, *restic_argv]


class ResticInstallAction(Action):
    """`restic.install` — ensure a pinned restic is available on the target.

    Detects `restic version`; if restic is already present it just reports the
    version (idempotent). Otherwise it downloads the pinned release for the
    server's architecture into `~/.local/bin/restic` (no root / no sudo) and
    verifies it. Touches no repo and no secret."""

    async def run(self, ctx: JobContext) -> None:
        from app.core import restic as rst

        with ctx.step("Detect restic"):
            res = await ctx.capture(["bash", "-lc", "restic version || true"])
            version = rst.parse_installed_version(res.stdout)
            if version:
                await ctx.emit(f"restic already installed: {version}")
                return
            await ctx.emit("restic not found on PATH — installing the pinned release.")

        with ctx.step("Detect architecture"):
            arch_res = await ctx.capture(["uname", "-m"])
            arch = (arch_res.stdout or "").strip()
            url = rst.install_url(arch)  # ResticError on an unsupported arch
            await ctx.emit(f"Architecture {arch}; fetching {url}")

        with ctx.step(f"Install restic {rst.RESTIC_VERSION}"):
            code = await ctx.stream(["bash", "-c", _RESTIC_INSTALL_SCRIPT, "_", url])
            if code != 0:
                raise RuntimeError(f"restic install exited with status {code}")
            await ctx.emit(
                "restic installed to ~/.local/bin/restic — ensure ~/.local/bin is "
                "on the PATH for scheduled runs."
            )


class ResticInitAction(Action):
    """`restic.init` — create the server's restic repository in its S3 bucket.

    Resolves the repo + S3 secrets (in memory), stages the 0600 env file, runs
    `restic init`, and flips `initialized` true. Re-running against an existing
    repo is a no-op restic reports as an error ("already initialized"); we treat
    that specific case as success so init stays idempotent."""

    async def run(self, ctx: JobContext) -> None:
        from app.core import restic as rst
        from app.core.commands import get_template, render

        repo, env = await _restic_prepare(ctx)
        cmd = render(get_template("restic.init"), {"repo": env.repository})
        env_path = await _stage_restic_env(ctx, env)
        try:
            with ctx.step("Initialise restic repository"):
                await ctx.emit(f"$ restic -r {rst.redacted_repository(env.repository)} init")
                res = await ctx.capture(_restic_wrap(env_path, list(cmd.argv)))
                combined = f"{res.stdout}\n{res.stderr}"
                for line in combined.splitlines():
                    if line.strip():
                        await ctx.emit(line)
                already = "already initialized" in combined or "already exists" in combined
                if res.exit_code != 0 and not already:
                    raise RuntimeError(f"restic init exited with status {res.exit_code}")
                repo.initialized = True
                ctx.session.commit()
                await ctx.emit(
                    "Repository ready."
                    if not already
                    else "Repository was already initialised — nothing to do."
                )
        finally:
            await ctx.capture(["rm", "-f", env_path])


class ResticBackupAction(Action):
    """`restic.backup` — snapshot the server's OS/config tier to its restic repo.

    Steps: resolve repo + secrets → stage the config set (existing config dirs +
    the `dpkg --get-selections` manifest) → stage the 0600 env file → run
    `restic backup` over that set → parse the snapshot id and record the evidence
    timestamps on the ResticRepo row. The env file + staging dir are always
    removed in `finally`. Non-idempotent (one snapshot per run)."""

    async def run(self, ctx: JobContext) -> None:
        from app.core import restic as rst
        from app.core.commands import get_template, render

        repo, env = await _restic_prepare(ctx)
        if not repo.initialized:
            raise rst.ResticError(
                "restic repository is not initialised — run init before a backup"
            )
        server = ctx.session.get(_server_model(), ctx.server_id)
        host = (server.hostname or server.name) if server else "server"

        env_path = await _stage_restic_env(ctx, env)
        try:
            with ctx.step("Stage config set + package manifest"):
                res = await ctx.capture(
                    ["bash", "-c", _RESTIC_STAGE_SCRIPT, "_", *rst.CONFIG_PATHS]
                )
                if res.exit_code != 0:
                    raise RuntimeError("failed to stage config set on the target")
                sources = [ln.strip() for ln in res.stdout.splitlines() if ln.strip()]
                if not sources:
                    raise RuntimeError("no config sources found to back up")
                await ctx.emit("Backing up: " + ", ".join(sources))

            cmd = render(
                get_template("restic.backup"),
                {"repo": env.repository, "host": host},
            )
            restic_argv = [*cmd.argv, *sources]
            with ctx.step("restic backup (config tier)"):
                await ctx.emit(
                    f"$ restic -r {rst.redacted_repository(env.repository)} backup "
                    f"--tag {rst.CONFIG_TAG} --host {host} [config set]"
                )
                res = await ctx.capture(_restic_wrap(env_path, restic_argv), timeout=3600)
                combined = f"{res.stdout}\n{res.stderr}"
                for line in combined.splitlines():
                    if line.strip():
                        await ctx.emit(line)
                # restic exit 3 = snapshot created but some files were unreadable
                # (root-only config as the non-sudo SSH user); still a valid
                # config snapshot, so 0 and 3 are both success.
                if res.exit_code not in (0, 3):
                    raise RuntimeError(f"restic backup exited with status {res.exit_code}")
                snap = rst.parse_snapshot_id(combined)
                repo.last_backup_at = _now_utc()
                if snap:
                    repo.last_snapshot_id = snap
                ctx.session.commit()
                await ctx.emit(
                    f"Config snapshot saved: {snap or '(id not parsed)'}"
                    + (" — some root-only files were skipped." if res.exit_code == 3 else "")
                )
        finally:
            await ctx.capture(["rm", "-f", env_path])
            await ctx.capture(["bash", "-c", 'rm -rf "$HOME/.fdm-restic-stage"'])


class ResticSnapshotsAction(Action):
    """`restic.snapshots` — list the config-tier snapshots in the repo (evidence).

    Read-only; resolves secrets, stages the env file, runs `restic snapshots`
    filtered to the config tag, and streams the listing. Idempotent."""

    async def run(self, ctx: JobContext) -> None:
        from app.core import restic as rst
        from app.core.commands import get_template, render

        repo, env = await _restic_prepare(ctx)
        cmd = render(get_template("restic.snapshots"), {"repo": env.repository})
        env_path = await _stage_restic_env(ctx, env)
        try:
            with ctx.step("List config snapshots"):
                await ctx.emit(
                    f"$ restic -r {rst.redacted_repository(env.repository)} "
                    f"snapshots --tag {rst.CONFIG_TAG}"
                )
                res = await ctx.capture(_restic_wrap(env_path, list(cmd.argv)))
                combined = f"{res.stdout}\n{res.stderr}"
                for line in combined.splitlines():
                    if line.strip():
                        await ctx.emit(line)
                if res.exit_code != 0:
                    raise RuntimeError(
                        f"restic snapshots exited with status {res.exit_code}"
                    )
        finally:
            await ctx.capture(["rm", "-f", env_path])


class ResticForgetAction(Action):
    """`restic.forget` — apply the repo's retention policy and prune (session 4.2).

    Reads the per-repo retention policy (keep-last/daily/weekly/monthly) off the
    ResticRepo, turns it into restic `--keep-*` flags, and runs
    `restic forget … --prune` scoped to the config tag. DESTRUCTIVE: it deletes
    snapshots and reclaims their data, so its template is non-idempotent — the
    engine never auto-retries it (golden rule destructive policy). If NO retention
    dimension is configured the action refuses to run (an empty policy would
    delete every snapshot). Records `last_forget_at` and streams restic's
    kept/removed summary as evidence. Per-repo lock via the template."""

    async def run(self, ctx: JobContext) -> None:
        from app.core import restic as rst
        from app.core.commands import get_template, render

        repo, env = await _restic_prepare(ctx)
        if not repo.initialized:
            raise rst.ResticError(
                "restic repository is not initialised — nothing to prune"
            )
        # Raises ResticError (→ job failure) when no policy is set: we never send
        # `forget --prune` with no keep flags, which would wipe every snapshot.
        keep_args = rst.build_forget_keep_args(repo)

        cmd = render(get_template("restic.forget"), {"repo": env.repository})
        restic_argv = [*cmd.argv, *keep_args]
        env_path = await _stage_restic_env(ctx, env)
        try:
            with ctx.step("restic forget --prune (retention policy)"):
                await ctx.emit(
                    f"$ restic -r {rst.redacted_repository(env.repository)} forget "
                    f"--tag {rst.CONFIG_TAG} --prune {' '.join(keep_args)}"
                )
                res = await ctx.capture(_restic_wrap(env_path, restic_argv), timeout=3600)
                combined = f"{res.stdout}\n{res.stderr}"
                for line in combined.splitlines():
                    if line.strip():
                        await ctx.emit(line)
                if res.exit_code != 0:
                    raise RuntimeError(
                        f"restic forget exited with status {res.exit_code}"
                    )
                repo.last_forget_at = _now_utc()
                ctx.session.commit()
                await ctx.emit("Retention prune complete.")
        finally:
            await ctx.capture(["rm", "-f", env_path])


class ResticCheckAction(Action):
    """`restic.check` — verify repository integrity (session 4.2).

    Runs `restic check --read-data-subset <subset>` (a fraction of the pack data
    is actually re-read + verified, keeping the check affordable on a large repo)
    and records the outcome on the ResticRepo: `last_check_at` always, plus
    `last_check_ok` and a credential-free `last_check_summary`. On a genuine
    failure it raises ONE breach alert through the shared 2.8/3.1 notification
    channel (`restic.check_failed` — not a forked path) and fails the job. A
    restic *lock* clash (a concurrent backup/prune holds the repo) — and likewise a
    connectivity/transient failure (S3 unreachable, DNS, throttling, timeout) — is
    an operational condition, not an integrity breach: it fails the job WITHOUT
    recording `ok=False` or alerting, since the check never actually verified the
    data. Read-oriented but non-idempotent so a failed check is never silently
    auto-retried (which would re-alert)."""

    async def run(self, ctx: JobContext) -> None:
        from app.core import restic as rst
        from app.core.commands import get_template, render
        from app.core.notifications import dispatch_restic_check_failed

        repo, env = await _restic_prepare(ctx)
        if not repo.initialized:
            raise rst.ResticError(
                "restic repository is not initialised — nothing to check"
            )
        subset = str(
            ctx.rendered.params_sanitized.get("subset") or rst.DEFAULT_CHECK_SUBSET
        )
        cmd = render(
            get_template("restic.check"), {"repo": env.repository, "subset": subset}
        )
        env_path = await _stage_restic_env(ctx, env)
        try:
            with ctx.step("restic check (integrity)"):
                await ctx.emit(
                    f"$ restic -r {rst.redacted_repository(env.repository)} check "
                    f"--read-data-subset {subset}"
                )
                res = await ctx.capture(_restic_wrap(env_path, list(cmd.argv)), timeout=3600)
                combined = f"{res.stdout}\n{res.stderr}"
                for line in combined.splitlines():
                    if line.strip():
                        await ctx.emit(line)

                # A repo-lock clash is not an integrity failure: fail the job but
                # do not record ok=False or raise a (false) breach alert.
                if res.exit_code != 0 and rst.is_restic_lock_error(combined):
                    raise RuntimeError(
                        "restic check could not run — the repository is locked by "
                        "another operation; not recording an integrity result"
                    )

                # Likewise a connectivity/transient failure (S3 unreachable, DNS,
                # throttling, timeout) means the check never verified anything —
                # not that the backup is corrupt. Fail the job without recording
                # ok=False or firing the DR breach alert, same as the lock case.
                if res.exit_code != 0 and rst.is_restic_transient_error(combined):
                    raise RuntimeError(
                        "restic check could not run — the repository was "
                        "unreachable (connectivity/transient error); not recording "
                        "an integrity result"
                    )

                ok, summary = rst.summarize_check(res.exit_code, combined)
                repo.last_check_at = _now_utc()
                repo.last_check_ok = ok
                repo.last_check_summary = summary
                ctx.session.commit()

                if not ok:
                    server = ctx.session.get(_server_model(), ctx.server_id)
                    server_name = (
                        (server.hostname or server.name) if server else "server"
                    )
                    # Reuse the shared channel groundwork — one breach alert.
                    dispatch_restic_check_failed(
                        ctx.session,
                        server_id=ctx.server_id,
                        server_name=server_name,
                        summary=summary,
                    )
                    raise RuntimeError(f"restic check failed: {summary}")
                await ctx.emit(f"Integrity check passed: {summary}")
        finally:
            await ctx.capture(["rm", "-f", env_path])


def _server_model():
    from app.models.server import Server

    return Server


def _now_utc():
    from datetime import UTC, datetime

    return datetime.now(UTC)




# --------------------------------------------------------------------------- #
# AI Agents module (session 5.1): jailed git snapshot / diff / apply / rollback.
#
# Every git operation below is a registered command template (fixed argv, cwd =
# the validated working dir, `..`-rejected), rendered as a sub-step here — never
# a shell string (golden rule 1). The orchestrators write the snapshot/diff and
# the apply/rollback disposition onto the AIAgentSession row so the review screen
# and the audit trail stay in sync.
# --------------------------------------------------------------------------- #


def _load_ai_session(ctx: JobContext, session_id: int):
    """Load the AIAgentSession row this job acts on, or None if it's gone."""
    from app.models.ai_agent import AIAgentSession

    return ctx.session.get(AIAgentSession, int(session_id))


async def _git(ctx: JobContext, action_name: str, params: dict, *, label: str):
    """Render + stream one AI git sub-template as its own step; return exit code."""
    from app.core.commands import get_template, render

    cmd = render(get_template(action_name), params)
    with ctx.step(label):
        await ctx.emit(f"$ {cmd.display}")
        code = await ctx.stream(cmd.argv, cwd=cmd.cwd)
    return code


async def _git_capture(ctx: JobContext, action_name: str, params: dict, *, label: str):
    """Render + capture one AI git sub-template as its own step; return the result."""
    from app.core.commands import get_template, render

    cmd = render(get_template(action_name), params)
    with ctx.step(label):
        await ctx.emit(f"$ {cmd.display}")
        res = await ctx.capture(cmd.argv, cwd=cmd.cwd)
    return res


class AIPreChangeSnapshotAction(Action):
    """`ai.pre_change_snapshot` — verify the jail is a git repo and record a
    pre-change snapshot (base commit + `git stash create` of any pre-existing
    dirty state) onto the session, so a later rollback restores it exactly.

    Runs for every session at launch: for a read-write session it is the gated
    pre-change backup; for a read-only session it is the safety baseline the
    end-of-session violation check reverts against. Idempotent (all reads)."""

    async def run(self, ctx: JobContext) -> None:
        import json

        params = ctx.rendered.params_sanitized
        working_dir = params["working_dir"]
        session = _load_ai_session(ctx, params["session_id"])

        with ctx.step("Verify git working directory"):
            res = await ctx.capture(
                ["git", "rev-parse", "--is-inside-work-tree"], cwd=working_dir
            )
            if res.exit_code != 0 or res.stdout.strip() != "true":
                if session is not None:
                    session.status = "error"
                    session.close_reason = "not_a_git_repo"
                    ctx.session.commit()
                raise RuntimeError(
                    f"{working_dir!r} is not a git working tree — a scoped AI "
                    "session needs a git repo to snapshot and diff."
                )

        base = ""
        snap = ""
        with ctx.step("Record pre-change snapshot"):
            head = await ctx.capture(["git", "rev-parse", "HEAD"], cwd=working_dir)
            if head.exit_code != 0:
                if session is not None:
                    session.status = "error"
                    session.close_reason = "no_commits"
                    ctx.session.commit()
                raise RuntimeError(
                    "the working tree has no commits yet; commit an initial state "
                    "before starting a scoped session so rollback has a base."
                )
            base = head.stdout.strip()
            # `git stash create` snapshots tracked+staged changes into a commit
            # object WITHOUT touching the working tree (empty output = clean tree).
            stash = await ctx.capture(["git", "stash", "create"], cwd=working_dir)
            snap = stash.stdout.strip()
            if session is not None:
                session.base_commit = base
                session.snapshot_ref = snap or None
                session.status = "ready"
                ctx.session.commit()
            await ctx.emit(
                f"Pre-change snapshot recorded: base {base[:12]}…"
                + (f", dirty-snapshot {snap[:12]}…" if snap else " (clean tree).")
            )
            await ctx.emit(
                "SNAPSHOT_RESULT "
                + json.dumps({"base_commit": base, "snapshot_ref": snap}),
                stream="result",
            )


class AICaptureDiffAction(Action):
    """`ai.capture_diff` — on session end, stage the whole jail and capture the
    `git diff` against the pre-change base for the review screen.

    For a read-only session any non-empty diff is a violation: this action
    reverts it in the same job (reset --hard base, clean untracked, re-apply the
    pre-existing dirty snapshot) and records a `read_only_violation`, so a
    read-only session can never leave a kept write behind (server-side)."""

    async def run(self, ctx: JobContext) -> None:
        import json

        params = ctx.rendered.params_sanitized
        working_dir = params["working_dir"]
        session = _load_ai_session(ctx, params["session_id"])
        if session is None:
            raise RuntimeError(f"AI session {params['session_id']} not found")
        base = session.base_commit
        if not base:
            raise RuntimeError("session has no pre-change base commit to diff against")

        await _git(
            ctx, "ai.git_add_all", {"working_dir": working_dir}, label="Stage working tree"
        )
        diff_res = await _git_capture(
            ctx,
            "ai.git_diff_cached",
            {"working_dir": working_dir, "base": base},
            label="Capture git diff",
        )
        diff_text = diff_res.stdout
        session.diff_text = diff_text
        changed = bool(diff_text.strip())
        await ctx.emit(
            f"Captured diff vs {base[:12]}… — "
            + ("changes present." if changed else "no changes.")
        )

        if session.read_only and changed:
            # A read-only session must not keep any write: revert to the snapshot.
            await ctx.emit(
                "READ_ONLY_VIOLATION — the read-only session modified the tree; "
                "reverting to the pre-change snapshot.",
                stream="result",
            )
            await _rollback_to_snapshot(ctx, working_dir, base, session.snapshot_ref)
            session.disposition = "rolledback"
            session.status = "rolledback"
            session.close_reason = "read_only_violation"
        else:
            session.status = "reviewing"
        ctx.session.commit()
        await ctx.emit(
            "DIFF_RESULT "
            + json.dumps(
                {
                    "changed": changed,
                    "bytes": len(diff_text),
                    "read_only_violation": bool(session.read_only and changed),
                }
            ),
            stream="result",
        )


async def _rollback_to_snapshot(
    ctx: JobContext, working_dir: str, base: str, snapshot_ref: str | None
) -> None:
    """Restore the jail to its pre-change snapshot exactly: hard-reset to the base
    commit, remove untracked (non-ignored) files, then re-apply any pre-existing
    dirty snapshot. Shared by rollback and the read-only violation revert."""
    code = await _git(
        ctx, "ai.git_reset_hard", {"working_dir": working_dir, "ref": base},
        label=f"Reset --hard to {base[:12]}…",
    )
    if code != 0:
        raise RuntimeError(f"git reset --hard exited with status {code}")
    code = await _git(
        ctx, "ai.git_clean", {"working_dir": working_dir},
        label="Remove untracked files",
    )
    if code != 0:
        raise RuntimeError(f"git clean exited with status {code}")
    if snapshot_ref:
        code = await _git(
            ctx, "ai.git_stash_apply", {"working_dir": working_dir, "ref": snapshot_ref},
            label="Restore pre-existing changes",
        )
        if code != 0:
            raise RuntimeError(f"git stash apply exited with status {code}")


class AIApplyAction(Action):
    """`ai.apply` — keep the agent's changes: stage everything and commit it as a
    durable, audited commit in the jailed working dir. Refuses on a read-only
    session (defence in depth; the API also 403s). A clean tree is a no-op."""

    async def run(self, ctx: JobContext) -> None:
        params = ctx.rendered.params_sanitized
        working_dir = params["working_dir"]
        message = params["message"]
        session = _load_ai_session(ctx, params["session_id"])
        if session is not None and session.read_only:
            raise RuntimeError("cannot apply changes for a read-only session")

        await _git(
            ctx, "ai.git_add_all", {"working_dir": working_dir}, label="Stage changes"
        )
        status = await _git_capture(
            ctx, "ai.git_status", {"working_dir": working_dir},
            label="Check for staged changes",
        )
        if not status.stdout.strip():
            await ctx.emit("No changes to apply — the working tree is clean.")
        else:
            code = await _git(
                ctx,
                "ai.git_commit",
                {"working_dir": working_dir, "message": message},
                label="Commit changes",
            )
            if code != 0:
                raise RuntimeError(f"git commit exited with status {code}")
            await ctx.emit("Applied — changes committed in the jailed working dir.")
        if session is not None:
            session.disposition = "applied"
            session.status = "applied"
            ctx.session.commit()


class AIRollbackAction(Action):
    """`ai.rollback` — discard the agent's changes and restore the pre-change
    snapshot exactly (reset --hard base, clean untracked, re-apply pre-existing
    dirty snapshot). Reads base/snapshot from the session row."""

    async def run(self, ctx: JobContext) -> None:
        params = ctx.rendered.params_sanitized
        working_dir = params["working_dir"]
        session = _load_ai_session(ctx, params["session_id"])
        if session is None:
            raise RuntimeError(f"AI session {params['session_id']} not found")
        base = session.base_commit
        if not base:
            raise RuntimeError("session has no pre-change base commit to roll back to")

        await _rollback_to_snapshot(ctx, working_dir, base, session.snapshot_ref)
        session.disposition = "rolledback"
        session.status = "rolledback"
        ctx.session.commit()
        await ctx.emit("Rolled back — working dir restored to the pre-change snapshot.")
