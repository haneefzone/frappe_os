"""Bench-create pre-flight (session 1.7): pure parsers, per-check evaluation,
and the full suite driven over a fake capture — no SSH. The blocking-failure
semantics (uv/node/disk block; mariadb/wkhtmltopdf/ports warn) are the contract
the create orchestration relies on to gate `bench init`."""

import asyncio

import pytest

from app.core.discovery import DiscoveryError
from app.core.jobs import CaptureResult
from app.core.preflight import (
    DEFAULT_BENCH_PORTS,
    classify_mariadb_auth,
    disk_argv,
    evaluate_disk,
    evaluate_mariadb,
    evaluate_node,
    evaluate_ports,
    evaluate_uv,
    evaluate_wkhtmltopdf,
    grants_allow_create_database,
    mariadb_admin_auth_argv,
    mariadb_admin_nopw_argv,
    parse_df_avail_bytes,
    parse_mariadb_server_version,
    parse_node_major,
    parse_snapshot_isolation,
    parse_version_tuple,
    probe_mariadb_admin,
    run_preflight,
    sibling_ports_from_benches,
    wkhtmltopdf_is_patched,
)
from app.core.version_matrix import get_entry


def df_pk(avail_kib: int) -> str:
    """A realistic `df -Pk <path>` output whose Available column (field 4, in
    1 KiB units) is `avail_kib`."""
    return (
        "Filesystem     1024-blocks     Used Available Capacity Mounted on\n"
        f"/dev/sda1        102400000 90000000 {avail_kib:>9} 95% /\n"
    )


# -- command builders -------------------------------------------------------- #


def test_disk_argv_builds_df_command():
    # DOO-1189: portable POSIX `df -Pk`, not the GNU-only `--output=avail`.
    assert disk_argv("/home/frappe") == ["df", "-Pk", "/home/frappe"]




def test_disk_argv_rejects_bad_and_dotdot_paths():
    with pytest.raises(DiscoveryError):
        disk_argv("relative/path")
    with pytest.raises(DiscoveryError, match="'\\.\\.'"):  # DOO-107
        disk_argv("/home/frappe/../etc")


# -- pure parsers ------------------------------------------------------------ #


def test_parse_version_tuple():
    assert parse_version_tuple("uv 0.5.11 (abc)") == (0, 5, 11)
    assert parse_version_tuple("mariadb from 11.8.2-MariaDB") == (11, 8, 2)
    assert parse_version_tuple("no numbers here") is None


def test_parse_mariadb_server_version_reads_server_not_client():
    # DOO-1189: the leading number is the *client* tool version; the server is
    # the `Distrib`/`from` token. parse_version_tuple got this wrong (false green).
    assert parse_mariadb_server_version(
        "mariadb  Ver 15.1 Distrib 11.8.8-MariaDB, for debian-linux-gnu"
    ) == (11, 8, 8)
    assert parse_mariadb_server_version(
        "mariadb from 11.8.8-MariaDB, client 15.2 for debian-linux-gnu (x86_64)"
    ) == (11, 8, 8)
    # No marker → fall back to the first dotted token.
    assert parse_mariadb_server_version("mariadb 10.11.6") == (10, 11, 6)


def test_parse_node_major():
    assert parse_node_major("v24.1.0") == 24
    assert parse_node_major("v18.20.4") == 18
    assert parse_node_major("not a version") is None


def test_wkhtmltopdf_is_patched():
    assert wkhtmltopdf_is_patched("wkhtmltopdf 0.12.6.1 (with patched qt)")
    assert not wkhtmltopdf_is_patched("wkhtmltopdf 0.12.6 (without patched qt? no)")
    assert not wkhtmltopdf_is_patched("wkhtmltopdf 0.12.5")


def test_parse_snapshot_isolation():
    assert parse_snapshot_isolation("ON") is True
    assert parse_snapshot_isolation("1\n") is True
    assert parse_snapshot_isolation("OFF") is False
    assert parse_snapshot_isolation("0") is False
    assert parse_snapshot_isolation("") is None


def test_parse_df_avail_bytes():
    # df -Pk reports 1 KiB units; parser returns bytes from field 4 of the row.
    assert parse_df_avail_bytes(df_pk(10 * 1024 * 1024)) == 10 * 1024 * 1024 * 1024
    assert parse_df_avail_bytes("Filesystem 1024-blocks Used Available Capacity Mounted\n") is None
    assert parse_df_avail_bytes("") is None


