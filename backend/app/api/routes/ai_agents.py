"""AI Agents module API (session 5.1): scoped agent configs + jailed sessions
with a pre-change git snapshot and a git-diff apply/rollback review.

Config CRUD (`ai:manage`):
- GET    /api/ai-agents                 list registered agents
- POST   /api/ai-agents                 register an agent
- GET    /api/ai-agents/{id}            one agent
- PATCH  /api/ai-agents/{id}            edit an agent
- DELETE /api/ai-agents/{id}            remove an agent

Sessions (`ai:operate`; only Admin + Developer hold it — security-sensitive):
- POST   /api/ai-agents/{id}/sessions           start a scoped session (enqueues
                                                 the pre-change snapshot job)
- POST   /api/ai-agents/sessions/{sid}/terminal issue the scoped terminal ticket
- POST   /api/ai-agents/sessions/{sid}/end      capture the git diff for review
- POST   /api/ai-agents/sessions/{sid}/apply    keep/commit the changes
- POST   /api/ai-agents/sessions/{sid}/rollback restore the pre-change snapshot
- GET    /api/ai-agents/sessions                recent sessions
- GET    /api/ai-agents/sessions/{sid}          one session (+ captured diff)

Golden rules bind: the server whitelist is enforced server-side (never the
browser); every mutation flows through the JobRunner (CommandJob + AuditLog);
the working dir is `..`-rejected; no raw shell interpolation anywhere.
"""

from __future__ import annotations

import secrets as _secrets
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.api.deps import CurrentUser, require
from app.api.routes.jobs import get_job_runner
from app.api.routes.terminal import _store_ticket
from app.config import get_settings
from app.core.commands import RenderError
from app.core.commands.templates import has_dotdot_segment
from app.core.jobs import JobRunner, LockConflict
from app.core.permissions import AI_MANAGE, AI_OPERATE
from app.db import get_db
from app.models import Server, SSHCredential
from app.models.ai_agent import (
    AIAgentAllowedServer,
    AIAgentConfig,
    AIAgentSession,
)
from app.models.terminal import TerminalSession
from app.schemas.ai_agent import (
    AgentConfigCreate,
    AgentConfigOut,
    AgentConfigUpdate,
    SessionOut,
    SessionStartRequest,
    SessionTicketOut,
)
from app.schemas.job import JobDetail

router = APIRouter(prefix="/api/ai-agents", tags=["ai-agents"])

DbSession = Annotated[Session, Depends(get_db)]
Runner = Annotated[JobRunner, Depends(get_job_runner)]

TARGET_TYPE = "ai_dir"


def _conflict(exc: LockConflict, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content={
            "error": {
                "code": "conflict",
                "message": message,
                "blocking_job_id": exc.blocking_job_id,
            }
        },
    )


def _set_allowed_servers(db: Session, agent: AIAgentConfig, server_ids: list[int]) -> None:
    """Replace the agent's server whitelist. Rejects unknown server ids (422)."""
    unique = list(dict.fromkeys(server_ids))
    for sid in unique:
        if db.get(Server, sid) is None:
            raise HTTPException(status_code=422, detail=f"Server {sid} not found.")
    agent.allowed_servers.clear()
    db.flush()
    for sid in unique:
        agent.allowed_servers.append(AIAgentAllowedServer(server_id=sid))


# --------------------------------------------------------------------------- #
# Agent config CRUD (ai:manage)
# --------------------------------------------------------------------------- #


@router.get("", response_model=list[AgentConfigOut])
def list_agents(
    db: DbSession,
    _: Annotated[object, Depends(require(AI_MANAGE))],
) -> list[AgentConfigOut]:
    rows = db.scalars(
        select(AIAgentConfig)
        .options(joinedload(AIAgentConfig.allowed_servers))
        .order_by(AIAgentConfig.name)
    ).unique().all()
    return [AgentConfigOut.from_model(a) for a in rows]


