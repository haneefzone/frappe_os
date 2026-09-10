"""DB-side helpers for the tool inventory (session 6.1).

`app/core/tools.py` stays pure (definitions + matrix + resolver, no DB) so the
resolver can be unit-tested without a session. Everything that touches rows
lives here: resolving a server's Frappe context, upserting `ServerTool` rows
after a scan, and reading the inventory back for the API.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.tools import (
    TOOL_DEFINITIONS,
    Requirement,
    ToolDefinition,
    assess,
    highest_frappe_major,
    resolve_requirements,
)
from app.models.bench import Bench
from app.models.server_tool import ServerTool
from app.models.settings import PlatformSettings


@dataclass(frozen=True)
class ServerToolContext:
    """Everything needed to judge a server's toolchain, resolved once per scan."""

    has_bench: bool
    major: str | None
    requirements: dict[str, Requirement]

    @property
    def major_known(self) -> bool:
        return self.major is not None


def build_context(db: Session, server_id: int) -> ServerToolContext:
    """Resolve the version-matrix row that applies to one server.

    The matrix row comes from the *highest* Frappe major across the server's
    benches — the toolchain is shared, so the newest bench sets the bar.
    """
    versions = list(
        db.scalars(select(Bench.frappe_version).where(Bench.server_id == server_id)).all()
    )
    major = highest_frappe_major(versions)
    overrides = PlatformSettings.get_or_create(db).version_matrix_overrides or {}
    return ServerToolContext(
        has_bench=bool(versions),
        major=major,
        requirements=resolve_requirements(major, overrides),
    )


def assess_tool(
    tool: ToolDefinition, detected_version: str | None, ctx: ServerToolContext
):
    return assess(
        tool,
        detected_version,
        ctx.requirements,
        has_bench=ctx.has_bench,
        major_known=ctx.major_known,
    )


def upsert_server_tool(
    db: Session,
    *,
    server_id: int,
    tool: ToolDefinition,
    detected_version: str | None,
    ctx: ServerToolContext,
    checked_at: datetime | None = None,
) -> ServerTool:
    """Write (or refresh) one tool's observed state for a server."""
    verdict = assess_tool(tool, detected_version, ctx)
    row = db.scalars(
        select(ServerTool).where(
            ServerTool.server_id == server_id, ServerTool.tool_id == tool.tool_id
        )
    ).first()
    if row is None:
        row = ServerTool(server_id=server_id, tool_id=tool.tool_id)
        db.add(row)

    row.detected_version = verdict.detected_version
    row.recommended_version = verdict.recommended_version
    row.status = verdict.status
    row.last_checked_at = checked_at or datetime.now(UTC)
    return row


def list_server_tools(db: Session, server_id: int) -> list[ServerTool]:
    """Every persisted row for a server, in registry order.

    Registry order (not alphabetical) so the UI's grouped checklist reads in the
    order the stack is actually built: core stack, then dev tools, then AI CLIs.
    """
    rows = {
        r.tool_id: r
        for r in db.scalars(
            select(ServerTool).where(ServerTool.server_id == server_id)
        ).all()
    }
    return [rows[t.tool_id] for t in TOOL_DEFINITIONS if t.tool_id in rows]
