"""Guided bench create (session 1.7): the create orchestration end to end over
fakes (pre-flight -> bench init -> register), the "pre-flight failure blocks
init" gate, template-injection guards on name/path, and the API surface
(create/preflight/version-matrix) with RBAC and validation. No Redis/RQ/SSH."""

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.routes.jobs import get_job_runner
from app.core.commands import RenderError, get_template, render
from app.core.jobs import CaptureResult, InMemoryJobBackend, JobRunner
from app.db import Base
from app.models import CommandJob, LogEntry, Server
from app.models.bench import Bench
from tests.conftest import csrf_headers, login

# A fresh bench's inspect output, so register() parses a version without the
# plain fallback.
INSPECT_NEW = """PROD=0
---COMMON_SITE_CONFIG---
{"webserver_port": 8000}
---BENCH_VERSION---
[{"name": "frappe", "version": "16.25.0"}]
---PYTHON---
Python 3.14.0
---NODE---
v24.1.0
---END---
"""


def probe_capture(
    *,
    uv=(0, "uv 0.5.11"),
    node=(0, "v24.1.0"),
    maria=(0, "mariadb 10.11.6"),
    wk=(0, "wkhtmltopdf 0.12.6.1 (with patched qt)"),
    disk=(0, (
        "Filesystem 1024-blocks Used Available Capacity Mounted on\n"
        "/dev/sda1 102400000 90000000 10485760 95% /\n"  # ~10 GiB avail (df -Pk)
    )),
):
    """Answers the six read-only pre-flight probes. Tool probes arrive wrapped in
    a login shell (`bash -lc <cmd>`, DOO-1189); df runs direct."""
    async def capture(argv, *, cwd=None, timeout=120.0):
        cmd = argv[2] if argv[:2] == ["bash", "-lc"] else " ".join(argv)
        head = cmd.split()[0] if cmd.split() else ""
        if head == "mariadb" and "innodb_snapshot_isolation" in cmd:
            return CaptureResult(0, "", "")  # snapshot query (unauthenticated)
        if head == "uv":
            return CaptureResult(uv[0], uv[1], "")
        if head == "node":
            return CaptureResult(node[0], node[1], "")
        if head == "mariadb":
            return CaptureResult(maria[0], maria[1], "")
        if head == "wkhtmltopdf":
            return CaptureResult(wk[0], wk[1], "")
        if head == "df":
            return CaptureResult(disk[0], disk[1], "")
        raise AssertionError(f"unexpected probe {argv}")

    return capture


class CreateExecutor:
    """Fake executor: answers pre-flight/inspect via capture, records streamed
    (bench init) argv via run so a test can assert init did or did not run."""

    def __init__(self, capture, *, init_exit=0):
        self._probe = capture
        self._init_exit = init_exit
        self.streamed: list[list[str]] = []

    async def capture(self, argv, *, cwd=None, timeout=120.0):
        # Login-shell tool probes (`bash -lc …`, DOO-1189) go to the probe fake;
        # the discovery inspect script is `bash -c <script>`.
        if argv[:2] == ["bash", "-c"]:  # the discovery inspect script
            return CaptureResult(0, INSPECT_NEW, "")
        if argv[:2] == ["bench", "version"]:
            return CaptureResult(0, "frappe 16.25.0", "")
        return await self._probe(argv, cwd=cwd, timeout=timeout)

    async def run(self, argv, *, cwd, run_as, on_line, cancel_check):
        self.streamed.append(list(argv))
        on_line("stdout", "bench init running…")
        return self._init_exit


def fake_factory(executor):
    class _CM:
        async def __aenter__(self):
            return executor

        async def __aexit__(self, *exc):
            return False

    return lambda server: _CM()


@pytest.fixture
def sf():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    yield factory
    engine.dispose()


@pytest.fixture
def server_id(sf):
    with sf() as db:
        s = Server(name="vm", hostname="10.0.0.7")
        db.add(s)
        db.commit()
        return s.id


def _create_job(sf, server_id, executor, *, name="frappe-bench", path="/home/frappe", version="16"):
    runner = JobRunner(sf, InMemoryJobBackend(), enqueue=lambda job: None)
    with sf() as db:
        job = runner.create(
            db,
            action_name="bench.create",
            server_id=server_id,
            target_type="bench",
            target_id=f"{path}/{name}",
            params={"frappe_version": version, "name": name, "path": path},
            priority="high",
            created_by=None,
        )
        job_id = job.id
    runner.run_job(job_id, executor_factory=fake_factory(executor))
    return job_id


# -- end to end -------------------------------------------------------------- #


