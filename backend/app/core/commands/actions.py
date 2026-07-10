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


class BackupAction(Action):
    """`site.backup` — the full backup engine (session 1.11).

    Detects dev vs production, runs the dev-bench Redis dance (gotcha #3) around
    `bench backup [--with-files]`, then inspects the site's backups dir to capture
    each artifact's absolute path, size and sha256 and record a `Backup` row. The
    API pre-creates a `pending` Backup row and passes its id so a failed backup
    still leaves a visible failed record. Non-idempotent: one row per run."""

    async def run(self, ctx: JobContext) -> None:
        params = ctx.rendered.params_sanitized
        site = params["site"]
        bench_path = params["bench_path"]
        with_files = params.get("with_files") == "1"
        backup_id = params.get("backup_id")

        bench = _load_bench(ctx, bench_path)
        queue_port, cache_port = _redis_ports(bench)
        is_dev = await _detect_bench_mode(ctx, bench_path)

        started_redis = False
        try:
            if is_dev:
                await _start_dev_redis(ctx, bench_path)
                started_redis = True
            await _run_backup(
                ctx,
                site=site,
                bench_path=bench_path,
                bench=bench,
                with_files=with_files,
                backup_id=backup_id,
            )
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
