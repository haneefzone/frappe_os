"""The command-template registry: the single source of every action the
platform can run. Future sessions register bench/site/backup templates here.

Starter templates (session 1.3):
- `system.echo_demo`   — multi-step demo, locked, used for the acceptance test.
- `server.detect_tools`— read-only toolchain inventory, lock-free.
"""

from __future__ import annotations

from app.core.commands.actions import (
    BenchPreflightAction,
    CreateBenchAction,
    CreateSiteAction,
    DetectToolsAction,
    DiscoverBenchesAction,
    EchoDemoAction,
    SetMaintenanceAction,
    SetSchedulerAction,
)
from app.core.commands.templates import (
    CommandTemplate,
    ParamSpec,
    UnknownAction,
)
from app.core.permissions import BENCH_OPERATE, SERVER_MANAGE, SITE_OPERATE
from app.core.version_matrix import SUPPORTED_BRANCHES, SUPPORTED_MAJORS

# Whitelist for free-text demo input: word chars plus a few safe punctuation
# marks. Excludes ; ` $ ( ) & | < > \n and quotes, so shell metacharacters and
# command substitution can never pass validation.
SAFE_TEXT = r"[\w .,:@/=+-]{1,200}"

# A comma-separated list of absolute base paths for bench discovery. Each path
# is only alnum + . _ - / — no shell metacharacters can pass, and the action
# re-validates every element before it reaches the server.
BASE_PATHS = r"(/[\w./-]{0,300})(,/[\w./-]{0,300})*"

# A bench directory name: starts alnum, then alnum plus . _ - (no slashes, no
# shell metacharacters). `bench init <name>` creates this dir under the path.
BENCH_NAME = r"[a-z0-9][a-z0-9._-]{1,80}"

# An absolute parent path the bench is created in (the bench user's home by
# default). Only alnum + . _ - / — the action/df builder re-validates it too.
ABS_PATH = r"/[\w./-]{1,300}"

# A Frappe site name = its host name (CLAUDE.md rule 1): starts alnum, then
# alnum plus . - (dots for FQDNs like test1.localhost; no slashes, no shell
# metacharacters). `bench new-site <name>` creates sites/<name> under the bench.
SITE_NAME = r"[a-z0-9][a-z0-9.-]{1,80}"

# A secret password value (admin / MariaDB root). Passed as its own argv element
# to `bench new-site` (execve, no shell), so injection isn't possible; this
# whitelist is a sanity bound: printable ASCII incl. space, no control chars.
SECRET_TEXT = r"[ -~]{1,128}"

# scheduler enable|disable and maintenance on|off are fixed sub-verbs of the
# bench command, validated as enums so nothing else can reach the CLI.
SCHEDULER_STATES = ("enable", "disable")
MAINTENANCE_STATES = ("on", "off")


_TEMPLATES: dict[str, CommandTemplate] = {}


def register(template: CommandTemplate) -> CommandTemplate:
    if template.action_name in _TEMPLATES:
        raise ValueError(f"duplicate template {template.action_name!r}")
    _TEMPLATES[template.action_name] = template
    return template


def get_template(action_name: str) -> CommandTemplate:
    try:
        return _TEMPLATES[action_name]
    except KeyError as exc:
        raise UnknownAction(action_name) from exc


def all_templates() -> list[CommandTemplate]:
    return sorted(_TEMPLATES.values(), key=lambda t: t.action_name)


register(
    CommandTemplate(
        action_name="system.echo_demo",
        argv=("echo", "{message}"),
        cwd=None,
        params=(ParamSpec("message", regex=SAFE_TEXT),),
        action_class=EchoDemoAction,
        idempotent=True,
        requires_lock=True,
        required_permission=SERVER_MANAGE,
        run_as=None,
    )
)

register(
    CommandTemplate(
        action_name="bench.discover",
        argv=("true",),  # nominal; DiscoverBenchesAction runs its own commands.
        cwd=None,
        params=(ParamSpec("base_paths", regex=BASE_PATHS, required=False),),
        action_class=DiscoverBenchesAction,
        idempotent=True,
        # A per-server lock (target_type=server) so two discoveries can't race
        # the upsert/vanish pass on the same server's benches.
        requires_lock=True,
        required_permission=SERVER_MANAGE,
        run_as=None,
    )
)

# --- Guided bench creation (session 1.7) --------------------------------- #

# The wizard's live, re-runnable pre-flight. Read-only probes, no lock; the job
# always succeeds if the checks ran (blocked-ness is in the result), so it is
# idempotent and a transient SSH blip auto-retries.
register(
    CommandTemplate(
        action_name="bench.preflight",
        argv=("true",),  # nominal; BenchPreflightAction runs the fixed probes.
        cwd=None,
        params=(
            ParamSpec("frappe_version", enum=SUPPORTED_MAJORS),
            ParamSpec("path", regex=ABS_PATH, is_path=True),
        ),
        action_class=BenchPreflightAction,
        idempotent=True,
        requires_lock=False,
        required_permission=BENCH_OPERATE,
        run_as=None,
    )
)

