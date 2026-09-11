#!/usr/bin/env bash
# DOO-1155 AC4/AC5 — regression test for the install re-runnability trap.
#
# The bug: install.sh derived FRESH_INSTALL from backend/.env existence and
# gated `createdb` on it. A first install that wrote .env then failed at/after
# `alembic upgrade head` left .env behind, so every rerun skipped provisioning.
# `dropdb fdm && ./install.sh` then failed forever at migrate against a DB that
# no longer existed, while the ERR trap falsely said "re-running is safe".
#
# The fix moved provisioning into install-lib.sh::provision_db, driven off
# ACTUAL DB state rather than the .env proxy. This test exercises that function
# directly (no root / apt / systemd needed) and proves the self-heal.
#
# Runs against any reachable PostgreSQL. Configure the admin (superuser) via:
#   PGHOST/PGPORT              (default 127.0.0.1 / 5432)
#   TEST_PG_ADMIN_USER         (default fdm)
#   TEST_PG_ADMIN_PASSWORD     (default empty — fine under trust auth)
# It only ever creates/drops throwaway role+db names suffixed with the PID; it
# never touches the real `fdm` database.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
# shellcheck source=../../install-lib.sh
. "$ROOT/install-lib.sh"

: "${PGHOST:=127.0.0.1}"
: "${PGPORT:=5432}"
export PGHOST PGPORT
ADMIN_USER="${TEST_PG_ADMIN_USER:-fdm}"
ADMIN_PASS="${TEST_PG_ADMIN_PASSWORD:-}"
ADMIN="env PGPASSWORD=$ADMIN_PASS psql -h $PGHOST -p $PGPORT -U $ADMIN_USER -d postgres -v ON_ERROR_STOP=1"

DB="fdm_reentry_test_$$"
ROLE="fdm_reentry_role_$$"
PASS="urlsafe_pw_$$"
URL="postgresql+psycopg://$ROLE:$PASS@127.0.0.1:5432/$DB"

cleanup() {
    $ADMIN -tAc "DROP DATABASE IF EXISTS \"$DB\"" >/dev/null 2>&1 || true
    $ADMIN -tAc "DROP ROLE IF EXISTS \"$ROLE\"" >/dev/null 2>&1 || true
}
trap cleanup EXIT

pass=0
check() { if [ "$1" = "$2" ]; then pass=$((pass+1)); echo "  ok: $3"; else echo "FAIL: $3 (want '$2', got '$1')" >&2; exit 1; fi; }

echo "== 1. URL parsing =="
check "$(pg_url_field "$URL" dbname)"   "$DB"        "dbname"
check "$(pg_url_field "$URL" user)"     "$ROLE"      "user"
check "$(pg_url_field "$URL" password)" "$PASS"      "password"
check "$(pg_url_field "$URL" host)"     "127.0.0.1"  "host"
check "$(pg_url_field "$URL" port)"     "5432"       "port"
check "$(is_local_host 127.0.0.1 && echo y || echo n)" y "loopback classified local"
check "$(is_local_host db.example.com && echo y || echo n)" n "remote classified non-local"

echo "== 2. first provision creates role + db =="
cleanup
check "$(provision_db "$ADMIN" "$DB" "$ROLE" "$PASS")" created "fresh provision reports created"

echo "== 3. provision is idempotent (the normal upgrade path) =="
check "$(provision_db "$ADMIN" "$DB" "$ROLE" "$PASS")" exists "second provision is a no-op"

echo "== 4. THE FOOTGUN: dropdb after a failed first install, .env kept =="
$ADMIN -tAc "DROP DATABASE \"$DB\"" >/dev/null
# Old code: .env present -> FRESH_INSTALL=0 -> createdb skipped -> migrate dies.
# New code: provisioning keys off actual DB state and re-creates it.
check "$(provision_db "$ADMIN" "$DB" "$ROLE" "$PASS")" created "dropdb && reinstall re-creates the DB"

echo "== 5. provisioned role can actually connect to the provisioned db =="
PGPASSWORD="$PASS" psql -h "$PGHOST" -p "$PGPORT" -U "$ROLE" -d "$DB" -tAc "SELECT 1" >/dev/null \
    && echo "  ok: role connects" \
    || { echo "FAIL: provisioned role cannot connect" >&2; exit 1; }

# ---------------------------------------------------------------------------
# DOO-1169: a MISSING database self-heals (sections 2-4), but a database whose
# schema is AHEAD of alembic_version does NOT — provision_db leaves the existing
# DB alone and `alembic upgrade head` then replays a pending migration into a
# DuplicateColumn/"already exists" failure. A plain rerun loops on it forever;
# the honest recovery is dropdb + reinstall. These sections prove (a) the drift
# signature is classified as drift, (b) unrelated failures are NOT, (c) the
# advertised recovery command actually clears the wedge.

