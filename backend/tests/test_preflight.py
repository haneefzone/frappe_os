"""Bench-create pre-flight (session 1.7): pure parsers, per-check evaluation,
and the full suite driven over a fake capture — no SSH. The blocking-failure
semantics (uv/node/disk block; mariadb/wkhtmltopdf/ports warn) are the contract
the create orchestration relies on to gate `bench init`."""

import asyncio

from app.core.jobs import CaptureResult
from app.core.preflight import (
    DEFAULT_BENCH_PORTS,
    evaluate_disk,
    evaluate_mariadb,
    evaluate_node,
    evaluate_ports,
    evaluate_uv,
    evaluate_wkhtmltopdf,
    parse_df_avail_bytes,
    parse_node_major,
    parse_snapshot_isolation,
    parse_version_tuple,
    run_preflight,
    sibling_ports_from_benches,
    wkhtmltopdf_is_patched,
)
from app.core.version_matrix import get_entry

# -- pure parsers ------------------------------------------------------------ #


def test_parse_version_tuple():
    assert parse_version_tuple("uv 0.5.11 (abc)") == (0, 5, 11)
    assert parse_version_tuple("mariadb from 11.8.2-MariaDB") == (11, 8, 2)
    assert parse_version_tuple("no numbers here") is None


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
    assert parse_df_avail_bytes("Avail\n10737418240\n") == 10737418240
    assert parse_df_avail_bytes("Avail\n") is None


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
    assert evaluate_disk(0, "Avail\n6000000000\n").status == "pass"
    low = evaluate_disk(0, "Avail\n1000000000\n")
    assert low.status == "fail" and low.blocking
    assert evaluate_disk(0, "Avail\n").status == "fail"


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
                 disk=(0, "Avail\n10737418240\n")):
    async def capture(argv, *, cwd=None, timeout=120.0):
        head = argv[0]
        if head == "uv":
            return CaptureResult(uv[0], uv[1], "")
        if head == "node":
            return CaptureResult(node[0], node[1], "")
        if head == "mariadb":
            if "--version" in argv:
                return CaptureResult(maria[0], maria[1], "")
            return CaptureResult(snapshot[0], snapshot[1], "")  # the snapshot query
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
    report = _run(make_capture(disk=(0, "Avail\n1000000\n")))
    assert report.blocked


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
