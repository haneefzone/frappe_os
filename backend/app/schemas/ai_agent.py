"""Request/response models for the AI Agents module API (session 5.1).

The two operator-supplied free-text fields — `command_template` and
`working_dir` — are validated here at the boundary against shell-safe whitelists
(no shell metacharacters, no `..` path segments), so a stored agent config can
never carry an injection payload into the jailed session or a git job.
"""

from __future__ import annotations

import re
from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from app.core.commands.templates import has_dotdot_segment
from app.models.ai_agent import AGENT_KINDS, AIAgentConfig, AIAgentSession

# The launcher command line. Alnum + a few safe punctuation marks and spaces —
# excludes ; ` $ ( ) & | < > \ ' " and newline, so it cannot chain commands or
# substitute. It is only ever the interactive command a scoped PTY runs after
# `cd <working_dir>`.
_COMMAND_RE = re.compile(r"[A-Za-z0-9 ._:/=@+-]{1,500}")
# An absolute working dir (matches the registry ABS_PATH shape).
_ABS_PATH_RE = re.compile(r"/[\w./-]{1,300}")


def _validate_working_dir(value: str) -> str:
    if _ABS_PATH_RE.fullmatch(value) is None:
        raise ValueError(
            "working_dir must be an absolute path (alnum . _ - / only)"
        )
    if has_dotdot_segment(value):
        raise ValueError("working_dir must not contain '..' path segments")
    return value


def _validate_command(value: str) -> str:
    value = value.strip()
    if _COMMAND_RE.fullmatch(value) is None:
        raise ValueError(
            "command_template contains disallowed characters (shell "
            "metacharacters are not permitted)"
        )
    return value


class AgentConfigBase(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    kind: str = "claude-code"
    command_template: str
    working_dir: str
    read_only: bool = True
    pre_change_backup: bool = True
    allowed_server_ids: list[int] = Field(default_factory=list)

    @field_validator("kind")
    @classmethod
    def _kind(cls, v: str) -> str:
        if v not in AGENT_KINDS:
            raise ValueError(f"kind must be one of {list(AGENT_KINDS)}")
        return v

    @field_validator("command_template")
    @classmethod
    def _cmd(cls, v: str) -> str:
        return _validate_command(v)

    @field_validator("working_dir")
    @classmethod
    def _wd(cls, v: str) -> str:
        return _validate_working_dir(v)


class AgentConfigCreate(AgentConfigBase):
    pass


class AgentConfigUpdate(BaseModel):
    """All fields optional (PATCH). Same whitelists as create where present."""

    name: str | None = Field(default=None, min_length=1, max_length=120)
    kind: str | None = None
    command_template: str | None = None
    working_dir: str | None = None
    read_only: bool | None = None
    pre_change_backup: bool | None = None
    allowed_server_ids: list[int] | None = None

    @field_validator("kind")
    @classmethod
    def _kind(cls, v: str | None) -> str | None:
        if v is not None and v not in AGENT_KINDS:
            raise ValueError(f"kind must be one of {list(AGENT_KINDS)}")
        return v

    @field_validator("command_template")
    @classmethod
    def _cmd(cls, v: str | None) -> str | None:
        return _validate_command(v) if v is not None else v

    @field_validator("working_dir")
    @classmethod
    def _wd(cls, v: str | None) -> str | None:
        return _validate_working_dir(v) if v is not None else v


class AgentConfigOut(BaseModel):
    id: int
    name: str
    kind: str
    command_template: str
    working_dir: str
    read_only: bool
    pre_change_backup: bool
    allowed_server_ids: list[int]
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_model(cls, agent: AIAgentConfig) -> AgentConfigOut:
        return cls(
            id=agent.id,
            name=agent.name,
            kind=agent.kind,
            command_template=agent.command_template,
            working_dir=agent.working_dir,
            read_only=agent.read_only,
            pre_change_backup=agent.pre_change_backup,
            allowed_server_ids=agent.allowed_server_ids,
            created_at=agent.created_at,
            updated_at=agent.updated_at,
        )


class SessionStartRequest(BaseModel):
    server_id: int


class SessionOut(BaseModel):
    id: int
    agent_id: int | None
    agent_name: str | None
    server_id: int
    server_name: str | None
    user_id: int | None
    ssh_username: str
    working_dir: str
    read_only: bool
    pre_change_backup: bool
    status: str
    base_commit: str | None
    disposition: str | None
    diff_text: str | None
    snapshot_job_id: int | None
    diff_job_id: int | None
    resolve_job_id: int | None
    close_reason: str | None
    started_at: datetime
    ended_at: datetime | None
    duration_seconds: int | None

    @classmethod
    def from_model(
        cls,
        s: AIAgentSession,
        *,
        agent_name: str | None = None,
        server_name: str | None = None,
        include_diff: bool = True,
    ) -> SessionOut:
        return cls(
            id=s.id,
            agent_id=s.agent_id,
            agent_name=agent_name,
            server_id=s.server_id,
            server_name=server_name,
            user_id=s.user_id,
            ssh_username=s.ssh_username,
            working_dir=s.working_dir,
            read_only=s.read_only,
            pre_change_backup=s.pre_change_backup,
            status=s.status,
            base_commit=s.base_commit,
            disposition=s.disposition,
            diff_text=s.diff_text if include_diff else None,
            snapshot_job_id=s.snapshot_job_id,
            diff_job_id=s.diff_job_id,
            resolve_job_id=s.resolve_job_id,
            close_reason=s.close_reason,
            started_at=s.started_at,
            ended_at=s.ended_at,
            duration_seconds=s.duration_seconds,
        )


class SessionTicketOut(BaseModel):
    session_id: int
    ticket: str
    server_name: str
    ssh_username: str
    working_dir: str
    read_only: bool
    ticket_ttl_seconds: int
