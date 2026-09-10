"""Observed toolchain state per server (session 6.1).

One row per (server, tool) recording what the last scan actually found. The
*definitions* (how to detect a tool, how to install it) live in code at
`app/core/tools.py` — only the observation lives here, so adding a tool to the
registry never needs a migration.

`recommended_version` is denormalised onto the row deliberately: it is the
resolver's verdict *at scan time*, which is what the operator saw when they
decided to act. Recomputing it on read would silently rewrite history after a
bench upgrade changed the matrix row.

Nothing here is a secret — a version banner is public information.
"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

# Mirrors app.core.tools.TOOL_STATUSES; see that module for the semantics.
SERVER_TOOL_STATUSES = ("ok", "outdated", "missing", "unknown")


class ServerTool(Base):
    """What one scan found for one tool on one server."""

    __tablename__ = "server_tools"
    __table_args__ = (
        # One row per tool per server — a scan upserts, it does not append.
        UniqueConstraint("server_id", "tool_id", name="uq_server_tools_server_tool"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    server_id: Mapped[int] = mapped_column(
        ForeignKey("servers.id", ondelete="CASCADE"), index=True
    )

    # The `ToolDefinition.tool_id` from the code registry (e.g. "wkhtmltopdf").
    # Not a foreign key: the registry is code, and a row for a tool that was
    # later removed from the registry is harmless historical data.
    tool_id: Mapped[str] = mapped_column(String(60))

    # The raw version the detect command reported, e.g. "0.12.6.1". None when
    # the binary was not found at all.
    detected_version: Mapped[str | None] = mapped_column(String(60))

    # The human-readable requirement label the resolver produced at scan time,
    # e.g. "3.11 – 3.12" or "0.12.6.1 (patched Qt)".
    recommended_version: Mapped[str | None] = mapped_column(String(60))

    status: Mapped[str] = mapped_column(String(20), default="unknown", index=True)

    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    server: Mapped["Server"] = relationship()  # noqa: F821
