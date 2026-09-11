#!/usr/bin/env bash
# FDM Platform — one-command installer.
#
#   curl -fsSL https://raw.githubusercontent.com/haneefzone/frappe_os/main/install.sh | sudo bash
#
# or from a checkout:
#
#   sudo bash install.sh
#
# Targets Ubuntu 22.04/24.04. Installs prerequisites (uv + Python 3.12+,
# Node 20 for the frontend build, PostgreSQL 16, Redis 7), installs the
# backend, builds the frontend, generates a .env with per-install secrets,
# starts fdm-api + fdm-worker under systemd, and prints the URL plus
# first-login credentials.
#
# Idempotent: re-running upgrades code and dependencies, re-runs migrations,
# and restarts services. It never regenerates secrets and never resets the
# admin password on an existing install. It (re-)creates the managed local
# database only when it is actually missing, so recovering a failed first
# install with `dropdb fdm && ./install.sh` works (DOO-1155).
#
# LIMIT (DOO-1169): re-running self-heals a MISSING managed database, but it
# does NOT reconcile one whose schema is AHEAD of its recorded alembic version
# (a leftover from an earlier aborted attempt whose alembic_version was never
# stamped forward). That surfaces as a DuplicateColumn/"already exists"
# migration failure; the installer detects it and prints the drop-and-reinstall
# recovery command rather than looping into the same error.
#
# Tunables (env vars, all optional):
#   FDM_HOME           install dir      (root: /opt/fdm-platform, else ~/.local/share/fdm-platform)
#   FDM_PORT           HTTP port        (default 8000)
#   FDM_REPO_URL       git source       (default https://github.com/haneefzone/frappe_os.git)
#   FDM_BRANCH         git branch       (default main)
#   FDM_ADMIN_EMAIL    first admin      (default admin@example.com)
#   FDM_DATABASE_URL   use an existing PostgreSQL instead of provisioning one
#   FDM_REDIS_URL      use an existing Redis (default redis://127.0.0.1:6379/0)
#   FDM_TRUSTED_PROXY_IPS  reverse-proxy IPs whose X-Forwarded-For the app
#                      trusts (default empty = none; set 127.0.0.1 when using
#                      deploy/nginx.conf — see deploy/README.md, SEC-M1)
#
# Non-root runs are supported for development/CI: apt/systemd steps are
# skipped (prerequisites must already exist), services start via nohup.
set -euo pipefail

# ----------------------------------------------------------------- plumbing
log()  { echo -e "\033[1;36m[fdm-install]\033[0m $*"; }
warn() { echo -e "\033[1;33m[fdm-install] WARN:\033[0m $*" >&2; }
die()  { echo -e "\033[1;31m[fdm-install] ERROR:\033[0m $*" >&2; exit 1; }
# ERR handler (DOO-1155/DOO-1169). Kept in a function so the captured migrate
# step below can suppress it (trap - ERR) around a failure it reports itself,
# then restore it, without duplicating this message. The message is honest for
# every state it can fire in: re-running self-heals a MISSING managed database
# but does NOT reconcile one whose schema is AHEAD of alembic_version, so it
# names that case and the drop-and-reinstall recovery instead of promising "no
# manual database surgery is required" (which was false at 5d24e4c).
on_install_error() {
    local rc=$? line="${1:-?}"
    # DOO-1175: derive the drop from the effective DATABASE_URL (host/port/user)
    # once it has been parsed, rather than hardcode a bare `sudo -u postgres
    # dropdb fdm` — that routes through pg_wrapper and can silently miss the
    # server the app is bound to. Before the URL is parsed, print the shape with
    # placeholders instead of a command that might target the wrong server.
    local recovery
    if declare -F recovery_cmd >/dev/null 2>&1 && [ -n "${DB_NAME:-}" ]; then
        recovery="$(recovery_cmd "$DB_NAME" "${DB_HOST:-127.0.0.1}" "${DB_PORT:-5432}" "${DB_USER:-fdm}")"
    else
        recovery="dropdb -h <host> -p <port> -U <user> <db> && curl -fsSL https://raw.githubusercontent.com/haneefzone/frappe_os/${FDM_BRANCH:-main}/install.sh | sudo bash   (host/port/user come from DATABASE_URL in backend/.env)"
    fi
    echo -e "\033[1;31m[fdm-install] FAILED\033[0m (line $line, exit $rc). Fix the cause shown above, then re-run this installer: a partial first install is resumed from real database state — a MISSING managed database is re-created, and existing secrets and already-migrated data are preserved. One case a plain rerun does NOT fix: a managed database whose schema is AHEAD of its recorded alembic version (a leftover from an earlier aborted attempt), which fails migration with a DuplicateColumn / \"already exists\" error. Recover that by dropping the managed database — addressing it explicitly by host/port/user, NOT via 'sudo -u postgres dropdb' which routes through pg_wrapper and can miss the server FDM uses (DOO-1175) — and reinstalling from the canonical source (the curl form always fetches a fresh installer; an in-tree 'sudo ./install.sh' would re-run a possibly-stale on-disk installer): ${recovery} (back it up with pg_dump first if it holds data you need)." >&2
}
trap 'on_install_error "$LINENO"' ERR