# -- individual checks ------------------------------------------------------- #

V16 = get_entry("16")


def test_uv_present_passes_missing_fails_and_blocks():
    ok = evaluate_uv(0, "uv 0.5.11", "")
    assert ok.status == "pass" and ok.blocking

    missing = evaluate_uv(127, "", "command not found")
    assert missing.status == "fail" and missing.blocking
    assert "uv" in missing.detail.lower()


def test_node_matches_matrix():
    assert evaluate_node(0, "v24.1.0", V16).status == "pass"
    off = evaluate_node(0, "v18.20.0", V16)
    assert off.status == "fail" and off.blocking
    missing = evaluate_node(127, "", V16)
    assert missing.status == "fail"


def test_node_range_for_v15():
    v15 = get_entry("15")
    assert evaluate_node(0, "v18.0.0", v15).status == "pass"
    assert evaluate_node(0, "v20.9.0", v15).status == "pass"
    assert evaluate_node(0, "v24.0.0", v15).status == "fail"


def test_mariadb_snapshot_isolation_gotcha5():
    # >= 11.6 with snapshot isolation ON -> warn (never blocks bench init).
    warn = evaluate_mariadb(0, "mariadb 11.8.2-MariaDB", snapshot=True)
    assert warn.status == "warn" and not warn.blocking
    assert "snapshot_isolation" in warn.detail

    # >= 11.6 already OFF -> pass.
    assert evaluate_mariadb(0, "mariadb 11.8.2", snapshot=False).status == "pass"
    # couldn't read -> advisory warn.
    assert evaluate_mariadb(0, "mariadb 11.8.2", snapshot=None).status == "warn"
    # < 11.6 -> no snapshot concern.
    assert evaluate_mariadb(0, "mariadb 10.11.6", snapshot=None).status == "pass"
    # missing client -> warn, not a block.
    assert evaluate_mariadb(127, "", snapshot=None).status == "warn"


def test_mariadb_gate_uses_server_version_not_client():
    # DOO-1189 regression: real client output where the *client* is 15.x but the
    # *server* is 11.8 (>= 11.6). The gotcha-#5 gate must fire on the server
    # version — snapshot ON must warn, and the reported version must be 11.8.8,
    # not 15.1. (Pre-fix this parsed 15.1 and never truly checked the server.)
    client_first = "mariadb  Ver 15.1 Distrib 11.8.8-MariaDB, for debian-linux-gnu"
    on = evaluate_mariadb(0, client_first, snapshot=True)
    assert on.status == "warn" and "11.8.8" in on.detail
    off = evaluate_mariadb(0, client_first, snapshot=False)
    assert off.status == "pass" and "11.8.8" in off.detail


def test_wkhtmltopdf_check():
    assert evaluate_wkhtmltopdf(0, "wkhtmltopdf 0.12.6.1 (with patched qt)").status == "pass"
    assert evaluate_wkhtmltopdf(0, "wkhtmltopdf 0.12.6").status == "warn"
    assert evaluate_wkhtmltopdf(127, "").status == "warn"


def test_ports_conflict_is_a_warning():
    assert evaluate_ports(set()).status == "pass"
    conflict = evaluate_ports({DEFAULT_BENCH_PORTS["webserver_port"]})
    assert conflict.status == "warn" and not conflict.blocking
    assert "8000" in conflict.detail


def test_disk_threshold():
    # 6 GiB free (> 5 GiB floor) -> pass.
    assert evaluate_disk(0, df_pk(6 * 1024 * 1024)).status == "pass"
    # ~1 GiB free -> a real host shortfall: blocking fail.
    low = evaluate_disk(0, df_pk(1 * 1024 * 1024))
    assert low.status == "fail" and low.is_blocking_failure


def test_disk_read_error_is_error_status_not_blocking():
    # DOO-1189: an unreadable df is a PROBE error, not a host shortfall — status
    # "error" (distinct from "fail") and it must NOT block bench init.
    err = evaluate_disk(0, "garbage with no avail column")
    assert err.status == "error"
    assert not err.is_blocking_failure
    nonzero = evaluate_disk(1, "")
    assert nonzero.status == "error" and not nonzero.is_blocking_failure


