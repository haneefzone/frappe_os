"""The command-template registry: the single source of every action the
platform can run. Future sessions register bench/site/backup templates here.

Starter templates (session 1.3):
- `system.echo_demo`   — multi-step demo, locked, used for the acceptance test.
- `server.detect_tools`— read-only toolchain inventory, lock-free.
"""

from __future__ import annotations

from app.core.commands.actions import (
    BackupAction,
    BenchBuildAction,
    BenchPreflightAction,
    BenchRestartAction,
    BenchUpdateAction,
    CreateBenchAction,
    CreateSiteAction,
    DetectToolsAction,
    DiscoverBenchesAction,
    EchoDemoAction,
    GetAppAction,
    InstallAppOnSiteAction,
    ListBranchesAction,
    MigrateAllSitesAction,
    RestoreAction,
    SetMaintenanceAction,
    SetSchedulerAction,
    SiteBackupAction,
    SiteMaintenanceAction,
    UninstallAppAction,
    ValidateBackupAction,
)
from app.core.commands.templates import (
    CommandTemplate,
    ParamSpec,
    UnknownAction,
)
from app.core.permissions import (
    APP_MANAGE,
    BACKUP_CREATE,
    BACKUP_RESTORE,
    BENCH_OPERATE,
    DANGER,
    SERVER_MANAGE,
    SITE_OPERATE,
)
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

# A Frappe app / module name = the install-app/uninstall-app argument and a bare
# marketplace get-app name. Lowercase alnum + underscore only.
APP_NAME = r"[a-z0-9_]{1,60}"

# A `bench get-app` source: a bare marketplace name OR an https/ssh repo URL.
# This is a *character* whitelist only — no shell metacharacters (space ; ` $ (
# ) & | < > \ or quotes) can pass. The HOST allowlist (github.com/gitlab.com,
# configurable) is enforced structurally in app.core.appsources.validate_repo_source
# before a source is ever stored or a job launched. Defence in depth.
APP_SOURCE = r"[A-Za-z0-9._:/@-]{1,200}"

# A git branch/ref name for --branch and the picker. No shell metacharacters.
BRANCH_NAME = r"[A-Za-z0-9._/-]{1,100}"

# A git remote URL for `git ls-remote` (never a bare name — the picker only
# lists branches for a repo). Same shell-safe whitelist as APP_SOURCE.
REPO_URL = r"[A-Za-z0-9._:/@-]{1,200}"

# A supervisor group target for `sudo supervisorctl restart <group>` on a
# production bench (session 1.10). The orchestrator builds it as
# `<bench-basename>:*` (restart every program in the bench's supervisor group),
# so the only shapes allowed are a bench-name-like token followed by the fixed
# `:*` wildcard — no spaces, no shell metacharacters. It is passed as its own
# argv element to `sudo -n` (execve, no shell), and matches the ratified sudoers
# allowlist line `supervisorctl restart *`.
SUPERVISOR_GROUP = r"[a-z0-9][a-z0-9._-]{0,80}:\*"

# A staged deploy key (PEM). Never placed into a shell command (written to a
# 0600 temp file via base64 through `capture`), so this is only a sanity bound:
# printable ASCII + whitespace (PEM is multiline). Carried on the job as one
# Fernet token and redacted from every log line.
DEPLOY_KEY = r"[\s!-~]{1,10000}"


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


# --- App sources & install (session 1.9) -------------------------------- #

# `bench get-app --branch {branch} {source}` — fetch an app onto a bench. The
# source is a marketplace name or an allowlisted repo URL (host checked in
# app.core.appsources before launch). Rendered as a sub-step by the orchestrator;
# runnable standalone (public sources only — the private deploy-key dance lives
# on the orchestrator + list-branches templates).
register(
    CommandTemplate(
        action_name="app.get",
        argv=("bench", "get-app", "--branch", "{branch}", "{source}"),
        cwd="{bench_path}",
        params=(
            ParamSpec("branch", regex=BRANCH_NAME),
            ParamSpec("source", regex=APP_SOURCE),
            ParamSpec("bench_path", regex=ABS_PATH, is_path=True),
        ),
        action_class=GetAppAction,
        idempotent=False,  # re-fetching over an existing app dir isn't safe.
        requires_lock=True,
        required_permission=APP_MANAGE,
        run_as=None,
    )
)

