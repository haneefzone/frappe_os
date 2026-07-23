"""Action-class permission catalogue and the default role matrix.

Every RBAC check uses `require("<action-class>")`; Role.permissions holds a
list of these strings ("*" = everything). Future routers must pick from this
catalogue so the role editor (session 1.12) can render a stable matrix.
"""

ALL = "*"

READ = "read"  # view servers/benches/sites/jobs/backups/monitoring
BENCH_OPERATE = "bench:operate"  # create/update/delete-safe bench operations
SITE_OPERATE = "site:operate"  # create site, migrate, cache ops, install apps
APP_MANAGE = "app:manage"  # get-app, app upgrades
BACKUP_CREATE = "backup:create"
BACKUP_RESTORE = "backup:restore"
JOB_MANAGE = "job:manage"  # retry/cancel jobs
TERMINAL_ACCESS = "terminal:access"
SERVER_MANAGE = "server:manage"  # register servers, SSH credentials
DANGER = "danger"  # drop site, delete bench, restore-over-existing
USER_MANAGE = "user:manage"
SETTINGS_MANAGE = "settings:manage"
SCHEDULE_MANAGE = "schedule:manage"  # create/edit/enable/disable recurring schedules
SSL_MANAGE = "ssl:manage"  # manage site domains, nginx vhosts, TLS certificates
REPORT_GENERATE = "report:generate"  # generate compliance / audit report exports (session 4.4)

# name -> permissions. Admin gets the wildcard; Read-only can never mutate
# (CLAUDE.md golden rule 7).
DEFAULT_ROLES: dict[str, list[str]] = {
    "Admin": [ALL],
    "Developer": [
        READ,
        SERVER_MANAGE,
        BENCH_OPERATE,
        SITE_OPERATE,
        APP_MANAGE,
        BACKUP_CREATE,
        BACKUP_RESTORE,
        JOB_MANAGE,
        TERMINAL_ACCESS,
        SCHEDULE_MANAGE,
        SSL_MANAGE,
        REPORT_GENERATE,
    ],
    "Operator": [
        READ,
        SITE_OPERATE,
        BACKUP_CREATE,
        JOB_MANAGE,
        SSL_MANAGE,
    ],
    "Read-only": [READ],
}


def role_allows(permissions: list[str], permission: str) -> bool:
    return ALL in permissions or permission in permissions