def test_sibling_ports_from_benches():
    class B:
        def __init__(self, **k):
            for name in (
                "webserver_port", "socketio_port", "redis_cache_port",
                "redis_queue_port", "redis_socketio_port", "file_watcher_port",
            ):
                setattr(self, name, k.get(name))

    rows = [B(webserver_port=8000, redis_queue_port=11000), B(socketio_port=9000)]
    assert sibling_ports_from_benches(rows) == {8000, 11000, 9000}


# -- full suite over a fake capture ------------------------------------------ #


def make_capture(*, uv=(0, "uv 0.5.11"), node=(0, "v24.1.0"), maria=(0, "mariadb 10.11.6"),
                 snapshot=(0, ""), wk=(0, "wkhtmltopdf 0.12.6.1 (with patched qt)"),
                 disk=None, seen=None):
    if disk is None:
        disk = (0, df_pk(10 * 1024 * 1024))

    async def capture(argv, *, cwd=None, timeout=120.0):
        if seen is not None:
            seen.append(list(argv))
        # DOO-1208: probes now pass *bare* argv (`["node","--version"]`); the SSH
        # exec layer (SSHService._wrap_command) is what runs every command through
        # the login shell, so probe and real `bench init` resolve identically. The
        # legacy `["bash","-lc",<cmd>]` shape is still unwrapped here for safety.
        cmd = argv[2] if argv[:2] == ["bash", "-lc"] else " ".join(argv)
        head = cmd.split()[0] if cmd.split() else ""
        if head == "uv":
            return CaptureResult(uv[0], uv[1], "")
        if head == "node":
            return CaptureResult(node[0], node[1], "")
        if head == "mariadb":
            if "innodb_snapshot_isolation" in cmd:
                return CaptureResult(snapshot[0], snapshot[1], "")  # the snapshot query
            return CaptureResult(maria[0], maria[1], "")
        if head == "wkhtmltopdf":
            return CaptureResult(wk[0], wk[1], "")
        if head == "df":
            return CaptureResult(disk[0], disk[1], "")
        raise AssertionError(f"unexpected probe: {argv}")

    return capture


def _run(capture, **kw):
    kw.setdefault("frappe_major", "16")
    kw.setdefault("path", "/home/frappe")
    kw.setdefault("sibling_ports", set())
    return asyncio.run(run_preflight(capture, **kw))


def test_run_preflight_all_clear():
    report = _run(make_capture())
    assert not report.blocked
    assert not report.has_warnings
    keys = {c.key for c in report.checks}
    assert keys == {"uv", "node", "mariadb", "wkhtmltopdf", "ports", "disk"}


def test_run_preflight_uv_missing_blocks():
    report = _run(make_capture(uv=(127, "")))
    assert report.blocked
    uv = next(c for c in report.checks if c.key == "uv")
    assert uv.status == "fail" and uv.is_blocking_failure


def test_run_preflight_low_disk_blocks():
    report = _run(make_capture(disk=(0, df_pk(1 * 1024 * 1024))))  # ~1 GiB
    assert report.blocked


def test_run_preflight_probes_are_bare_argv_login_wrap_is_the_exec_layer():
    # DOO-1208: the probes hand the exec layer *bare* argv and let
    # SSHService._wrap_command apply the login shell, so a probe resolves its
    # interpreter through the exact same wrapper as the `bench init` it clears.
    # Pre-flight must NOT pre-wrap in `bash -lc` itself — that split wrapping was
    # the divergence that let pre-flight pass while the real command failed. (The
    # login-shell wrapping itself is asserted at the exec layer in
    # test_server_ssh.py::test_wrap_command_runs_bench_through_login_shell.)
    seen: list[list[str]] = []
    _run(make_capture(seen=seen))
    assert ["uv", "--version"] in seen
    assert ["node", "--version"] in seen
    assert ["wkhtmltopdf", "--version"] in seen
    assert ["mariadb", "--version"] in seen
    assert ["df", "-Pk", "/home/frappe"] in seen
    # No probe smuggles its own `bash -lc` wrapper — that belongs to the exec layer.
    assert not any(argv[:2] == ["bash", "-lc"] for argv in seen)


def test_run_preflight_node_via_nvm_not_falsely_blocked():
    # DOO-1189 headline regression: the login shell surfaces the nvm node (v24),
    # so v16 is NOT blocked. (The incident: a non-login shell saw system node 20
    # and blocked bench init, which read as "required apps not installing".)
    report = _run(make_capture(node=(0, "v24.18.0")), frappe_major="16")
    node = next(c for c in report.checks if c.key == "node")
    assert node.status == "pass"
    assert not report.blocked