@router.post("", status_code=201, response_model=AgentConfigOut)
def create_agent(
    body: AgentConfigCreate,
    db: DbSession,
    _: Annotated[object, Depends(require(AI_MANAGE))],
) -> AgentConfigOut:
    if db.scalar(select(AIAgentConfig).where(AIAgentConfig.name == body.name)):
        raise HTTPException(status_code=422, detail=f"An agent named {body.name!r} exists.")
    agent = AIAgentConfig(
        name=body.name,
        kind=body.kind,
        command_template=body.command_template,
        working_dir=body.working_dir,
        read_only=body.read_only,
        pre_change_backup=body.pre_change_backup,
    )
    db.add(agent)
    db.flush()
    _set_allowed_servers(db, agent, body.allowed_server_ids)
    db.commit()
    db.refresh(agent)
    return AgentConfigOut.from_model(agent)


def _get_agent(db: Session, agent_id: int) -> AIAgentConfig:
    agent = db.scalars(
        select(AIAgentConfig)
        .options(joinedload(AIAgentConfig.allowed_servers))
        .where(AIAgentConfig.id == agent_id)
    ).first()
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found.")
    return agent


@router.get("/{agent_id}", response_model=AgentConfigOut)
def get_agent(
    agent_id: int,
    db: DbSession,
    _: Annotated[object, Depends(require(AI_MANAGE))],
) -> AgentConfigOut:
    return AgentConfigOut.from_model(_get_agent(db, agent_id))


@router.patch("/{agent_id}", response_model=AgentConfigOut)
def update_agent(
    agent_id: int,
    body: AgentConfigUpdate,
    db: DbSession,
    _: Annotated[object, Depends(require(AI_MANAGE))],
) -> AgentConfigOut:
    agent = _get_agent(db, agent_id)
    if body.name is not None and body.name != agent.name:
        if db.scalar(select(AIAgentConfig).where(AIAgentConfig.name == body.name)):
            raise HTTPException(status_code=422, detail=f"An agent named {body.name!r} exists.")
        agent.name = body.name
    for field in ("kind", "command_template", "working_dir", "read_only", "pre_change_backup"):
        val = getattr(body, field)
        if val is not None:
            setattr(agent, field, val)
    if body.allowed_server_ids is not None:
        _set_allowed_servers(db, agent, body.allowed_server_ids)
    db.commit()
    db.refresh(agent)
    return AgentConfigOut.from_model(agent)


@router.delete("/{agent_id}", status_code=204)
def delete_agent(
    agent_id: int,
    db: DbSession,
    _: Annotated[object, Depends(require(AI_MANAGE))],
) -> None:
    agent = _get_agent(db, agent_id)
    db.delete(agent)
    db.commit()


# --------------------------------------------------------------------------- #
# Sessions (ai:operate)
# --------------------------------------------------------------------------- #


def _session_out(db: Session, s: AIAgentSession, *, include_diff: bool = True) -> SessionOut:
    agent = db.get(AIAgentConfig, s.agent_id) if s.agent_id else None
    server = db.get(Server, s.server_id)
    return SessionOut.from_model(
        s,
        agent_name=agent.name if agent else None,
        server_name=server.name if server else None,
        include_diff=include_diff,
    )


@router.post("/{agent_id}/sessions", status_code=201, response_model=SessionOut)
def start_session(
    agent_id: int,
    body: SessionStartRequest,
    db: DbSession,
    runner: Runner,
    user: CurrentUser,
    _: Annotated[object, Depends(require(AI_OPERATE))],
) -> SessionOut:
    """Start a scoped session: enforce the server whitelist + working-dir jail,
    then enqueue the pre-change snapshot job. For a read-write agent the
    pre-change backup must be enabled (any change is gated behind it)."""
    agent = _get_agent(db, agent_id)

    # Server-side scoping: the session may only run on a whitelisted server.
    if body.server_id not in agent.allowed_server_ids:
        raise HTTPException(
            status_code=403,
            detail="This server is not in the agent's allowed-servers whitelist.",
        )
    server = db.scalars(
        select(Server).options(joinedload(Server.credential)).where(Server.id == body.server_id)
    ).first()
    if server is None:
        raise HTTPException(status_code=404, detail="Server not found.")
    cred: SSHCredential | None = server.credential
    if cred is None:
        raise HTTPException(status_code=422, detail="Server has no SSH credential configured.")

    # Defence in depth: re-validate the jail server-side (never trust a stored value).
    if has_dotdot_segment(agent.working_dir):
        raise HTTPException(
            status_code=422, detail="Agent working_dir contains '..' path segments."
        )

    # Gate any change behind a pre-change backup (golden rule 5 posture).
    if not agent.read_only and not agent.pre_change_backup:
        raise HTTPException(
            status_code=422,
            detail="A read-write agent must have pre-change backup enabled to start a session.",
        )

    session = AIAgentSession(
        agent_id=agent.id,
        server_id=server.id,
        user_id=user.id,
        ssh_username=cred.username,
        working_dir=agent.working_dir,
        read_only=agent.read_only,
        pre_change_backup=agent.pre_change_backup,
        status="starting",
        started_at=datetime.now(UTC),
    )
    db.add(session)
    db.commit()
    db.refresh(session)

    # Always take a snapshot: it is the read-write rollback base AND the read-only
    # violation-revert baseline. Cheap (all reads; stash-create doesn't touch WT).
    try:
        job = runner.create(
            db,
            action_name="ai.pre_change_snapshot",
            server_id=server.id,
            target_type=TARGET_TYPE,
            target_id=agent.working_dir,
            params={"working_dir": agent.working_dir, "session_id": str(session.id)},
            priority="default",
            created_by=user.id,
        )
    except RenderError as exc:
        db.delete(session)
        db.commit()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except LockConflict as exc:
        db.delete(session)
        db.commit()
        return _conflict(exc, f"A session is already active on {agent.working_dir!r}.")

    session.snapshot_job_id = job.id
    db.commit()
    db.refresh(session)
    return _session_out(db, session)


