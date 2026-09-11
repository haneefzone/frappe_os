"""Tests for the tool installer (session 6.1).

Covers the pure version parser and matrix resolver (including the two gotchas
the product exists to encode: #2 `uv` is required for *every* Frappe version,
and #6 wkhtmltopdf must be the patched-Qt 0.12.6.1), the safety invariants over
the command templates, the scan/install actions against a mocked SSH executor,
and the API surface + RBAC.

Root-needing installs are covered here by mocked SSH only — the current test
target has no sudo (see DOO-128), so `sudo -n` on a real box is out of reach in
this session. The mocked coverage asserts the *shape* of what would run: that
every root install goes through an argv that maps 1:1 onto a line in the
`docs/implementation-plan.md` allowlist, and in particular that wkhtmltopdf
never hands root a bench-user-writable path.
"""

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.routes.jobs import get_job_runner
from app.core.commands import get_template
from app.core.commands.registry import NODE_MAJORS
from app.core.jobs import CaptureResult, InMemoryJobBackend, JobRunner
from app.core.toolinventory import build_context, list_server_tools
from app.core.tools import (
    ANY,
    GROUP_AI,
    GROUP_CORE,
    GROUP_DEV,
    TOOL_DEFINITIONS,
    TOOL_GROUPS,
    TOOL_STATUSES,
    TOOLS_BY_ID,
    Requirement,
    assess,
    format_version,
    frappe_major,
    highest_frappe_major,
    parse_version,
    resolve_requirements,
)
from app.db import Base
from app.models.bench import Bench
from app.models.server import Server
from app.models.server_tool import ServerTool
from app.models.settings import PlatformSettings
from tests.conftest import csrf_headers, login


@pytest.fixture
def sf():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    yield factory
    engine.dispose()


def _seed_server(db, *, frappe_version: str | None = "16.24.0", benches: int = 1) -> int:
    server = Server(name="vm", hostname="203.0.113.7")
    db.add(server)
    db.flush()
    for i in range(benches):
        db.add(
            Bench(
                server_id=server.id,
                name=f"bench-{i}",
                path=f"/home/frappe/bench-{i}",
                frappe_version=frappe_version,
            )
        )
    db.commit()
    return server.id


# --------------------------------------------------------------------------- #
# Version parsing
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "banner,expected",
    [
        ("git version 2.43.0", (2, 43, 0)),
        ("Python 3.14.1", (3, 14, 1)),
        ("v24.1.0", (24, 1, 0)),
        ("jq-1.7", (1, 7)),
        ("Redis server v=7.0.15 sha=00000000:0", (7, 0, 15)),
        ("mariadb from 11.8.2-MariaDB, client 15.2 for debian-linux-gnu", (11, 8, 2)),
        ("nginx version: nginx/1.24.0", (1, 24, 0)),
        # The one that matters: the patched-Qt build carries a 4th component.
        ("wkhtmltopdf 0.12.6.1 (with patched qt)", (0, 12, 6, 1)),
        ("uv 0.5.11", (0, 5, 11)),
    ],
)
def test_parse_version_handles_every_tool_banner(banner, expected):
    assert parse_version(banner) == expected


def test_parse_version_returns_none_for_unparseable():
    assert parse_version("") is None
    assert parse_version(None) is None
    assert parse_version("command not found") is None


def test_format_version_roundtrips():
    assert format_version((0, 12, 6, 1)) == "0.12.6.1"


# --------------------------------------------------------------------------- #
# Requirement semantics
# --------------------------------------------------------------------------- #


def test_ceiling_compares_at_its_own_precision():
    """A ceiling of (18,) accepts every 18.x and rejects 20.x."""
    req = Requirement("16–18", min_version=(16,), max_version=(18,))
    assert req.satisfied_by((18, 20, 4))
    assert req.satisfied_by((16, 0, 0))
    assert not req.satisfied_by((20, 0, 0))
    assert not req.satisfied_by((14, 0, 0))