echo "== 6. is_schema_drift_error classifies the DOO-1169 signatures =="
DRIFT_MSG='sqlalchemy.exc.ProgrammingError: (psycopg.errors.DuplicateColumn) column "last_check_ok" of relation "restic_snapshots" already exists'
check "$(is_schema_drift_error "$DRIFT_MSG" && echo y || echo n)"        y "DuplicateColumn output is drift"
check "$(is_schema_drift_error 'relation "foo" already exists' && echo y || echo n)" y "\"already exists\" output is drift"
check "$(is_schema_drift_error 'psycopg.OperationalError: connection refused' && echo y || echo n)" n "connection error is NOT drift"
check "$(is_schema_drift_error 'Target database is not up to date.' && echo y || echo n)" n "plain out-of-date is NOT drift"

echo "== 7. recovery_cmd derives the drop from the effective DATABASE_URL (DOO-1175) =="
# DOO-1175: `sudo -u postgres dropdb` routes through Debian pg_wrapper and can
# target a different cluster than the one the app is bound to (an embedded PG on
# :5432 vs the Debian cluster on :5433 — the exact DOO-1162 loop). The drop must
# name host/port/user explicitly so it hits the precise server DATABASE_URL uses,
# and must NEVER use --if-exists (on the wrong server a miss looks like success).
# DOO-1174: the reinstall half stays the checkout-age-independent curl form.
check "$(recovery_cmd fdm 127.0.0.1 5432 fdm)" \
    "dropdb -h 127.0.0.1 -p 5432 -U fdm fdm && curl -fsSL https://raw.githubusercontent.com/haneefzone/frappe_os/main/install.sh | sudo bash" \
    "recovery_cmd names host/port/user (curl reinstall)"
check "$(recovery_cmd "$DB" 127.0.0.1 5432 "$ROLE")" \
    "dropdb -h 127.0.0.1 -p 5432 -U $ROLE $DB && curl -fsSL https://raw.githubusercontent.com/haneefzone/frappe_os/main/install.sh | sudo bash" \
    "recovery_cmd honours DB name + user"
check "$(recovery_cmd fdm db.internal 6543 fdmadmin)" \
    "dropdb -h db.internal -p 6543 -U fdmadmin fdm && curl -fsSL https://raw.githubusercontent.com/haneefzone/frappe_os/main/install.sh | sudo bash" \
    "recovery_cmd honours non-default host/port/user"
check "$(recovery_cmd fdm 127.0.0.1 5432 fdm release-2.0)" \
    "dropdb -h 127.0.0.1 -p 5432 -U fdm fdm && curl -fsSL https://raw.githubusercontent.com/haneefzone/frappe_os/release-2.0/install.sh | sudo bash" \
    "recovery_cmd honours an explicit branch arg"
check "$(FDM_BRANCH=stable recovery_cmd fdm 127.0.0.1 5432 fdm)" \
    "dropdb -h 127.0.0.1 -p 5432 -U fdm fdm && curl -fsSL https://raw.githubusercontent.com/haneefzone/frappe_os/stable/install.sh | sudo bash" \
    "recovery_cmd defaults branch to \$FDM_BRANCH"
check "$(recovery_cmd fdm)" \
    "dropdb -h 127.0.0.1 -p 5432 -U fdm fdm && curl -fsSL https://raw.githubusercontent.com/haneefzone/frappe_os/main/install.sh | sudo bash" \
    "bare recovery_cmd defaults to loopback/5432/fdm"
# The footguns must be gone: no pg_wrapper drop, no --if-exists masking a miss,
# no in-tree './install.sh' rerun; and it must still fetch a fresh installer.
check "$(recovery_cmd fdm | grep -c 'sudo -u postgres dropdb')" 0 "recovery_cmd no longer assumes 'sudo -u postgres'"
check "$(recovery_cmd fdm | grep -c -- '--if-exists')"          0 "recovery_cmd never suggests --if-exists"
check "$(recovery_cmd fdm | grep -c 'dropdb -h [^ ]* -p [^ ]* -U ')" 1 "recovery_cmd names host/port/user"
check "$(recovery_cmd fdm | grep -c 'sudo \./install\.sh')" 0 "recovery_cmd no longer emits in-tree ./install.sh"
check "$(recovery_cmd fdm | grep -c 'curl -fsSL')"          1 "recovery_cmd fetches a fresh installer"