def test_run_preflight_disk_read_error_does_not_block():
    # A broken df probe surfaces as a non-blocking "error", not a false block.
    report = _run(make_capture(disk=(1, "")))
    assert not report.blocked
    assert report.has_errors
    disk = next(c for c in report.checks if c.key == "disk")
    assert disk.status == "error"


def test_run_preflight_mariadb_server_version_drives_gate():
    # Client 15.1 / server 11.8.8 with snapshot ON -> a (non-blocking) warning
    # naming the SERVER version. Pre-fix this parsed 15.1 and skipped the check.
    report = _run(make_capture(
        maria=(0, "mariadb  Ver 15.1 Distrib 11.8.8-MariaDB, for debian-linux-gnu"),
        snapshot=(0, "ON"),
    ))
    maria = next(c for c in report.checks if c.key == "mariadb")
    assert maria.status == "warn" and "11.8.8" in maria.detail
    assert not report.blocked


def test_run_preflight_warnings_do_not_block():
    # wkhtmltopdf unpatched + a port conflict = warnings only.
    report = _run(
        make_capture(wk=(0, "wkhtmltopdf 0.12.6")),
        sibling_ports={DEFAULT_BENCH_PORTS["webserver_port"]},
    )
    assert not report.blocked
    assert report.has_warnings


def test_run_preflight_queries_snapshot_isolation_when_mariadb_new():
    # MariaDB 11.8 with snapshot isolation ON -> a (non-blocking) warning, and
    # the suite still isn't blocked.
    report = _run(make_capture(maria=(0, "mariadb 11.8.2-MariaDB"), snapshot=(0, "ON")))
    maria = next(c for c in report.checks if c.key == "mariadb")
    assert maria.status == "warn"
    assert not report.blocked


# -- MariaDB admin-login probe (DOO-1237) ------------------------------------ #
#
# `bench new-site` creates the site DB via pymysql over TCP as root. DOO-1232
# proved three distinct outcomes on the target: (a) 1045 wrong/no valid
# password, (b) 1698 unix_socket-only account unreachable over TCP, (c) the
# probe can't run at all. The probe must name (a)/(b) as blocking fails and
# treat (c) as a non-blocking "error" (the df read-error rule), never gating a
# create on a broken/absent client.

# A realistic `SHOW GRANTS` line for a full admin (what a healthy root returns).
GRANT_ALL = "GRANT ALL PRIVILEGES ON *.* TO `root`@`%` IDENTIFIED BY PASSWORD '*abc'"
# Error strings the mariadb client prints (code appears in the message).
ERR_1698 = "ERROR 1698 (28000): Access denied for user 'root'@'localhost'"
ERR_1045 = "ERROR 1045 (28000): Access denied for user 'root'@'localhost' (using password: YES)"
ERR_2002 = "ERROR 2002 (HY000): Can't connect to server on '127.0.0.1'"


def test_mariadb_admin_argv_connects_over_tcp_with_password():
    # Password is `-p<pw>` (no space), its own argv element; TCP host/port are
    # explicit so the probe mirrors pymysql, not the socket default.
    argv = mariadb_admin_auth_argv("root", "s3cret", host="127.0.0.1", port=3306)
    assert argv[0] == "mariadb"
    assert "-h" in argv and "127.0.0.1" in argv
    assert "-P" in argv and "3306" in argv
    assert "-proot".replace("root", "s3cret") == "-ps3cret"
    assert "-ps3cret" in argv
    assert "SHOW GRANTS FOR CURRENT_USER()" in argv
    # No `bash -lc` wrapper — that belongs to the exec layer (DOO-1208).
    assert argv[:2] != ["bash", "-lc"]


def test_mariadb_admin_nopw_argv_has_no_password_flag():
    argv = mariadb_admin_nopw_argv("root")
    assert not any(a.startswith("-p") for a in argv)  # an empty -p would prompt/hang
    assert "SELECT 1" in argv


def test_classify_mariadb_auth_states():
    assert classify_mariadb_auth(0, "GRANT ...", "") == "ok"
    assert classify_mariadb_auth(1, "", ERR_1698) == "socket_plugin"
    assert classify_mariadb_auth(1, "", ERR_1045) == "denied"
    # Anything without a 1045/1698 verdict → cannot_run (never gates a create).
    assert classify_mariadb_auth(1, "", ERR_2002) == "cannot_run"
    assert classify_mariadb_auth(127, "", "mariadb: command not found") == "cannot_run"