IS_ROOT=0; [ "$(id -u)" -eq 0 ] && IS_ROOT=1
HAVE_SYSTEMD=0; [ "$IS_ROOT" -eq 1 ] && [ -d /run/systemd/system ] && HAVE_SYSTEMD=1

if [ "$IS_ROOT" -eq 1 ]; then
    RUN_USER="fdm"
    FDM_HOME="${FDM_HOME:-/opt/fdm-platform}"
else
    RUN_USER="$(id -un)"
    FDM_HOME="${FDM_HOME:-$HOME/.local/share/fdm-platform}"
    warn "Running without root: prerequisite installation and systemd are skipped."
fi

FDM_PORT="${FDM_PORT:-8000}"
FDM_REPO_URL="${FDM_REPO_URL:-https://github.com/haneefzone/frappe_os.git}"
FDM_BRANCH="${FDM_BRANCH:-main}"
FDM_ADMIN_EMAIL="${FDM_ADMIN_EMAIL:-admin@example.com}"
FDM_REDIS_URL="${FDM_REDIS_URL:-redis://127.0.0.1:6379/0}"
FDM_TRUSTED_PROXY_IPS="${FDM_TRUSTED_PROXY_IPS:-}"

BACKEND_DIR="$FDM_HOME/backend"
FRONTEND_DIR="$FDM_HOME/frontend"
ENV_FILE="$BACKEND_DIR/.env"

# Run a command as the service user, from a given directory, with a minimal
# predictable environment (root installs use runuser; non-root runs direct).
as_fdm() {
    local dir="$1"; shift
    if [ "$IS_ROOT" -eq 1 ]; then
        runuser -u "$RUN_USER" -- env -C "$dir" \
            HOME="$FDM_HOME" PATH="/usr/local/bin:/usr/bin:/bin" "$@"
    else
        env -C "$dir" "$@"
    fi
}

# ------------------------------------------------------------ OS detection
if [ -r /etc/os-release ]; then
    . /etc/os-release
    OS_ID="${ID:-unknown}" OS_CODENAME="${VERSION_CODENAME:-unknown}"
else
    OS_ID=unknown OS_CODENAME=unknown
fi
case "$OS_ID:$OS_CODENAME" in
    ubuntu:jammy|ubuntu:noble) : ;;
    *) warn "Untested OS ($OS_ID $OS_CODENAME). This installer targets Ubuntu 22.04/24.04; continuing anyway." ;;
esac

# ------------------------------------------------------- apt prerequisites
apt_install() {
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "$@" >/dev/null
}

