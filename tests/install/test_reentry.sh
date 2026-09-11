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

echo "== 7. recovery_cmd names the exact drop-and-reinstall command =="
check "$(recovery_cmd fdm)"  "sudo -u postgres dropdb fdm && sudo ./install.sh"  "recovery_cmd fdm"
check "$(recovery_cmd "$DB")" "sudo -u postgres dropdb $DB && sudo ./install.sh" "recovery_cmd honours DB name"

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

echo
echo "PASS ($pass checks): install re-entry self-heal + schema-drift recovery verified (DOO-1155 AC4/AC5, DOO-1169)."
