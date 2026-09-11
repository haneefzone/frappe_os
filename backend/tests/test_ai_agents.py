"""AI Agents module (session 5.1): scoped agent configs + jailed sessions with a
pre-change git snapshot and a git-diff apply/rollback review.

The action-level tests run the real orchestrators (`ai.pre_change_snapshot`,
`ai.capture_diff`, `ai.apply`, `ai.rollback`) through the JobRunner against a
**real git repo** in a temp dir (a `GitExecutor` shells the fixed argv), so the
guarantees are verified for real: the pre-change snapshot runs before the
session, a rollback restores the working dir *exactly*, and a read-only session
that modified the tree is auto-reverted. The render tests prove the working-dir
jail (`..`) and command/message whitelists reject injection. The API tests cover
RBAC (`ai:manage` / `ai:operate`), the server whitelist, and the read-write
pre-change-backup gate. No Redis/RQ/SSH.
"""

import subprocess

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.routes.jobs import get_job_runner
from app.core.commands import RenderError, get_template, render
from app.core.jobs import CaptureResult, InMemoryJobBackend, JobRunner
from app.core.security import get_secrets_service
from app.db import Base
from app.models import CommandJob, Server
from app.models.ai_agent import AIAgentSession
from tests.conftest import csrf_headers, login

# --------------------------------------------------------------------------- #
# Real-git executor + harness
# --------------------------------------------------------------------------- #


class GitExecutor:
    """Runs each fixed argv for real (git) in `cwd`. Records streamed commands."""

    def __init__(self) -> None:
        self.streamed: list[list[str]] = []

    async def capture(self, argv, *, cwd=None, timeout=120.0):
        p = subprocess.run(argv, cwd=cwd, capture_output=True, text=True)
        return CaptureResult(p.returncode, p.stdout, p.stderr)

    async def run(self, argv, *, cwd, run_as, on_line, cancel_check):
        self.streamed.append(list(argv))
        p = subprocess.run(argv, cwd=cwd, capture_output=True, text=True)
        for line in (p.stdout + p.stderr).splitlines():
            on_line("stdout", line)
        return p.returncode


def _git(cwd, *args):
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=cwd, check=True, capture_output=True, text=True,
    )


def _init_repo(path) -> str:
    """Init a repo with one commit (a.txt='hello'); return the base commit sha."""
    _git(path, "init", "-q", "-b", "main")
    (path / "a.txt").write_text("hello\n")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "initial")
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=path, capture_output=True, text=True
    )
    return head.stdout.strip()


@pytest.fixture
def sf():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    yield factory
    engine.dispose()


def fake_factory(executor):
    class _CM:
        async def __aenter__(self):
            return executor

        async def __aexit__(self, *exc):
            return False

    return lambda server: _CM()


def _make_session(sf, working_dir, *, read_only, base=None) -> tuple[int, int]:
    with sf() as db:
        s = Server(name="vm", hostname="10.0.0.9")
        db.add(s)
        db.commit()
        session = AIAgentSession(
            agent_id=None,
            server_id=s.id,
            user_id=None,
            ssh_username="frappe",
            working_dir=working_dir,
            read_only=read_only,
            pre_change_backup=not read_only,
            status="ready" if base else "starting",
            base_commit=base,
        )
        db.add(session)
        db.commit()
        return s.id, session.id


def _run(sf, *, action, server_id, target_id, params, executor):
    runner = JobRunner(
        sf, InMemoryJobBackend(), enqueue=lambda job: None, secrets=get_secrets_service()
    )
    with sf() as db:
        job = runner.create(
            db, action_name=action, server_id=server_id, target_type="ai_dir",
            target_id=target_id, params=params, priority="default", created_by=None,
        )
        job_id = job.id
    runner.run_job(job_id, executor_factory=fake_factory(executor))
    return job_id


# --------------------------------------------------------------------------- #
# Pre-change snapshot runs before the session
# --------------------------------------------------------------------------- #


def test_snapshot_records_base_before_session(tmp_path, sf):
    base = _init_repo(tmp_path)
    wd = str(tmp_path)
    server_id, sid = _make_session(sf, wd, read_only=False)

    job_id = _run(
        sf, action="ai.pre_change_snapshot", server_id=server_id, target_id=wd,
        params={"working_dir": wd, "session_id": str(sid)}, executor=GitExecutor(),
    )
    with sf() as db:
        assert db.get(CommandJob, job_id).status == "success"
        session = db.get(AIAgentSession, sid)
        assert session.base_commit == base
        assert session.status == "ready"