if [ "$IS_ROOT" -eq 1 ]; then
    log "Installing base packages (git, curl, rsync, gnupg)…"
    apt-get update -qq
    apt_install ca-certificates curl git rsync gnupg

    # --- PostgreSQL 16 (PGDG pins the version on both 22.04 and 24.04) ---
    if pg_isready -h 127.0.0.1 -p 5432 -q 2>/dev/null; then
        log "PostgreSQL already running on :5432 — reusing it."
    elif command -v pg_ctlcluster >/dev/null 2>&1; then
        log "PostgreSQL installed but not responding — starting it."
        systemctl enable --now postgresql 2>/dev/null || service postgresql start
    else
        log "Installing PostgreSQL 16 (PGDG)…"
        install -d -m 755 /etc/apt/keyrings
        curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
            -o /etc/apt/keyrings/pgdg.asc
        echo "deb [signed-by=/etc/apt/keyrings/pgdg.asc] https://apt.postgresql.org/pub/repos/apt ${OS_CODENAME}-pgdg main" \
            > /etc/apt/sources.list.d/pgdg.list
        apt-get update -qq
        apt_install postgresql-16
        systemctl enable --now postgresql 2>/dev/null || service postgresql start
    fi

    # --- Redis 7 (24.04 apt ships 7.x; 22.04 needs the redis.io repo) ---
    if redis-cli -u "$FDM_REDIS_URL" ping 2>/dev/null | grep -q PONG; then
        log "Redis already answering at $FDM_REDIS_URL — reusing it."
    else
        if ! command -v redis-server >/dev/null 2>&1; then
            if [ "$OS_CODENAME" = "jammy" ]; then
                log "Installing Redis 7 (packages.redis.io)…"
                install -d -m 755 /etc/apt/keyrings
                curl -fsSL https://packages.redis.io/gpg \
                    | gpg --dearmor --yes -o /etc/apt/keyrings/redis.gpg
                echo "deb [signed-by=/etc/apt/keyrings/redis.gpg] https://packages.redis.io/deb ${OS_CODENAME} main" \
                    > /etc/apt/sources.list.d/redis.list
                apt-get update -qq
            else
                log "Installing Redis…"
            fi
            apt_install redis-server
        fi
        systemctl enable --now redis-server 2>/dev/null || service redis-server start
    fi

    # --- Node 20 (build-time only, for `vite build`) ---
    NODE_MAJOR="$(node -p 'process.versions.node.split(".")[0]' 2>/dev/null || echo 0)"
    if [ "$NODE_MAJOR" -lt 20 ]; then
        log "Installing Node.js 20 (NodeSource)…"
        install -d -m 755 /etc/apt/keyrings
        curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
            | gpg --dearmor --yes -o /etc/apt/keyrings/nodesource.gpg
        echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_20.x nodistro main" \
            > /etc/apt/sources.list.d/nodesource.list
        apt-get update -qq
        apt_install nodejs
    fi

    # --- uv (manages Python 3.12+ and the backend venv) ---
    if ! command -v uv >/dev/null 2>&1; then
        log "Installing uv…"
        curl -LsSf https://astral.sh/uv/install.sh \
            | env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh >/dev/null
    fi
    UV_BIN="$(command -v uv)"

    # --- service account ---
    if ! id "$RUN_USER" >/dev/null 2>&1; then
        log "Creating system user '$RUN_USER'…"
        useradd --system --create-home --home-dir "$FDM_HOME" --shell /usr/sbin/nologin "$RUN_USER"
    fi
    install -d -o "$RUN_USER" -g "$RUN_USER" "$FDM_HOME"
else
    # Non-root: verify instead of install.
    MISSING=()
    command -v git   >/dev/null || MISSING+=(git)
    command -v rsync >/dev/null || MISSING+=(rsync)
    command -v curl  >/dev/null || MISSING+=(curl)
    NODE_MAJOR="$(node -p 'process.versions.node.split(".")[0]' 2>/dev/null || echo 0)"
    [ "$NODE_MAJOR" -ge 20 ] || MISSING+=("node>=20")
    [ "${#MISSING[@]}" -eq 0 ] || die "Missing prerequisites (non-root runs cannot apt-install): ${MISSING[*]}"
    if ! command -v uv >/dev/null 2>&1; then
        log "Installing uv (user-level)…"
        curl -LsSf https://astral.sh/uv/install.sh | env UV_NO_MODIFY_PATH=1 sh >/dev/null
    fi
    UV_BIN="$(command -v uv || echo "$HOME/.local/bin/uv")"
    if [ -z "${FDM_DATABASE_URL:-}" ] && [ ! -f "$FDM_HOME/backend/.env" ]; then
        die "Non-root installs cannot provision PostgreSQL. Set FDM_DATABASE_URL to an existing database."
    fi
    mkdir -p "$FDM_HOME"
fi

# ------------------------------------------------------------- fetch code
# When run from inside a checkout (sudo bash install.sh), sync that tree;
# when piped from curl, clone/pull FDM_REPO_URL.
SOURCE_DIR=""
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
    CANDIDATE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    [ -f "$CANDIDATE/backend/pyproject.toml" ] && SOURCE_DIR="$CANDIDATE"
fi