def test_create_runs_preflight_init_and_registers(sf, server_id):
    ex = CreateExecutor(probe_capture())
    job_id = _create_job(sf, server_id, ex, name="bench-16")

    with sf() as db:
        job = db.get(CommandJob, job_id)
        assert job.status == "success", job.status
        # bench init actually ran, with the derived branch + name.
        assert any(
            a[:4] == ["bench", "init", "--frappe-branch", "version-16"] and a[-1] == "bench-16"
            for a in ex.streamed
        ), ex.streamed
        # the new bench was registered (single-bench upsert, version parsed).
        bench = db.scalars(select(Bench).where(Bench.server_id == server_id)).one()
        assert bench.path == "/home/frappe/bench-16"
        assert bench.frappe_version == "16.25.0"


def test_preflight_failure_blocks_init(sf, server_id):
    # uv missing -> blocking pre-flight failure -> bench init must never run.
    ex = CreateExecutor(probe_capture(uv=(127, "")))
    job_id = _create_job(sf, server_id, ex)

    with sf() as db:
        job = db.get(CommandJob, job_id)
        assert job.status == "failure"
        assert ex.streamed == []  # init never streamed
        assert db.scalars(select(Bench)).first() is None  # nothing registered
        logs = db.scalars(select(LogEntry).where(LogEntry.job_id == job_id)).all()
        assert any("not running bench init" in log.content for log in logs)


def test_create_is_not_auto_retried_on_blocking_preflight(sf, server_id):
    # bench.create is non-idempotent, so a determinate preflight block fails once
    # (no 3x auto-retry of a doomed create).
    ex = CreateExecutor(probe_capture(uv=(127, "")))
    job_id = _create_job(sf, server_id, ex)
    with sf() as db:
        assert db.get(CommandJob, job_id).retry_count == 0


def test_create_fails_cleanly_when_bench_init_errors(sf, server_id):
    ex = CreateExecutor(probe_capture(), init_exit=1)
    job_id = _create_job(sf, server_id, ex)
    with sf() as db:
        job = db.get(CommandJob, job_id)
        assert job.status == "failure"
        # init was attempted, but nothing got registered.
        assert ex.streamed
        assert db.scalars(select(Bench)).first() is None


# -- template injection guards ---------------------------------------------- #


@pytest.mark.parametrize(
    "bad_name",
    ["frappe; rm -rf /", "foo && reboot", "../escape", "Name With Caps", "a`whoami`", "x$(id)"],
)
def test_bench_init_rejects_malicious_names(bad_name):
    params = {"branch": "version-16", "name": bad_name, "path": "/home/frappe"}
    with pytest.raises(RenderError):
        render(get_template("bench.init"), params)


@pytest.mark.parametrize(
    "bad_path",
    ["/home/frappe; rm -rf /", "relative/path", "/home/$(id)", "/home/`whoami`", "/home/a b"],
)
def test_bench_create_rejects_malicious_paths(bad_path):
    params = {"frappe_version": "16", "name": "ok", "path": bad_path}
    with pytest.raises(RenderError):
        render(get_template("bench.create"), params)


@pytest.mark.parametrize(
    "bad_path",
    ["/home/frappe/../etc", "/home/../root", "/..", "/home/frappe/..", "/../"],
)
def test_bench_create_rejects_dotdot_paths(bad_path):
    # DOO-107: the path allowlist permits `.` and `/`, so `..` traversal must be
    # rejected explicitly even though it is not a shell-injection vector.
    params = {"frappe_version": "16", "name": "ok", "path": bad_path}
    with pytest.raises(RenderError, match="'\\.\\.'"):
        render(get_template("bench.create"), params)
    # Same guard on the raw init template and the read-only pre-flight.
    with pytest.raises(RenderError, match="'\\.\\.'"):
        render(get_template("bench.init"), {"branch": "version-16", "name": "ok", "path": bad_path})
    with pytest.raises(RenderError, match="'\\.\\.'"):
        render(get_template("bench.preflight"), {"frappe_version": "16", "path": bad_path})


def test_bench_create_allows_dotfile_paths():
    # A single-dot segment or a dotfile dir name is fine — only `..` is refused.
    rc = render(
        get_template("bench.create"),
        {"frappe_version": "16", "name": "ok", "path": "/home/.frappe/benches"},
    )
    assert rc.params_sanitized["path"] == "/home/.frappe/benches"


def test_bench_create_rejects_unknown_version():
    params = {"frappe_version": "17", "name": "ok", "path": "/home/frappe"}
    with pytest.raises(RenderError):
        render(get_template("bench.create"), params)