def test_snapshot_rejects_non_git_dir(tmp_path, sf):
    wd = str(tmp_path)  # not a git repo
    server_id, sid = _make_session(sf, wd, read_only=False)
    job_id = _run(
        sf, action="ai.pre_change_snapshot", server_id=server_id, target_id=wd,
        params={"working_dir": wd, "session_id": str(sid)}, executor=GitExecutor(),
    )
    with sf() as db:
        assert db.get(CommandJob, job_id).status == "failure"
        assert db.get(AIAgentSession, sid).status == "error"


# --------------------------------------------------------------------------- #
# Rollback restores the working dir exactly
# --------------------------------------------------------------------------- #


def test_rollback_restores_exactly(tmp_path, sf):
    base = _init_repo(tmp_path)
    wd = str(tmp_path)
    server_id, sid = _make_session(sf, wd, read_only=False, base=base)

    # The agent modifies a tracked file and adds a new untracked one.
    (tmp_path / "a.txt").write_text("HACKED\n")
    (tmp_path / "b.txt").write_text("new file\n")

    # End: capture the diff for review.
    _run(
        sf, action="ai.capture_diff", server_id=server_id, target_id=wd,
        params={"working_dir": wd, "session_id": str(sid)}, executor=GitExecutor(),
    )
    with sf() as db:
        session = db.get(AIAgentSession, sid)
        assert session.status == "reviewing"
        assert "HACKED" in session.diff_text
        assert "b.txt" in session.diff_text

    # Roll back: restore the pre-change snapshot exactly.
    job_id = _run(
        sf, action="ai.rollback", server_id=server_id, target_id=wd,
        params={"working_dir": wd, "session_id": str(sid)}, executor=GitExecutor(),
    )
    with sf() as db:
        assert db.get(CommandJob, job_id).status == "success"
        session = db.get(AIAgentSession, sid)
        assert session.status == "rolledback"
        assert session.disposition == "rolledback"
    # The working dir is byte-for-byte back to the pre-change state.
    assert (tmp_path / "a.txt").read_text() == "hello\n"
    assert not (tmp_path / "b.txt").exists()


# --------------------------------------------------------------------------- #
# Apply keeps + commits the changes
# --------------------------------------------------------------------------- #


def test_apply_commits_changes(tmp_path, sf):
    base = _init_repo(tmp_path)
    wd = str(tmp_path)
    server_id, sid = _make_session(sf, wd, read_only=False, base=base)
    (tmp_path / "a.txt").write_text("kept change\n")

    _run(
        sf, action="ai.capture_diff", server_id=server_id, target_id=wd,
        params={"working_dir": wd, "session_id": str(sid)}, executor=GitExecutor(),
    )
    job_id = _run(
        sf, action="ai.apply", server_id=server_id, target_id=wd,
        params={"working_dir": wd, "session_id": str(sid),
                "message": f"fdm-ai: apply session {sid}"},
        executor=GitExecutor(),
    )
    with sf() as db:
        assert db.get(CommandJob, job_id).status == "success"
        assert db.get(AIAgentSession, sid).status == "applied"
    # The change persists AND a new commit was created on top of base.
    assert (tmp_path / "a.txt").read_text() == "kept change\n"
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, capture_output=True, text=True
    ).stdout.strip()
    assert head != base
    log = subprocess.run(
        ["git", "log", "-1", "--pretty=%s"], cwd=tmp_path, capture_output=True, text=True
    ).stdout.strip()
    assert log == f"fdm-ai: apply session {sid}"


# --------------------------------------------------------------------------- #
# Read-only blocks writes: any change is auto-reverted at session end
# --------------------------------------------------------------------------- #


