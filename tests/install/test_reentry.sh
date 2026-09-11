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

echo
echo "PASS ($pass checks): install re-entry self-heal verified (DOO-1155 AC4/AC5)."