def test_ceiling_at_minor_precision():
    req = Requirement("3.11–3.12", min_version=(3, 11), max_version=(3, 12))
    assert req.satisfied_by((3, 12, 7))
    assert not req.satisfied_by((3, 13, 0))
    assert not req.satisfied_by((3, 10, 9))


def test_any_requirement_accepts_everything():
    assert ANY.satisfied_by((0, 0, 1))


# --------------------------------------------------------------------------- #
# Frappe major resolution
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "raw,expected",
    [("16.24.0", "16"), ("v15", "15"), ("15.x", "15"), (None, None), ("", None)],
)
def test_frappe_major(raw, expected):
    assert frappe_major(raw) == expected


def test_highest_major_wins_because_the_toolchain_is_shared():
    """A v16 bench beside a v14 bench still needs Node 24."""
    assert highest_frappe_major(["14.0.0", "16.24.0", "15.1.0"]) == "16"


def test_highest_major_of_no_benches_is_none():
    assert highest_frappe_major([]) is None
    assert highest_frappe_major([None]) is None


# --------------------------------------------------------------------------- #
# Matrix resolution
# --------------------------------------------------------------------------- #


def test_v16_row_matches_claude_md_matrix():
    req = resolve_requirements("16")
    assert req["python"].satisfied_by((3, 14, 0))
    assert not req["python"].satisfied_by((3, 12, 0))
    assert req["node"].satisfied_by((24, 3, 0))
    assert not req["node"].satisfied_by((20, 0, 0))
    assert req["mariadb"].satisfied_by((11, 8, 2))
    assert not req["mariadb"].satisfied_by((10, 5, 0))


def test_v14_and_v15_rows_match_claude_md_matrix():
    v14 = resolve_requirements("14")
    assert v14["python"].satisfied_by((3, 10, 12))
    assert not v14["python"].satisfied_by((3, 11, 0))
    assert v14["node"].satisfied_by((16, 20, 0))
    assert v14["node"].satisfied_by((18, 19, 0))
    assert not v14["node"].satisfied_by((20, 0, 0))

    v15 = resolve_requirements("15")
    assert v15["python"].satisfied_by((3, 11, 9))
    assert v15["python"].satisfied_by((3, 12, 1))
    assert not v15["python"].satisfied_by((3, 14, 0))
    assert v15["node"].satisfied_by((20, 11, 0))
    assert not v15["node"].satisfied_by((24, 0, 0))


def test_mariadb_recommendation_is_advice_not_a_floor():
    """10.11 is supported on v16; only <10.6 is non-compliant (11.8 is advice)."""
    req = resolve_requirements("16")["mariadb"]
    assert req.satisfied_by((10, 11, 0))
    assert "11.8" in req.label  # the recommendation still reaches the UI chip


def test_settings_override_replaces_the_shipped_rule_with_an_open_floor():
    req = resolve_requirements("16", {"16": {"node": "26"}})
    assert req["node"].satisfied_by((26, 0, 0))
    # An override must not impose a ceiling — it is an escape hatch for a fleet
    # that moved ahead of the matrix.
    assert req["node"].satisfied_by((28, 1, 0))
    assert not req["node"].satisfied_by((24, 0, 0))


def test_override_for_another_major_is_ignored():
    req = resolve_requirements("16", {"15": {"node": "26"}})
    assert req["node"].satisfied_by((24, 0, 0))


def test_unparseable_override_is_ignored_rather_than_crashing():
    req = resolve_requirements("16", {"16": {"node": "latest"}})
    assert req["node"].satisfied_by((24, 0, 0))


# --------------------------------------------------------------------------- #
# assess() — the four statuses, and the two gotchas
# --------------------------------------------------------------------------- #


def _assess(tool_id, detected, major="16", *, has_bench=True):
    return assess(
        TOOLS_BY_ID[tool_id],
        detected,
        resolve_requirements(major),
        has_bench=has_bench,
        major_known=major is not None,
    )