# The real `bench --site X install-app APP`. Rendered as a sub-step by the
# orchestrator (which wraps it in the dev-bench Redis dance); never launched on
# its own path — see InstallAppOnSiteAction.
register(
    CommandTemplate(
        action_name="app.install",
        argv=("bench", "--site", "{site}", "install-app", "{app}"),
        cwd="{bench_path}",
        params=(
            ParamSpec("site", regex=SITE_NAME),
            ParamSpec("app", regex=APP_NAME),
            ParamSpec("bench_path", regex=ABS_PATH, is_path=True),
        ),
        action_class=InstallAppOnSiteAction,  # unused directly; see note above.
        idempotent=False,
        requires_lock=True,
        required_permission=APP_MANAGE,
        run_as=None,
    )
)

# The orchestrator POST /api/sites/{id}/apps launches: get-app (if a source is
# given, with the deploy-key dance for private repos) -> install-app (Redis
# dance) -> register the matrix row, all in ONE job locked on the site. The
# deploy key is carried encrypted on the job and resolved at render time.
register(
    CommandTemplate(
        action_name="site.install_app",
        argv=("true",),  # nominal; InstallAppOnSiteAction drives the real steps.
        cwd=None,
        params=(
            ParamSpec("site", regex=SITE_NAME),
            ParamSpec("bench_path", regex=ABS_PATH, is_path=True),
            ParamSpec("app", regex=APP_NAME),
            ParamSpec("source", regex=APP_SOURCE, required=False),
            ParamSpec("branch", regex=BRANCH_NAME, required=False),
            ParamSpec("source_name", regex=APP_NAME, required=False),
            ParamSpec("deploy_key", regex=DEPLOY_KEY, secret=True, required=False),
        ),
        action_class=InstallAppOnSiteAction,
        idempotent=False,
        requires_lock=True,
        required_permission=APP_MANAGE,
        run_as=None,
        secret_sources={"deploy_key": "job"},
    )
)

# `bench --site X uninstall-app APP --yes` — DESTRUCTIVE (danger). Wrapped in the
# Redis dance; type-the-app-name confirm enforced in the UI + API. Auto pre-op
# backup (rule 5) wired when the backup engine lands (session 1.11).
register(
    CommandTemplate(
        action_name="app.uninstall",
        argv=("bench", "--site", "{site}", "uninstall-app", "{app}", "--yes"),
        cwd="{bench_path}",
        params=(
            ParamSpec("site", regex=SITE_NAME),
            ParamSpec("app", regex=APP_NAME),
            ParamSpec("bench_path", regex=ABS_PATH, is_path=True),
        ),
        action_class=UninstallAppAction,
        idempotent=False,  # destructive: never auto-retried.
        requires_lock=True,
        required_permission=DANGER,
        run_as=None,
    )
)

# `git ls-remote --heads {url}` for the branch picker. Read-only; emits a
# BRANCHES_RESULT json line the wizard tails. Private sources pass a deploy key
# (carried encrypted on the job) for the same GIT_SSH_COMMAND dance.
register(
    CommandTemplate(
        action_name="app.list_branches",
        argv=("git", "ls-remote", "--heads", "{url}"),
        cwd=None,
        params=(
            ParamSpec("url", regex=REPO_URL),
            ParamSpec("deploy_key", regex=DEPLOY_KEY, secret=True, required=False),
        ),
        action_class=ListBranchesAction,
        idempotent=True,  # read-only, safely retried.
        requires_lock=False,
        required_permission=APP_MANAGE,
        run_as=None,
        secret_sources={"deploy_key": "job"},
    )
)


# --- Maintenance actions (session 1.10) --------------------------------- #

# Site-level maintenance ops. Each is a single fixed `bench --site X <verb>`
# wrapped by SiteMaintenanceAction in the dev-bench Redis dance (gotcha #3):
# `migrate` clears caches / enqueues jobs and `clear-cache` flushes redis, so on
# a dev bench (where `bench start` isn't running) the bench-owned Redis must be
# up or they fail `Error 111`. Locked per site so two maintenance ops on the
# same site can't race; SITE_OPERATE so Operators can run them.

# `bench --site X migrate` — run pending schema patches. Long-running and not
# auto-retried (a determinate patch failure must not silently re-run).
register(
    CommandTemplate(
        action_name="site.migrate",
        argv=("bench", "--site", "{site}", "migrate"),
        cwd="{bench_path}",
        params=(
            ParamSpec("site", regex=SITE_NAME),
            ParamSpec("bench_path", regex=ABS_PATH, is_path=True),
        ),
        action_class=SiteMaintenanceAction,
        idempotent=False,
        requires_lock=True,
        required_permission=SITE_OPERATE,
        run_as=None,
    )
)

# `bench --site X clear-cache` — flush the site's redis + in-process caches.
# Safely repeatable, so idempotent (a transient blip auto-retries).
register(
    CommandTemplate(
        action_name="site.clear_cache",
        argv=("bench", "--site", "{site}", "clear-cache"),
        cwd="{bench_path}",
        params=(
            ParamSpec("site", regex=SITE_NAME),
            ParamSpec("bench_path", regex=ABS_PATH, is_path=True),
        ),
        action_class=SiteMaintenanceAction,
        idempotent=True,
        requires_lock=True,
        required_permission=SITE_OPERATE,
        run_as=None,
    )
)

# `bench --site X clear-website-cache` — flush only the website/page cache.
register(
    CommandTemplate(
        action_name="site.clear_website_cache",
        argv=("bench", "--site", "{site}", "clear-website-cache"),
        cwd="{bench_path}",
        params=(
            ParamSpec("site", regex=SITE_NAME),
            ParamSpec("bench_path", regex=ABS_PATH, is_path=True),
        ),
        action_class=SiteMaintenanceAction,
        idempotent=True,
        requires_lock=True,
        required_permission=SITE_OPERATE,
        run_as=None,
    )
)

# `bench --site X backup` — a lightweight db-only safety backup (no --with-files).
# Registered so the bench.update orchestrator (and the 1.11 backup engine) render
# their db-only pre-step through the safe registry. Not launched on its own path;
# SiteBackupAction only runs the command as a safety net (no Backup row).
register(
    CommandTemplate(
        action_name="site.backup_db",
        argv=("bench", "--site", "{site}", "backup"),
        cwd="{bench_path}",
        params=(
            ParamSpec("site", regex=SITE_NAME),
            ParamSpec("bench_path", regex=ABS_PATH, is_path=True),
        ),
        action_class=SiteBackupAction,
        idempotent=True,  # a db dump is safely repeatable.
        requires_lock=True,
        required_permission=BACKUP_CREATE,
        run_as=None,
    )
)

# `bench --site X backup --with-files` — a full backup (db + public + private
# files). Rendered as a sub-step by the 1.11 BackupAction engine; never launched
# on its own path.
register(
    CommandTemplate(
        action_name="site.backup_files",
        argv=("bench", "--site", "{site}", "backup", "--with-files"),
        cwd="{bench_path}",
        params=(
            ParamSpec("site", regex=SITE_NAME),
            ParamSpec("bench_path", regex=ABS_PATH, is_path=True),
        ),
        action_class=SiteBackupAction,
        idempotent=True,
        requires_lock=True,
        required_permission=BACKUP_CREATE,
        run_as=None,
    )
)

# `bench build` — (re)compile the bench's JS/CSS assets. No site, no redis;
# safely repeatable. Locked per bench.
register(
    CommandTemplate(
        action_name="bench.build",
        argv=("bench", "build"),
        cwd="{bench_path}",
        params=(ParamSpec("bench_path", regex=ABS_PATH, is_path=True),),
        action_class=BenchBuildAction,
        idempotent=True,
        requires_lock=True,
        required_permission=BENCH_OPERATE,
        run_as=None,
    )
)

# `sudo -n supervisorctl restart <group>` — the real restart command for a
# PRODUCTION bench. Rendered as a sub-step by BenchRestartAction (which builds
# `<bench-basename>:*` and detects dev/prod first); never launched on its own
# path. `sudo -n` fails loudly (no password prompt) if the ratified sudoers
# allowlist line isn't installed on the server.
register(
    CommandTemplate(
        action_name="bench.supervisor_restart",
        argv=("sudo", "-n", "supervisorctl", "restart", "{group}"),
        cwd=None,
        params=(ParamSpec("group", regex=SUPERVISOR_GROUP),),
        action_class=BenchRestartAction,  # unused directly; see note above.
        idempotent=False,
        requires_lock=True,
        required_permission=BENCH_OPERATE,
        run_as=None,
    )
)