def test_valid_render_builds_safe_argv():
    params = {"branch": "version-16", "name": "frappe-bench", "path": "/home/frappe"}
    rc = render(get_template("bench.init"), params)
    assert rc.argv == ["bench", "init", "--frappe-branch", "version-16", "frappe-bench"]
    assert rc.cwd == "/home/frappe"


# -- API --------------------------------------------------------------------- #


@pytest.fixture
def benches_client(client, db_session):
    runner = JobRunner(lambda: db_session, InMemoryJobBackend(), enqueue=lambda job: None)
    client.app.dependency_overrides[get_job_runner] = lambda: runner
    yield client
    client.app.dependency_overrides.pop(get_job_runner, None)


@pytest.fixture
def api_server(db_session):
    s = Server(name="vm-a", hostname="10.0.0.1")
    db_session.add(s)
    db_session.commit()
    return s.id


def test_version_matrix_endpoint(benches_client, api_server):
    login(benches_client, "readonly@example.com")
    resp = benches_client.get("/api/benches/version-matrix")
    assert resp.status_code == 200
    entries = {e["major"]: e for e in resp.json()["entries"]}
    assert set(entries) == {"14", "15", "16"}
    assert entries["16"]["line"] == "Python 3.14 · Node 24 · MariaDB 11.8"
    assert entries["16"]["branch"] == "version-16"


def test_create_launches_pending_job(benches_client, api_server):
    login(benches_client, "developer@example.com")
    resp = benches_client.post(
        "/api/benches",
        json={"server_id": api_server, "frappe_version": "16", "name": "bench-16",
              "path": "/home/frappe"},
        headers=csrf_headers(benches_client),
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["status"] == "pending"
    assert data["action_name"] == "bench.create"
    assert data["target_id"] == "/home/frappe/bench-16"
    assert data["priority"] == "high"


def test_create_rejects_bad_name_422(benches_client, api_server):
    login(benches_client, "developer@example.com")
    resp = benches_client.post(
        "/api/benches",
        json={"server_id": api_server, "frappe_version": "16", "name": "bad; rm -rf /",
              "path": "/home/frappe"},
        headers=csrf_headers(benches_client),
    )
    assert resp.status_code == 422


def test_create_rejects_unknown_version_422(benches_client, api_server):
    login(benches_client, "developer@example.com")
    resp = benches_client.post(
        "/api/benches",
        json={"server_id": api_server, "frappe_version": "99", "name": "ok",
              "path": "/home/frappe"},
        headers=csrf_headers(benches_client),
    )
    assert resp.status_code == 422


def test_readonly_cannot_create(benches_client, api_server):
    login(benches_client, "readonly@example.com")
    resp = benches_client.post(
        "/api/benches",
        json={"server_id": api_server, "frappe_version": "16", "name": "ok",
              "path": "/home/frappe"},
        headers=csrf_headers(benches_client),
    )
    assert resp.status_code == 403


def test_create_unknown_server_404(benches_client):
    login(benches_client, "admin@example.com")
    resp = benches_client.post(
        "/api/benches",
        json={"server_id": 999999, "frappe_version": "16", "name": "ok", "path": "/home/frappe"},
        headers=csrf_headers(benches_client),
    )
    assert resp.status_code == 404


def test_create_second_run_conflicts_409(benches_client, api_server):
    login(benches_client, "developer@example.com")
    body = {"server_id": api_server, "frappe_version": "16", "name": "dup", "path": "/home/frappe"}
    first = benches_client.post("/api/benches", json=body, headers=csrf_headers(benches_client))
    assert first.status_code == 201
    second = benches_client.post("/api/benches", json=body, headers=csrf_headers(benches_client))
    assert second.status_code == 409
    assert second.json()["error"]["blocking_job_id"] == first.json()["id"]


def test_preflight_launches_pending_job(benches_client, api_server):
    login(benches_client, "developer@example.com")
    resp = benches_client.post(
        "/api/benches/preflight",
        json={"server_id": api_server, "frappe_version": "16", "path": "/home/frappe"},
        headers=csrf_headers(benches_client),
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["action_name"] == "bench.preflight"


def test_preflight_rejects_bad_version_422(benches_client, api_server):
    login(benches_client, "developer@example.com")
    resp = benches_client.post(
        "/api/benches/preflight",
        json={"server_id": api_server, "frappe_version": "nope", "path": "/home/frappe"},
        headers=csrf_headers(benches_client),
    )
    assert resp.status_code == 422


def test_readonly_cannot_preflight(benches_client, api_server):
    login(benches_client, "readonly@example.com")
    resp = benches_client.post(
        "/api/benches/preflight",
        json={"server_id": api_server, "frappe_version": "16", "path": "/home/frappe"},
        headers=csrf_headers(benches_client),
    )
    assert resp.status_code == 403
