"""restic config-tier DR backups (session 4.1 — Full-system DR).

Covers the pure helpers (repo URI, env-file quoting, install URL, output
parsing), the three actions driven through the REAL JobContext over an in-memory
executor (so the env-file secret handling, log redaction and step bookkeeping are
exercised for real), and the API surface (configure + job launches + RBAC).

The golden-rule-6 assertions are the point of this suite: the restic repo
password and the S3 keys must never appear on any argv the executor sees, in any
persisted job param, or in any stored/redacted log line — only inside the 0600
env file (staged as base64) that restic sources.
"""

import base64

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.routes.jobs import get_job_runner
from app.core import restic as rst
from app.core.commands import RenderError, get_template, render
from app.core.commands.actions import (
    _RESTIC_ENV_WRAP,
    _RESTIC_INSTALL_SCRIPT,
    _RESTIC_STAGE_SCRIPT,
    _WRITE_KEY_SCRIPT,
)
from app.core.jobs import CaptureResult, InMemoryJobBackend, JobRunner
from app.core.security import get_secrets_service
from app.db import Base
from app.models import AuditLog, CommandJob, LogEntry, Server
from app.models.notification import Notification
from app.models.restic import ResticRepo
from app.models.storage import StorageTarget
from tests.conftest import csrf_headers, login

PASSWORD = "rEst1c-Secr3t pw!"  # contains a space + shell metachars on purpose
ACCESS_KEY = "AKIA_TEST_KEY"
SECRET_KEY = "s3cr3t/AWS+key=="


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #


def test_repository_uri_minio_keeps_scheme_and_prefix():
    t = StorageTarget(
        name="m", provider="minio",
        endpoint_url="https://minio.local:9000", bucket="fdm", region=None,
    )
    assert rst.repository_uri(t, "restic/server-3") == (
        "s3:https://minio.local:9000/fdm/restic/server-3"
    )


def test_repository_uri_aws_derives_regional_host():
    t = StorageTarget(
        name="a", provider="aws", endpoint_url=None, bucket="b", region="me-central-1"
    )
    assert rst.repository_uri(t, "") == "s3:s3.me-central-1.amazonaws.com/b"
    t2 = StorageTarget(name="a2", provider="aws", endpoint_url=None, bucket="b", region=None)
    assert rst.repository_uri(t2, "") == "s3:s3.amazonaws.com/b"


def test_repository_uri_no_bucket_raises():
    t = StorageTarget(name="x", provider="aws", bucket="", region="eu")
    with pytest.raises(rst.ResticError):
        rst.repository_uri(t, "p")


def test_normalize_prefix_rejects_dotdot():
    assert rst.normalize_prefix("/a/b/") == "a/b"
    with pytest.raises(rst.ResticError):
        rst.normalize_prefix("a/../b")


def test_env_file_content_shell_quotes_every_value():
    env = rst.ResticEnv(
        repository="s3:x", password=PASSWORD,
        access_key=ACCESS_KEY, secret_key=SECRET_KEY, region="me-central-1",
    )
    body = env.env_file_content()
    # The password has a space + `!`; sourcing it must be safe → shlex-quoted.
    assert "RESTIC_PASSWORD='rEst1c-Secr3t pw!'" in body
    # Values with only shell-safe chars are emitted unquoted (both source fine).
    assert "AWS_ACCESS_KEY_ID=AKIA_TEST_KEY" in body
    assert "AWS_SECRET_ACCESS_KEY=s3cr3t/AWS+key==" in body
    assert "AWS_DEFAULT_REGION=me-central-1" in body
    # RESTIC_REPOSITORY is a non-secret argv element, never in the env file.
    assert "RESTIC_REPOSITORY" not in body
    assert env.secret_values == (PASSWORD, ACCESS_KEY, SECRET_KEY)


def test_install_url_maps_arch():
    assert rst.install_url("x86_64").endswith("linux_amd64.bz2")
    assert rst.install_url("aarch64").endswith("linux_arm64.bz2")
    with pytest.raises(rst.ResticError):
        rst.install_url("mips")


def test_parse_installed_version_and_snapshot_id():
    assert rst.parse_installed_version("restic 0.16.4 compiled with go1.21") == "0.16.4"
    assert rst.parse_installed_version("bash: restic: command not found") is None
    out = "Files: 3 new\nsnapshot 1a2b3c4d saved\n"
    assert rst.parse_snapshot_id(out) == "1a2b3c4d"
    assert rst.parse_snapshot_id("nothing here") is None