# The actual `bench init`. Registered so the create orchestration renders its
# argv through the safe registry (never string interpolation). Not launched on
# its own path today — CreateBenchAction renders and runs it as a step.
register(
    CommandTemplate(
        action_name="bench.init",
        argv=("bench", "init", "--frappe-branch", "{branch}", "{name}"),
        cwd="{path}",
        params=(
            ParamSpec("branch", enum=SUPPORTED_BRANCHES),
            ParamSpec("name", regex=BENCH_NAME),
            ParamSpec("path", regex=ABS_PATH, is_path=True),
        ),
        action_class=CreateBenchAction,  # unused directly; see note above.
        idempotent=False,  # creating a bench is not safely auto-retried.
        requires_lock=True,
        required_permission=BENCH_OPERATE,
        run_as=None,
    )
)

# The orchestrator the API launches: pre-flight steps -> bench init -> register,
# all in one non-idempotent, locked job keyed on the new bench's path.
register(
    CommandTemplate(
        action_name="bench.create",
        argv=("true",),  # nominal; CreateBenchAction drives the real steps.
        cwd=None,
        params=(
            ParamSpec("frappe_version", enum=SUPPORTED_MAJORS),
            ParamSpec("name", regex=BENCH_NAME),
            ParamSpec("path", regex=ABS_PATH, is_path=True),
        ),
        action_class=CreateBenchAction,
        idempotent=False,
        requires_lock=True,
        required_permission=BENCH_OPERATE,
        run_as=None,
    )
)

# --- Site creation & controls (session 1.8) ----------------------------- #

# The real `bench new-site`, non-interactive per gotcha #4 (omitting
# --mariadb-root-username causes an interactive prompt that hangs the job). Both
# passwords are separate argv elements (execve, no shell), so nothing is
# interpolated. `--mariadb-user-host-login-scope=%` replaces the deprecated
# `--no-mariadb-socket`. Registered so CreateSiteAction renders it through the
# safe registry; never launched on its own path.
register(
    CommandTemplate(
        action_name="site.new",
        argv=(
            "bench",
            "new-site",
            "{site}",
            "--mariadb-root-username",
            "root",
            "--mariadb-root-password",
            "{db_root_pw}",
            "--admin-password",
            "{admin_pw}",
            "--mariadb-user-host-login-scope=%",
        ),
        cwd="{bench_path}",
        params=(
            ParamSpec("site", regex=SITE_NAME),
            ParamSpec("db_root_pw", regex=SECRET_TEXT, secret=True),
            ParamSpec("admin_pw", regex=SECRET_TEXT, secret=True),
            ParamSpec("bench_path", regex=ABS_PATH, is_path=True),
        ),
        action_class=CreateSiteAction,  # unused directly; see note above.
        idempotent=False,
        requires_lock=True,
        required_permission=SITE_OPERATE,
        run_as=None,
    )
)

# The orchestrator the API launches: detect dev/prod, run the dev-bench Redis
# dance (gotcha #3) around `bench new-site`, then register the site. One
# non-idempotent, locked job keyed on the bench so two site creates on the same
# dev bench can't fight over its shared Redis.
register(
    CommandTemplate(
        action_name="site.create",
        argv=("true",),  # nominal; CreateSiteAction drives the real steps.
        cwd=None,
        params=(
            ParamSpec("site", regex=SITE_NAME),
            ParamSpec("bench_path", regex=ABS_PATH, is_path=True),
            ParamSpec("db_root_pw", regex=SECRET_TEXT, secret=True),
            ParamSpec("admin_pw", regex=SECRET_TEXT, secret=True),
        ),
        action_class=CreateSiteAction,
        idempotent=False,
        requires_lock=True,
        required_permission=SITE_OPERATE,
        run_as=None,
        secret_sources={
            # Pulled from the server settings, server-side, never the browser.
            "db_root_pw": "server:mariadb_root_password_enc",
            # Supplied by the operator in the wizard, carried encrypted on the job.
            "admin_pw": "job",
        },
    )
)

# Fast site toggles. Plain single bench commands (no Redis dance — they don't
# enqueue background jobs), locked per site so two toggles on the same site
# can't race. SetScheduler/SetMaintenance also record the new state on the row.
register(
    CommandTemplate(
        action_name="site.set_scheduler",
        argv=("bench", "--site", "{site}", "scheduler", "{state}"),
        cwd="{bench_path}",
        params=(
            ParamSpec("site", regex=SITE_NAME),
            ParamSpec("bench_path", regex=ABS_PATH, is_path=True),
            ParamSpec("state", enum=SCHEDULER_STATES),
        ),
        action_class=SetSchedulerAction,
        idempotent=True,  # setting a flag is safely repeatable.
        requires_lock=True,
        required_permission=SITE_OPERATE,
        run_as=None,
    )
)

register(
    CommandTemplate(
        action_name="site.set_maintenance",
        argv=("bench", "--site", "{site}", "set-maintenance-mode", "{state}"),
        cwd="{bench_path}",
        params=(
            ParamSpec("site", regex=SITE_NAME),
            ParamSpec("bench_path", regex=ABS_PATH, is_path=True),
            ParamSpec("state", enum=MAINTENANCE_STATES),
        ),
        action_class=SetMaintenanceAction,
        idempotent=True,
        requires_lock=True,
        required_permission=SITE_OPERATE,
        run_as=None,
    )
)

register(
    CommandTemplate(
        action_name="server.detect_tools",
        argv=("true",),  # nominal; DetectToolsAction runs its own fixed commands.
        cwd=None,
        params=(),
        action_class=DetectToolsAction,
        idempotent=True,
        requires_lock=False,
        required_permission=SERVER_MANAGE,
        run_as=None,
    )
)