def test_read_only_reverts_writes(tmp_path, sf):
    base = _init_repo(tmp_path)
    wd = str(tmp_path)
    server_id, sid = _make_session(sf, wd, read_only=True, base=base)

    # Despite read-only, something wrote to the tree — it must not survive.
    (tmp_path / "a.txt").write_text("SNEAKY WRITE\n")
    (tmp_path / "c.txt").write_text("sneaky new file\n")

    job_id = _run(
        sf, action="ai.capture_diff", server_id=server_id, target_id=wd,
        params={"working_dir": wd, "session_id": str(sid)}, executor=GitExecutor(),
    )
    with sf() as db:
        assert db.get(CommandJob, job_id).status == "success"
        session = db.get(AIAgentSession, sid)
        assert session.status == "rolledback"
        assert session.close_reason == "read_only_violation"
    # The write was reverted — no kept change from a read-only session.
    assert (tmp_path / "a.txt").read_text() == "hello\n"
    assert not (tmp_path / "c.txt").exists()


def test_read_only_clean_session_no_violation(tmp_path, sf):
    base = _init_repo(tmp_path)
    wd = str(tmp_path)
    server_id, sid = _make_session(sf, wd, read_only=True, base=base)
    # No changes made.
    _run(
        sf, action="ai.capture_diff", server_id=server_id, target_id=wd,
        params={"working_dir": wd, "session_id": str(sid)}, executor=GitExecutor(),
    )
    with sf() as db:
        session = db.get(AIAgentSession, sid)
        assert session.status == "reviewing"
        assert session.close_reason is None


# --------------------------------------------------------------------------- #
# Working-dir jail + command/message injection (render-level)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("bad", ["/srv/../etc", "/a/../b", "/x/..", ".."])
def test_working_dir_rejects_dotdot(bad):
    with pytest.raises(RenderError):
        render(get_template("ai.git_add_all"), {"working_dir": bad})


@pytest.mark.parametrize(
    "bad",
    ["/x; rm -rf /", "/x && whoami", "/x`id`", "/x$(id)", "/x|cat", "/x\n/y"],
)
def test_working_dir_rejects_shell_metachars(bad):
    with pytest.raises(RenderError):
        render(get_template("ai.git_reset_hard"), {"working_dir": bad, "ref": "a" * 8})


@pytest.mark.parametrize("bad", ["a" * 8 + ";id", "$(id)", "../x", "abc def"])
def test_git_sha_param_rejects_injection(bad):
    with pytest.raises(RenderError):
        render(get_template("ai.git_reset_hard"), {"working_dir": "/srv/app", "ref": bad})


@pytest.mark.parametrize(
    "bad", ["msg; rm -rf /", "msg`id`", "msg$(id)", "msg && whoami", "msg\nrm"]
)
def test_commit_message_rejects_injection(bad):
    with pytest.raises(RenderError):
        render(
            get_template("ai.git_commit"),
            {"working_dir": "/srv/app", "message": bad},
        )


def test_reset_hard_renders_as_own_argv():
    r = render(get_template("ai.git_reset_hard"), {"working_dir": "/srv/app", "ref": "abc1234"})
    assert r.argv == ["git", "reset", "--hard", "abc1234"]
    assert r.cwd == "/srv/app"


# --------------------------------------------------------------------------- #
# API: RBAC + server whitelist + read-write gate
# --------------------------------------------------------------------------- #


@pytest.fixture
def ai_client(client, db_session):
    runner = JobRunner(
        lambda: db_session, InMemoryJobBackend(), enqueue=lambda job: None,
        secrets=get_secrets_service(),
    )
    client.app.dependency_overrides[get_job_runner] = lambda: runner
    yield client
    client.app.dependency_overrides.pop(get_job_runner, None)


@pytest.fixture
def server_id(db_session):
    s = Server(name="vm-a", hostname="10.0.0.1")
    db_session.add(s)
    db_session.commit()
    return s.id


def _agent_payload(server_id, **kw):
    body = {
        "name": "claude-1",
        "kind": "claude-code",
        "command_template": "claude",
        "working_dir": "/home/frappe/frappe-bench/apps/erpnext",
        "read_only": True,
        "pre_change_backup": True,
        "allowed_server_ids": [server_id],
    }
    body.update(kw)
    return body


def test_readonly_user_cannot_create_agent(ai_client, server_id):
    login(ai_client, "readonly@example.com")
    resp = ai_client.post(
        "/api/ai-agents", json=_agent_payload(server_id), headers=csrf_headers(ai_client)
    )
    assert resp.status_code == 403