def test_gotcha_6_distro_wkhtmltopdf_is_outdated():
    """0.12.6 (distro, unpatched Qt) must NOT pass as current; 0.12.6.1 must."""
    assert _assess("wkhtmltopdf", "0.12.6").status == "outdated"
    assert _assess("wkhtmltopdf", "0.12.6.1").status == "ok"


def test_gotcha_6_holds_on_every_matrix_row():
    for major in ("14", "15", "16"):
        assert _assess("wkhtmltopdf", "0.12.6", major).status == "outdated"


def test_gotcha_2_missing_uv_is_missing_on_any_server_with_a_bench():
    """`uv` is required by the bench CLI for EVERY Frappe version, including
    v14/v15 — and even when the matrix row cannot be resolved."""
    for major in ("14", "15", "16"):
        assert _assess("uv", None, major).status == "missing"
    # No Frappe version recorded, but benches exist -> still a hard `missing`,
    # never softened to `unknown`.
    verdict = assess(
        TOOLS_BY_ID["uv"],
        None,
        resolve_requirements(None),
        has_bench=True,
        major_known=False,
    )
    assert verdict.status == "missing"


def test_uv_is_flagged_critical_in_the_registry():
    assert TOOLS_BY_ID["uv"].critical is True


def test_version_dependent_tools_are_unknown_without_a_bench():
    """Claiming Node 20 is correct without knowing the Frappe version would be a
    false green."""
    for tool_id in ("python3", "node", "mariadb"):
        verdict = assess(
            TOOLS_BY_ID[tool_id],
            "20.0.0",
            resolve_requirements(None),
            has_bench=False,
            major_known=False,
        )
        assert verdict.status == "unknown", tool_id
        assert verdict.recommended_version == "unknown"


def test_version_independent_tools_are_still_judged_without_a_bench():
    verdict = assess(
        TOOLS_BY_ID["wkhtmltopdf"],
        "0.12.6",
        resolve_requirements(None),
        has_bench=False,
        major_known=False,
    )
    assert verdict.status == "outdated"


def test_missing_and_unparseable_versions():
    assert _assess("git", None).status == "missing"
    assert _assess("git", "not-a-version").status == "unknown"


def test_every_status_is_in_the_declared_vocabulary():
    for tool in TOOL_DEFINITIONS:
        for detected in (None, "1.0.0", "0.12.6.1"):
            verdict = _assess(tool.tool_id, detected)
            assert verdict.status in TOOL_STATUSES


# --------------------------------------------------------------------------- #
# Registry invariants (golden rule 1 + rule 4)
# --------------------------------------------------------------------------- #


def test_registry_covers_the_issue_spec_groups():
    core = {t.tool_id for t in TOOL_DEFINITIONS if t.group == GROUP_CORE}
    assert core == {
        "git", "python3", "uv", "node", "mariadb", "redis-server",
        "wkhtmltopdf", "bench", "nginx", "supervisor",
    }
    dev = {t.tool_id for t in TOOL_DEFINITIONS if t.group == GROUP_DEV}
    assert dev == {"code-server", "gh", "htop", "jq"}
    ai = {t.tool_id for t in TOOL_DEFINITIONS if t.group == GROUP_AI}
    assert ai == {"claude-code"}


def test_tool_ids_are_unique_and_groups_are_declared():
    ids = [t.tool_id for t in TOOL_DEFINITIONS]
    assert len(ids) == len(set(ids))
    for tool in TOOL_DEFINITIONS:
        assert tool.group in TOOL_GROUPS


def test_every_install_action_resolves_to_a_registered_template():
    for tool in TOOL_DEFINITIONS:
        if tool.install_action:
            assert get_template(tool.install_action) is not None, tool.tool_id


def test_install_templates_are_per_tool_never_generic():
    """One template per tool — no shared/generic 'run this installer' action."""
    actions = [t.install_action for t in TOOL_DEFINITIONS if t.install_action]
    assert len(actions) == len(set(actions))