def test_stage_script_emits_every_existing_config_path(tmp_path):
    """Run the REAL _RESTIC_STAGE_SCRIPT under bash exactly as the action invokes
    it (``bash -c SCRIPT "_" <CONFIG_PATHS…>``) and assert every existing path is
    echoed — including the FIRST one. Regression guard for DOO-377: a spurious
    ``shift`` before ``for p in "$@"`` silently dropped $1 (=/etc/nginx) from
    every snapshot. The in-memory executor stubs this script's stdout, so only a
    real-shell test catches it."""
    import os
    import subprocess

    # Fake `dpkg` so the test does not depend on the host having it and stays fast.
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "dpkg").write_text("#!/bin/sh\necho 'nginx\\tinstall'\n")
    (bindir / "dpkg").chmod(0o755)

    home = tmp_path / "home"
    home.mkdir()

    # Mirror CONFIG_PATHS ordering with real temp dirs; leave the 3rd absent so we
    # also prove non-existent paths are skipped (and that skipping does not shift
    # the surviving ones out of alignment).
    present = [tmp_path / "etc" / name for name in ("nginx", "supervisor", "mysql")]
    for p in present:
        p.mkdir(parents=True)
    absent = tmp_path / "etc" / "redis"  # deliberately not created
    argv_paths = [str(present[0]), str(present[1]), str(absent), str(present[2])]

    env = {**os.environ, "HOME": str(home), "PATH": f"{bindir}:{os.environ['PATH']}"}
    proc = subprocess.run(
        ["bash", "-c", _RESTIC_STAGE_SCRIPT, "_", *argv_paths],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    lines = proc.stdout.splitlines()

    # First line is the staged dpkg manifest path; the rest are the existing configs.
    assert lines[0].endswith("dpkg-selections.txt")
    assert str(present[0]) in lines, "first config path (nginx analogue) was dropped"
    emitted_configs = [ln for ln in lines[1:] if ln in {str(p) for p in present}]
    assert emitted_configs == [str(p) for p in present]
    assert str(absent) not in lines


def test_resolve_env_requires_target_password_and_keys():
    secrets = get_secrets_service()
    repo = ResticRepo(server_id=1, prefix="p", password_enc=None)
    with pytest.raises(rst.ResticError):
        rst.resolve_env(repo, None, secrets=secrets)  # no target
    target = StorageTarget(name="t", bucket="b", region="eu", provider="aws")
    with pytest.raises(rst.ResticError):
        rst.resolve_env(repo, target, secrets=secrets)  # no password
    repo.password_enc = secrets.encrypt(PASSWORD)
    with pytest.raises(rst.ResticError):
        rst.resolve_env(repo, target, secrets=secrets)  # no S3 keys
    target.access_key_enc = secrets.encrypt(ACCESS_KEY)
    target.secret_key_enc = secrets.encrypt(SECRET_KEY)
    env = rst.resolve_env(repo, target, secrets=secrets)
    assert env.password == PASSWORD and env.access_key == ACCESS_KEY


# --------------------------------------------------------------------------- #
# 4.2 retention + integrity pure helpers
# --------------------------------------------------------------------------- #


def test_forget_keep_args_builds_flags_for_every_set_dimension():
    repo = ResticRepo(server_id=1, keep_daily=7, keep_weekly=4, keep_monthly=6)
    assert rst.forget_keep_args(repo) == [
        "--keep-daily", "7", "--keep-weekly", "4", "--keep-monthly", "6",
    ]
    # Only the set dimensions are included.
    assert rst.forget_keep_args(ResticRepo(server_id=1, keep_daily=7)) == [
        "--keep-daily", "7",
    ]


def test_forget_keep_args_refuses_with_no_policy_set():
    """No keep dimension set at all → refuse rather than send an unpolicied
    `forget --prune` that would delete every snapshot (golden rule 1)."""
    with pytest.raises(rst.ResticError):
        rst.forget_keep_args(ResticRepo(server_id=1))


def test_forget_keep_args_rejects_sub_one_value():
    with pytest.raises(rst.ResticError):
        rst.forget_keep_args(ResticRepo(server_id=1, keep_daily=0))


@pytest.mark.parametrize("subset", ["5%", "100%", "1/10", "50G", "1.5T", "500"])
def test_validate_read_data_subset_accepts_restic_grammar(subset):
    assert rst.validate_read_data_subset(subset) == subset


def test_validate_read_data_subset_none_and_blank_are_structural_check():
    assert rst.validate_read_data_subset(None) is None
    assert rst.validate_read_data_subset("  ") is None
    assert rst.validate_read_data_subset(" 5% ") == "5%"  # trimmed


@pytest.mark.parametrize("bad", ["5%; rm -rf /", "$(id)", "`id`", "5% 10%", "abc"])
def test_validate_read_data_subset_rejects_non_grammar(bad):
    with pytest.raises(rst.ResticError):
        rst.validate_read_data_subset(bad)


def test_parse_check_result_clean_repo():
    ok, msg = rst.parse_check_result("create exclusive lock\nno errors were found\n")
    assert ok is True and msg == "no errors were found"


def test_parse_check_result_detects_damage():
    ok, msg = rst.parse_check_result(
        "pack 1a2b3c4d: damaged\nCheck failed: 1 error(s)"
    )
    assert ok is False
    assert "damaged" in msg.lower() or "error" in msg.lower()


def test_parse_check_result_no_output_is_a_failure():
    ok, msg = rst.parse_check_result("")
    assert ok is False
    assert "no output" in msg


# --------------------------------------------------------------------------- #
# Registry / render
# --------------------------------------------------------------------------- #


def test_templates_registered_with_server_manage_and_no_secret_params():
    for action in (
        "restic.install", "restic.init", "restic.backup", "restic.snapshots",
        "restic.forget", "restic.check",
    ):
        t = get_template(action)
        assert t.required_permission == "server:manage"
        # Secrets flow via env, never as template params (rule 6).
        assert t.secret_params == set()


def test_forget_is_never_auto_retried_check_is():
    """Destructive forget --prune must never auto-retry (golden rule destructive
    policy); the read-only check is safe to retry a transient SSH blip."""
    assert get_template("restic.forget").idempotent is False
    assert get_template("restic.check").idempotent is True


def test_forget_and_check_templates_render_fixed_prefix():
    uri = "s3:https://minio.local:9000/fdm/restic/s3"
    assert render(get_template("restic.forget"), {"repo": uri}).argv == [
        "restic", "-r", uri, "forget", "--prune",
    ]
    assert render(get_template("restic.check"), {"repo": uri}).argv == [
        "restic", "-r", uri, "check",
    ]


def test_backup_template_renders_tag_and_host():
    rc = render(
        get_template("restic.backup"),
        {"repo": "s3:https://minio.local:9000/fdm/restic/s3", "host": "srv.local"},
    )
    assert rc.argv == [
        "restic", "-r", "s3:https://minio.local:9000/fdm/restic/s3",
        "backup", "--tag", "fdm-config-tier", "--host", "srv.local",
    ]


@pytest.mark.parametrize("bad", ["s3:a b", "s3:x;rm -rf /", "s3:`id`", "file:/etc"])
def test_repo_uri_rejects_shell_metacharacters(bad):
    with pytest.raises(RenderError):
        render(get_template("restic.init"), {"repo": bad})


# --------------------------------------------------------------------------- #
# Action harness (real JobContext over an in-memory executor)
# --------------------------------------------------------------------------- #


@pytest.fixture
def sf():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    yield factory
    engine.dispose()


class ResticExecutor:
    """Fake executor for the restic actions. Records every argv `run`/`capture`
    sees (so tests can assert no secret ever lands on a command line) and answers
    the staging / detect / restic subcommands with canned output."""

    def __init__(self, *, restic_present=False, backup_stdout=None, backup_exit=0,
                 init_stdout="created restic repository abc", init_exit=0,
                 forget_stdout="removed 2 snapshots\n", forget_exit=0,
                 check_stdout="no errors were found\n", check_exit=0):
        self.restic_present = restic_present
        self.backup_stdout = backup_stdout or "Files: 3 new\nsnapshot 1a2b3c4d saved"
        self.backup_exit = backup_exit
        self.init_stdout = init_stdout
        self.init_exit = init_exit
        self.forget_stdout = forget_stdout
        self.forget_exit = forget_exit
        self.check_stdout = check_stdout
        self.check_exit = check_exit
        self.captured: list[list[str]] = []
        self.streamed: list[list[str]] = []
        self.env_files: list[str] = []  # decoded env-file bodies staged on target

    def _restic_subcmd(self, argv):
        # argv = ["bash","-c",_RESTIC_ENV_WRAP,"_",env_path,"restic","-r",uri,SUBCMD,...]
        return argv[8] if len(argv) > 8 else None

    async def capture(self, argv, *, cwd=None, timeout=120.0):
        self.captured.append(list(argv))
        if argv[:3] == ["bash", "-c", _WRITE_KEY_SCRIPT]:
            # Decode the staged env file so the test can inspect its content.
            self.env_files.append(base64.b64decode(argv[4]).decode())
            return CaptureResult(0, "", "")
        if argv[:3] == ["bash", "-c", _RESTIC_STAGE_SCRIPT]:
            return CaptureResult(
                0,
                "/home/frappe/.fdm-restic-stage/dpkg-selections.txt\n/etc/nginx\n/etc/redis\n",
                "",
            )
        is_rm = argv[:2] == ["rm", "-f"]
        is_stage_rm = argv[:2] == ["bash", "-c"] and "fdm-restic-stage" in argv[2]
        if is_rm or is_stage_rm:
            return CaptureResult(0, "", "")
        if argv[:2] == ["bash", "-lc"] and "restic version" in argv[2]:
            return CaptureResult(0, "restic 0.16.4 compiled" if self.restic_present else "", "")
        if argv == ["uname", "-m"]:
            return CaptureResult(0, "x86_64", "")
        if argv[:2] == ["bash", "-c"] and argv[2] == _RESTIC_ENV_WRAP:
            sub = self._restic_subcmd(argv)
            if sub == "init":
                return CaptureResult(self.init_exit, self.init_stdout, "")
            if sub == "backup":
                return CaptureResult(self.backup_exit, self.backup_stdout, "")
            if sub == "snapshots":
                return CaptureResult(0, "ID  Host  Tags\n1a2b3c4d srv fdm-config-tier", "")
            if sub == "forget":
                return CaptureResult(self.forget_exit, self.forget_stdout, "")
            if sub == "check":
                return CaptureResult(self.check_exit, self.check_stdout, "")
        raise AssertionError(f"unexpected capture {argv}")

    async def run(self, argv, *, cwd, run_as, on_line, cancel_check):
        self.streamed.append(list(argv))
        if argv[:3] == ["bash", "-c", _RESTIC_INSTALL_SCRIPT]:
            on_line("stdout", "restic 0.16.4 compiled")
        return 0


def fake_factory(executor):
    class _CM:
        async def __aenter__(self):
            return executor

        async def __aexit__(self, *exc):
            return False

    return lambda server: _CM()


def _server(db, **kw):
    s = Server(name=kw.get("name", "vm"), hostname=kw.get("hostname", "srv.local"))
    db.add(s)
    db.commit()
    return s.id


def _target(db):
    secrets = get_secrets_service()
    t = StorageTarget(
        name="minio", provider="minio",
        endpoint_url="https://minio.local:9000", bucket="fdm", region="me-central-1",
        access_key_enc=secrets.encrypt(ACCESS_KEY),
        secret_key_enc=secrets.encrypt(SECRET_KEY),
        use_ssl=True, enabled=True,
    )
    db.add(t)
    db.commit()
    return t.id


def _repo(
    db, server_id, target_id, *, initialized=False, prefix="restic/server",
    keep_daily=None, keep_weekly=None, keep_monthly=None, check_read_data_subset=None,
):
    secrets = get_secrets_service()
    r = ResticRepo(
        server_id=server_id, storage_target_id=target_id, prefix=prefix,
        password_enc=secrets.encrypt(PASSWORD), initialized=initialized,
        keep_daily=keep_daily, keep_weekly=keep_weekly, keep_monthly=keep_monthly,
        check_read_data_subset=check_read_data_subset,
    )
    db.add(r)
    db.commit()
    return r.id


def _run(sf, *, action, server_id, params, executor):
    runner = JobRunner(
        sf, InMemoryJobBackend(), enqueue=lambda job: None, secrets=get_secrets_service()
    )
    with sf() as db:
        job = runner.create(
            db, action_name=action, server_id=server_id,
            target_type="server", target_id=str(server_id),
            params=params, priority="default", created_by=None,
        )
        job_id = job.id
    runner.run_job(job_id, executor_factory=fake_factory(executor))
    return job_id


def _no_secret_on_any_argv(executor):
    """No secret value ever appears as an argv element the executor runs (they
    live only inside the base64 env-file blob)."""
    for argv in executor.captured + executor.streamed:
        for i, tok in enumerate(argv):
            # The base64 env-file blob is argv[4] of the write-key capture; it is
            # opaque base64, not the plaintext, so exclude it from this check.
            if argv[:3] == ["bash", "-c", _WRITE_KEY_SCRIPT] and i == 4:
                continue
            assert PASSWORD not in tok
            assert SECRET_KEY not in tok
            assert ACCESS_KEY not in tok


def test_init_action_creates_repo_and_flips_initialized(sf):
    with sf() as db:
        sid = _server(db)
        tid = _target(db)
        _repo(db, sid, tid)
    ex = ResticExecutor()
    repo_uri = "s3:https://minio.local:9000/fdm/restic/server"
    job_id = _run(sf, action="restic.init", server_id=sid,
                  params={"repo": repo_uri}, executor=ex)
    with sf() as db:
        assert db.get(CommandJob, job_id).status == "success"
        assert db.scalars(select(ResticRepo)).first().initialized is True
    # restic init ran through the env-file wrapper, with the repo URI on argv.
    assert any(
        a[:3] == ["bash", "-c", _RESTIC_ENV_WRAP] and "init" in a for a in ex.captured
    )
    # The env file carried the secrets; no secret ever hit a command line.
    assert ex.env_files and "RESTIC_PASSWORD" in ex.env_files[0]
    _no_secret_on_any_argv(ex)
    # env file removed afterward.
    assert any(a[:2] == ["rm", "-f"] for a in ex.captured)


def test_init_already_initialized_is_success(sf):
    with sf() as db:
        sid = _server(db)
        tid = _target(db)
        _repo(db, sid, tid)
    ex = ResticExecutor(init_exit=1, init_stdout="Fatal: repository master key already initialized")
    job_id = _run(sf, action="restic.init", server_id=sid,
                  params={"repo": "s3:https://minio.local:9000/fdm/restic/server"}, executor=ex)
    with sf() as db:
        assert db.get(CommandJob, job_id).status == "success"


def test_backup_action_snapshots_config_and_records_evidence(sf):
    with sf() as db:
        sid = _server(db, hostname="prod-01")
        tid = _target(db)
        _repo(db, sid, tid, initialized=True)
    ex = ResticExecutor()
    job_id = _run(
        sf, action="restic.backup", server_id=sid,
        params={"repo": "s3:https://minio.local:9000/fdm/restic/server", "host": "prod-01"},
        executor=ex,
    )
    with sf() as db:
        assert db.get(CommandJob, job_id).status == "success"
        repo = db.scalars(select(ResticRepo)).first()
        assert repo.last_snapshot_id == "1a2b3c4d"
        assert repo.last_backup_at is not None
    # The config set + dpkg manifest were staged and handed to restic backup.
    assert any(a[:3] == ["bash", "-c", _RESTIC_STAGE_SCRIPT] for a in ex.captured)
    backup_argv = next(
        a for a in ex.captured
        if a[:3] == ["bash", "-c", _RESTIC_ENV_WRAP] and "backup" in a
    )
    assert "/etc/nginx" in backup_argv
    assert "/home/frappe/.fdm-restic-stage/dpkg-selections.txt" in backup_argv
    assert "fdm-config-tier" in backup_argv
    _no_secret_on_any_argv(ex)


def test_backup_tolerates_exit_3_partial_read(sf):
    with sf() as db:
        sid = _server(db)
        tid = _target(db)
        _repo(db, sid, tid, initialized=True)
    ex = ResticExecutor(backup_exit=3)  # some root-only files unreadable
    job_id = _run(
        sf, action="restic.backup", server_id=sid,
        params={"repo": "s3:https://minio.local:9000/fdm/restic/server", "host": "srv.local"},
        executor=ex,
    )
    with sf() as db:
        assert db.get(CommandJob, job_id).status == "success"


def test_backup_refuses_when_not_initialized(sf):
    with sf() as db:
        sid = _server(db)
        tid = _target(db)
        _repo(db, sid, tid, initialized=False)
    ex = ResticExecutor()
    job_id = _run(
        sf, action="restic.backup", server_id=sid,
        params={"repo": "s3:https://minio.local:9000/fdm/restic/server", "host": "srv.local"},
        executor=ex,
    )
    with sf() as db:
        assert db.get(CommandJob, job_id).status == "failure"


def test_backup_redacts_secret_that_leaks_into_output(sf):
    """If restic ever echoed a secret, the log redactor (fed by register_secret)
    masks it in the stored log lines."""
    with sf() as db:
        sid = _server(db)
        tid = _target(db)
        _repo(db, sid, tid, initialized=True)
    leaky = f"debug: using key {SECRET_KEY} pw {PASSWORD}\nsnapshot deadbeef saved"
    ex = ResticExecutor(backup_stdout=leaky)
    job_id = _run(
        sf, action="restic.backup", server_id=sid,
        params={"repo": "s3:https://minio.local:9000/fdm/restic/server", "host": "srv.local"},
        executor=ex,
    )
    with sf() as db:
        lines = db.scalars(select(LogEntry).where(LogEntry.job_id == job_id)).all()
        blob = "\n".join(line.content for line in lines)
    assert SECRET_KEY not in blob
    assert PASSWORD not in blob
    assert "••••" in blob


def test_install_action_detects_present_restic(sf):
    with sf() as db:
        sid = _server(db)
    ex = ResticExecutor(restic_present=True)
    job_id = _run(sf, action="restic.install", server_id=sid, params={}, executor=ex)
    with sf() as db:
        assert db.get(CommandJob, job_id).status == "success"
    # Detected as present → no install stream.
    assert not any(a[:3] == ["bash", "-c", _RESTIC_INSTALL_SCRIPT] for a in ex.streamed)


def test_install_action_installs_when_absent(sf):
    with sf() as db:
        sid = _server(db)
    ex = ResticExecutor(restic_present=False)
    job_id = _run(sf, action="restic.install", server_id=sid, params={}, executor=ex)
    with sf() as db:
        assert db.get(CommandJob, job_id).status == "success"
    assert any(a[:3] == ["bash", "-c", _RESTIC_INSTALL_SCRIPT] for a in ex.streamed)


# --------------------------------------------------------------------------- #
# 4.2 action execution: restic.forget + restic.check
# --------------------------------------------------------------------------- #


def test_forget_action_applies_retention_and_records_evidence(sf):
    with sf() as db:
        sid = _server(db)
        tid = _target(db)
        _repo(db, sid, tid, initialized=True, keep_daily=7, keep_weekly=4)
    ex = ResticExecutor()
    job_id = _run(
        sf, action="restic.forget", server_id=sid,
        params={"repo": "s3:https://minio.local:9000/fdm/restic/server"}, executor=ex,
    )
    with sf() as db:
        assert db.get(CommandJob, job_id).status == "success"
        repo = db.scalars(select(ResticRepo)).first()
        assert repo.last_forget_at is not None
    forget_argv = next(
        a for a in ex.captured
        if a[:3] == ["bash", "-c", _RESTIC_ENV_WRAP] and "forget" in a
    )
    assert "--prune" in forget_argv
    assert forget_argv[forget_argv.index("--keep-daily") + 1] == "7"
    assert forget_argv[forget_argv.index("--keep-weekly") + 1] == "4"
    assert "--keep-monthly" not in forget_argv
    _no_secret_on_any_argv(ex)
    # runner.create() audits every job launch (rule 2) — this run is no exception.
    with sf() as db:
        assert db.scalars(
            select(AuditLog).where(AuditLog.job_id == job_id)
        ).first() is not None


def test_forget_action_refuses_with_no_retention_policy(sf):
    with sf() as db:
        sid = _server(db)
        tid = _target(db)
        _repo(db, sid, tid, initialized=True)  # no keep_* set at all
    ex = ResticExecutor()
    job_id = _run(
        sf, action="restic.forget", server_id=sid,
        params={"repo": "s3:https://minio.local:9000/fdm/restic/server"}, executor=ex,
    )
    with sf() as db:
        assert db.get(CommandJob, job_id).status == "failure"
        assert db.scalars(select(ResticRepo)).first().last_forget_at is None
    # The action refused before ever running restic forget on the target.
    assert not any(
        a[:3] == ["bash", "-c", _RESTIC_ENV_WRAP] and "forget" in a for a in ex.captured
    )


def test_forget_action_refuses_when_not_initialized(sf):
    with sf() as db:
        sid = _server(db)
        tid = _target(db)
        _repo(db, sid, tid, initialized=False, keep_daily=7)
    job_id = _run(
        sf, action="restic.forget", server_id=sid,
        params={"repo": "s3:https://minio.local:9000/fdm/restic/server"},
        executor=ResticExecutor(),
    )
    with sf() as db:
        assert db.get(CommandJob, job_id).status == "failure"


def test_check_action_passes_and_records_timestamp(sf):
    with sf() as db:
        sid = _server(db)
        tid = _target(db)
        _repo(db, sid, tid, initialized=True)
    ex = ResticExecutor(check_stdout="create exclusive lock\nno errors were found\n")
    job_id = _run(
        sf, action="restic.check", server_id=sid,
        params={"repo": "s3:https://minio.local:9000/fdm/restic/server"}, executor=ex,
    )
    with sf() as db:
        assert db.get(CommandJob, job_id).status == "success"
        repo = db.scalars(select(ResticRepo)).first()
        assert repo.last_check_at is not None
        assert repo.last_check_ok is True
        assert repo.last_check_message == "no errors were found"
        # No breach alert on a clean check.
        assert db.scalars(select(Notification)).all() == []
    _no_secret_on_any_argv(ex)


def test_check_action_with_read_data_subset_passes_the_selector(sf):
    with sf() as db:
        sid = _server(db)
        tid = _target(db)
        _repo(db, sid, tid, initialized=True, check_read_data_subset="5%")
    ex = ResticExecutor()
    _run(
        sf, action="restic.check", server_id=sid,
        params={"repo": "s3:https://minio.local:9000/fdm/restic/server"}, executor=ex,
    )
    check_argv = next(
        a for a in ex.captured
        if a[:3] == ["bash", "-c", _RESTIC_ENV_WRAP] and "check" in a
    )
    assert check_argv[-2:] == ["--read-data-subset", "5%"]


def test_check_action_failure_records_evidence_and_fires_alert(sf):
    """An induced failed check (session 4.2 acceptance): the job fails, the
    evidence stamps a failure, and a `backup.check_failed` alert reaches every
    active user's in-app feed — the same 2.8 dispatch channel `config.drift`
    already uses, not a forked alert path."""
    with sf() as db:
        sid = _server(db, name="prod-1")
        tid = _target(db)
        _repo(db, sid, tid, initialized=True)
        from app.models.auth import Role, User

        role = Role(name="Admin", permissions=["server:manage"])
        db.add(role)
        db.commit()
        user = User(
            email="ops@example.com", role_id=role.id, is_active=True,
            password_hash="x", full_name="Ops",
        )
        db.add(user)
        db.commit()
    ex = ResticExecutor(
        check_exit=1,
        check_stdout="pack 1a2b3c4d: damaged\nCheck failed: 1 error(s)",
    )
    job_id = _run(
        sf, action="restic.check", server_id=sid,
        params={"repo": "s3:https://minio.local:9000/fdm/restic/server"}, executor=ex,
    )
    with sf() as db:
        job = db.get(CommandJob, job_id)
        assert job.status == "failure"
        # A deterministic damage result is NOT auto-retried (DOO-1034): the
        # check ran and reproduced damage, so retrying would only repeat the
        # hours-long `--read-data-subset` re-read for the same answer.
        assert job.retry_count == 0
        repo = db.scalars(select(ResticRepo)).first()
        assert repo.last_check_at is not None
        assert repo.last_check_ok is False
        assert "damaged" in repo.last_check_message.lower()
        # Exactly ONE `backup.check_failed` alert per active user for the single
        # failed event — i.e. the dispatcher fires once (one fan-out), not once
        # per retry attempt (the pre-DOO-1034 defect re-dispatched on all 4).
        # (The orthogonal `job.failure` alert `_to_terminal` emits is excluded.)
        from app.models.auth import User

        active_users = db.scalars(
            select(User).where(User.is_active.is_(True))
        ).all()
        check_notifs = db.scalars(
            select(Notification).where(
                Notification.event_type == "backup.check_failed"
            )
        ).all()
        assert len(check_notifs) == len(active_users)
        notif = check_notifs[0]
        assert notif.event_type == "backup.check_failed"
        assert "prod-1" in notif.title
        # The alert body names the failure reason, never raw restic output that
        # could in principle carry a path/credential fragment.
        assert notif.body
    # And the expensive `restic check` itself is invoked exactly once, not 4×.
    check_calls = [
        a for a in ex.captured
        if a[:3] == ["bash", "-c", _RESTIC_ENV_WRAP] and "check" in a
    ]
    assert len(check_calls) == 1


def test_check_action_refuses_when_not_initialized(sf):
    with sf() as db:
        sid = _server(db)
        tid = _target(db)
        _repo(db, sid, tid, initialized=False)
    job_id = _run(
        sf, action="restic.check", server_id=sid,
        params={"repo": "s3:https://minio.local:9000/fdm/restic/server"},
        executor=ResticExecutor(),
    )
    with sf() as db:
        assert db.get(CommandJob, job_id).status == "failure"


# --------------------------------------------------------------------------- #
# API surface (configure + job launches + RBAC)
# --------------------------------------------------------------------------- #


@pytest.fixture
def rc_client(client, db_session):
    runner = JobRunner(
        lambda: db_session, InMemoryJobBackend(), enqueue=lambda job: None,
        secrets=get_secrets_service(),
    )
    client.app.dependency_overrides[get_job_runner] = lambda: runner
    yield client
    client.app.dependency_overrides.pop(get_job_runner, None)


@pytest.fixture
def api_env(db_session):
    sid = _server(db_session, name="vm-a", hostname="10.0.0.1")
    tid = _target(db_session)
    return {"server_id": sid, "target_id": tid}


def test_configure_repo_encrypts_password_and_masks_in_audit(rc_client, db_session, api_env):
    login(rc_client, "developer@example.com")
    resp = rc_client.put(
        f"/api/servers/{api_env['server_id']}/restic-repo",
        json={
            "storage_target_id": api_env["target_id"],
            "prefix": "restic/vm-a",
            "password": PASSWORD,
        },
        headers=csrf_headers(rc_client),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["password_set"] is True
    assert body["kind"] == "config"
    assert body["storage_target_name"] == "minio"
    # Password is encrypted, never stored in the clear.
    repo = db_session.scalars(select(ResticRepo)).first()
    assert repo.password_enc and repo.password_enc != PASSWORD
    assert get_secrets_service().decrypt(repo.password_enc) == PASSWORD
    # Audit row records the action but never the password value.
    audits = db_session.scalars(
        select(AuditLog).where(AuditLog.action == "restic.repo.configure")
    ).all()
    assert audits
    assert PASSWORD not in str(audits[-1].params_masked)
    assert audits[-1].params_masked.get("credential_state") == "set"


def test_configure_rejects_dotdot_prefix(rc_client, db_session, api_env):
    login(rc_client, "developer@example.com")
    resp = rc_client.put(
        f"/api/servers/{api_env['server_id']}/restic-repo",
        json={"storage_target_id": api_env["target_id"], "prefix": "a/../b", "password": PASSWORD},
        headers=csrf_headers(rc_client),
    )
    assert resp.status_code == 422


def test_backup_endpoint_launches_job_without_secret_params(rc_client, db_session, api_env):
    _repo(db_session, api_env["server_id"], api_env["target_id"], initialized=True)
    login(rc_client, "developer@example.com")
    resp = rc_client.post(
        f"/api/servers/{api_env['server_id']}/restic-repo/backup",
        headers=csrf_headers(rc_client),
    )
    assert resp.status_code == 201, resp.text
    job = db_session.scalars(
        select(CommandJob).where(CommandJob.action_name == "restic.backup")
    ).first()
    assert job is not None
    # Params carry only the non-secret repo URI + host; no credential material.
    params = job.params_sanitized
    assert set(params) == {"repo", "host"}
    assert PASSWORD not in str(params) and SECRET_KEY not in str(params)


def test_backup_endpoint_422_when_not_initialized(rc_client, db_session, api_env):
    _repo(db_session, api_env["server_id"], api_env["target_id"], initialized=False)
    login(rc_client, "developer@example.com")
    resp = rc_client.post(
        f"/api/servers/{api_env['server_id']}/restic-repo/backup",
        headers=csrf_headers(rc_client),
    )
    assert resp.status_code == 422


def test_init_endpoint_422_when_repo_unconfigured(rc_client, db_session, api_env):
    login(rc_client, "developer@example.com")
    # No repo row at all → 404.
    resp = rc_client.post(
        f"/api/servers/{api_env['server_id']}/restic-repo/init",
        headers=csrf_headers(rc_client),
    )
    assert resp.status_code == 404


def test_readonly_cannot_configure_or_backup(rc_client, db_session, api_env):
    _repo(db_session, api_env["server_id"], api_env["target_id"], initialized=True)
    login(rc_client, "readonly@example.com")
    h = csrf_headers(rc_client)
    put = rc_client.put(
        f"/api/servers/{api_env['server_id']}/restic-repo",
        json={"storage_target_id": api_env["target_id"], "password": PASSWORD}, headers=h,
    )
    assert put.status_code == 403
    for path in ("install", "init", "backup", "snapshots", "forget", "check"):
        resp = rc_client.post(
            f"/api/servers/{api_env['server_id']}/restic-repo/{path}", headers=h
        )
        assert resp.status_code == 403, path


def test_readonly_can_view_evidence(rc_client, db_session, api_env):
    _repo(db_session, api_env["server_id"], api_env["target_id"], initialized=True)
    login(rc_client, "readonly@example.com")
    resp = rc_client.get(f"/api/servers/{api_env['server_id']}/restic-repo")
    assert resp.status_code == 200
    assert resp.json()["kind"] == "config"
    lst = rc_client.get("/api/restic-repos")
    assert lst.status_code == 200 and len(lst.json()) == 1


# --------------------------------------------------------------------------- #
# 4.2 API surface: retention config + forget/check job launches
# --------------------------------------------------------------------------- #


def test_configure_persists_retention_policy_and_check_subset(rc_client, db_session, api_env):
    login(rc_client, "developer@example.com")
    resp = rc_client.put(
        f"/api/servers/{api_env['server_id']}/restic-repo",
        json={
            "storage_target_id": api_env["target_id"],
            "password": PASSWORD,
            "keep_daily": 7,
            "keep_weekly": 4,
            "check_read_data_subset": "5%",
        },
        headers=csrf_headers(rc_client),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["keep_daily"] == 7
    assert body["keep_weekly"] == 4
    assert body["keep_monthly"] is None
    assert body["check_read_data_subset"] == "5%"
    audits = db_session.scalars(
        select(AuditLog).where(AuditLog.action == "restic.repo.configure")
    ).all()
    assert audits[-1].params_masked.get("keep_daily") == 7


def test_configure_rejects_bad_read_data_subset(rc_client, db_session, api_env):
    login(rc_client, "developer@example.com")
    resp = rc_client.put(
        f"/api/servers/{api_env['server_id']}/restic-repo",
        json={
            "storage_target_id": api_env["target_id"],
            "password": PASSWORD,
            "check_read_data_subset": "5%; rm -rf /",
        },
        headers=csrf_headers(rc_client),
    )
    assert resp.status_code == 422


def test_forget_endpoint_launches_job(rc_client, db_session, api_env):
    _repo(
        db_session, api_env["server_id"], api_env["target_id"],
        initialized=True, keep_daily=7, keep_weekly=4, keep_monthly=6,
    )
    login(rc_client, "developer@example.com")
    resp = rc_client.post(
        f"/api/servers/{api_env['server_id']}/restic-repo/forget",
        headers=csrf_headers(rc_client),
    )
    assert resp.status_code == 201, resp.text
    job = db_session.scalars(
        select(CommandJob).where(CommandJob.action_name == "restic.forget")
    ).first()
    assert job is not None
    # DOO-1105: the effective keep-policy is recorded alongside the repo so the
    # forget evidence chain answers "what retention governed this prune?" from
    # the audit alone. Rendered dims are stringified positive ints (non-secret).
    assert set(job.params_sanitized) == {"repo", "keep_daily", "keep_weekly", "keep_monthly"}
    assert job.params_sanitized["keep_daily"] == "7"
    assert job.params_sanitized["keep_weekly"] == "4"
    assert job.params_sanitized["keep_monthly"] == "6"
    # runner.create() funnels params_sanitized straight into the AuditLog
    # (already_masked=True), so params_masked carries the same keep dims — and
    # mask_params never blanks them ("keep" contains no sensitive token).
    audit = db_session.scalars(
        select(AuditLog).where(AuditLog.job_id == job.id)
    ).first()
    assert audit is not None
    assert audit.params_masked.get("keep_daily") == "7"
    assert audit.params_masked.get("keep_weekly") == "4"
    assert audit.params_masked.get("keep_monthly") == "6"


def test_forget_endpoint_422_without_retention_policy(rc_client, db_session, api_env):
    _repo(db_session, api_env["server_id"], api_env["target_id"], initialized=True)
    login(rc_client, "developer@example.com")
    resp = rc_client.post(
        f"/api/servers/{api_env['server_id']}/restic-repo/forget",
        headers=csrf_headers(rc_client),
    )
    assert resp.status_code == 422


def test_forget_endpoint_422_when_not_initialized(rc_client, db_session, api_env):
    _repo(db_session, api_env["server_id"], api_env["target_id"], initialized=False, keep_daily=7)
    login(rc_client, "developer@example.com")
    resp = rc_client.post(
        f"/api/servers/{api_env['server_id']}/restic-repo/forget",
        headers=csrf_headers(rc_client),
    )
    assert resp.status_code == 422


def test_check_endpoint_launches_job(rc_client, db_session, api_env):
    _repo(db_session, api_env["server_id"], api_env["target_id"], initialized=True)
    login(rc_client, "developer@example.com")
    resp = rc_client.post(
        f"/api/servers/{api_env['server_id']}/restic-repo/check",
        headers=csrf_headers(rc_client),
    )
    assert resp.status_code == 201, resp.text
    job = db_session.scalars(
        select(CommandJob).where(CommandJob.action_name == "restic.check")
    ).first()
    assert job is not None


def test_check_endpoint_422_when_not_initialized(rc_client, db_session, api_env):
    _repo(db_session, api_env["server_id"], api_env["target_id"], initialized=False)
    login(rc_client, "developer@example.com")
    resp = rc_client.post(
        f"/api/servers/{api_env['server_id']}/restic-repo/check",
        headers=csrf_headers(rc_client),
    )
    assert resp.status_code == 422