echo "== 8. schema-ahead DB: migrate fails as drift, and the recovery works =="
# Seed the exact wedge: alembic_version stamped BEHIND, but the object a pending
# migration would create already present (schema AHEAD). Uses only psql, so it
# runs anywhere the rest of this test does — no backend app / alembic needed.
seed_ahead() {
    PGPASSWORD="$PASS" psql -h "$PGHOST" -p "$PGPORT" -U "$ROLE" -d "$DB" -v ON_ERROR_STOP=1 >/dev/null <<'SQL'
CREATE TABLE IF NOT EXISTS alembic_version (version_num varchar(32) NOT NULL);
DELETE FROM alembic_version;
INSERT INTO alembic_version (version_num) VALUES ('b7e2d9f4c1a8');
CREATE TABLE IF NOT EXISTS restic_snapshots (id serial PRIMARY KEY);
ALTER TABLE restic_snapshots ADD COLUMN last_check_ok boolean;
SQL
}
# With alembic_version stamped BEHIND, `alembic upgrade head` runs ONLY the
# migrations after that revision — here the pending d4e2f7a9c6b1 ADD COLUMN,
# NOT the earlier CREATE TABLE (alembic believes it is already applied). Against
# the seeded (ahead) schema that ADD COLUMN fails with DuplicateColumn — the
# real symptom of the DOO-1169 wedge.
pending_migration_ddl() {
    PGPASSWORD="$PASS" psql -h "$PGHOST" -p "$PGPORT" -U "$ROLE" -d "$DB" -v ON_ERROR_STOP=1 \
        -c 'ALTER TABLE restic_snapshots ADD COLUMN last_check_ok boolean' 2>&1
}
# On a CLEAN database alembic runs the whole chain from zero: the earlier
# CREATE TABLE then the d4e2f7a9c6b1 ADD COLUMN. This is what the recovery path
# produces, and it must succeed.
clean_migration_from_zero() {
    PGPASSWORD="$PASS" psql -h "$PGHOST" -p "$PGPORT" -U "$ROLE" -d "$DB" -v ON_ERROR_STOP=1 >/dev/null 2>&1 <<'SQL'
CREATE TABLE restic_snapshots (id serial PRIMARY KEY);
ALTER TABLE restic_snapshots ADD COLUMN last_check_ok boolean;
SQL
}
seed_ahead
set +e
mig_out="$(pending_migration_ddl)"; mig_rc=$?
set -e
check "$([ "$mig_rc" -ne 0 ] && echo fail || echo ok)" fail "migrate against schema-ahead DB fails"
check "$(is_schema_drift_error "$mig_out" && echo y || echo n)" y "the failure is detected as drift"

echo "== 9. the advertised recovery command actually clears the wedge =="
# Exactly what recovery_cmd tells the operator to do: drop the DB, then let the
# installer's provisioning re-create it clean, then migrate from zero.
$ADMIN -tAc "DROP DATABASE \"$DB\"" >/dev/null
check "$(provision_db "$ADMIN" "$DB" "$ROLE" "$PASS")" created "recovery re-creates a clean DB"
check "$(clean_migration_from_zero && echo ok || echo fail)" ok "from-zero migration now succeeds on the clean DB"

# ---------------------------------------------------------------------------
# DOO-1174: the DOO-1169 recovery command ('dropdb && sudo ./install.sh') cannot
# work on a STALE checkout. 'sudo ./install.sh' run from inside the install dir
# takes install.sh's "skipping code sync" branch, so it re-executes whatever
# installer code is already on disk. On a pre-fix checkout that is the OLD
# installer (no drift detection), so dropdb + in-tree rerun dies at migrate
# again and loops forever. These sections reproduce that with real git repos
# (offline — origin is a local bare repo) and a drifted DB, and prove:
#   (a) a stale checkout is detected (checkout_is_behind), so install.sh warns;
#   (b) the on-disk stale installer genuinely cannot recover (no drift logic);
#   (c) the curl-fetched installer IS drift-aware, and after dropdb the DB
#       migrates clean — i.e. the recovery_cmd curl form escapes the loop.

GITROOT="$(mktemp -d "${TMPDIR:-/tmp}/fdm_stale_ck.$$.XXXXXX")"
cleanup_git() { rm -rf "$GITROOT" 2>/dev/null || true; }
trap 'cleanup_git; cleanup' EXIT
GIT="git -c user.email=t@t -c user.name=t -c init.defaultBranch=main -c advice.detachedHead=false -c protocol.file.allow=always"