def test_install_templates_serialise_per_server_and_never_auto_retry():
    for tool in TOOL_DEFINITIONS:
        if not tool.install_action:
            continue
        tpl = get_template(tool.install_action)
        # Rule 4: two installs on one server must never run concurrently —
        # package managers take their own locks and fail hard.
        assert tpl.requires_lock is True, tool.tool_id
        assert tpl.lock_class == "tools", tool.tool_id
        # A half-finished package install replayed blind corrupts dpkg.
        assert tpl.idempotent is False, tool.tool_id


def test_detect_only_tools_have_no_install_template():
    """A database major upgrade and a system-interpreter swap are not buttons."""
    for tool_id in ("python3", "mariadb"):
        assert TOOLS_BY_ID[tool_id].install_action is None
        assert TOOLS_BY_ID[tool_id].note  # the UI explains why


def test_userspace_installs_never_use_sudo():
    """uv/node/bench/gh/code-server/claude-code must run as the bench owner."""
    for tool in TOOL_DEFINITIONS:
        if not tool.install_action or tool.needs_root:
            continue
        argv = get_template(tool.install_action).argv
        assert "sudo" not in " ".join(argv), tool.tool_id


def test_root_installs_map_onto_explicit_allowlist_lines():
    """Every root install is `sudo -n <absolute path> ...` with no wildcard."""
    for tool in TOOL_DEFINITIONS:
        if not tool.install_action or not tool.needs_root:
            continue
        argv = get_template(tool.install_action).argv
        assert argv[0] == "sudo" and argv[1] == "-n", tool.tool_id
        assert argv[2].startswith("/"), tool.tool_id
        assert "*" not in " ".join(argv), tool.tool_id


def test_wkhtmltopdf_never_hands_root_a_bench_writable_path():
    """DOO-255 regression guard.

    Downloading the .deb as the bench user and then running `sudo dpkg -i` on it
    is a TOCTOU root escalation: a .deb's maintainer scripts run as root, so
    dpkg on a file the caller can rewrite is arbitrary root code execution. The
    install must go through the fixed root-owned wrapper, which pins the URL and
    digest itself and stages into a root-only directory.
    """
    argv = get_template("tool.install_wkhtmltopdf").argv
    assert argv == ("sudo", "-n", "/usr/local/sbin/fdm-wkhtmltopdf", "install")
    joined = " ".join(argv)
    assert "dpkg" not in joined
    assert "$HOME" not in joined and "/home/" not in joined


def test_node_major_is_an_enum_of_matrix_sanctioned_versions():
    assert NODE_MAJORS == ("16", "18", "20", "24")
    tpl = get_template("tool.install_node")
    (param,) = tpl.params
    assert param.name == "node_major"
    assert set(param.enum) == set(NODE_MAJORS)


def test_scan_template_is_read_only_lock_free_and_retryable():
    tpl = get_template("server.scan_tools")
    assert tpl.requires_lock is False
    assert tpl.idempotent is True


# --------------------------------------------------------------------------- #
# Actions against a mocked SSH executor
# --------------------------------------------------------------------------- #


class ToolExecutor:
    """Fake SSH executor scripted for the scan/install actions.

    `installed` maps a tool's detect binary to the version banner it prints;
    anything absent from the map exits 127 like a real missing binary.
    """

    def __init__(self, installed: dict[str, str], *, install_exit: int = 0):
        self.installed = dict(installed)
        self.install_exit = install_exit
        self.streamed: list[list[str]] = []

    async def capture(self, argv, *, cwd=None, timeout=120.0):
        binary = argv[0]
        if binary in self.installed:
            return CaptureResult(0, self.installed[binary], "")
        return CaptureResult(127, "", f"{binary}: command not found")

    async def run(self, argv, *, cwd, run_as, on_line, cancel_check):
        self.streamed.append(list(argv))
        return self.install_exit


def _factory(executor):
    class _CM:
        async def __aenter__(self):
            return executor

        async def __aexit__(self, *exc):
            return False

    return lambda server: _CM()


