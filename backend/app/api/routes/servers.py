"""Server registry API: CRUD plus a streamed connection test.

RBAC (CLAUDE.md rule 7): Admin/Developer (server:manage) mutate and test;
everyone with `read` can list/view. Secrets go in as write-only fields and
never come back out — responses expose only booleans about what is configured.
"""

import asyncio
import json
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.api.deps import require
from app.audit import Audit
from app.core.permissions import READ, SERVER_MANAGE
from app.core.security import SecretsService, generate_ed25519_keypair, get_secrets_service
from app.core.ssh import ConnectionCheck, SSHService, get_ssh_service
from app.db import get_db
from app.models import Server, SSHCredential
from app.schemas.server import (
    CredentialIn,
    ServerCreate,
    ServerCreated,
    ServerOut,
    ServerUpdate,
)

router = APIRouter(prefix="/api/servers", tags=["servers"])

DbSession = Annotated[Session, Depends(get_db)]
Secrets = Annotated[SecretsService, Depends(get_secrets_service)]
Ssh = Annotated[SSHService, Depends(get_ssh_service)]


def _apply_credential(
    cred: SSHCredential, data: CredentialIn, secrets: SecretsService
) -> str | None:
    """Populate a credential row from input, encrypting every secret. Returns the
    one-time public key if the platform generated a key pair, else None.

    The pinned host key is intentionally preserved across rotation — it belongs
    to the host, not the credential.
    """
    cred.username = data.username
    cred.auth_type = data.auth_type
    cred.sudo_mode = data.sudo_mode
    cred.private_key_enc = None
    cred.passphrase_enc = None
    cred.password_enc = None

    generated_public_key: str | None = None
    if data.auth_type == "password":
        cred.password_enc = secrets.encrypt(data.password or "")
    else:
        if data.generate:
            private_pem, generated_public_key = generate_ed25519_keypair()
            cred.private_key_enc = secrets.encrypt(private_pem)
        else:
            cred.private_key_enc = secrets.encrypt(data.private_key or "")
        if data.passphrase:
            cred.passphrase_enc = secrets.encrypt(data.passphrase)
    return generated_public_key


def _get_server(db: Session, server_id: int) -> Server:
    server = db.scalars(
        select(Server).options(joinedload(Server.credential)).where(Server.id == server_id)
    ).first()
    if server is None:
        raise HTTPException(status_code=404, detail="Server not found.")
    return server


@router.get("", response_model=list[ServerOut])
def list_servers(db: DbSession, _: Annotated[object, Depends(require(READ))]) -> list[ServerOut]:
    servers = db.scalars(
        select(Server).options(joinedload(Server.credential)).order_by(Server.name)
    ).all()
    return [ServerOut.from_model(s) for s in servers]


@router.get("/{server_id}", response_model=ServerOut)
def get_server(
    server_id: int, db: DbSession, _: Annotated[object, Depends(require(READ))]
) -> ServerOut:
    return ServerOut.from_model(_get_server(db, server_id))


@router.post("", response_model=ServerCreated, status_code=201)
def create_server(
    body: ServerCreate,
    db: DbSession,
    secrets: Secrets,
    audit: Audit,
    _: Annotated[object, Depends(require(SERVER_MANAGE))],
) -> ServerCreated:
    if db.scalars(select(Server).where(Server.name == body.name)).first():
        raise HTTPException(status_code=409, detail=f"A server named {body.name!r} already exists.")

    server = Server(
        name=body.name,
        hostname=body.hostname,
        ssh_port=body.ssh_port,
        env_tag=body.env_tag,
        tags=body.tags,
        notes=body.notes,
    )
    if body.mariadb_root_password:
        server.mariadb_root_password_enc = secrets.encrypt(body.mariadb_root_password)
    cred = SSHCredential(server=server)
    generated_public_key = _apply_credential(cred, body.credential, secrets)
    db.add(server)
    db.commit()
    db.refresh(server)

    audit.record(
        action="server.register",
        summary=f"Registered server {server.name} ({server.hostname})",
        entity_type="server",
        entity_id=server.id,
        params={"name": server.name, "hostname": server.hostname, "env_tag": server.env_tag},
    )
    base = ServerOut.from_model(server)
    return ServerCreated(**base.model_dump(), generated_public_key=generated_public_key)