echo "== 10. DOO-1174: build a stale checkout (offline origin) =="
ORIGIN="$GITROOT/origin.git"
WORK="$GITROOT/opt-fdm-platform"
SEED="$GITROOT/seed"
$GIT init -q --bare "$ORIGIN"
$GIT init -q "$SEED"
mkdir -p "$SEED/backend"
: > "$SEED/backend/pyproject.toml"   # marks SEED a valid SOURCE_DIR for install.sh
# --- commit 1: the OLD installer — the footgun recovery, and NO drift logic.
cat > "$SEED/install.sh" <<'OLD'
#!/usr/bin/env bash
# Pre-DOO-1169 installer stub: dies at migrate, prints the in-tree recovery.
echo "[fdm-install] migrating…"
echo "ERROR: re-run with: sudo -u postgres dropdb fdm && sudo ./install.sh" >&2
exit 1
OLD
$GIT -C "$SEED" add -A && $GIT -C "$SEED" commit -qm "old installer (no drift detection)"
$GIT -C "$SEED" remote add origin "$ORIGIN"
$GIT -C "$SEED" push -q origin main
OLD_SHA="$($GIT -C "$SEED" rev-parse HEAD)"
# --- commit 2 on origin: the FIXED installer — the real, drift-aware install.sh.
cp "$ROOT/install.sh" "$SEED/install.sh"
cp "$ROOT/install-lib.sh" "$SEED/install-lib.sh"
$GIT -C "$SEED" add -A && $GIT -C "$SEED" commit -qm "fixed installer (DOO-1169/1174)"
$GIT -C "$SEED" push -q origin main
# The operator's checkout: cloned, then pinned BACK to the old commit (stale).
$GIT clone -q "$ORIGIN" "$WORK"
$GIT -C "$WORK" reset -q --hard "$OLD_SHA"
check "$($GIT -C "$WORK" rev-parse HEAD)" "$OLD_SHA" "work checkout pinned to the pre-fix commit"

echo "== 11. DOO-1174: checkout_is_behind flags the stale tree (and stays quiet otherwise) =="
# Behind by exactly one commit -> install.sh's in-tree branch will warn.
check "$(checkout_is_behind "$WORK" main)" 1 "stale checkout reported 1 commit behind"
# Not a git checkout / offline-ish -> silent (never blocks an air-gapped install).
check "$(checkout_is_behind "$GITROOT/does-not-exist" main)" "" "non-repo path is silent"
# After updating to origin tip -> up to date -> silent.
UP="$GITROOT/uptodate"; $GIT clone -q "$ORIGIN" "$UP"
check "$(checkout_is_behind "$UP" main)" "" "current checkout is silent"

echo "== 12. DOO-1174: old in-tree recovery loops; curl-fetched installer recovers =="
# (b) The on-disk stale installer the in-tree './install.sh' would re-run has no
#     drift detection — it can only die and re-print the same looping advice.
check "$(grep -c 'is_schema_drift_error' "$WORK/install.sh")" 0 \
    "stale on-disk installer cannot detect drift (in-tree rerun loops)"
check "$(grep -c 'sudo \./install\.sh' "$WORK/install.sh")" 1 \
    "stale installer even prints the looping in-tree recovery"
# (c) What the curl form actually fetches is origin's tip installer, which IS
#     drift-aware and prints the escape-the-loop curl recovery.
FETCHED="$GITROOT/fetched-install.sh"
$GIT -C "$WORK" show origin/main:install.sh > "$FETCHED"
check "$(grep -q 'is_schema_drift_error' "$FETCHED" && echo y || echo n)" y "curl-fetched installer detects drift"
# The fixed installer's drift recovery advertises the age-independent curl form.
check "$(grep -qF 'raw.githubusercontent.com/haneefzone/frappe_os' "$FETCHED" && echo y || echo n)" y \
    "curl-fetched installer advertises the age-independent curl recovery"
# And the DB half: reproduce the drifted DB, then run exactly what the recovery
# does (dropdb -> provision clean -> migrate from zero) and prove it reaches a
# good state. This is the state the curl-fetched installer drives the DB to.
$ADMIN -tAc "DROP DATABASE IF EXISTS \"$DB\"" >/dev/null
$ADMIN -tAc "SELECT 1 FROM pg_roles WHERE rolname='$ROLE'" | grep -q 1 \
    || $ADMIN -tAc "CREATE ROLE \"$ROLE\" LOGIN PASSWORD '$PASS'" >/dev/null
$ADMIN -tAc "CREATE DATABASE \"$DB\" OWNER \"$ROLE\"" >/dev/null
seed_ahead
set +e
mig_out2="$(pending_migration_ddl)"; mig_rc2=$?
set -e
check "$([ "$mig_rc2" -ne 0 ] && echo fail || echo ok)" fail "stale-checkout DB is drifted (migrate fails)"
check "$(is_schema_drift_error "$mig_out2" && echo y || echo n)" y "and the failure is drift (drift-aware installer would catch it)"
# recovery: dropdb + fresh provision + from-zero migration.
$ADMIN -tAc "DROP DATABASE \"$DB\"" >/dev/null
check "$(provision_db "$ADMIN" "$DB" "$ROLE" "$PASS")" created "recovery re-creates a clean DB"
check "$(clean_migration_from_zero && echo ok || echo fail)" ok "from-zero migration succeeds after the curl recovery"

echo
echo "PASS ($pass checks): install re-entry self-heal + schema-drift recovery + stale-checkout recovery verified (DOO-1155 AC4/AC5, DOO-1169, DOO-1174)."