def _run(sf, server_id, action, params, executor):
    runner = JobRunner(sf, InMemoryJobBackend(), enqueue=lambda job: None)
    with sf() as db:
        job = runner.create(
            db,
            action_name=action,
            server_id=server_id,
            target_type="server",
            target_id=None,
            params=params,
            priority="default",
            created_by=None,
        )
        job_id = job.id
    runner.run_job(job_id, executor_factory=_factory(executor))
    return job_id


# A realistic v16 box: the acceptance target's stack.
V16_BOX = {
    "git": "git version 2.43.0",
    "python3": "Python 3.14.1",
    "uv": "uv 0.5.11",
    "node": "v24.3.0",
    "mariadb": "mariadb from 11.8.2-MariaDB, client 15.2 for debian-linux-gnu",
    "redis-server": "Redis server v=7.0.15 sha=00000000:0",
    "bench": "5.25.9",
    "nginx": "nginx version: nginx/1.24.0",
    "supervisord": "3.4.0",
    "jq": "jq-1.7",
}


def test_scan_persists_a_verdict_for_every_registered_tool(sf):
    with sf() as db:
        server_id = _seed_server(db, frappe_version="16.24.0")

    _run(sf, server_id, "server.scan_tools", {}, ToolExecutor(V16_BOX))

    with sf() as db:
        rows = {r.tool_id: r for r in list_server_tools(db, server_id)}
        assert set(rows) == {t.tool_id for t in TOOL_DEFINITIONS}
        # Present and matching the v16 row.
        assert rows["node"].detected_version == "24.3.0"
        assert rows["node"].status == "ok"
        assert rows["python3"].status == "ok"
        assert rows["mariadb"].status == "ok"
        assert rows["uv"].status == "ok"
        # Absent from the box.
        assert rows["wkhtmltopdf"].status == "missing"
        assert rows["gh"].status == "missing"
        assert rows["claude-code"].status == "missing"
        assert rows["htop"].detected_version is None
        for row in rows.values():
            assert row.last_checked_at is not None


def test_scan_flags_an_outdated_node_against_the_v16_row(sf):
    with sf() as db:
        server_id = _seed_server(db, frappe_version="16.24.0")
    box = dict(V16_BOX, node="v20.11.0")

    _run(sf, server_id, "server.scan_tools", {}, ToolExecutor(box))

    with sf() as db:
        rows = {r.tool_id: r for r in list_server_tools(db, server_id)}
        assert rows["node"].status == "outdated"
        assert rows["node"].detected_version == "20.11.0"
        assert "24" in rows["node"].recommended_version


def test_scan_flags_the_distro_wkhtmltopdf_as_outdated(sf):
    """Gotcha #6, end to end through a real scan."""
    with sf() as db:
        server_id = _seed_server(db)
    box = dict(V16_BOX, wkhtmltopdf="wkhtmltopdf 0.12.6")

    _run(sf, server_id, "server.scan_tools", {}, ToolExecutor(box))

    with sf() as db:
        rows = {r.tool_id: r for r in list_server_tools(db, server_id)}
        assert rows["wkhtmltopdf"].status == "outdated"
        assert rows["wkhtmltopdf"].recommended_version == "0.12.6.1 (patched Qt)"


def test_scan_flags_missing_uv_on_a_v14_server(sf):
    """Gotcha #2, end to end: a v14 box still needs uv."""
    with sf() as db:
        server_id = _seed_server(db, frappe_version="14.0.0")
    box = {k: v for k, v in V16_BOX.items() if k != "uv"}
    box["python3"] = "Python 3.10.12"
    box["node"] = "v18.19.0"

    _run(sf, server_id, "server.scan_tools", {}, ToolExecutor(box))

    with sf() as db:
        rows = {r.tool_id: r for r in list_server_tools(db, server_id)}
        assert rows["uv"].status == "missing"
        # ...and the v14 row is what judged the rest.
        assert rows["python3"].status == "ok"
        assert rows["node"].status == "ok"