def test_grants_allow_create_database():
    assert grants_allow_create_database(GRANT_ALL)
    assert grants_allow_create_database("GRANT SELECT, CREATE, DROP ON *.* TO `x`@`%`")
    # CREATE VIEW / CREATE ROUTINE are NOT the CREATE DATABASE privilege.
    assert not grants_allow_create_database("GRANT SELECT, CREATE VIEW ON *.* TO `x`@`%`")
    # CREATE on a single schema is not the global privilege bench needs.
    assert not grants_allow_create_database("GRANT ALL PRIVILEGES ON `mydb`.* TO `x`@`%`")
    assert not grants_allow_create_database("")


def _admin_capture(*, auth, nopw=None):
    """Fake capture for the admin probe: `auth` is the (exit, stdout, stderr) the
    password connection returns; `nopw` (optional) the no-password re-probe."""
    async def capture(argv, *, cwd=None, timeout=120.0):
        query = argv[-1]
        if query == "SELECT 1":  # the no-password disambiguation probe
            assert nopw is not None, "no-password probe not expected"
            return CaptureResult(*nopw)
        return CaptureResult(*auth)
    return capture


def _probe(**kw):
    return asyncio.run(probe_mariadb_admin(**kw))


def test_probe_admin_pass_when_authenticated_with_create():
    r = _probe(capture=_admin_capture(auth=(0, GRANT_ALL, "")), password="pw")
    assert r.key == "mariadb_admin" and r.status == "pass"
    assert not r.is_blocking_failure and r.blocking


def test_probe_admin_authenticated_but_no_create_priv_blocks():
    r = _probe(
        capture=_admin_capture(auth=(0, "GRANT SELECT ON *.* TO `root`@`%`", "")),
        password="pw",
    )
    assert r.status == "fail" and r.is_blocking_failure
    assert "CREATE DATABASE" in r.detail


def test_probe_admin_socket_plugin_1698_blocks_state_b():
    # (b) unix_socket-only account: 1698 straight off the password probe.
    r = _probe(capture=_admin_capture(auth=(1, "", ERR_1698)), password="pw")
    assert r.status == "fail" and r.is_blocking_failure
    assert "1698" in r.detail and "unix_socket" in r.detail
    assert "IDENTIFIED VIA mysql_native_password" in r.detail  # actionable remedy


def test_probe_admin_wrong_password_1045_blocks_state_a():
    # (a) wrong password: 1045 with the password AND 1045 without it (a real
    # password account rejecting the value) → "wrong password", not unix_socket.
    r = _probe(
        capture=_admin_capture(auth=(1, "", ERR_1045), nopw=(1, "", ERR_1045)),
        password="wrong",
    )
    assert r.status == "fail" and r.is_blocking_failure
    assert "1045" in r.detail and "password" in r.detail.lower()


def test_probe_admin_1045_with_password_but_1698_without_is_socket_plugin():
    # DOO-1232's exact box: password probe → 1045, no-password probe → 1698. The
    # account is unix_socket-only; report state (b), not "wrong password".
    r = _probe(
        capture=_admin_capture(auth=(1, "", ERR_1045), nopw=(1, "", ERR_1698)),
        password="root",
    )
    assert r.status == "fail" and r.is_blocking_failure
    assert "1698" in r.detail and "unix_socket" in r.detail


def test_probe_admin_cannot_run_is_error_not_blocking_state_c():
    # (c) connection refused / client missing → non-blocking "error" (never gates
    # a create), same shape as the df read-error fix.
    r = _probe(capture=_admin_capture(auth=(1, "", ERR_2002)), password="pw")
    assert r.status == "error"
    assert not r.is_blocking_failure
    r2 = _probe(capture=_admin_capture(auth=(127, "", "command not found")), password="pw")
    assert r2.status == "error" and not r2.is_blocking_failure


def test_probe_admin_capture_exception_is_error_not_blocking():
    async def boom(argv, *, cwd=None, timeout=120.0):
        raise RuntimeError("ssh channel died")
    r = asyncio.run(probe_mariadb_admin(boom, password="pw"))
    assert r.status == "error" and not r.is_blocking_failure