@router.post("/sessions/{session_id}/terminal", response_model=SessionTicketOut)
async def open_terminal(
    session_id: int,
    db: DbSession,
    user: CurrentUser,
    _: Annotated[object, Depends(require(AI_OPERATE))],
) -> SessionTicketOut:
    """Issue a single-use scoped terminal ticket. The session must be `ready`
    (the pre-change snapshot has completed). The WS drops into the jailed working
    dir and launches the registered agent command."""
    session = db.get(AIAgentSession, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found.")
    if session.status == "starting":
        raise HTTPException(
            status_code=409,
            detail="The pre-change snapshot is still running; retry when it completes.",
        )
    if session.status != "ready":
        raise HTTPException(
            status_code=409,
            detail=f"Session is {session.status!r}; a terminal can only open a ready session.",
        )
    server = db.scalars(
        select(Server).options(joinedload(Server.credential)).where(Server.id == session.server_id)
    ).first()
    if server is None or server.credential is None:
        raise HTTPException(status_code=422, detail="Server/credential missing.")

    agent = db.get(AIAgentConfig, session.agent_id) if session.agent_id else None
    command = agent.command_template if agent else ":"

    # Build the jail launch line server-side. working_dir is shell-quoted; the
    # command is whitelist-validated (no shell metacharacters). The read-only
    # marker is exported so an agent (or the operator) can honour it.
    import shlex

    ro = "1" if session.read_only else "0"
    init_command = f"cd {shlex.quote(session.working_dir)} && FDM_READ_ONLY={ro} {command}"

    # Reuse the 1.5 terminal machinery: a TerminalSession audit row + a ticket.
    term = TerminalSession(
        server_id=server.id,
        user_id=user.id,
        ssh_username=session.ssh_username,
        status="open",
        started_at=datetime.now(UTC),
    )
    db.add(term)
    db.commit()
    db.refresh(term)

    ticket = _secrets.token_urlsafe(32)
    await _store_ticket(
        ticket, term.id, server.id, user.id, init_command=init_command
    )
    return SessionTicketOut(
        session_id=session.id,
        ticket=ticket,
        server_name=server.name,
        ssh_username=session.ssh_username,
        working_dir=session.working_dir,
        read_only=session.read_only,
        ticket_ttl_seconds=get_settings().terminal_ticket_ttl_seconds,
    )


def _finalize_timing(session: AIAgentSession) -> None:
    if session.ended_at is None:
        session.ended_at = datetime.now(UTC)
        session.duration_seconds = int(
            (session.ended_at - session.started_at).total_seconds()
        )


@router.post("/sessions/{session_id}/end", status_code=201, response_model=JobDetail)
def end_session(
    session_id: int,
    db: DbSession,
    runner: Runner,
    user: CurrentUser,
    _: Annotated[object, Depends(require(AI_OPERATE))],
) -> JobDetail:
    """End the session: capture the `git diff` of the jail for the review screen.
    A read-only session that modified the tree is auto-reverted inside the job."""
    session = db.get(AIAgentSession, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found.")
    if session.status in ("applied", "rolledback"):
        raise HTTPException(status_code=409, detail="Session is already resolved.")

    try:
        job = runner.create(
            db,
            action_name="ai.capture_diff",
            server_id=session.server_id,
            target_type=TARGET_TYPE,
            target_id=session.working_dir,
            params={"working_dir": session.working_dir, "session_id": str(session.id)},
            priority="default",
            created_by=user.id,
        )
    except RenderError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except LockConflict as exc:
        return _conflict(exc, f"A job is already running on {session.working_dir!r}.")

    session.diff_job_id = job.id
    _finalize_timing(session)
    db.commit()
    db.refresh(job)
    return JobDetail.from_model(job)


@router.post("/sessions/{session_id}/apply", status_code=201, response_model=JobDetail)
def apply_session(
    session_id: int,
    db: DbSession,
    runner: Runner,
    user: CurrentUser,
    _: Annotated[object, Depends(require(AI_OPERATE))],
) -> JobDetail:
    """Keep the agent's changes — stage + commit them in the jailed dir."""
    session = db.get(AIAgentSession, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found.")
    if session.read_only:
        raise HTTPException(
            status_code=403, detail="A read-only session's changes cannot be applied."
        )
    if session.status != "reviewing":
        raise HTTPException(
            status_code=409,
            detail=f"Session is {session.status!r}; end it and capture the diff before applying.",
        )
    message = f"fdm-ai: apply session {session.id}"
    try:
        job = runner.create(
            db,
            action_name="ai.apply",
            server_id=session.server_id,
            target_type=TARGET_TYPE,
            target_id=session.working_dir,
            params={
                "working_dir": session.working_dir,
                "session_id": str(session.id),
                "message": message,
            },
            priority="default",
            created_by=user.id,
        )
    except RenderError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except LockConflict as exc:
        return _conflict(exc, f"A job is already running on {session.working_dir!r}.")

    session.resolve_job_id = job.id
    db.commit()
    db.refresh(job)
    return JobDetail.from_model(job)


@router.post("/sessions/{session_id}/rollback", status_code=201, response_model=JobDetail)
def rollback_session(
    session_id: int,
    db: DbSession,
    runner: Runner,
    user: CurrentUser,
    _: Annotated[object, Depends(require(AI_OPERATE))],
) -> JobDetail:
    """Discard the agent's changes — restore the pre-change snapshot exactly."""
    session = db.get(AIAgentSession, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found.")
    if session.status not in ("reviewing", "ready"):
        raise HTTPException(
            status_code=409,
            detail=f"Session is {session.status!r}; nothing to roll back.",
        )
    if not session.base_commit:
        raise HTTPException(
            status_code=422, detail="Session has no pre-change snapshot to restore."
        )
    try:
        job = runner.create(
            db,
            action_name="ai.rollback",
            server_id=session.server_id,
            target_type=TARGET_TYPE,
            target_id=session.working_dir,
            params={"working_dir": session.working_dir, "session_id": str(session.id)},
            priority="default",
            created_by=user.id,
        )
    except RenderError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except LockConflict as exc:
        return _conflict(exc, f"A job is already running on {session.working_dir!r}.")

    session.resolve_job_id = job.id
    _finalize_timing(session)
    db.commit()
    db.refresh(job)
    return JobDetail.from_model(job)


@router.get("/sessions", response_model=list[SessionOut])
def list_sessions(
    db: DbSession,
    _: Annotated[object, Depends(require(AI_OPERATE))],
) -> list[SessionOut]:
    rows = db.scalars(
        select(AIAgentSession).order_by(AIAgentSession.started_at.desc()).limit(200)
    ).all()
    # The diff can be large; omit it from the list (fetched per-session on review).
    return [_session_out(db, s, include_diff=False) for s in rows]


@router.get("/sessions/{session_id}", response_model=SessionOut)
def get_session(
    session_id: int,
    db: DbSession,
    _: Annotated[object, Depends(require(AI_OPERATE))],
) -> SessionOut:
    session = db.get(AIAgentSession, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found.")
    return _session_out(db, session, include_diff=True)