def test_scan_is_an_upsert_not_an_append(sf):
    with sf() as db:
        server_id = _seed_server(db)

    _run(sf, server_id, "server.scan_tools", {}, ToolExecutor(V16_BOX))
    _run(sf, server_id, "server.scan_tools", {}, ToolExecutor(dict(V16_BOX, node="v24.4.0")))

    with sf() as db:
        rows = db.scalars(
            select(ServerTool).where(ServerTool.server_id == server_id)
        ).all()
        assert len(rows) == len(TOOL_DEFINITIONS)
        node = next(r for r in rows if r.tool_id == "node")
        assert node.detected_version == "24.4.0"


def test_scan_reports_unknown_for_version_dependent_tools_without_a_bench(sf):
    with sf() as db:
        server_id = _seed_server(db, benches=0)

    _run(sf, server_id, "server.scan_tools", {}, ToolExecutor(V16_BOX))

    with sf() as db:
        rows = {r.tool_id: r for r in list_server_tools(db, server_id)}
        assert rows["node"].status == "unknown"
        assert rows["python3"].status == "unknown"
        # Version-independent rules still apply.
        assert rows["git"].status == "ok"


def test_userspace_install_runs_the_pinned_script_and_reverifies(sf):
    """The acceptance path: a no-sudo install into $HOME, then re-detect."""
    with sf() as db:
        server_id = _seed_server(db)
    # gh is absent before the install and present after it.
    executor = ToolExecutor(dict(V16_BOX))

    class InstallingExecutor(ToolExecutor):
        async def run(self, argv, *, cwd, run_as, on_line, cancel_check):
            self.streamed.append(list(argv))
            self.installed["gh"] = "gh version 2.63.2 (2026-01-01)"
            return 0

    executor = InstallingExecutor(dict(V16_BOX))
    _run(sf, server_id, "tool.install_gh", {}, executor)

    (argv,) = executor.streamed
    assert "sudo" not in " ".join(argv)  # userspace, no root
    assert argv[0] == "bash"
    with sf() as db:
        row = db.scalars(
            select(ServerTool).where(
                ServerTool.server_id == server_id, ServerTool.tool_id == "gh"
            )
        ).one()
        assert row.detected_version == "2.63.2"
        assert row.status == "ok"


def test_install_fails_loudly_when_the_tool_is_still_absent_afterwards(sf):
    """A green exit code with no binary is a failure, not a success."""
    from app.models.job import CommandJob

    with sf() as db:
        server_id = _seed_server(db)
    executor = ToolExecutor(dict(V16_BOX))  # `gh` never appears

    job_id = _run(sf, server_id, "tool.install_gh", {}, executor)

    with sf() as db:
        job = db.get(CommandJob, job_id)
        assert job.status == "failure"


def test_install_fails_when_the_installer_exits_nonzero(sf):
    from app.models.job import CommandJob

    with sf() as db:
        server_id = _seed_server(db)
    executor = ToolExecutor(dict(V16_BOX), install_exit=1)

    job_id = _run(sf, server_id, "tool.install_gh", {}, executor)

    with sf() as db:
        assert db.get(CommandJob, job_id).status == "failure"


def test_root_install_renders_the_exact_allowlisted_argv(sf):
    """Reduced-fidelity coverage for the root path (no sudo on the test target).

    Asserts the argv that *would* reach the box is the one the sudoers allowlist
    names — one package per line, absolute binary path, no wildcard.
    """
    with sf() as db:
        server_id = _seed_server(db)

    class RootInstallExecutor(ToolExecutor):
        async def run(self, argv, *, cwd, run_as, on_line, cancel_check):
            self.streamed.append(list(argv))
            self.installed["htop"] = "htop 3.2.2"
            return 0

    executor = RootInstallExecutor(dict(V16_BOX))
    _run(sf, server_id, "tool.install_htop", {}, executor)

    (argv,) = executor.streamed
    assert argv == ["sudo", "-n", "/usr/bin/apt-get", "install", "-y", "htop"]


