"""AI Agents module — scoped agent configs + jailed diff/apply/rollback sessions
(session 5.1).

An `AIAgentConfig` is an operator-registered CLI agent (e.g. Claude Code) that
can be launched inside a **jailed** working directory on a whitelisted server.
Every change an agent makes is gated behind a pre-change git snapshot and a
git-diff review: on session end the working dir's `git diff` is captured, and the
operator either **applies** (commits/keeps) or **rolls back** (restores the
pre-change snapshot exactly). All the git work runs as command-template jobs
(CommandJob + AuditLog); nothing is interpolated into a shell string.

Server-side scoping (golden rule 7 analog): a session may only be launched on a
server in the agent's `allowed_servers` whitelist, enforced in the API, never the
browser. Least-privilege: the session runs as the server's stored SSH/bench user.
"""

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

# The two agent kinds the UI offers. `claude-code` is the first-class Claude Code
# CLI card; `custom` is any other CLI whose command template the operator supplies.
AGENT_KINDS = ("claude-code", "custom")

# Session lifecycle:
#   starting   — row created; for a read-write agent the pre-change snapshot job
#                is enqueued and must succeed before a terminal ticket is issued.
#   ready      — snapshot done (or read-only, no snapshot needed); terminal may open.
#   reviewing  — the operator ended the session; the git diff has been captured.
#   applied    — the operator kept/committed the changes.
#   rolledback — the operator restored the pre-change snapshot.
#   error      — a snapshot/diff job failed, or a read-only session was found to
#                have modified the tree (a read_only_violation).
SESSION_STATUSES = (
    "starting",
    "ready",
    "reviewing",
    "applied",
    "rolledback",
    "error",
)


class AIAgentConfig(Base):
    """A registered, scoped AI agent (Claude Code / custom CLI)."""

    __tablename__ = "ai_agent_configs"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, index=True)

    # claude-code | custom (see AGENT_KINDS).
    kind: Mapped[str] = mapped_column(String(20), default="claude-code")

    # The launcher command line the session runs inside the jailed working dir,
    # e.g. "claude" or "claude --permission-mode plan". Validated at save time
    # against a shell-safe whitelist (no ; ` $ ( ) & | < > or newlines), so it
    # cannot chain or substitute; it is only ever the interactive command a
    # scoped PTY runs after `cd <working_dir>`.
    command_template: Mapped[str] = mapped_column(String(500))

    # Absolute path the session is jailed to (`cd <working_dir>` on connect).
    # ABS_PATH-validated with no `..` segments so it cannot climb its parent.
    working_dir: Mapped[str] = mapped_column(String(300))

    # Read-only mode: no pre-change snapshot is taken, apply is forbidden, and any
    # filesystem change detected at session end is auto-rolled-back (no write can
    # be kept). Safe default = True.
    read_only: Mapped[bool] = mapped_column(Boolean, default=True)

    # Take a pre-change git snapshot before a read-write session so a rollback can
    # restore the working dir exactly. Required for any read-write session.
    pre_change_backup: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    allowed_servers: Mapped[list["AIAgentAllowedServer"]] = relationship(
        back_populates="agent",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    @property
    def allowed_server_ids(self) -> list[int]:
        return [a.server_id for a in self.allowed_servers]


class AIAgentAllowedServer(Base):
    """Whitelist row: an agent may only start a session on a listed server."""

    __tablename__ = "ai_agent_allowed_servers"
    __table_args__ = (
        UniqueConstraint("agent_id", "server_id", name="uq_ai_agent_server"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    agent_id: Mapped[int] = mapped_column(
        ForeignKey("ai_agent_configs.id", ondelete="CASCADE"), index=True
    )
    server_id: Mapped[int] = mapped_column(
        ForeignKey("servers.id", ondelete="CASCADE"), index=True
    )

    agent: Mapped["AIAgentConfig"] = relationship(back_populates="allowed_servers")


class AIAgentSession(Base):
    """One scoped agent session and its diff/apply/rollback lifecycle."""

    __tablename__ = "ai_agent_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Keep the row if the agent config is deleted so history/audit survive.
    agent_id: Mapped[int | None] = mapped_column(
        ForeignKey("ai_agent_configs.id", ondelete="SET NULL"), index=True, nullable=True
    )
    server_id: Mapped[int] = mapped_column(
        ForeignKey("servers.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True, nullable=True
    )
    ssh_username: Mapped[str] = mapped_column(String(120))

    # Snapshot of the agent's config at launch (the config may change later).
    working_dir: Mapped[str] = mapped_column(String(300))
    read_only: Mapped[bool] = mapped_column(Boolean, default=True)
    pre_change_backup: Mapped[bool] = mapped_column(Boolean, default=True)

    status: Mapped[str] = mapped_column(String(20), default="starting", index=True)

    # Pre-change git snapshot: the base commit the session started from, and the
    # `git stash create` sha capturing any pre-existing dirty state (empty/NULL if
    # the tree was clean). A rollback resets --hard to base_commit, cleans
    # untracked files, then re-applies snapshot_ref if present — restoring exactly.
    base_commit: Mapped[str | None] = mapped_column(String(64), nullable=True)
    snapshot_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # The captured `git diff` shown on the review screen.
    diff_text: Mapped[str | None] = mapped_column(Text, nullable=True)

    # applied | rolledback — set once the operator resolves the review.
    disposition: Mapped[str | None] = mapped_column(String(20), nullable=True)

    # Audit trail: the jobs that ran each phase (SET NULL so a pruned job row
    # doesn't cascade-delete the session history).
    snapshot_job_id: Mapped[int | None] = mapped_column(
        ForeignKey("command_jobs.id", ondelete="SET NULL"), nullable=True
    )
    diff_job_id: Mapped[int | None] = mapped_column(
        ForeignKey("command_jobs.id", ondelete="SET NULL"), nullable=True
    )
    resolve_job_id: Mapped[int | None] = mapped_column(
        ForeignKey("command_jobs.id", ondelete="SET NULL"), nullable=True
    )

    close_reason: Mapped[str | None] = mapped_column(String(80), nullable=True)

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
