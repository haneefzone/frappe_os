"""Browser SSH terminal: ticket-gated WebSocket bridge (FDM 1.5).

Flow:
  1. POST /api/terminal/sessions  (RBAC: terminal:access → Developer+)
     → creates a TerminalSession row (status='open') + stores a short-lived
       single-use Redis ticket.  Returns {session_id, ticket}.
  2. WS  /api/terminal/ws?ticket=...
     → validates + deletes the ticket (single-use), opens an AsyncSSH PTY,
       bridges bytes between the WS and the PTY.
     → handles {"type":"resize","cols":N,"rows":N} JSON frames from the client.
     → injects an idle-warning message 60 s before TERMINAL_IDLE_TIMEOUT_SECONDS.
     → closes + finalises the TerminalSession row (ended_at, duration) on
       disconnect for any reason.

Security:
  - Private keys never leave the backend; only the opaque ticket goes to the
    client, and it is consumed on first use.
  - Ticket stored as Redis key terminal:ticket:{uuid} with a TTL; deleted
    atomically on first WS handshake.
  - WS endpoint has its own auth path (ticket validates that the HTTP
    POST /api/terminal/sessions succeeded as a privileged user).
"""

from __future__ import annotations

import asyncio
import hmac
import json
import secrets
from datetime import UTC, datetime
from typing import Annotated

import asyncssh
import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.api.deps import require
from app.config import get_settings
from app.core.permissions import TERMINAL_ACCESS
from app.core.security import get_secrets_service
from app.core.ssh import host_key_string, normalize_host_key
from app.db import SessionLocal, get_db
from app.models import Server, SSHCredential, User
from app.models.terminal import TerminalSession

router = APIRouter(prefix="/api/terminal", tags=["terminal"])

DbSession = Annotated[Session, Depends(get_db)]

# Redis ticket key prefix.
_TICKET_PREFIX = "terminal:ticket:"

# How far before idle-timeout we inject the warning.
_WARN_BEFORE_SECONDS = 60


# --------------------------------------------------------------------------- #
# Redis helper.
# --------------------------------------------------------------------------- #

def _redis() -> aioredis.Redis:
    return aioredis.from_url(get_settings().redis_url, decode_responses=True)


async def _store_ticket(
    ticket: str,
    session_id: int,
    server_id: int,
    user_id: int,
    *,
    init_command: str | None = None,
) -> None:
    r = _redis()
    try:
        ttl = get_settings().terminal_ticket_ttl_seconds
        payload = {"session_id": session_id, "server_id": server_id, "user_id": user_id}
        # A scoped AI-agent session (session 5.1) supplies the jail's launch line
        # ("cd <working_dir> && <agent command>") which is written to the PTY on
        # connect. It is server-built from validated fields — never raw browser
        # input — so it can't inject beyond the operator-registered command.
        if init_command is not None:
            payload["init_command"] = init_command
        await r.set(f"{_TICKET_PREFIX}{ticket}", json.dumps(payload), ex=ttl)
    finally:
        await r.aclose()


async def _consume_ticket(ticket: str) -> dict | None:
    """Atomic get-and-delete: returns the payload dict or None if not found."""
    r = _redis()
    try:
        key = f"{_TICKET_PREFIX}{ticket}"
        # Use a pipeline to atomically get then delete.
        pipe = r.pipeline()
        pipe.get(key)
        pipe.delete(key)
        results = await pipe.execute()
        raw = results[0]
        if not raw:
            return None
        return json.loads(raw)
    finally:
        await r.aclose()


# --------------------------------------------------------------------------- #
# POST /api/terminal/sessions — create session + issue ticket.
# --------------------------------------------------------------------------- #


class SessionCreateIn(BaseModel):
    server_id: int


class SessionCreated(BaseModel):
    session_id: int
    ticket: str
    server_name: str
    ssh_username: str
    ticket_ttl_seconds: int


@router.post("/sessions", response_model=SessionCreated)
async def create_terminal_session(
    body: SessionCreateIn,
    db: DbSession,
    user: Annotated[User, Depends(require(TERMINAL_ACCESS))],
) -> SessionCreated:
    server = db.scalars(
        select(Server).options(joinedload(Server.credential)).where(Server.id == body.server_id)
    ).first()
    if server is None:
        raise HTTPException(status_code=404, detail="Server not found.")
    cred: SSHCredential | None = server.credential
    if cred is None:
        raise HTTPException(status_code=422, detail="Server has no SSH credential configured.")

    session = TerminalSession(
        server_id=server.id,
        user_id=user.id,
        ssh_username=cred.username,
        status="open",
        started_at=datetime.now(UTC),
    )
    db.add(session)
    db.commit()
    db.refresh(session)

    ticket = secrets.token_urlsafe(32)
    await _store_ticket(ticket, session.id, server.id, user.id)

    return SessionCreated(
        session_id=session.id,
        ticket=ticket,
        server_name=server.name,
        ssh_username=cred.username,
        ticket_ttl_seconds=get_settings().terminal_ticket_ttl_seconds,
    )


# --------------------------------------------------------------------------- #
# GET /api/terminal/sessions — list recent sessions for audit view.
# --------------------------------------------------------------------------- #


class SessionOut(BaseModel):
    id: int
    server_id: int
    server_name: str | None
    user_id: int | None
    user_email: str | None
    ssh_username: str
    status: str
    started_at: datetime
    ended_at: datetime | None
    duration_seconds: int | None
    close_reason: str | None


@router.get("/sessions", response_model=list[SessionOut])
def list_terminal_sessions(
    db: DbSession,
    _: Annotated[User, Depends(require(TERMINAL_ACCESS))],
) -> list[SessionOut]:
    rows = db.scalars(
        select(TerminalSession).order_by(TerminalSession.started_at.desc()).limit(200)
    ).all()
    server_names: dict[int, str] = {}
    user_emails: dict[int, str] = {}
    result = []
    for r in rows:
        if r.server_id not in server_names:
            s = db.get(Server, r.server_id)
            server_names[r.server_id] = s.name if s else "(deleted)"
        if r.user_id and r.user_id not in user_emails:
            from app.models.auth import User as UserModel
            u = db.get(UserModel, r.user_id)
            user_emails[r.user_id] = u.email if u else "(deleted)"
        result.append(
            SessionOut(
                id=r.id,
                server_id=r.server_id,
                server_name=server_names.get(r.server_id),
                user_id=r.user_id,
                user_email=user_emails.get(r.user_id or -1),
                ssh_username=r.ssh_username,
                status=r.status,
                started_at=r.started_at,
                ended_at=r.ended_at,
                duration_seconds=r.duration_seconds,
                close_reason=r.close_reason,
            )
        )
    return result


# --------------------------------------------------------------------------- #
# WS /api/terminal/ws?ticket=...
# --------------------------------------------------------------------------- #


def _finalize(session_id: int, reason: str, started_at: datetime) -> None:
    """Synchronous finalizer for the session row (runs in a thread pool)."""
    ended = datetime.now(UTC)
    duration = int((ended - started_at).total_seconds())
    with SessionLocal() as db:
        session = db.get(TerminalSession, session_id)
        if session is not None:
            session.status = "closed"
            session.ended_at = ended
            session.duration_seconds = duration
            session.close_reason = reason
            db.commit()