def test_wkhtmltopdf_install_goes_through_the_root_owned_wrapper(sf):
    with sf() as db:
        server_id = _seed_server(db)

    class WrapperExecutor(ToolExecutor):
        async def run(self, argv, *, cwd, run_as, on_line, cancel_check):
            self.streamed.append(list(argv))
            self.installed["wkhtmltopdf"] = "wkhtmltopdf 0.12.6.1 (with patched qt)"
            return 0

    executor = WrapperExecutor(dict(V16_BOX))
    _run(sf, server_id, "tool.install_wkhtmltopdf", {}, executor)

    (argv,) = executor.streamed
    assert argv == ["sudo", "-n", "/usr/local/sbin/fdm-wkhtmltopdf", "install"]
    with sf() as db:
        row = db.scalars(
            select(ServerTool).where(
                ServerTool.server_id == server_id,
                ServerTool.tool_id == "wkhtmltopdf",
            )
        ).one()
        assert row.status == "ok"


# --------------------------------------------------------------------------- #
# API + RBAC
# --------------------------------------------------------------------------- #


@pytest.fixture
def api(client, db_session):
    runner = JobRunner(lambda: db_session, InMemoryJobBackend(), enqueue=lambda job: None)
    client.app.dependency_overrides[get_job_runner] = lambda: runner
    yield client
    client.app.dependency_overrides.pop(get_job_runner, None)


@pytest.fixture
def server_id(db_session):
    return _seed_server(db_session)


def test_list_tools_returns_the_full_registry_before_any_scan(api, server_id):
    login(api, "admin@example.com")
    res = api.get(f"/api/servers/{server_id}/tools")
    assert res.status_code == 200
    body = res.json()
    # EmptyState signal for the UI.
    assert body["last_scanned_at"] is None
    assert body["frappe_major"] == "16"
    assert [g["group"] for g in body["groups"]] == list(TOOL_GROUPS)
    listed = {t["tool_id"] for g in body["groups"] for t in g["tools"]}
    assert listed == {t.tool_id for t in TOOL_DEFINITIONS}
    for group in body["groups"]:
        assert group["ok_count"] == 0
        assert group["total_count"] == len(group["tools"])


def test_list_tools_reports_group_counts_after_a_scan(api, db_session, server_id):
    db_session.add(
        ServerTool(
            server_id=server_id,
            tool_id="git",
            detected_version="2.43.0",
            recommended_version="any",
            status="ok",
            last_checked_at=datetime.now(UTC),
        )
    )
    db_session.commit()

    login(api, "admin@example.com")
    body = api.get(f"/api/servers/{server_id}/tools").json()
    assert body["last_scanned_at"] is not None
    core = next(g for g in body["groups"] if g["group"] == GROUP_CORE)
    assert core["ok_count"] == 1


def test_list_tools_404s_for_an_unknown_server(api):
    login(api, "admin@example.com")
    assert api.get("/api/servers/9999/tools").status_code == 404


def test_readonly_can_view(api, server_id):
    login(api, "readonly@example.com")
    assert api.get(f"/api/servers/{server_id}/tools").status_code == 200


def test_readonly_cannot_scan(api, server_id):
    login(api, "readonly@example.com")
    res = api.post(
        f"/api/servers/{server_id}/tools/scan", headers=csrf_headers(api)
    )
    assert res.status_code == 403


def test_readonly_cannot_install(api, server_id):
    login(api, "readonly@example.com")
    res = api.post(
        f"/api/servers/{server_id}/tools/gh/install", headers=csrf_headers(api)
    )
    assert res.status_code == 403


def test_admin_scan_returns_202_and_a_job(api, server_id):
    login(api, "admin@example.com")
    res = api.post(f"/api/servers/{server_id}/tools/scan", headers=csrf_headers(api))
    assert res.status_code == 202
    assert res.json()["action_name"] == "server.scan_tools"


