"""Server registry + per-server SSH credentials.

Secrets live only as Fernet tokens in the *_enc columns (CLAUDE.md rule 6):
private keys, passphrases and passwords are encrypted at rest with
FDM_SECRET_KEY and never stored or returned in plaintext.
"""

from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

# JSONB on Postgres, plain JSON elsewhere (SQLite test fallback).
TagsJSON = JSON().with_variant(JSONB(), "postgresql")


class Server(Base):
    """A managed Ubuntu host. Reached over SSH (agentless, the default) or, when
    `connection_type='local'`, the FDM host itself via local subprocess (DOO-1199)."""

    __tablename__ = "servers"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    # How the platform reaches this host (DOO-1199 / DOO-1196):
    #   ssh   -> agentless over AsyncSSH (needs a credential + hostname).
    #   local -> the machine FDM itself runs on; jobs execute as local
    #            subprocesses, no SSH, no credential (see core.jobs.LocalShellExecutor).
    # server_default='ssh' backfills every pre-existing row to today's behaviour.
    connection_type: Mapped[str] = mapped_column(
        String(10), nullable=False, server_default="ssh", default="ssh"
    )
    hostname: Mapped[str] = mapped_column(String(255))
    ssh_port: Mapped[int] = mapped_column(Integer, default=22)
    os_version: Mapped[str | None] = mapped_column(String(120))
    # unknown | online | offline | error — set by the last connection test.
    status: Mapped[str] = mapped_column(String(20), default="unknown")
    # prod | staging | dev — drives the environment badge + guardrails.
    env_tag: Mapped[str] = mapped_column(String(10), default="dev")
    tags: Mapped[list] = mapped_column(TagsJSON, default=list)
    notes: Mapped[str | None] = mapped_column(Text)
    # The host's MariaDB root password, Fernet-encrypted at rest (rule 6). Used
    # server-side by `bench new-site` (gotcha #4) so the browser never sends it;
    # NULL until an operator sets it on the server's settings. Never returned in
    # plaintext — the API exposes only a "is it set" boolean.
    mariadb_root_password_enc: Mapped[str | None] = mapped_column(Text)
    # Per-server override for the SSH connection-pool cap (session 2.6): the max
    # concurrent AsyncSSH sessions the platform opens to this host at once. NULL
    # = use the platform default (Settings.ssh_max_sessions_per_server). Lower it
    # for a small/shared host, raise it for a beefy build server.
    ssh_pool_limit: Mapped[int | None] = mapped_column(Integer)
    last_seen: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    credential: Mapped["SSHCredential | None"] = relationship(
        back_populates="server", uselist=False, cascade="all, delete-orphan"
    )


class SSHCredential(Base):
    """How the platform authenticates to one Server. All secret material is a
    Fernet token; the plaintext never touches the DB, logs, or API responses."""

    __tablename__ = "ssh_credentials"

    id: Mapped[int] = mapped_column(primary_key=True)
    server_id: Mapped[int] = mapped_column(
        ForeignKey("servers.id", ondelete="CASCADE"), unique=True, index=True
    )
    username: Mapped[str] = mapped_column(String(64))
    # key | password
    auth_type: Mapped[str] = mapped_column(String(10), default="key")

    # Fernet tokens (never plaintext). Which are populated depends on auth_type.
    private_key_enc: Mapped[str | None] = mapped_column(Text)
    passphrase_enc: Mapped[str | None] = mapped_column(Text)
    password_enc: Mapped[str | None] = mapped_column(Text)

    # Pinned host key (OpenSSH one-line public-key form), captured on first
    # connect and verified on every connect thereafter (TOFU pinning).
    known_host_key: Mapped[str | None] = mapped_column(Text)

    # nopasswd (sudo -n works) | none (no sudo granted).
    sudo_mode: Mapped[str] = mapped_column(String(20), default="nopasswd")

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    server: Mapped[Server] = relationship(back_populates="credential")
