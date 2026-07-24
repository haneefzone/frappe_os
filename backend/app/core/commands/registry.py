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
    CertbotIssueAction,
    CertbotRenewAction,
    CreateBenchAction,
    CreateSiteAction,
    DetectToolsAction,
    DiscoverBenchesAction,
    DnsCheckAction,
    DriftCheckAction,
    EchoDemoAction,
    GetAppAction,
    InstallAppOnSiteAction,
    ListBranchesAction,
    MigrateAllSitesAction,
    MoveBackupAction,
    RenderVhostAction,
    RestartServiceAction,
    RestoreAction,
    SetMaintenanceAction,
    SetSchedulerAction,
    SetupProductionAction,
    SiteBackupAction,
    SiteMaintenanceAction,
    SslExpiryScanAction,
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
    BACKUP_TRANSFER,
    BENCH_OPERATE,
    DANGER,
    SERVER_MANAGE,
    SITE_OPERATE,
    SSL_MANAGE,
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

# A custom domain / hostname (session 2.4): lowercase FQDN, at least one dot,
# labels of 1–63 chars from alnum + hyphen. No slashes, no shell metacharacters,
# so it is injection-safe as its own argv element to certbot / in an nginx
# server_name. The DNS/cert actions never build a shell string from it.
DOMAIN_NAME = r"[a-z0-9](?:[a-z0-9-]{0,62})(?:\.[a-z0-9](?:[a-z0-9-]{0,62}))+"

# A contact email for Let's Encrypt registration (certbot -m). Character
# whitelist only (no shell metacharacters); passed as its own argv element.
EMAIL = r"[a-zA-Z0-9._%+-]{1,64}@[a-zA-Z0-9.-]{1,190}\.[a-zA-Z]{2,63}"

# A numeric Domain-row id threaded into a domain/SSL job so the action can update
# the right row (dns_ok / cert_expires_at). Digits only.
DOMAIN_ID = r"[0-9]{1,12}"

# nginx vhost TLS toggle: whether render_vhost emits the 443 server block.
SSL_STATES = ("on", "off")

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
        # Creating a site writes its site_config.json and may touch the bench's
        # common_site_config.json — capture both as the new baseline.
        writes_config=("site_config", "common_site_config"),
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
        # Writes the scheduler flag into the site's site_config.json (session 6.7
        # drift baseline moves with this managed change).
        writes_config=("site_config",),
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
        writes_config=("site_config",),
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
# Redis dance; type-the-app-name confirm enforced in the UI + API. An automatic
# pre-op backup (rule 5) is taken before the uninstall by `UninstallAppAction`.
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
            # Optional S3-compatible target to push the artifacts to after the
            # backup is recorded (session 2.2); its keys are read server-side.
            ParamSpec("storage_target_id", regex=BACKUP_ID, required=False),
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

# `backup.move_across_servers` — copy an offsite backup down onto another server,
# re-verifying each artifact's sha256 on arrival, and register the moved copy
# (session 2.6). Runs on the DESTINATION server; the source backup must already
# be offsite (its keys/target are passed in). Locked on the destination site so a
# move can't race a backup/restore of the same site. Non-idempotent (creates one
# Backup row), so it must never auto-retry into a duplicate.
register(
    CommandTemplate(
        action_name="backup.move_across_servers",
        argv=("true",),  # nominal; MoveBackupAction drives the real steps.
        cwd=None,
        params=(
            ParamSpec("backup_id", regex=BACKUP_ID),
            ParamSpec("storage_target_id", regex=BACKUP_ID),
            # Absolute destination directory on the target server (the site's
            # backups dir); the platform computes it, never the user's shell.
            ParamSpec("dest_dir", regex=ABS_PATH, is_path=True),
            ParamSpec("target_site", regex=SITE_NAME),
            ParamSpec("target_bench_id", regex=BACKUP_ID),
        ),
        action_class=MoveBackupAction,
        idempotent=False,
        requires_lock=True,
        required_permission=BACKUP_TRANSFER,
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

# --- Monitoring: restart a managed service (session 1.12) ---------------- #

# The four services the monitoring grid shows and can restart. Passed as its own
# argv element to `sudo -n systemctl restart <service>` (execve, no shell); the
# enum is the whitelist and it matches the ratified sudoers allowlist lines.
SERVICE_NAMES = ("nginx", "mariadb", "redis-server", "supervisor")

register(
    CommandTemplate(
        action_name="server.restart_service",
        argv=("sudo", "-n", "systemctl", "restart", "{service}"),
        cwd=None,
        params=(ParamSpec("service", enum=SERVICE_NAMES),),
        action_class=RestartServiceAction,
        # A failed restart must not silently retry; the operator re-clicks.
        idempotent=False,
        # Per (server, service) lock: never restart the same service twice at once.
        requires_lock=True,
        required_permission=SERVER_MANAGE,
        run_as=None,
    )
)


# --- Production setup (session 2.5) ------------------------------------- #

# A Linux username for `bench setup production <user>` (the bench-owner the
# generated supervisor/nginx config runs as). POSIX-portable shape; passed as its
# own argv element (execve, no shell) and re-validated by the fdm-elevate helper.
LINUX_USER = r"[a-z_][a-z0-9_-]{0,31}"

# The real conversion command, run under the TEMPORARY single-command sudoers
# drop-in installed by `fdm-elevate grant`. `{bench_bin}` is the absolute bench
# path the helper reported (server-sourced, ABS_PATH-validated — never user
# input); `sudo -n` fails loudly if the drop-in isn't installed. Rendered as a
# sub-step by SetupProductionAction; never launched on its own path.
register(
    CommandTemplate(
        action_name="bench.setup_production_run",
        argv=("sudo", "-n", "{bench_bin}", "setup", "production", "{production_user}"),
        cwd="{bench_path}",
        params=(
            ParamSpec("bench_bin", regex=ABS_PATH, is_path=True),
            ParamSpec("production_user", regex=LINUX_USER),
            ParamSpec("bench_path", regex=ABS_PATH, is_path=True),
        ),
        action_class=SetupProductionAction,  # unused directly; see note above.
        idempotent=False,
        requires_lock=True,
        required_permission=BENCH_OPERATE,
        run_as=None,
    )
)

# `sudo -n nginx -t` — the post-generation nginx config gate. Absolute path
# matches the ratified sudoers allowlist line (`/usr/sbin/nginx -t`); `sudo -n`
# fails loudly if that line isn't installed. No params. Rendered by
# SetupProductionAction; also reusable by the 2.4 SSL vhost work.
register(
    CommandTemplate(
        action_name="bench.nginx_test",
        argv=("sudo", "-n", "/usr/sbin/nginx", "-t"),
        cwd=None,
        params=(),
        action_class=SetupProductionAction,  # unused directly; see note above.
        idempotent=True,  # read-only config test, safely repeatable.
        requires_lock=False,
        required_permission=BENCH_OPERATE,
        run_as=None,
    )
)

# The orchestrator POST /api/benches/{id}/setup-production launches (high queue,
# long-running): detect dev/prod (refuse if already prod), pre-backup + hash the
# nginx/supervisor config, install a time-boxed single-command sudoers drop-in,
# run `bench setup production <user>`, toggle is_production, diff the config
# before/after, `nginx -t`, and ALWAYS revoke the drop-in. Locked per bench.
# Non-idempotent — a determinate failure is never auto-retried on a half-convert.
register(
    CommandTemplate(
        action_name="bench.setup_production",
        argv=("true",),  # nominal; SetupProductionAction drives the real steps.
        cwd=None,
        params=(
            ParamSpec("bench_path", regex=ABS_PATH, is_path=True),
            ParamSpec("production_user", regex=LINUX_USER),
        ),
        action_class=SetupProductionAction,
        idempotent=False,
        requires_lock=True,
        required_permission=BENCH_OPERATE,
        run_as=None,
        # Production conversion regenerates the root nginx + supervisor config —
        # move those (reduced-fidelity) baselines with the managed change.
        writes_config=("nginx.conf", "supervisor.conf", "supervisor.confd"),
    )
)


# --- Scheduled maintenance (session 2.1) -------------------------------- #

# `backup.retention_sweep` — prune a site's backups down to its retention policy.
# Nominal argv ("true"); RetentionSweepAction reads the site's Backup rows, logs a
# dry-run kept/removed summary, then `rm -f`s the excess artifacts (each an
# absolute argv element under private/backups/) and deletes their rows. It NEVER
# removes the newest/only backup. Destructive (deletes) so non-idempotent (never
# auto-retried) and locked per site so a sweep and a backup can't race. Gated on
# BACKUP_CREATE: only Operator+ (who may create backups) can run one, and the
# retention policy itself is only editable by schedule managers (Admin/Developer).
# Imported locally (not via the shared import block) to keep concurrent-session
# edits to this file collision-free.
from app.core.commands.actions import (  # noqa: E402
    RetentionSweepAction as _RetentionSweepAction,
)

# A small positive integer for retention counts/windows (1..99999). Its own argv
# element; never reaches a shell.
_POSITIVE_INT = r"[1-9][0-9]{0,4}"

register(
    CommandTemplate(
        action_name="backup.retention_sweep",
        argv=("true",),
        cwd=None,
        params=(
            ParamSpec("site", regex=SITE_NAME),
            ParamSpec("bench_path", regex=ABS_PATH, is_path=True),
            ParamSpec("keep_last", regex=_POSITIVE_INT, required=False),
            ParamSpec("keep_days", regex=_POSITIVE_INT, required=False),
        ),
        action_class=_RetentionSweepAction,
        idempotent=False,
        requires_lock=True,
        required_permission=BACKUP_CREATE,
        run_as=None,
    )
)


# --------------------------------------------------------------------------- #
# Domains & SSL (session 2.4)
#
# nginx.render_vhost / ssl.certbot_* touch nginx and issue certs, so they take a
# per-server lock (requires_lock=True, target_type="server" at the API) — two
# nginx-mutating jobs must never race on the same server (golden rule 4), which
# together with the remote flock is the "write under filelock" requirement.
# They need `ssl:manage`. domain.dns_check / ssl.expiry_scan are read-only and
# idempotent (auto-retry a transient SSH blip). certbot + `systemctl reload
# nginx` require the two Session-2.4 sudoers allowlist lines added to the
# ratified allowlist in docs/implementation-plan.md (installed at
# /etc/sudoers.d/fdm-platform); `sudo -n` fails loudly if they are absent.
# --------------------------------------------------------------------------- #

register(
    CommandTemplate(
        action_name="domain.dns_check",
        argv=("true",),  # nominal; DnsCheckAction runs getent + the IP probe.
        cwd=None,
        params=(
            ParamSpec("domain", regex=DOMAIN_NAME),
            ParamSpec("domain_id", regex=DOMAIN_ID),
            ParamSpec("site", regex=SITE_NAME),
            ParamSpec("bench_path", regex=ABS_PATH, is_path=True),
        ),
        action_class=DnsCheckAction,
        idempotent=True,
        requires_lock=False,
        required_permission=SSL_MANAGE,
        run_as=None,
    )
)

register(
    CommandTemplate(
        action_name="nginx.render_vhost",
        argv=("true",),  # nominal; RenderVhostAction writes + validates + reloads.
        cwd=None,
        params=(
            ParamSpec("domain", regex=DOMAIN_NAME),
            ParamSpec("domain_id", regex=DOMAIN_ID),
            ParamSpec("site", regex=SITE_NAME),
            ParamSpec("bench_path", regex=ABS_PATH, is_path=True),
            ParamSpec("ssl", enum=SSL_STATES),
        ),
        action_class=RenderVhostAction,
        idempotent=False,
        requires_lock=True,
        required_permission=SSL_MANAGE,
        run_as=None,
        # Regenerates the bench's platform-managed nginx vhost dir — capture it
        # as the new baseline so a managed vhost change is not flagged as drift.
        writes_config=("nginx_vhosts",),
    )
)

register(
    CommandTemplate(
        action_name="ssl.certbot_issue",
        argv=("true",),  # nominal; CertbotIssueAction drives certbot + vhost.
        cwd=None,
        params=(
            ParamSpec("domain", regex=DOMAIN_NAME),
            ParamSpec("domain_id", regex=DOMAIN_ID),
            ParamSpec("site", regex=SITE_NAME),
            ParamSpec("bench_path", regex=ABS_PATH, is_path=True),
            ParamSpec("email", regex=EMAIL),
        ),
        action_class=CertbotIssueAction,
        idempotent=False,
        requires_lock=True,
        required_permission=SSL_MANAGE,
        run_as=None,
    )
)

register(
    CommandTemplate(
        action_name="ssl.certbot_renew",
        argv=("true",),  # nominal; CertbotRenewAction renews the site's certs.
        cwd=None,
        params=(
            ParamSpec("site", regex=SITE_NAME),
            ParamSpec("bench_path", regex=ABS_PATH, is_path=True),
        ),
        action_class=CertbotRenewAction,
        idempotent=True,
        requires_lock=True,
        required_permission=SSL_MANAGE,
        run_as=None,
    )
)

register(
    CommandTemplate(
        action_name="ssl.expiry_scan",
        argv=("true",),  # nominal; SslExpiryScanAction reads certbot certificates.
        cwd=None,
        params=(
            ParamSpec("site", regex=SITE_NAME),
            ParamSpec("bench_path", regex=ABS_PATH, is_path=True),
        ),
        action_class=SslExpiryScanAction,
        idempotent=True,
        requires_lock=False,
        required_permission=SSL_MANAGE,
        run_as=None,
    )
)


# Drift detection (session 6.7): re-hash every tracked config artefact on a
# server and diff against baseline. Read-only (cat/find only), so idempotent and
# safe to auto-retry. Per-server lock so two checks never race. Server-scoped:
# no params, target_type "server". Requires server:manage to launch (it may
# establish first-sight baselines); schedulable via the 2.1 Schedule model.
register(
    CommandTemplate(
        action_name="server.drift_check",
        argv=("true",),  # nominal; DriftCheckAction reads + hashes the artefacts.
        cwd=None,
        params=(),
        action_class=DriftCheckAction,
        idempotent=True,
        requires_lock=True,
        required_permission=SERVER_MANAGE,
        run_as=None,
    )
)

# --------------------------------------------------------------------------- #
# restic config-tier DR backups (session 4.1)
#
# Each managed server has one restic repository (its OS/config tier) inside an
# existing 2.2 StorageTarget bucket. The repo password + S3 keys are secrets that
# reach restic ONLY via its process environment (a 0600 env file the action
# stages + sources), never on an argv element and never logged (golden rule 6) —
# so these templates declare NO secret params: their argv is entirely non-secret
# (the repo URI, the fixed config tag, the snapshot host). `restic.install`
# needs no repo; init/backup/snapshots take a per-server lock so two restic ops
# on the same server can't race the repo. All gated on server:manage
# (Admin/Developer manage; Operator/Read-only cannot — golden rule 7).
# Imported locally to keep concurrent-session edits to this file collision-free.
# --------------------------------------------------------------------------- #
from app.core.commands.actions import (  # noqa: E402
    ResticBackupAction as _ResticBackupAction,
)
from app.core.commands.actions import (  # noqa: E402
    ResticInitAction as _ResticInitAction,
)
from app.core.commands.actions import (  # noqa: E402
    ResticInstallAction as _ResticInstallAction,
)
from app.core.commands.actions import (  # noqa: E402
    ResticSnapshotsAction as _ResticSnapshotsAction,
)
from app.core.restic import CONFIG_TAG as _CONFIG_TAG  # noqa: E402

# A restic S3 repository URI: `s3:<endpoint-or-host>/<bucket>[/<prefix>]`. Built
# server-side from the 2.2 StorageTarget (never fresh user input on this path),
# but validated as its own argv element anyway: a shell-safe whitelist (no
# spaces, no ; ` $ ( ) & | < > \ or quotes) so it can never break out of its
# argv slot even inside the env-file wrapper.
RESTIC_REPO_URI = r"s3:[A-Za-z0-9._:/-]{1,300}"

# The --host label restic stamps on a snapshot: the managed server's hostname (or
# name). Shell-safe hostname whitelist; its own argv element.
RESTIC_HOST = r"[A-Za-z0-9][A-Za-z0-9._-]{0,120}"

# `restic.install` — detect restic; install the pinned release to ~/.local/bin
# when absent (no root). Read-only-ish (never touches the repo/secrets); no lock.
register(
    CommandTemplate(
        action_name="restic.install",
        argv=("true",),  # nominal; ResticInstallAction drives detect + install.
        cwd=None,
        params=(),
        action_class=_ResticInstallAction,
        idempotent=True,  # detect-or-install is safely repeatable.
        requires_lock=False,
        required_permission=SERVER_MANAGE,
        run_as=None,
    )
)

# `restic init` — create the repository in the bucket. The action wraps this
# rendered argv in the env-file sourcing wrapper so RESTIC_PASSWORD/AWS_* reach
# restic via env, never argv. Non-idempotent at the template level; the action
# treats "already initialized" as success. Per-server lock.
register(
    CommandTemplate(
        action_name="restic.init",
        argv=("restic", "-r", "{repo}", "init"),
        cwd=None,
        params=(ParamSpec("repo", regex=RESTIC_REPO_URI),),
        action_class=_ResticInitAction,
        idempotent=False,
        requires_lock=True,
        required_permission=SERVER_MANAGE,
        run_as=None,
    )
)

# Session 6.2: the platform-local `report.generate` template lives in its own
# module because it depends on the reports package (nothing else in the registry
# does) and because a local, non-SSH template deserves to be visibly separate
# from the remote-command catalogue above. Imported last, for the side effect of
# registering itself — `register` is already defined by this point.
from app.core.commands import report_actions as _report_actions  # noqa: E402,F401
# `restic backup` — snapshot the OS/config tier. The action appends the (constant)
# config source paths + the staged dpkg manifest to this rendered prefix and wraps
# it in the env-file wrapper. One snapshot per run, so non-idempotent. Per-server
# lock so a backup and an init/snapshots can't race the repo.
register(
    CommandTemplate(
        action_name="restic.backup",
        argv=("restic", "-r", "{repo}", "backup", "--tag", _CONFIG_TAG, "--host", "{host}"),
        cwd=None,
        params=(
            ParamSpec("repo", regex=RESTIC_REPO_URI),
            ParamSpec("host", regex=RESTIC_HOST),
        ),
        action_class=_ResticBackupAction,
        idempotent=False,
        requires_lock=True,
        required_permission=SERVER_MANAGE,
        run_as=None,
    )
)

# `restic snapshots` — read-only evidence listing of the config-tier snapshots.
# Idempotent (safe to retry). Per-server lock kept off so it never blocks/510s a
# concurrent read; the action only reads.
register(
    CommandTemplate(
        action_name="restic.snapshots",
        argv=("restic", "-r", "{repo}", "snapshots", "--tag", _CONFIG_TAG),
        cwd=None,
        params=(ParamSpec("repo", regex=RESTIC_REPO_URI),),
        action_class=_ResticSnapshotsAction,
        idempotent=True,
        requires_lock=False,
        required_permission=SERVER_MANAGE,
        run_as=None,
    )
)
