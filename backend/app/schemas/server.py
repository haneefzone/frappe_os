"""Request/response models for the server registry.

Secrets are write-only: they enter through *_key/password fields and leave only
as booleans ("a key is set") — never as plaintext or Fernet tokens.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from app.models import Server, SSHCredential

EnvTag = Literal["prod", "staging", "dev"]
AuthType = Literal["key", "password"]
SudoMode = Literal["nopasswd", "none"]


class CredentialIn(BaseModel):
    """Auth config for a server. Exactly one secret path applies:
    - auth_type="key" + generate=true  -> platform generates an ed25519 key
    - auth_type="key" + private_key      -> caller pasted/uploaded a private key
    - auth_type="password" + password    -> password auth
    """

    username: str = Field(min_length=1, max_length=64)
    auth_type: AuthType = "key"
    sudo_mode: SudoMode = "nopasswd"
    generate: bool = False
    private_key: str | None = None
    passphrase: str | None = None
    password: str | None = None

    @model_validator(mode="after")
    def _one_valid_secret_path(self) -> "CredentialIn":
        if self.auth_type == "password":
            if not self.password:
                raise ValueError("password is required when auth_type is 'password'")
        else:  # key
            if not self.generate and not self.private_key:
                raise ValueError(
                    "provide a private_key, or set generate=true to have the "
                    "platform create an ed25519 key pair"
                )
            if self.generate and self.private_key:
                raise ValueError("choose either generate=true or a pasted private_key, not both")
        return self


class ServerCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    hostname: str = Field(min_length=1, max_length=255)
    ssh_port: int = Field(default=22, ge=1, le=65535)
    env_tag: EnvTag = "dev"
    tags: list[str] = Field(default_factory=list)
    notes: str | None = None
    credential: CredentialIn


class ServerUpdate(BaseModel):
    """All fields optional; provide `credential` to rotate auth (re-pins host key)."""

    name: str | None = Field(default=None, min_length=1, max_length=120)
    hostname: str | None = Field(default=None, min_length=1, max_length=255)
    ssh_port: int | None = Field(default=None, ge=1, le=65535)
    env_tag: EnvTag | None = None
    tags: list[str] | None = None
    notes: str | None = None
    credential: CredentialIn | None = None


class CredentialOut(BaseModel):
    username: str
    auth_type: AuthType
    sudo_mode: SudoMode
    # Booleans only — the platform never echoes secret material back.
    has_private_key: bool
    has_password: bool
    host_key_pinned: bool

    @classmethod
    def from_model(cls, cred: SSHCredential) -> "CredentialOut":
        return cls(
            username=cred.username,
            auth_type=cred.auth_type,  # type: ignore[arg-type]
            sudo_mode=cred.sudo_mode,  # type: ignore[arg-type]
            has_private_key=bool(cred.private_key_enc),
            has_password=bool(cred.password_enc),
            host_key_pinned=bool(cred.known_host_key),
        )


class ServerOut(BaseModel):
    id: int
    name: str
    hostname: str
    ssh_port: int
    os_version: str | None
    status: str
    env_tag: str
    tags: list[str]
    notes: str | None
    last_seen: datetime | None
    created_at: datetime
    updated_at: datetime
    credential: CredentialOut | None

    @classmethod
    def from_model(cls, server: Server) -> "ServerOut":
        return cls(
            id=server.id,
            name=server.name,
            hostname=server.hostname,
            ssh_port=server.ssh_port,
            os_version=server.os_version,
            status=server.status,
            env_tag=server.env_tag,
            tags=list(server.tags or []),
            notes=server.notes,
            last_seen=server.last_seen,
            created_at=server.created_at,
            updated_at=server.updated_at,
            credential=CredentialOut.from_model(server.credential) if server.credential else None,
        )


class ServerCreated(ServerOut):
    """Create response. `generated_public_key` is the one-and-only time the
    server-generated public key is returned, for the user to install."""

    generated_public_key: str | None = None