RSYNC_EXCLUDES=(--exclude .git --exclude backend/.venv --exclude backend/.env
    --exclude backend/.install-complete
    --exclude frontend/node_modules --exclude frontend/dist
    --exclude '/run' --exclude '/logs'
    --exclude '__pycache__' --exclude '.pytest_cache' --exclude '.ruff_cache')

if [ -n "$SOURCE_DIR" ] && [ "$SOURCE_DIR" != "$FDM_HOME" ]; then
    log "Installing from local checkout $SOURCE_DIR → $FDM_HOME"
    rsync -a --delete "${RSYNC_EXCLUDES[@]}" "$SOURCE_DIR/" "$FDM_HOME/"
elif [ -n "$SOURCE_DIR" ]; then
    log "Running from the install directory itself — skipping code sync."
    # DOO-1174: an in-tree run executes whatever installer code is on disk. If
    # this checkout is behind origin the operator is silently running stale code
    # (and would get stale recovery advice). Flag it so we can warn once the
    # helper lib is sourced below; the fetch itself is done there.
    IN_TREE_RUN=1
elif [ -d "$FDM_HOME/.git" ]; then
    log "Existing install found — pulling $FDM_BRANCH from $FDM_REPO_URL"
    git -C "$FDM_HOME" fetch --depth 1 origin "$FDM_BRANCH"
    git -C "$FDM_HOME" checkout -q "$FDM_BRANCH" 2>/dev/null || git -C "$FDM_HOME" checkout -qb "$FDM_BRANCH"
    git -C "$FDM_HOME" reset --hard "origin/$FDM_BRANCH"
else
    log "Cloning $FDM_REPO_URL ($FDM_BRANCH) → $FDM_HOME"
    git clone --depth 1 --branch "$FDM_BRANCH" "$FDM_REPO_URL" "$FDM_HOME.tmp.$$"
    # Move contents (FDM_HOME may exist as the user's empty home dir).
    rsync -a "$FDM_HOME.tmp.$$/" "$FDM_HOME/" && rm -rf "$FDM_HOME.tmp.$$"
fi
if [ "$IS_ROOT" -eq 1 ]; then chown -R "$RUN_USER:$RUN_USER" "$FDM_HOME"; fi

# Load reusable installer helpers now that the code tree is in place. Shipped in
# the repo so it is present after the fetch above regardless of curl-pipe vs
# local-checkout install (DOO-1155). shellcheck source=install-lib.sh
[ -f "$FDM_HOME/install-lib.sh" ] || die "install-lib.sh missing from $FDM_HOME — incomplete checkout/clone."
. "$FDM_HOME/install-lib.sh"

# DOO-1174: for an in-tree run, warn (do not silently proceed) when this
# checkout is behind origin. Running stale installer code is the root enabler of
# the drop-and-reinstall loop — a pre-fix checkout re-runs the old installer,
# hits the same failure, and prints recovery advice that cannot work. This is
# advisory only: checkout_is_behind stays silent (and never fails) when offline,
# detached, or already current, so it does not block air-gapped installs.
if [ "${IN_TREE_RUN:-0}" -eq 1 ]; then
    behind_count="$(checkout_is_behind "$SOURCE_DIR" "$FDM_BRANCH")"
    if [ -n "$behind_count" ]; then
        warn "This checkout ($SOURCE_DIR) is $behind_count commit(s) behind origin/$FDM_BRANCH — you are installing OLDER code than what is published, so any failure/recovery advice it prints may be stale. Update first:
    git -C \"$SOURCE_DIR\" fetch origin $FDM_BRANCH && git -C \"$SOURCE_DIR\" reset --hard origin/$FDM_BRANCH && sudo ./install.sh
or reinstall from the canonical source (immune to checkout age):
    curl -fsSL https://raw.githubusercontent.com/haneefzone/frappe_os/$FDM_BRANCH/install.sh | sudo bash"
    fi
fi

# -------------------------------------------------------- backend install
log "Installing backend (uv sync, Python 3.12+ auto-managed)…"
as_fdm "$BACKEND_DIR" "$UV_BIN" sync --frozen >/dev/null
PYBIN="$BACKEND_DIR/.venv/bin/python"

# ------------------------------------------------- secrets + configuration
FRESH_INSTALL=0
if [ -f "$ENV_FILE" ]; then
    log "Keeping existing $ENV_FILE (secrets and DB credentials unchanged)."
