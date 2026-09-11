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
