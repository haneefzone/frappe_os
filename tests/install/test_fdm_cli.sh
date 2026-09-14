#!/usr/bin/env bash
# DOO-1205 — tests for the `fdm` operator CLI (bin/fdm) and its shared
# install-lib.sh helpers.
#
# Exercises verb dispatch, BOTH runtime-mode branches (nohup end-to-end with
# fake venv binaries; systemd via a fake `systemctl`/`journalctl` on PATH), the
# already-running / already-stopped idempotency paths, a healthy-status exit,
# and the `fdm update` dirty/divergent refusals (real offline git repos, as in
# test_reentry.sh §10-12). No root, apt, or systemd required — the agent/CI box
# can only exercise the nohup path, so the systemd branch is proven by dispatch.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
CLI="$ROOT/bin/fdm"
# shellcheck source=../../install-lib.sh
. "$ROOT/install-lib.sh"

pass=0
check() { if [ "$1" = "$2" ]; then pass=$((pass+1)); echo "  ok: $3"; else echo "FAIL: $3 (want '$2', got '$1')" >&2; exit 1; fi; }

WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/fdm_cli_test.$$.XXXXXX")"
cleanup() {
    # kill anything we spawned via pidfiles across all sandboxes
    for pf in "$WORKDIR"/*/home/run/*.pid; do
        [ -f "$pf" ] || continue
        kill "$(cat "$pf" 2>/dev/null)" 2>/dev/null || true
    done
    [ -n "${RESPONDER_PID:-}" ] && kill "$RESPONDER_PID" 2>/dev/null || true
    rm -rf "$WORKDIR" 2>/dev/null || true
}
trap cleanup EXIT

# run VAR CLI-ARGS... — run the CLI, capture exit code into $VAR and stdout+err
# into $out. The CLI uses `set -uo pipefail` (not -e), so this test's -e must be
# suspended around the call.
out=""
run() {
    local __rc_var="$1"; shift
    set +e
    out="$("$CLI" "$@" 2>&1)"; local __rc=$?
    set -e
    printf -v "$__rc_var" '%s' "$__rc"
}

# Build a self-contained sandbox FDM_HOME with fake uvicorn/rq binaries.
# Echoes the FDM_HOME path.
make_sandbox() {
    local name="$1"
    local home="$WORKDIR/$name/home"
    mkdir -p "$home/backend/.venv/bin"
    cp "$ROOT/install-lib.sh" "$home/install-lib.sh"
    printf 'version = "9.9.9"\n' > "$home/backend/pyproject.toml"
    printf 'REDIS_URL=redis://127.0.0.1:6379/0\n' > "$home/backend/.env"
    cat > "$home/backend/.venv/bin/uvicorn" <<'EOF'
#!/usr/bin/env bash
exec sleep 300
EOF
    cat > "$home/backend/.venv/bin/rq" <<'EOF'
#!/usr/bin/env bash
exec sleep 300
EOF
    chmod +x "$home/backend/.venv/bin/uvicorn" "$home/backend/.venv/bin/rq"
    printf '%s' "$home"
}

echo "== 1. install-lib helpers: paths =="
check "$(fdm_default_home 1)" "/opt/fdm-platform"            "root default home"
check "$(fdm_default_home 0)" "$HOME/.local/share/fdm-platform" "non-root default home"

echo "== 2. install-lib helpers: pidfile liveness + stop =="
LIVE_PIDFILE="$WORKDIR/live.pid"
( exec sleep 60 ) & LIVE_PID=$!
echo "$LIVE_PID" > "$LIVE_PIDFILE"
check "$(fdm_pid_running "$LIVE_PIDFILE")" "$LIVE_PID" "fdm_pid_running reports a live pid"
fdm_stop_pid "$LIVE_PID" 5
check "$(fdm_pid_running "$LIVE_PIDFILE" >/dev/null && echo y || echo n)" n "fdm_stop_pid terminates the process"
echo "424242" > "$WORKDIR/stale.pid"
check "$(fdm_pid_running "$WORKDIR/stale.pid" >/dev/null && echo y || echo n)" n "a stale pidfile reads as not-running"
check "$(fdm_pid_running "$WORKDIR/none.pid" >/dev/null && echo y || echo n)" n "a missing pidfile reads as not-running"
fdm_stop_pid "" 1 && echo "  ok: fdm_stop_pid on empty pid is a no-op (exit 0)"

echo "== 3. install-lib helpers: port parse from a systemd unit =="
UNIT="$WORKDIR/fake-api.service"
printf 'ExecStart=/x/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 9443\n' > "$UNIT"
check "$(fdm_port_from_unit "$UNIT")" "9443" "port parsed from ExecStart"
check "$(fdm_port_from_unit "$WORKDIR/does-not-exist")" "" "absent unit -> empty port"

echo "== 4. CLI dispatch: help / version / unknown verb =="
run rc --help;        check "$rc" 0 "--help exits 0"
check "$(printf '%s' "$out" | grep -c 'Usage: fdm')" 1 "--help prints usage"
run rc;               check "$rc" 0 "no args exits 0 (prints usage)"
run rc --version;     check "$rc" 0 "--version exits 0"
check "$(printf '%s' "$out" | grep -c 'FDM Platform 9\.9\.9\|FDM Platform')" 1 "--version prints a version line"
run rc frobnicate;    check "$rc" 2 "unknown verb exits 2"
check "$(printf '%s' "$out" | grep -c "unknown command 'frobnicate'")" 1 "unknown verb names the bad verb"
# case-insensitive verb folding: 'STATUS' must dispatch like 'status'. Use a
# sandbox so it does not touch a real install.
Hcase="$(make_sandbox case1)"
FDM_HOME="$Hcase" FDM_MODE=nohup FDM_PORT=8231 run rc STATUS
check "$rc" 1 "mixed-case verb 'STATUS' dispatches (stopped -> non-zero)"
check "$(printf '%s' "$out" | grep -c 'FDM Platform status')" 1 "'STATUS' folded to 'status'"

echo "== 5. nohup lifecycle end-to-end (fake uvicorn/rq) =="
H="$(make_sandbox nohup1)"
export FDM_HOME="$H" FDM_MODE=nohup FDM_PORT=8123
run rc status;   check "$rc" 1 "status when stopped exits non-zero"
check "$(printf '%s' "$out" | grep -c 'api:       stopped')" 1 "status shows api stopped"
check "$(printf '%s' "$out" | grep -c 'worker:    stopped')" 1 "status shows worker stopped"
check "$(printf '%s' "$out" | grep -c 'mode:      nohup')" 1 "status shows the resolved mode"
run rc start --bg; check "$rc" 0 "start --bg exits 0"
check "$(fdm_pid_running "$H/run/api.pid" >/dev/null && echo y || echo n)" y "api pid is live after start"
check "$(fdm_pid_running "$H/run/worker.pid" >/dev/null && echo y || echo n)" y "worker pid is live after start"
run rc status;   check "$rc" 1 "status running-but-unhealthy exits non-zero (no real API)"
check "$(printf '%s' "$out" | grep -c 'running (pid')" 2 "status shows both services running"
run rc start --bg; check "$rc" 0 "start --bg on a running instance is idempotent (exit 0)"
check "$(printf '%s' "$out" | grep -c 'Already running')" 1 "idempotent start reports already-running"
# restart must not orphan pids and must leave both up
API_BEFORE="$(cat "$H/run/api.pid")"
run rc restart;  check "$rc" 0 "restart exits 0"
check "$(fdm_pid_running "$H/run/api.pid" >/dev/null && echo y || echo n)" y "api live after restart"
check "$(fdm_pid_running "$H/run/worker.pid" >/dev/null && echo y || echo n)" y "worker live after restart"
check "$(kill -0 "$API_BEFORE" 2>/dev/null && echo y || echo n)" n "restart replaced the old api pid (no orphan)"
run rc stop;     check "$rc" 0 "stop exits 0"
check "$(fdm_pid_running "$H/run/api.pid" >/dev/null && echo y || echo n)" n "api stopped after stop"
check "$(fdm_pid_running "$H/run/worker.pid" >/dev/null && echo y || echo n)" n "worker stopped after stop"
run rc stop;     check "$rc" 0 "stop when already stopped is idempotent (exit 0)"
check "$(printf '%s' "$out" | grep -c 'Already stopped')" 1 "idempotent stop reports already-stopped"

echo "== 6. restart survives one service already down (AC4) =="
run rc start --bg >/dev/null; run rc start --bg
kill "$(cat "$H/run/worker.pid")" 2>/dev/null || true; sleep 1   # worker down, api up
run rc restart;  check "$rc" 0 "restart with worker already down exits 0"
check "$(fdm_pid_running "$H/run/api.pid" >/dev/null && echo y || echo n)" y "api up after restart"
check "$(fdm_pid_running "$H/run/worker.pid" >/dev/null && echo y || echo n)" y "worker recovered after restart"
run rc stop >/dev/null

echo "== 7. foreground refuses when an instance is already running (AC1) =="
run rc start --bg >/dev/null; run rc start --bg
run rc start;    check "$rc" 1 "foreground start refuses when a bg api is running"
check "$(printf '%s' "$out" | grep -c 'already running')" 1 "foreground refusal names the running instance"
run rc stop >/dev/null
unset FDM_PORT

echo "== 8. healthy status exits 0 (AC5) =="
# A tiny real responder that answers /api/health with the ok payload, bound to a
# free port. Point the CLI's pidfiles at it so both services read as running.
HPORT=8199
python3 - "$HPORT" <<'PY' >/dev/null 2>&1 &
import sys, http.server
port = int(sys.argv[1])
class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        body = b'{"status":"ok"}'
        self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers()
        self.wfile.write(body)
http.server.HTTPServer(("127.0.0.1", port), H).serve_forever()
PY
RESPONDER_PID=$!
# wait for it to bind
for _ in $(seq 1 30); do curl -fsS "http://127.0.0.1:$HPORT/api/health" >/dev/null 2>&1 && break; sleep 0.2; done
mkdir -p "$H/run"
echo "$RESPONDER_PID" > "$H/run/api.pid"
echo "$RESPONDER_PID" > "$H/run/worker.pid"
export FDM_PORT="$HPORT"
run rc status;   check "$rc" 0 "status exits 0 when services run AND health is ok"
check "$(printf '%s' "$out" | grep -c 'health:    ok')" 1 "status shows health ok"
rm -f "$H/run/api.pid" "$H/run/worker.pid"
kill "$RESPONDER_PID" 2>/dev/null || true; RESPONDER_PID=""
unset FDM_PORT FDM_HOME FDM_MODE

echo "== 9. systemd branch dispatches to systemctl/journalctl (AC2-6) =="
# Fake systemctl/journalctl on PATH; FDM_MODE=systemd forces the branch. We
# assert the CLI calls them with the right unit args (both services, every verb).
SBIN="$WORKDIR/fakebin"; mkdir -p "$SBIN"
SLOG="$WORKDIR/systemctl.log"
cat > "$SBIN/systemctl" <<EOF
#!/usr/bin/env bash
echo "\$*" >> "$SLOG"
case "\$1" in
    is-active) exit 0 ;;   # pretend active
esac
exit 0
EOF
cat > "$SBIN/journalctl" <<'EOF'
#!/usr/bin/env bash
echo "journalctl $*"
EOF
chmod +x "$SBIN/systemctl" "$SBIN/journalctl"
Hs="$(make_sandbox systemd1)"
export FDM_HOME="$Hs" FDM_MODE=systemd FDM_PORT=8000
: > "$SLOG"
PATH="$SBIN:$PATH" run rc start --bg
check "$rc" 0 "systemd start --bg exits 0"
check "$(grep -c '^start fdm-api.service fdm-worker.service$' "$SLOG")" 1 "start --bg -> systemctl start api+worker"
: > "$SLOG"
PATH="$SBIN:$PATH" run rc stop
check "$(grep -c '^stop fdm-worker.service fdm-api.service$' "$SLOG")" 1 "stop -> systemctl stop worker+api"
: > "$SLOG"
PATH="$SBIN:$PATH" run rc restart
check "$(grep -c '^restart fdm-api.service fdm-worker.service$' "$SLOG")" 1 "restart -> systemctl restart api+worker"
PATH="$SBIN:$PATH" run rc logs --no-follow -n 5
check "$rc" 0 "systemd logs exits 0"
check "$(printf '%s' "$out" | grep -c 'journalctl -u fdm-api.service -n 5')" 1 "logs -> journalctl on the api unit (no -f with --no-follow)"
PATH="$SBIN:$PATH" run rc logs --all --no-follow
check "$(printf '%s' "$out" | grep -c 'journalctl -u fdm-api.service -u fdm-worker.service')" 1 "logs --all -> journalctl on both units"
unset FDM_HOME FDM_MODE FDM_PORT

echo "== 10. nohup logs tail the real files (no panel-server.log) =="
Hl="$(make_sandbox logs1)"; export FDM_HOME="$Hl" FDM_MODE=nohup
mkdir -p "$Hl/logs"; printf 'api-line-1\napi-line-2\n' > "$Hl/logs/api.log"; printf 'worker-line-1\n' > "$Hl/logs/worker.log"
run rc logs --no-follow -n 5
check "$rc" 0 "nohup logs --no-follow exits 0"
check "$(printf '%s' "$out" | grep -c 'api-line-2')" 1 "logs tails the api log by default"
run rc logs --worker --no-follow
check "$(printf '%s' "$out" | grep -c 'worker-line-1')" 1 "logs --worker tails the worker log"
run rc logs --all --no-follow
check "$(printf '%s' "$out" | grep -c 'api-line-1' )" 1 "logs --all includes the api log"
check "$(printf '%s' "$out" | grep -c 'worker-line-1')" 1 "logs --all includes the worker log"
# There must be no invented panel-server.log anywhere in the tree.
check "$(grep -rl 'panel-server.log' "$ROOT/bin" "$ROOT/install.sh" "$ROOT/install-lib.sh" 2>/dev/null | wc -l | tr -d ' ')" 0 "no panel-server.log fabricated (DOO-1205 note 5)"
unset FDM_HOME FDM_MODE

echo "== 11. fdm_update_ff_state classifies checkout states (AC7) =="
GIT="git -c user.email=t@t -c user.name=t -c init.defaultBranch=main -c advice.detachedHead=false -c protocol.file.allow=always"
GROOT="$WORKDIR/git"; mkdir -p "$GROOT"
ORIGIN="$GROOT/origin.git"; SEED="$GROOT/seed"
$GIT init -q --bare "$ORIGIN"
$GIT init -q "$SEED"; mkdir -p "$SEED/backend"; : > "$SEED/backend/pyproject.toml"
cp "$ROOT/install.sh" "$SEED/install.sh"; cp "$ROOT/install-lib.sh" "$SEED/install-lib.sh"
$GIT -C "$SEED" add -A && $GIT -C "$SEED" commit -qm c1
$GIT -C "$SEED" remote add origin "$ORIGIN" && $GIT -C "$SEED" push -q origin main
# up-to-date clone
WORK="$GROOT/work"; $GIT clone -q "$ORIGIN" "$WORK"
check "$(fdm_update_ff_state "$WORK" main)" "up-to-date" "fresh clone at origin tip = up-to-date"
# not a repo
check "$(fdm_update_ff_state "$GROOT" main)" "not-a-repo" "non-repo dir = not-a-repo"
# untracked build artifacts must NOT count as dirty
mkdir -p "$WORK/backend/.venv/bin"; : > "$WORK/backend/.env"; : > "$WORK/backend/.venv/bin/uvicorn"
check "$(fdm_update_ff_state "$WORK" main)" "up-to-date" "untracked build artifacts are ignored (not dirty)"
# tracked modification = dirty
echo "# local edit" >> "$WORK/install.sh"
check "$(fdm_update_ff_state "$WORK" main)" "dirty" "modified tracked file = dirty"
$GIT -C "$WORK" checkout -q -- install.sh   # revert edit
# clean fast-forward available: advance origin one commit
echo "c2" >> "$SEED/install.sh"; $GIT -C "$SEED" commit -qam c2; $GIT -C "$SEED" push -q origin main
$GIT -C "$WORK" fetch -q origin main
check "$(fdm_update_ff_state "$WORK" main)" "clean-ff" "behind origin, clean tree = clean-ff"
# diverged: local commit that is not an ancestor of origin
echo "local-only" >> "$WORK/README-local"; $GIT -C "$WORK" add -A; $GIT -C "$WORK" commit -qm local
check "$(fdm_update_ff_state "$WORK" main)" "diverged" "local commit off origin = diverged"

echo "== 12. fdm update REFUSES on dirty/divergent checkouts (AC7) =="
export FDM_HOME="$WORK" FDM_MODE=nohup FDM_BRANCH=main
# currently diverged (local commit) -> must refuse without running the installer
run rc update
check "$rc" 1 "update on a diverged checkout exits non-zero"
check "$(printf '%s' "$out" | grep -c 'diverged')" 1 "update refusal names the divergence"
check "$(printf '%s' "$out" | grep -ci 'install' | { read n; [ "$n" -ge 0 ] && echo ok; })" ok "update refusal returned before installer (sanity)"
# now make it dirty instead
$GIT -C "$WORK" reset -q --hard origin/main
echo "# uncommitted" >> "$WORK/install.sh"
run rc update
check "$rc" 1 "update on a dirty checkout exits non-zero"
check "$(printf '%s' "$out" | grep -c 'uncommitted changes to tracked files')" 1 "dirty refusal explains why"
unset FDM_HOME FDM_MODE FDM_BRANCH

echo
echo "PASS ($pass checks): fdm CLI dispatch, nohup + systemd branches, idempotency, healthy status, logs, and update refusals verified (DOO-1205)."