# The orchestrator POST /api/benches/{id}/restart launches: detect dev vs
# production; on production run `sudo supervisorctl restart <group>:*`; on a dev
# bench fail with an informative message (dev benches are started manually with
# `bench start`). Non-idempotent so the dev informative-failure isn't retried.
register(
    CommandTemplate(
        action_name="bench.restart",
        argv=("true",),  # nominal; BenchRestartAction drives the real steps.
        cwd=None,
        params=(ParamSpec("bench_path", regex=ABS_PATH, is_path=True),),
        action_class=BenchRestartAction,
        idempotent=False,
        requires_lock=True,
        required_permission=BENCH_OPERATE,
        run_as=None,
    )
)

# The orchestrator POST /api/benches/{id}/migrate-all launches: iterate the
# bench's known active sites and run `bench --site X migrate` for each as its own
# ordered step, wrapped once in the dev-bench Redis dance. A per-site failure is
# recorded and the run continues; the job fails at the end if any site failed.
# SITE_OPERATE (same underlying op as a single site migrate).
register(
    CommandTemplate(
        action_name="bench.migrate_all",
        argv=("true",),  # nominal; MigrateAllSitesAction drives the real steps.
        cwd=None,
        params=(ParamSpec("bench_path", regex=ABS_PATH, is_path=True),),
        action_class=MigrateAllSitesAction,
        idempotent=False,
        requires_lock=True,
        required_permission=SITE_OPERATE,
        run_as=None,
    )
)

# The orchestrator POST /api/benches/{id}/update launches (high queue,
# long-running): a lightweight db-only safety backup of every site FIRST, then
# `bench update`. Wrapped once in the dev-bench Redis dance. Non-idempotent — a
# determinate update failure is never auto-retried on top of a half-update.
register(
    CommandTemplate(
        action_name="bench.update",
        argv=("bench", "update"),
        cwd="{bench_path}",
        params=(ParamSpec("bench_path", regex=ABS_PATH, is_path=True),),
        action_class=BenchUpdateAction,
        idempotent=False,
        requires_lock=True,
        required_permission=BENCH_OPERATE,
        run_as=None,
    )
)


# --- Backup & guided restore (session 1.11) ----------------------------- #

# A backup id (digits) the validate/restore actions load the Backup row by.
BACKUP_ID = r"[0-9]{1,12}"
# --with-files toggle carried as an enum so nothing but "0"/"1" reaches the CLI.
WITH_FILES = ("0", "1")
# The restore target mode; each drives a distinct orchestration path.
RESTORE_MODES = ("same_site", "new_site", "different_bench")
# A base64 Fernet-style encryption_key copied from a source site_config backup
# into the target site_config (gotcha #7). Passed as its own argv element to
# `bench set-config` (execve, no shell); this is only a shell-safe sanity bound.
ENCRYPTION_KEY = r"[A-Za-z0-9+/=_-]{1,120}"

# The full backup engine POST /api/sites/{id}/backups launches: run `bench
# backup [--with-files]` wrapped in the dev-bench Redis dance, then inspect the
# backups dir to capture each artifact's path/size/sha256 and record a Backup
# row. Locked per site so two backups of the same site can't race the artifact
# parse. Non-idempotent: a Backup row is created per run, so an auto-retry must
# not silently produce a duplicate half-recorded row.
register(
    CommandTemplate(
        action_name="site.backup",
        argv=("true",),  # nominal; BackupAction drives the real steps.
        cwd=None,
        params=(
            ParamSpec("site", regex=SITE_NAME),
            ParamSpec("bench_path", regex=ABS_PATH, is_path=True),
            ParamSpec("with_files", enum=WITH_FILES),
            # The pending Backup row created by the API before the job runs, so a
            # failed backup still leaves a visible (failed) record.
            ParamSpec("backup_id", regex=BACKUP_ID, required=False),
        ),
        action_class=BackupAction,
        idempotent=False,
        requires_lock=True,
        required_permission=BACKUP_CREATE,
        run_as=None,
    )
)

# `backup.validate` — re-verify every stored artifact checksum against the file
# on the server and read the config backup's encryption_key presence + version.
# Read-only (no writes to the server); idempotent so a transient SSH blip retries.
register(
    CommandTemplate(
        action_name="backup.validate",
        argv=("true",),  # nominal; ValidateBackupAction drives the real steps.
        cwd=None,
        params=(
            ParamSpec("backup_id", regex=BACKUP_ID),
            ParamSpec("site", regex=SITE_NAME),
            ParamSpec("bench_path", regex=ABS_PATH, is_path=True),
        ),
        action_class=ValidateBackupAction,
        idempotent=True,
        requires_lock=False,
        required_permission=BACKUP_CREATE,
        run_as=None,
    )
)