def test_developer_creates_agent(ai_client, server_id, db_session):
    login(ai_client, "developer@example.com")
    resp = ai_client.post(
        "/api/ai-agents", json=_agent_payload(server_id), headers=csrf_headers(ai_client)
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["allowed_server_ids"] == [server_id]


def test_create_agent_rejects_working_dir_dotdot(ai_client, server_id):
    login(ai_client, "developer@example.com")
    resp = ai_client.post(
        "/api/ai-agents",
        json=_agent_payload(server_id, working_dir="/home/frappe/../etc"),
        headers=csrf_headers(ai_client),
    )
    assert resp.status_code == 422


def test_create_agent_rejects_command_injection(ai_client, server_id):
    login(ai_client, "developer@example.com")
    resp = ai_client.post(
        "/api/ai-agents",
        json=_agent_payload(server_id, command_template="claude; rm -rf /"),
        headers=csrf_headers(ai_client),
    )
    assert resp.status_code == 422


def test_start_session_rejects_non_whitelisted_server(ai_client, server_id, db_session):
    login(ai_client, "developer@example.com")
    other = Server(name="vm-b", hostname="10.0.0.2")
    db_session.add(other)
    db_session.commit()
    created = ai_client.post(
        "/api/ai-agents", json=_agent_payload(server_id), headers=csrf_headers(ai_client)
    ).json()
    resp = ai_client.post(
        f"/api/ai-agents/{created['id']}/sessions",
        json={"server_id": other.id},
        headers=csrf_headers(ai_client),
    )
    assert resp.status_code == 403


def test_read_write_agent_requires_pre_change_backup(ai_client, server_id, db_session):
    """A read-write agent with pre-change backup off cannot start a session."""
    login(ai_client, "developer@example.com")
    # Need an SSH credential for the server so we reach the backup gate.
    from app.models import SSHCredential
    cred = SSHCredential(
        server_id=server_id, username="frappe", auth_type="password",
        password_enc=get_secrets_service().encrypt("pw"),
    )
    db_session.add(cred)
    db_session.commit()
    created = ai_client.post(
        "/api/ai-agents",
        json=_agent_payload(server_id, read_only=False, pre_change_backup=False),
        headers=csrf_headers(ai_client),
    ).json()
    resp = ai_client.post(
        f"/api/ai-agents/{created['id']}/sessions",
        json={"server_id": server_id},
        headers=csrf_headers(ai_client),
    )
    assert resp.status_code == 422


def test_start_session_enqueues_snapshot(ai_client, server_id, db_session):
    login(ai_client, "developer@example.com")
    from app.models import SSHCredential
    cred = SSHCredential(
        server_id=server_id, username="frappe", auth_type="password",
        password_enc=get_secrets_service().encrypt("pw"),
    )
    db_session.add(cred)
    db_session.commit()
    created = ai_client.post(
        "/api/ai-agents", json=_agent_payload(server_id), headers=csrf_headers(ai_client)
    ).json()
    resp = ai_client.post(
        f"/api/ai-agents/{created['id']}/sessions",
        json={"server_id": server_id},
        headers=csrf_headers(ai_client),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "starting"
    assert body["snapshot_job_id"] is not None
    job = db_session.get(CommandJob, body["snapshot_job_id"])
    assert job.action_name == "ai.pre_change_snapshot"


def test_apply_forbidden_for_read_only_session(ai_client, server_id, db_session):
    login(ai_client, "developer@example.com")
    session = AIAgentSession(
        agent_id=None, server_id=server_id, user_id=None, ssh_username="frappe",
        working_dir="/home/frappe/app", read_only=True, pre_change_backup=False,
        status="reviewing", base_commit="a" * 12,
    )
    db_session.add(session)
    db_session.commit()
    resp = ai_client.post(
        f"/api/ai-agents/sessions/{session.id}/apply", headers=csrf_headers(ai_client)
    )
    assert resp.status_code == 403


def test_terminal_ticket_rejects_unready_session(ai_client, server_id, db_session):
    login(ai_client, "developer@example.com")
    session = AIAgentSession(
        agent_id=None, server_id=server_id, user_id=None, ssh_username="frappe",
        working_dir="/home/frappe/app", read_only=True, pre_change_backup=False,
        status="starting",
    )
    db_session.add(session)
    db_session.commit()
    resp = ai_client.post(
        f"/api/ai-agents/sessions/{session.id}/terminal", headers=csrf_headers(ai_client)
    )
    assert resp.status_code == 409