@router.websocket("/ws")
async def terminal_ws(websocket: WebSocket, ticket: str | None = None) -> None:
    """WebSocket bridge: ticket → SSH PTY → bidirectional byte bridge."""
    # 1) Accept first so that close codes (4003/4004) are delivered as proper
    #    WS Close frames rather than an HTTP 4xx rejection (which browsers see
    #    as close code 1006).
    await websocket.accept()

    if not ticket:
        await websocket.close(code=4003, reason="ticket required")
        return

    # 2) Consume ticket (single-use, atomic).
    payload = await _consume_ticket(ticket)
    if payload is None:
        await websocket.close(code=4003, reason="invalid or expired ticket")
        return

    session_id: int = payload["session_id"]
    server_id: int = payload["server_id"]

    # 3) Load server + credential from DB.
    with SessionLocal() as db:
        server = db.scalars(
            select(Server)
            .options(joinedload(Server.credential))
            .where(Server.id == server_id)
        ).first()
        if server is None:
            await websocket.close(code=4004, reason="server not found")
            return
        cred = server.credential
        if cred is None:
            await websocket.close(code=4004, reason="no credential")
            return

        # Decrypt secrets now, before the db session closes.
        secrets_svc = get_secrets_service()
        connect_options: dict = {
            "host": server.hostname,
            "port": server.ssh_port,
            "username": cred.username,
            "known_hosts": None,
        }
        if cred.auth_type == "password":
            connect_options["password"] = secrets_svc.decrypt(cred.password_enc or "")
        else:
            passphrase = secrets_svc.decrypt(cred.passphrase_enc) if cred.passphrase_enc else None
            private_key = asyncssh.import_private_key(
                secrets_svc.decrypt(cred.private_key_enc or ""), passphrase
            )
            connect_options["client_keys"] = [private_key]

        # Snapshot host-key state for B2 pinning check (must happen before db closes).
        known_host_key: str | None = cred.known_host_key
        cred_id: int = cred.id

        # Snapshot what we need from the session row.
        session_row = db.get(TerminalSession, session_id)
        started_at = session_row.started_at if session_row else datetime.now(UTC)

    settings = get_settings()
    idle_timeout = settings.terminal_idle_timeout_seconds
    warn_at = idle_timeout - _WARN_BEFORE_SECONDS

    close_reason = "disconnected"

    try:
        # 4) Open AsyncSSH connection with host-key pinning (mirrors SSHService._open).
        async with asyncssh.connect(**connect_options) as ssh_conn:
            presented = host_key_string(ssh_conn)
            if known_host_key:
                if not hmac.compare_digest(
                    normalize_host_key(known_host_key), presented
                ):
                    await websocket.close(code=4004, reason="host key mismatch")
                    await asyncio.to_thread(_finalize, session_id, "host_key_mismatch", started_at)
                    return
            else:
                # First connect: pin the key (trust-on-first-use, same as SSHService).
                with SessionLocal() as pin_db:
                    cred_row = pin_db.get(SSHCredential, cred_id)
                    if cred_row is not None and not cred_row.known_host_key:
                        cred_row.known_host_key = presented
                        pin_db.commit()

            process = await ssh_conn.create_process(
                term_type="xterm-256color",
                request_pty=True,
                encoding=None,
            )

            # Scoped AI-agent session (5.1): drop into the jailed working dir and
            # launch the registered agent command. Server-built line only.
            init_command = payload.get("init_command")
            if init_command:
                process.stdin.write((init_command + "\n").encode("utf-8"))

            idle_timer = asyncio.get_running_loop().time()
            warned = False

            async def read_pty() -> None:
                """Relay bytes from SSH PTY → WebSocket."""
                nonlocal close_reason
                try:
                    async for chunk in process.stdout:
                        if isinstance(chunk, str):
                            await websocket.send_bytes(chunk.encode("utf-8", errors="replace"))
                        else:
                            await websocket.send_bytes(chunk)
                except asyncssh.misc.DisconnectError:
                    close_reason = "ssh_disconnect"
                except Exception:
                    close_reason = "pty_error"

            pty_reader = asyncio.create_task(read_pty())

            try:
                while True:
                    # Idle check: how long since last keypress?
                    elapsed = asyncio.get_running_loop().time() - idle_timer

                    remaining = idle_timeout - elapsed
                    if remaining <= 0:
                        warning = (
                            b"\r\n\x1b[33m[FDM] Session idle timeout."
                            b" Closing connection.\x1b[0m\r\n"
                        )
                        await websocket.send_bytes(warning)
                        close_reason = "idle_timeout"
                        break

                    if not warned and elapsed >= warn_at:
                        warn_msg = (
                            f"\r\n\x1b[33m[FDM] Idle for {warn_at}s. "
                            f"Session closes in {_WARN_BEFORE_SECONDS}s of inactivity.\x1b[0m\r\n"
                        )
                        await websocket.send_bytes(warn_msg.encode())
                        # Text control frame so the UI badge/indicator can react.
                        await websocket.send_text(
                            json.dumps({
                                "type": "idle_warning",
                                "remaining_seconds": _WARN_BEFORE_SECONDS,
                            })
                        )
                        warned = True

                    # Wait for a WS message with a poll interval so idle timer fires.
                    try:
                        data = await asyncio.wait_for(
                            websocket.receive(),
                            timeout=min(remaining, 5.0),
                        )
                    except TimeoutError:
                        continue

                    # Client disconnect.
                    if data["type"] == "websocket.disconnect":
                        close_reason = "client_disconnect"
                        break

                    # Bytes = raw terminal input; pass as-is (encoding=None on process).
                    if data.get("bytes"):
                        idle_timer = asyncio.get_running_loop().time()
                        warned = False
                        process.stdin.write(data["bytes"])

                    # Text = control JSON {"type":"resize","cols":N,"rows":N}
                    elif data.get("text"):
                        try:
                            msg = json.loads(data["text"])
                        except (ValueError, TypeError):
                            continue
                        if msg.get("type") == "resize":
                            cols = int(msg.get("cols", 80))
                            rows = int(msg.get("rows", 24))
                            process.change_terminal_size(cols, rows)

            finally:
                pty_reader.cancel()
                try:
                    await pty_reader
                except (asyncio.CancelledError, Exception):
                    pass
                process.terminate()

    except WebSocketDisconnect:
        close_reason = "client_disconnect"
    except asyncssh.misc.DisconnectError:
        close_reason = "ssh_disconnect"
    except Exception as exc:
        close_reason = f"error:{type(exc).__name__}"
    finally:
        # Finalise the audit row in a thread-pool worker so we don't block.
        await asyncio.to_thread(_finalize, session_id, close_reason, started_at)
        try:
            await websocket.close()
        except Exception:
            pass
