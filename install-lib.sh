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

# recovery_bind_elsewhere NEWDB [HOST] [PORT] [USER] [BRANCH] — print the
# recovery command for a database the installer did NOT provision: an external
# FDM_DATABASE_URL target, or a server the D1 adoption guard refused. HOST/PORT/
# USER identify the PostgreSQL the operator controls; NEWDB is a FRESH, empty
# database name to bind FDM at (defaulting to a placeholder the operator fills).
#
# DOO-1176 (MD decision folding into DOO-1175): the installer must NEVER advise
# dropping a database it did not create. `dropdb` is retired for this whole
# class of failure. Aimed at a foreign server squatting the port it destroys
# someone else's data and STILL loops (the next run re-adopts that server);
# aimed at a cluster it cannot reach it is a silent no-op that reads as success —
# the exact trap the last five corrections fell into. The honest recovery is to
# bind FDM at a fresh database the operator provisions on a server they own —
# `createdb` a clean DB, then point FDM_DATABASE_URL at it, host/port/user named.
# No dropdb, and (like recovery_cmd) never --if-exists: on the wrong server a
# miss must not read as success.
recovery_bind_elsewhere() {
    local newdb="${1:-<new-empty-db>}"
    local host="${2:-127.0.0.1}"
    local port="${3:-5432}"
    local user="${4:-fdm}"
    local branch="${5:-${FDM_BRANCH:-main}}"
    printf "sudo -u postgres createdb -p %s -O %s %s && sudo FDM_DATABASE_URL='postgresql+psycopg://%s:<password>@%s:%s/%s' bash -c 'curl -fsSL https://raw.githubusercontent.com/haneefzone/frappe_os/%s/install.sh | bash'" \
        "$port" "$user" "$newdb" "$user" "$host" "$port" "$newdb" "$branch"
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

# ---------------------------------------------------------------------------
# DOO-1205 — service-management helpers shared by install.sh and the `fdm` CLI.
#
# These live here (not copy-pasted into bin/fdm) so the installer and the CLI
# drive the SAME nohup lifecycle: pidfiles at $FDM_HOME/run/{api,worker}.pid,
# logs at $FDM_HOME/logs/{api,worker}.log, and the exact graceful-then-SIGKILL
# stop the installer's start_bg has always used. A `fdm stop` that killed
# differently from how install.sh replaces a stale process would be a subtle
# split-brain; funnelling both through one function prevents that. Kept pure
# (functions only, side effects only when called) so tests/install can source
# and exercise them without root, apt, or systemd.

# fdm_default_home IS_ROOT — echo the default FDM_HOME for a root(1)/non-root(0)
# install, mirroring install.sh's rule (/opt/fdm-platform as root, else
# ~/.local/share/fdm-platform). An explicit FDM_HOME env override is applied by
# the caller BEFORE falling back to this; this is only the default.
fdm_default_home() {
    if [ "${1:-0}" -eq 1 ]; then
        printf '/opt/fdm-platform'
    else
        printf '%s/.local/share/fdm-platform' "$HOME"
    fi
}

# fdm_pid_running PIDFILE — if PIDFILE names a live process, print its PID and
# return 0; otherwise print nothing and return 1. A stale pidfile (process gone)
# is treated as not-running so callers can replace it.
fdm_pid_running() {
    local pidfile="$1" pid
    [ -f "$pidfile" ] || return 1
    pid="$(cat "$pidfile" 2>/dev/null || true)"
    [ -n "$pid" ] || return 1
    if kill -0 "$pid" 2>/dev/null; then
        printf '%s' "$pid"
        return 0
    fi
    return 1
}

# fdm_stop_pid PID [TIMEOUT] — stop PID gracefully: SIGTERM, wait up to TIMEOUT
# seconds (default 15) for it to exit, then SIGKILL. This is exactly the replace
# logic install.sh:start_bg has always used for a stale pidfile; `fdm stop`
# reuses it so an operator stop and an installer restart behave identically. The
# worker's units carry TimeoutStopSec=30 because bench init/update jobs are long
# — callers stopping the worker pass 30 to match. Best-effort: returns 0 even if
# PID is empty or already gone.
fdm_stop_pid() {
    local pid="$1" timeout="${2:-15}"
    [ -n "$pid" ] || return 0
    kill -0 "$pid" 2>/dev/null || return 0
    kill "$pid" 2>/dev/null || true
    local _i
    for _i in $(seq 1 "$timeout"); do
        kill -0 "$pid" 2>/dev/null || break
        sleep 1
    done
    kill -9 "$pid" 2>/dev/null || true
    return 0
}

# fdm_start_bg IS_ROOT RUN_USER BACKEND_DIR FDM_HOME NAME -- CMD... — nohup a
# service from BACKEND_DIR, tracking its pid at FDM_HOME/run/NAME.pid and logging
# to FDM_HOME/logs/NAME.log. Graceful-replaces any existing instance first (via
# fdm_stop_pid), so it is idempotent. Root runs drop to RUN_USER via runuser with
# a minimal env, mirroring install.sh's as_fdm. Returns 0 on success, 1 if the
# process dies within 2s of starting (caller reports the log path).
fdm_start_bg() {
    local is_root="$1" run_user="$2" backend_dir="$3" home="$4" name="$5"; shift 5
    [ "${1:-}" = "--" ] && shift
    local pidfile="$home/run/$name.pid"
    if [ -f "$pidfile" ]; then
        fdm_stop_pid "$(cat "$pidfile" 2>/dev/null || true)" 15
    fi
    mkdir -p "$home/run" "$home/logs"
    if [ "$is_root" -eq 1 ]; then
        nohup runuser -u "$run_user" -- env -C "$backend_dir" \
            HOME="$home" PATH="/usr/local/bin:/usr/bin:/bin" "$@" \
            >"$home/logs/$name.log" 2>&1 &
    else
        nohup env -C "$backend_dir" "$@" >"$home/logs/$name.log" 2>&1 &
    fi
    echo $! > "$pidfile"
    sleep 2
    kill -0 "$(cat "$pidfile")" 2>/dev/null || return 1
    return 0
}

# fdm_port_from_unit FILE — parse the uvicorn `--port N` out of a systemd unit's
# ExecStart line; print nothing when FILE is absent or has no --port. Lets the
# CLI recover the port a systemd install actually serves on without the operator
# re-supplying FDM_PORT.
fdm_port_from_unit() {
    local file="$1"
    [ -f "$file" ] || return 0
    sed -n 's/.*--port \([0-9][0-9]*\).*/\1/p' "$file" | head -1
}

# fdm_update_ff_state DIR BRANCH — classify whether DIR (a git checkout of the
# FDM code) can be safely fast-forwarded to origin/BRANCH, for `fdm update`.
# Prints exactly one token; NEVER fetches (the caller fetches first so this stays
# a pure, testable inspection):
#   no-git      git is unavailable
#   not-a-repo  DIR has no .git (installed via rsync/curl-clone-then-artifacts)
#   no-origin   origin/BRANCH is unknown (never fetched / wrong branch)
#   dirty       TRACKED files are modified — refuse rather than clobber edits.
#               Untracked build artifacts (.venv, dist, .env, logs) are IGNORED:
#               an in-tree install is untracked-dirty by construction, so keying
#               on them would make update refuse forever.
#   diverged    HEAD is not an ancestor of origin/BRANCH — a non-fast-forward;
#               refuse rather than force local commits away.
#   up-to-date  HEAD already equals origin/BRANCH
#   clean-ff    a clean fast-forward is available
fdm_update_ff_state() {
    local dir="$1" branch="${2:-main}"
    command -v git >/dev/null 2>&1 || { printf 'no-git'; return 0; }
    [ -d "$dir/.git" ] || { printf 'not-a-repo'; return 0; }
    if [ -n "$(git -C "$dir" status --porcelain --untracked-files=no 2>/dev/null)" ]; then
        printf 'dirty'; return 0
    fi
    local head origin
    head="$(git -C "$dir" rev-parse HEAD 2>/dev/null || echo '')"
    origin="$(git -C "$dir" rev-parse "origin/$branch" 2>/dev/null || echo '')"
    [ -n "$origin" ] || { printf 'no-origin'; return 0; }
    [ "$head" = "$origin" ] && { printf 'up-to-date'; return 0; }
    if git -C "$dir" merge-base --is-ancestor HEAD "origin/$branch" 2>/dev/null; then
        printf 'clean-ff'
    else
        printf 'diverged'
    fi
}