def test_admin_install_returns_202_and_the_per_tool_action(api, server_id):
    login(api, "admin@example.com")
    res = api.post(
        f"/api/servers/{server_id}/tools/gh/install", headers=csrf_headers(api)
    )
    assert res.status_code == 202
    body = res.json()
    assert body["action_name"] == "tool.install_gh"
    assert body["id"]


def test_install_of_an_unknown_tool_id_404s_before_any_command(api, server_id):
    """Golden rule 1: the path segment is a registry lookup, never a command."""
    login(api, "admin@example.com")
    res = api.post(
        f"/api/servers/{server_id}/tools/rm%20-rf/install", headers=csrf_headers(api)
    )
    assert res.status_code == 404


def test_install_of_a_detect_only_tool_422s_with_an_explanation(api, server_id):
    login(api, "admin@example.com")
    res = api.post(
        f"/api/servers/{server_id}/tools/mariadb/install", headers=csrf_headers(api)
    )
    assert res.status_code == 422
    assert "data migration" in res.json()["error"]["message"]


def test_node_major_is_resolved_server_side_from_the_matrix(api, db_session, server_id):
    """The client cannot ask for a Node the matrix does not sanction."""
    from app.models.job import CommandJob

    login(api, "admin@example.com")
    res = api.post(
        f"/api/servers/{server_id}/tools/node/install",
        headers=csrf_headers(api),
        json={"node_major": "12"},  # ignored — never read from the body
    )
    assert res.status_code == 202
    job = db_session.get(CommandJob, res.json()["id"])
    assert job.params_sanitized["node_major"] == "24"  # the v16 row


def test_node_major_reaches_the_rendered_argv_not_just_the_stored_param():
    """Regression (DOO-1067): the resolved major must actually reach the shell.

    The stored `params_sanitized["node_major"]` being right is NOT enough — the
    script has to reference the substituted value, not an unset `$NODE_MAJOR`
    shell var (which `set -u` would abort on, failing every install). Assert the
    literal major appears in the rendered argv and no NODE_MAJOR shell var
    survives.
    """
    from app.core.commands import get_template
    from app.core.commands.templates import render

    rendered = render(get_template("tool.install_node"), {"node_major": "24"})
    script = " ".join(rendered.argv)
    assert "nvm install \"24\"" in script
    assert "nvm alias default \"24\"" in script
    assert "NODE_MAJOR" not in script  # no unset shell var left behind


def test_node_install_422s_when_there_is_no_bench_to_resolve_against(api, db_session):
    sid = _seed_server(db_session, benches=0)
    login(api, "admin@example.com")
    res = api.post(f"/api/servers/{sid}/tools/node/install", headers=csrf_headers(api))
    assert res.status_code == 422
    assert "no bench" in res.json()["error"]["message"]


def test_second_install_on_the_same_server_conflicts(api, db_session, server_id):
    """Rule 4: two installs on one server must never run concurrently."""
    login(api, "admin@example.com")
    first = api.post(
        f"/api/servers/{server_id}/tools/gh/install", headers=csrf_headers(api)
    )
    assert first.status_code == 202
    second = api.post(
        f"/api/servers/{server_id}/tools/jq/install", headers=csrf_headers(api)
    )
    assert second.status_code == 409
    assert second.json()["error"]["blocking_job_id"] == first.json()["id"]


# --------------------------------------------------------------------------- #
# Settings overrides reach the resolver
# --------------------------------------------------------------------------- #


def test_settings_override_reaches_build_context(db_session):
    sid = _seed_server(db_session)
    settings = PlatformSettings.get_or_create(db_session)
    settings.version_matrix_overrides = {"16": {"node": "26"}}
    db_session.commit()

    ctx = build_context(db_session, sid)
    assert ctx.major == "16"
    assert ctx.requirements["node"].satisfied_by((26, 0, 0))
    assert not ctx.requirements["node"].satisfied_by((24, 0, 0))


def test_settings_default_is_an_empty_override_map(db_session):
    sid = _seed_server(db_session)
    ctx = build_context(db_session, sid)
    assert ctx.requirements["node"].satisfied_by((24, 0, 0))