else
    FRESH_INSTALL=1
    log "Generating per-install secrets and $ENV_FILE…"
    JWT_SECRET="$("$PYBIN" -c 'import secrets; print(secrets.token_urlsafe(48))')"
    FERNET_KEY="$("$PYBIN" -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
    if [ -n "${FDM_DATABASE_URL:-}" ]; then
        DATABASE_URL="$FDM_DATABASE_URL"
    else
        DB_PASS="$("$PYBIN" -c 'import secrets; print(secrets.token_urlsafe(24))')"
        DATABASE_URL="postgresql+psycopg://fdm:${DB_PASS}@127.0.0.1:5432/fdm"
    fi
    umask 077
    cat > "$ENV_FILE" <<EOF
# Generated by install.sh on first install — per-install secrets, never commit.
DEBUG=false
DATABASE_URL=${DATABASE_URL}
REDIS_URL=${FDM_REDIS_URL}
FDM_SECRET_KEY=${FERNET_KEY}
JWT_SECRET=${JWT_SECRET}
FRONTEND_DIST=${FRONTEND_DIR}/dist
CORS_ORIGINS=http://localhost:${FDM_PORT}
# Plain-HTTP install: cookies cannot carry the Secure flag or logins fail.
# Once you put FDM behind an HTTPS reverse proxy, set COOKIE_SECURE=true.
COOKIE_SECURE=false
LOG_LEVEL=INFO
# Reverse proxies whose X-Forwarded-For to trust; empty = none (SEC-M1).
TRUSTED_PROXY_IPS=${FDM_TRUSTED_PROXY_IPS}
EOF
    umask 022
    if [ "$IS_ROOT" -eq 1 ]; then chown "$RUN_USER:$RUN_USER" "$ENV_FILE"; fi
fi

# The worker's Redis URL must match the app's even on upgrades where .env
# predates this run — read the effective value back from .env.
REDIS_URL_EFFECTIVE="$( (grep -E '^REDIS_URL=' "$ENV_FILE" || true) | head -1 | cut -d= -f2-)"
REDIS_URL_EFFECTIVE="${REDIS_URL_EFFECTIVE:-$FDM_REDIS_URL}"

# ------------------------------------------------------- database provision
# DOO-1155: provisioning is driven off ACTUAL database state, never off .env
# existence. A first install that wrote .env and then died at/after migrate can
# now recover with `dropdb fdm && ./install.sh` — the DB is simply re-created
# when missing. We only ever touch a loopback, installer-managed PostgreSQL as
# root; a user who points FDM_DATABASE_URL at their own (or a remote) database
# owns it entirely and we never provision it.
DB_URL_EFFECTIVE="$(env_value DATABASE_URL "$ENV_FILE")"
DB_NAME="$(pg_url_field "$DB_URL_EFFECTIVE" dbname)"
DB_USER="$(pg_url_field "$DB_URL_EFFECTIVE" user)"
DB_HOST="$(pg_url_field "$DB_URL_EFFECTIVE" host)"
DB_PORT="$(pg_url_field "$DB_URL_EFFECTIVE" port)"; DB_PORT="${DB_PORT:-5432}"
DB_PASS_EFFECTIVE="$(pg_url_field "$DB_URL_EFFECTIVE" password)"
if [ "$IS_ROOT" -eq 1 ] && [ -z "${FDM_DATABASE_URL:-}" ] \
        && is_local_host "$DB_HOST" && [ -n "$DB_NAME" ] && [ -n "$DB_USER" ]; then
    # DOO-1175: `pg_isready` on :$DB_PORT is NOT evidence the server listening
    # there is one this installer manages. Before provisioning we verify that the
    # superuser account we provision *through* (`runuser -u postgres -- psql`,
    # routed by Debian pg_wrapper) actually administers the SAME server the app's
    # DATABASE_URL points at — by comparing its live listening port to $DB_PORT.
    # If they differ, a foreign PostgreSQL squats on the target port (the failure
    # mode behind DOO-1162: an embedded dev instance on :5432 while the Debian
    # cluster the superuser owns is on :5433). Provisioning would then create the
    # role/DB on one server while the app connects to another, and any drop we
    # later advise would miss. Fail loudly with the FDM_DATABASE_URL escape hatch
    # rather than silently adopt a database we do not own.
    admin_port="$(runuser -u postgres -- psql -tAc 'SHOW port' 2>/dev/null | tr -d '[:space:]')"
    if [ -z "$admin_port" ]; then
        die "Cannot reach a PostgreSQL superuser (\`runuser -u postgres -- psql\`) to provision '$DB_NAME'. Ensure the managed PostgreSQL is installed and running, or point FDM at a database you control with FDM_DATABASE_URL."
    fi
    if [ "$admin_port" != "$DB_PORT" ]; then
        die "Refusing to adopt a foreign PostgreSQL on port $DB_PORT.