@router.patch("/{server_id}", response_model=ServerCreated)
def update_server(
    server_id: int,
    body: ServerUpdate,
    db: DbSession,
    secrets: Secrets,
    audit: Audit,
    _: Annotated[object, Depends(require(SERVER_MANAGE))],
) -> ServerCreated:
    server = _get_server(db, server_id)

    if body.name is not None and body.name != server.name:
        if db.scalars(select(Server).where(Server.name == body.name)).first():
            raise HTTPException(
                status_code=409, detail=f"A server named {body.name!r} already exists."
            )
        server.name = body.name
    # A hostname change points the credential at a different host, so the pinned
    # TOFU key belongs to the old machine. Drop it (fail-open on rotation) so the
    # next test re-pins against the new host instead of reporting a false MITM.
    if (
        body.hostname is not None
        and body.hostname != server.hostname
        and server.credential is not None
    ):
        server.credential.known_host_key = None

    for field in ("hostname", "ssh_port", "env_tag", "tags", "notes"):
        value = getattr(body, field)
        if value is not None:
            setattr(server, field, value)

    # MariaDB root password: a value sets it, an empty string clears it, None
    # (absent) leaves it untouched.
    if body.mariadb_root_password is not None:
        server.mariadb_root_password_enc = (
            secrets.encrypt(body.mariadb_root_password)
            if body.mariadb_root_password
            else None
        )

    generated_public_key: str | None = None
    if body.credential is not None:
        cred = server.credential or SSHCredential(server=server)
        generated_public_key = _apply_credential(cred, body.credential, secrets)

    db.commit()
    db.refresh(server)
    audit.record(
        action="server.update",
        summary=f"Updated server {server.name}",
        entity_type="server",
        entity_id=server.id,
        params={"credential_rotated": body.credential is not None},
    )
    base = ServerOut.from_model(server)
    return ServerCreated(**base.model_dump(), generated_public_key=generated_public_key)


@router.delete("/{server_id}", status_code=204)
def delete_server(
    server_id: int,
    db: DbSession,
    audit: Audit,
    _: Annotated[object, Depends(require(SERVER_MANAGE))],
) -> None:
    server = _get_server(db, server_id)
    name = server.name
    db.delete(server)
    db.commit()
    audit.record(
        action="server.delete",
        summary=f"Deleted server {name}",
        entity_type="server",
        entity_id=server_id,
    )


def _summary(result: ConnectionCheck) -> dict:
    return {
        "ssh_ok": result.ssh_ok,
        "whoami": result.whoami,
        "sudo_ok": result.sudo_ok,
        "lsb_release": result.lsb_release,
        "tools": result.tools,
        "error": result.error,
    }


def _persist_check(
    db: Session, server: Server, cred: SSHCredential, result: ConnectionCheck
) -> None:
    if result.ssh_ok:
        server.status = "online"
        server.last_seen = datetime.now(UTC)
        if result.lsb_release:
            server.os_version = result.lsb_release
        # Trust-on-first-use: pin the host key the first time we see it.
        if not cred.known_host_key and result.host_key:
            cred.known_host_key = result.host_key
    else:
        server.status = "error" if result.error else "offline"
    db.commit()


@router.post("/{server_id}/test")
async def test_connection(
    server_id: int,
    db: DbSession,
    svc: Ssh,
    _: Annotated[object, Depends(require(SERVER_MANAGE))],
) -> StreamingResponse:
    """Stream the connection test as Server-Sent Events: one JSON `data:` frame
    per check (ssh, whoami, sudo, os, each tool) then a final `done` frame with
    the whole result. The server row's status/os/last-seen and the pinned host
    key are persisted when the test finishes."""
    server = _get_server(db, server_id)
    cred = server.credential
    if cred is None:
        raise HTTPException(status_code=400, detail="This server has no SSH credential configured.")

    async def event_stream():
        queue: asyncio.Queue = asyncio.Queue()

        async def emit(event: dict) -> None:
            await queue.put(event)

        async def worker() -> None:
            try:
                result = await svc.check_connection(server, cred, emit=emit)
                _persist_check(db, server, cred, result)
                await queue.put({"check": "done", **_summary(result)})
            except Exception as exc:  # never let the stream hang open
                await queue.put({"check": "error", "error": str(exc)})
            finally:
                await queue.put(None)

        task = asyncio.create_task(worker())
        try:
            while True:
                event = await queue.get()
                if event is None:
                    break
                yield f"data: {json.dumps(event)}\n\n"
        finally:
            await task
            await svc.close_all()

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )
