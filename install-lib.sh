# shellcheck shell=bash
# FDM Platform — reusable installer helpers (DOO-1155).
#
# Sourced by install.sh; also sourced directly by tests/install/test_reentry.sh
# so the install-time DB-provisioning logic can be exercised in CI without root,
# apt, or systemd. Keep this file free of side effects: define functions only.
#
# WHY THIS EXISTS (DOO-1155 AC4): install.sh used to derive FRESH_INSTALL from
# the mere existence of backend/.env and gate database provisioning on it. A
# first install that wrote .env and then died at/after `alembic upgrade head`
# left .env behind, so every rerun took the "upgrade" path and skipped
# createdb — making `dropdb fdm && ./install.sh` fail forever at migrate. The
# fix is to drive provisioning off ACTUAL database state (via provision_db),
# never off a proxy file. See verify-the-remedy-not-just-the-diagnosis.

# env_value KEY FILE — print the value of KEY=... from an env file (first hit,
# everything after the first '='). Empty if the file or key is absent. Mirrors
# the REDIS_URL_EFFECTIVE read that install.sh already does inline.
env_value() {
    local key="$1" file="$2"
    [ -f "$file" ] || return 0
    (grep -E "^${key}=" "$file" || true) | head -1 | cut -d= -f2-
}

# pg_url_field URL FIELD — parse a SQLAlchemy/libpq URL
#   scheme://user:password@host:port/dbname[?params]
# and print one of: user | password | host | port | dbname.
# The scheme may carry a driver suffix (postgresql+psycopg://…); it is ignored.
# FDM's generated password is secrets.token_urlsafe(24) — [A-Za-z0-9_-], no URL
# metacharacters — so no percent-decoding is required.
pg_url_field() {
    local url="$1" field="$2"
    local rest="${url#*://}"          # strip scheme (incl. +driver)
    local userinfo="${rest%%@*}"      # user[:password]
    local hostportdb="${rest#*@}"     # host[:port]/dbname[?params]
    hostportdb="${hostportdb%%\?*}"   # drop query string
    local hostport="${hostportdb%%/*}"
    local dbname="${hostportdb#*/}"
    local user="${userinfo%%:*}"
    local password="" host="" port=""
    case "$userinfo" in *:*) password="${userinfo#*:}";; esac
    host="${hostport%%:*}"
    case "$hostport" in *:*) port="${hostport#*:}";; esac
    # No '/' in hostportdb means there was no dbname.
    case "$hostportdb" in */*) : ;; *) dbname="" ;; esac
    case "$field" in
        user)     printf '%s' "$user" ;;
        password) printf '%s' "$password" ;;
        host)     printf '%s' "$host" ;;
        port)     printf '%s' "$port" ;;
        dbname)   printf '%s' "$dbname" ;;
        *) return 2 ;;
    esac
}

# is_local_host HOST — true when HOST is a loopback address (or empty, i.e. a
# unix-socket / default-local URL). Used to decide whether a DB is one the
# installer may provision, vs a remote DB it must never touch.
is_local_host() {
    case "$1" in
        127.0.0.1|localhost|::1|"") return 0 ;;
        *) return 1 ;;
    esac
}

# provision_db ADMIN DB USER PASS — ensure a PostgreSQL role and database exist,
# idempotently, driven purely by current DB state.
#   ADMIN : a psql command PREFIX run as a DB superuser, e.g.
#             "runuser -u postgres -- psql"  (root install)
#             "psql -h 127.0.0.1 -U fdm -d postgres"  (CI/test)
#           '-tAc <sql>' is appended per call, so ADMIN must NOT already carry -c.
#   DB/USER : identifiers (double-quoted here; simple lowercase in practice).
#   PASS    : role login password (single-quoted SQL literal).
# Prints "created" if it created the database this call, else "exists".
# Any psql failure returns non-zero so the caller's `set -e` aborts loudly.
provision_db() {
    local admin="$1" db="$2" user="$3" pass="$4"
    # ensure role (reset password to the effective value so a drifted/leftover
    # role from a prior aborted attempt still matches .env)
    if [ "$($admin -tAc "SELECT 1 FROM pg_roles WHERE rolname='$user'")" = "1" ]; then
        $admin -tAc "ALTER ROLE \"$user\" LOGIN PASSWORD '$pass'" >/dev/null
    else
        $admin -tAc "CREATE ROLE \"$user\" LOGIN PASSWORD '$pass'" >/dev/null
    fi
    # ensure database
    if [ "$($admin -tAc "SELECT 1 FROM pg_database WHERE datname='$db'")" = "1" ]; then
        printf 'exists'
    else
        $admin -tAc "CREATE DATABASE \"$db\" OWNER \"$user\"" >/dev/null
        printf 'created'
    fi
}

# recovery_cmd DBNAME [HOST] [PORT] [USER] [BRANCH] — print the single, canonical
# recovery command for a managed install wedged by database drift. Centralised
# (DOO-1169) so the migrate failure handler and the tests all quote the same
# string instead of drifting out of sync. DBNAME defaults to fdm; HOST/PORT/USER
# default to the installer's managed loopback PostgreSQL (127.0.0.1/5432/fdm);
# BRANCH defaults to $FDM_BRANCH or main.
#
# DOO-1175: the drop MUST be derived from the effective DATABASE_URL — host,
# port AND user named explicitly — rather than assume `sudo -u postgres dropdb`.
# `sudo -u postgres dropdb` routes through Debian's pg_wrapper, which resolves to
# whatever cluster /etc/postgresql-common/user_clusters (or the single Debian
# cluster) points at — NOT necessarily the server the app is bound to. On a host
# where a non-Debian PostgreSQL (e.g. an embedded dev instance with loopback
# trust auth and no `postgres` role) listens on the target port, `sudo -u
# postgres dropdb fdm` silently targets a different cluster and the drop is a
# no-op — the exact loop DOO-1162 was stuck in. `dropdb -h HOST -p PORT -U USER`
# pins the drop to the precise server the installer actually uses. For the same
# reason we NEVER emit --if-exists: on the wrong server a miss is
# indistinguishable from success, which is how a no-op passed as a fix.
#
# DOO-1174: the reinstall half MUST fetch a fresh installer from the canonical
# source rather than re-run the local checkout. The population that hits drift is,
# by construction, disproportionately on an OLD checkout (drift detection only
# fires when re-running after a prior aborted attempt), and `sudo ./install.sh`
# run from inside /opt/fdm-platform takes install.sh's "skipping code sync"
# branch — it re-executes whatever code is already on disk. On a pre-fix
# checkout that is the old installer with no drift detection, so
# `dropdb && ./install.sh` dies at migrate again and loops forever. The curl
# form bypasses the on-disk tree entirely, so it recovers regardless of how
# stale the checkout is. .env secrets survive because dropdb touches only the DB.
recovery_cmd() {
    local db="${1:-fdm}"
    local host="${2:-127.0.0.1}"
    local port="${3:-5432}"
    local user="${4:-fdm}"
    local branch="${5:-${FDM_BRANCH:-main}}"
    printf 'dropdb -h %s -p %s -U %s %s && curl -fsSL https://raw.githubusercontent.com/haneefzone/frappe_os/%s/install.sh | sudo bash' \
        "$host" "$port" "$user" "$db" "$branch"
}

# checkout_is_behind DIR BRANCH — for an in-tree install run (install.sh invoked
# from inside its own checkout, which takes the "skipping code sync" branch),
# report whether DIR's HEAD is behind or diverged from origin/BRANCH. This is
# the root enabler of the DOO-1174 loop: an in-tree run silently executes
# whatever installer code is on disk, so a stale checkout runs stale code (and
# prints stale, wrong recovery advice) with no signal to the operator. A cheap
# shallow fetch + compare lets install.sh warn instead of installing older code
# silently.
#
# Prints the number of commits HEAD is behind origin/BRANCH when behind/diverged
# (always >= 1 so a stale checkout is never reported as current), and prints
# NOTHING when up to date, not a git checkout, git is unavailable, or origin is
# unreachable — an offline or detached install must never be blocked by this
# advisory check. Always returns 0 so callers under `set -e` are safe.
checkout_is_behind() {
    local dir="$1" branch="${2:-main}"
    command -v git >/dev/null 2>&1 || return 0
    [ -d "$dir/.git" ] || return 0
    git -C "$dir" fetch --quiet --depth 1 origin "$branch" >/dev/null 2>&1 || return 0
    local head origin n
    head="$(git -C "$dir" rev-parse HEAD 2>/dev/null || echo '')"
    origin="$(git -C "$dir" rev-parse "origin/$branch" 2>/dev/null || echo '')"
    [ -n "$origin" ] || return 0
    [ "$head" = "$origin" ] && return 0
    # HEAD differs from origin. rev-list gives the exact behind count; a diverged
    # or shallow tree with no merge base can report 0 despite the SHA mismatch,
    # so floor the answer at 1 — a differing HEAD is never "current".
    n="$(git -C "$dir" rev-list --count "HEAD..origin/$branch" 2>/dev/null || echo '')"
    { [ -n "$n" ] && [ "$n" -gt 0 ] 2>/dev/null; } || n=1
    printf '%s' "$n"
}

# is_schema_drift_error TEXT — true when captured `alembic upgrade head` output
# carries the signature of a database whose schema is AHEAD of alembic_version:
# a pending migration tries to create an object that already exists. This is the
# DOO-1169 case — a leftover DB from an earlier aborted attempt whose
# alembic_version was never stamped forward. provision_db cannot fix it (it only
# creates a MISSING database), so a plain rerun replays the same migration into
# the identical DuplicateColumn/DuplicateTable failure and loops forever. The
# honest recovery is dropdb + reinstall, NOT another rerun. Signatures cover
# psycopg's Duplicate* error classes and Postgres' "... already exists" text.
is_schema_drift_error() {
    printf '%s' "$1" | grep -qiE \
        'DuplicateColumn|DuplicateTable|DuplicateObject|psycopg2?\.errors\.Duplicate|already exists'
}