Something is listening on 127.0.0.1:$DB_PORT (where DATABASE_URL points), but the
PostgreSQL this installer can administer as the 'postgres' superuser is on port
$admin_port — they are different servers. \`pg_isready\` on :$DB_PORT is not proof the
server there is FDM's own. Provisioning or dropping via the 'postgres' superuser
would target :$admin_port and miss the server the app actually uses.
Fix it one of two ways:
  • Free port $DB_PORT (stop the other PostgreSQL) so FDM's managed server binds it,
    then re-run this installer; or
  • Point FDM at the exact database you want with FDM_DATABASE_URL — FDM then treats
    it as yours and neither creates nor drops it, e.g.:
      sudo FDM_DATABASE_URL='postgresql+psycopg://<user>:<pass>@127.0.0.1:$DB_PORT/<db>' \\
        bash -c 'curl -fsSL https://raw.githubusercontent.com/haneefzone/frappe_os/${FDM_BRANCH}/install.sh | bash'"
    fi
    log "Verified the 'postgres' superuser administers port $DB_PORT (FDM's managed server)."
    log "Ensuring PostgreSQL role '$DB_USER' + database '$DB_NAME' exist…"
    if [ "$(provision_db "runuser -u postgres -- psql" \
            "$DB_NAME" "$DB_USER" "$DB_PASS_EFFECTIVE")" = created ]; then
        log "Created database '$DB_NAME'."
    else
        log "Database '$DB_NAME' already exists — reusing it."
    fi
elif [ -n "${FDM_DATABASE_URL:-}" ]; then
    log "Using externally-provided FDM_DATABASE_URL — skipping provisioning."
elif [ "$IS_ROOT" -ne 1 ]; then
    log "Non-root run — assuming the database already exists (cannot provision)."
fi

# ---------------------------------------------------------------- migrate
# DOO-1169: capture the migration output instead of discarding it to /dev/null.
# provision_db above only re-creates a MISSING database; it cannot reconcile a
# managed database whose schema is AHEAD of alembic_version (a leftover from an
# earlier aborted attempt). Such a DB replays a pending migration into a
# DuplicateColumn/"already exists" failure, and a plain rerun loops forever on
# it. Detect that signature and stop with the exact drop-and-reinstall recovery
# command instead of a silent, self-repeating failure.
log "Running database migrations (alembic upgrade head)…"
# Suppress the generic ERR handler for the duration of the captured run: this
# block reports migration failure itself (with a targeted recovery message) and
# would otherwise fire the trap too — a command-substitution assignment trips
# the ERR trap even under `set +e`.
trap - ERR
set +e
migrate_out="$(as_fdm "$BACKEND_DIR" "$BACKEND_DIR/.venv/bin/alembic" upgrade head 2>&1)"
migrate_rc=$?
set -e
trap 'on_install_error "$LINENO"' ERR
if [ "$migrate_rc" -ne 0 ]; then
    printf '%s\n' "$migrate_out" >&2
    if is_schema_drift_error "$migrate_out"; then
        die "Migration failed: the managed database '$DB_NAME' has a schema AHEAD of its recorded alembic version — most likely a leftover from an earlier aborted install. This installer re-creates a MISSING database but cannot reconcile a drifted one, so re-running as-is will keep hitting this same error. Recover with:

    $(recovery_cmd "$DB_NAME" "${DB_HOST:-127.0.0.1}" "$DB_PORT" "$DB_USER")

That drops the managed database (named by the host, port and user from your effective DATABASE_URL — NOT a bare 'sudo -u postgres dropdb', which routes through pg_wrapper and can silently miss the server FDM actually uses, DOO-1175) and fetches a fresh installer from the canonical source to rebuild it cleanly. The curl form (rather than a bare 'sudo ./install.sh') is deliberate: an in-tree rerun re-executes this same on-disk installer, so a checkout that predates the fix would just loop on this error (DOO-1174). Your .env secrets are preserved (dropdb touches only the database). If '$DB_NAME' holds data you need, back it up first: pg_dump -h ${DB_HOST:-127.0.0.1} -p $DB_PORT -U $DB_USER $DB_NAME > fdm-backup.sql"
    fi
    die "Database migration failed (alembic upgrade head, exit $migrate_rc) — see the error above."
fi

# --------------------------------------------------------------- seed / wizard
# Session 6.4: fresh installs are set up via the browser wizard at /setup.
# The CLI seed is kept for headless / automated installs; pass FDM_ADMIN_EMAIL
# and FDM_ADMIN_PASSWORD to activate it (e.g. CI, Docker, provisioning scripts).
#
# DOO-1155: whether setup still needs doing is decided by a completion marker
# written only after a run fully succeeds — NOT by FRESH_INSTALL (which keys on
# .env and so wrongly reads a partial first install, whose migrate died after
# .env was written, as a completed upgrade — hiding the /setup URL the operator
# still needs). A run that dies before the marker is written is correctly
# treated as setup-pending on the next re-run. The CLI seed is idempotent (it
# never resets an existing admin), so re-seeding a resumed install is safe.
MARKER_FILE="$BACKEND_DIR/.install-complete"
SETUP_PENDING=1; [ -f "$MARKER_FILE" ] && SETUP_PENDING=0
ADMIN_PASSWORD="${FDM_ADMIN_PASSWORD:-}"
if [ "$SETUP_PENDING" -eq 1 ] && [ -n "$ADMIN_PASSWORD" ]; then
    log "Headless seed: creating admin user $FDM_ADMIN_EMAIL via CLI…"
    as_fdm "$BACKEND_DIR" "$BACKEND_DIR/.venv/bin/python" -m app.seed \
        --admin-email "$FDM_ADMIN_EMAIL" --admin-password "$ADMIN_PASSWORD" >/dev/null
elif [ "$SETUP_PENDING" -eq 1 ]; then
    log "Setup pending: admin account will be created via the browser wizard (/setup)."
fi

# ------------------------------------------------------------ frontend build
log "Building frontend (npm ci && vite build — first run takes a few minutes)…"
as_fdm "$FRONTEND_DIR" npm ci --no-audit --no-fund --loglevel=error >/dev/null
as_fdm "$FRONTEND_DIR" npm run build --silent >/dev/null

# ----------------------------------------------------------------- services
start_services_systemd() {
    log "Installing systemd services fdm-api + fdm-worker…"
    cat > /etc/systemd/system/fdm-api.service <<EOF
[Unit]
Description=FDM Platform API (uvicorn)
After=network-online.target postgresql.service redis-server.service
Wants=network-online.target

[Service]
User=$RUN_USER
WorkingDirectory=$BACKEND_DIR
ExecStart=$BACKEND_DIR/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port $FDM_PORT
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
    cat > /etc/systemd/system/fdm-worker.service <<EOF
[Unit]
Description=FDM Platform job worker (RQ: high default low)
After=network-online.target postgresql.service redis-server.service
Wants=network-online.target

[Service]
User=$RUN_USER
WorkingDirectory=$BACKEND_DIR
ExecStart=$BACKEND_DIR/.venv/bin/rq worker --url $REDIS_URL_EFFECTIVE high default low
Restart=on-failure
RestartSec=3
# bench init/update jobs are long; let a deploy wait for graceful shutdown.
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable --now fdm-api.service fdm-worker.service
    systemctl restart fdm-api.service fdm-worker.service
}

start_bg() {
    # start_bg <name> <cmd...> — nohup a service from $BACKEND_DIR as the
    # service user, tracking the pid so re-runs replace the old process.
    local name="$1"; shift
    local pidfile="$FDM_HOME/run/$name.pid"
    if [ -f "$pidfile" ]; then
        local old_pid; old_pid="$(cat "$pidfile")"
        kill "$old_pid" 2>/dev/null || true
        for _ in $(seq 1 15); do
            kill -0 "$old_pid" 2>/dev/null || break
            sleep 1
        done
        kill -9 "$old_pid" 2>/dev/null || true
    fi
    if [ "$IS_ROOT" -eq 1 ]; then
        nohup runuser -u "$RUN_USER" -- env -C "$BACKEND_DIR" \
            HOME="$FDM_HOME" PATH="/usr/local/bin:/usr/bin:/bin" "$@" \
            >"$FDM_HOME/logs/$name.log" 2>&1 &
    else
        nohup env -C "$BACKEND_DIR" "$@" >"$FDM_HOME/logs/$name.log" 2>&1 &
    fi
    echo $! > "$pidfile"
    sleep 2
    kill -0 "$(cat "$pidfile")" 2>/dev/null \
        || die "Service '$name' exited right after start — see $FDM_HOME/logs/$name.log"
}

start_services_nohup() {
    warn "systemd unavailable — starting via nohup (logs in $FDM_HOME/logs). Not for production."
    mkdir -p "$FDM_HOME/logs" "$FDM_HOME/run"
    if [ "$IS_ROOT" -eq 1 ]; then chown -R "$RUN_USER:$RUN_USER" "$FDM_HOME/logs" "$FDM_HOME/run"; fi
    start_bg api "$BACKEND_DIR/.venv/bin/uvicorn" app.main:app --host 0.0.0.0 --port "$FDM_PORT"
    start_bg worker "$BACKEND_DIR/.venv/bin/rq" worker --url "$REDIS_URL_EFFECTIVE" high default low
}

if [ "$HAVE_SYSTEMD" -eq 1 ]; then start_services_systemd; else start_services_nohup; fi

# ------------------------------------------------------------- health check
log "Waiting for the API to come up on :$FDM_PORT…"
HEALTH=""
for _ in $(seq 1 60); do
    HEALTH="$(curl -fsS "http://127.0.0.1:$FDM_PORT/api/health" 2>/dev/null || true)"
    if echo "$HEALTH" | grep -q '"status":"ok"'; then break; fi
    sleep 1
done
echo "$HEALTH" | grep -q '"status":"ok"' \
    || die "API did not become healthy. Logs: journalctl -u fdm-api (or $FDM_HOME/logs/api.log)"
curl -fsS "http://127.0.0.1:$FDM_PORT/" | grep -qi '<div id="app">' \
    || die "Login page not served at http://127.0.0.1:$FDM_PORT/"

# The install is fully up. Drop the completion marker so a later re-run is
# unambiguously an upgrade and a run that dies earlier is resumed as setup-
# pending (DOO-1155). Excluded from rsync so it survives a code sync.
: > "$MARKER_FILE"
if [ "$IS_ROOT" -eq 1 ]; then chown "$RUN_USER:$RUN_USER" "$MARKER_FILE"; fi

# ------------------------------------------------------------------ summary
HOST_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo
echo "=================================================================="
echo "  FDM Platform is running."
echo
echo "  URL:          http://${HOST_IP:-127.0.0.1}:$FDM_PORT"
echo "  Health:       $HEALTH"
if [ "$SETUP_PENDING" -eq 1 ] && [ -n "$ADMIN_PASSWORD" ]; then
    echo "  Login:        $FDM_ADMIN_EMAIL"
    echo "  Password:     $ADMIN_PASSWORD"
    echo "                (shown once — change it after first login)"
elif [ "$SETUP_PENDING" -eq 1 ]; then
    echo "  Setup wizard: http://${HOST_IP:-127.0.0.1}:$FDM_PORT/setup"
    echo "                Open this URL in your browser to complete setup."
    echo "  Headless:     FDM_ADMIN_EMAIL=... FDM_ADMIN_PASSWORD=... ./install.sh"
    echo "                (non-interactive / CI installs)"
else
    echo "  Login:        unchanged (existing install upgraded)"
fi
echo
echo "  Config:       $ENV_FILE"
if [ "$HAVE_SYSTEMD" -eq 1 ]; then
    echo "  Services:     systemctl status fdm-api fdm-worker"
    echo "  Logs:         journalctl -u fdm-api -f"
else
    echo "  Services:     nohup (dev mode) — logs in $FDM_HOME/logs/"
fi
echo "  Upgrade:      re-run this installer (idempotent)"
echo "  Reset admin:  cd $BACKEND_DIR && .venv/bin/python -m app.seed \\"
echo "                    --admin-email <email> --admin-password <new-password>"
echo
echo "  Serving plain HTTP. Before exposing this beyond a trusted network,"
echo "  put it behind an HTTPS reverse proxy and set COOKIE_SECURE=true."
echo "=================================================================="
