"""Terminal session audit records (FDM 1.5).

Each browser SSH session creates one row on open (status='open') and
finalizes it on disconnect with ended_at + duration. The private key is
never stored here — see SSHCredential. Session records satisfy the
CLAUDE.md golden rule 2 (no silent mutations).
"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

SESSION_STATUSES = ("open", "closed", "error")


class TerminalSession(Base):
    """Audit record for one browser SSH terminal session."""

    __tablename__ = "terminal_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    server_id: Mapped[int] = mapped_column(
        ForeignKey("servers.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True, nullable=True
    )
    ssh_username: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(20), default="open")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    close_reason: Mapped[str | None] = mapped_column(String(80), nullable=True)
