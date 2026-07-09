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
    DetectToolsAction,
    DiscoverBenchesAction,
    EchoDemoAction,
)
from app.core.commands.templates import (
    CommandTemplate,
    ParamSpec,
    UnknownAction,
)
from app.core.permissions import BENCH_OPERATE, SERVER_MANAGE
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
            ParamSpec("path", regex=ABS_PATH),
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
            ParamSpec("path", regex=ABS_PATH),
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
            ParamSpec("path", regex=ABS_PATH),
        ),
        action_class=CreateBenchAction,
        idempotent=False,
        requires_lock=True,
        required_permission=BENCH_OPERATE,
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