# Internal restore sub-commands, rendered by RestoreAction (never launched on
# their own path). `--force` per gotcha #7; paths are absolute artifact paths.
register(
    CommandTemplate(
        action_name="site.restore_db",
        argv=("bench", "--site", "{site}", "--force", "restore", "{db_path}"),
        cwd="{bench_path}",
        params=(
            ParamSpec("site", regex=SITE_NAME),
            ParamSpec("bench_path", regex=ABS_PATH, is_path=True),
            ParamSpec("db_path", regex=ABS_PATH, is_path=True),
        ),
        action_class=RestoreAction,  # unused directly; see note above.
        idempotent=False,
        requires_lock=True,
        required_permission=BACKUP_RESTORE,
        run_as=None,
    )
)

register(
    CommandTemplate(
        action_name="site.restore_files",
        argv=(
            "bench", "--site", "{site}", "--force", "restore", "{db_path}",
            "--with-public-files", "{public_files}",
            "--with-private-files", "{private_files}",
        ),
        cwd="{bench_path}",
        params=(
            ParamSpec("site", regex=SITE_NAME),
            ParamSpec("bench_path", regex=ABS_PATH, is_path=True),
            ParamSpec("db_path", regex=ABS_PATH, is_path=True),
            ParamSpec("public_files", regex=ABS_PATH, is_path=True),
            ParamSpec("private_files", regex=ABS_PATH, is_path=True),
        ),
        action_class=RestoreAction,  # unused directly; see note above.
        idempotent=False,
        requires_lock=True,
        required_permission=BACKUP_RESTORE,
        run_as=None,
    )
)

# `bench --site X set-config encryption_key <key>` — copy the source site's
# encryption_key into the target site_config (gotcha #7). The key is a secret:
# masked in the display, never streamed or persisted.
register(
    CommandTemplate(
        action_name="site.set_encryption_key",
        argv=(
            "bench", "--site", "{site}", "set-config", "encryption_key", "{key}",
        ),
        cwd="{bench_path}",
        params=(
            ParamSpec("site", regex=SITE_NAME),
            ParamSpec("bench_path", regex=ABS_PATH, is_path=True),
            ParamSpec("key", regex=ENCRYPTION_KEY, secret=True),
        ),
        action_class=RestoreAction,  # unused directly; see note above.
        idempotent=True,  # writing the same key twice is harmless.
        requires_lock=True,
        required_permission=BACKUP_RESTORE,
        run_as=None,
    )
)

# The guided restore orchestrator POST /api/restores launches: (optional) create
# the target site (new_site mode), an automatic pre-restore backup if the target
# already holds data, `bench --force restore` (db [+ files]), copy the source
# encryption_key into the target site_config, then `bench migrate` — gotcha #7
# exactly. Wrapped once in the dev-bench Redis dance. Locked per target site.
# BACKUP_RESTORE launches it; a destructive same-site/over-existing restore also
# requires the typed-site-name confirm, enforced in the API.
register(
    CommandTemplate(
        action_name="site.restore",
        argv=("true",),  # nominal; RestoreAction drives the real steps.
        cwd=None,
        params=(
            ParamSpec("site", regex=SITE_NAME),
            ParamSpec("bench_path", regex=ABS_PATH, is_path=True),
            ParamSpec("mode", enum=RESTORE_MODES),
            ParamSpec("with_files", enum=WITH_FILES),
            ParamSpec("backup_id", regex=BACKUP_ID),
            ParamSpec("db_path", regex=ABS_PATH, is_path=True),
            ParamSpec("public_files", regex=ABS_PATH, is_path=True, required=False),
            ParamSpec("private_files", regex=ABS_PATH, is_path=True, required=False),
            ParamSpec("config_path", regex=ABS_PATH, is_path=True, required=False),
            # new_site mode only: create the target first (gotcha #4).
            ParamSpec("admin_pw", regex=SECRET_TEXT, secret=True, required=False),
            ParamSpec("db_root_pw", regex=SECRET_TEXT, secret=True, required=False),
        ),
        action_class=RestoreAction,
        idempotent=False,
        requires_lock=True,
        required_permission=BACKUP_RESTORE,
        run_as=None,
        secret_sources={
            # Supplied by the operator (new_site mode), carried encrypted on the job.
            "admin_pw": "job",
            # Pulled from the server settings, server-side, never the browser.
            "db_root_pw": "server:mariadb_root_password_enc",
        },
    )
)
