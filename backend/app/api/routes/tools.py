"""Tool installer API (session 6.1).

- GET  /api/servers/{id}/tools                      the per-server stack checklist
- POST /api/servers/{id}/tools/scan                 -> server.scan_tools job
- POST /api/servers/{id}/tools/{tool_id}/install    -> tool.install_<tool> job

The checklist is assembled from the *code* registry joined onto whatever the
last scan persisted, so a never-scanned server still returns every known tool
(status `unknown`) and the UI can render its EmptyState from `last_scanned_at`.

RBAC (rule 7): viewing needs `read`; scanning needs `tool:scan` (Operator and
up — it enqueues a job, and Read-only can never mutate); installing needs
`server:manage` (Admin/Developer), because it changes what is on the box.

Install dispatch never builds a command from the `tool_id` path segment: the id
is looked up in the registry, and it is the *definition* that names which fixed
per-tool template runs (golden rule 1). An unknown id 404s here.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, require
from app.api.routes.jobs import get_job_runner
from app.core.commands import RenderError
from app.core.jobs import JobRunner, LockConflict
from app.core.permissions import READ, SERVER_MANAGE, TOOL_SCAN, role_allows
from app.core.toolinventory import build_context, list_server_tools
from app.core.tools import TOOL_DEFINITIONS, TOOL_GROUPS, TOOLS_BY_ID
from app.core.version_matrix import get_entry
from app.db import get_db
from app.models.server import Server
from app.schemas.job import JobDetail
from app.schemas.tool import ServerToolsOut, ToolGroupOut, ToolOut

router = APIRouter(prefix="/api", tags=["tools"])

DbSession = Annotated[Session, Depends(get_db)]
Runner = Annotated[JobRunner, Depends(get_job_runner)]

SCAN_ACTION = "server.scan_tools"


def _get_server_or_404(db: Session, server_id: int) -> Server:
    server = db.get(Server, server_id)
    if server is None:
        raise HTTPException(status_code=404, detail="Server not found.")
    return server


def _require(user, permission: str) -> None:
    if not role_allows(list(user.role.permissions or []), permission):
        raise HTTPException(
            status_code=403,
            detail=f"Role {user.role.name!r} lacks the {permission!r} permission.",
        )


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


@router.get("/servers/{server_id}/tools", response_model=ServerToolsOut)
def list_tools(
    server_id: int, db: DbSession, _: Annotated[object, Depends(require(READ))]
) -> ServerToolsOut:
    """The full checklist: every registered tool, with its last observation."""
    _get_server_or_404(db, server_id)
    observed = {row.tool_id: row for row in list_server_tools(db, server_id)}
    ctx = build_context(db, server_id)

    rows: list[ToolOut] = []
    for tool in TOOL_DEFINITIONS:
        row = observed.get(tool.tool_id)
        rows.append(
            ToolOut(
                tool_id=tool.tool_id,
                display_name=tool.display_name,
                group=tool.group,
                detected_version=row.detected_version if row else None,
                recommended_version=row.recommended_version if row else None,
                status=row.status if row else "unknown",
                last_checked_at=row.last_checked_at if row else None,
                installable=tool.install_action is not None,
                needs_root=tool.needs_root,
                critical=tool.critical,
                note=tool.note,
            )
        )

    groups = []
    for group in TOOL_GROUPS:
        members = [r for r in rows if r.group == group]
        groups.append(
            ToolGroupOut(
                group=group,
                tools=members,
                ok_count=sum(1 for r in members if r.status == "ok"),
                total_count=len(members),
            )
        )

    checked = [r.last_checked_at for r in rows if r.last_checked_at is not None]
    return ServerToolsOut(
        server_id=server_id,
        frappe_major=ctx.major,
        last_scanned_at=max(checked) if checked else None,
        groups=groups,
    )


@router.post("/servers/{server_id}/tools/scan", status_code=202, response_model=JobDetail)
def scan_tools(server_id: int, db: DbSession, runner: Runner, user: CurrentUser):
    """Re-detect every registered tool on the server and persist the verdicts."""
    _require(user, TOOL_SCAN)
    _get_server_or_404(db, server_id)
    try:
        job = runner.create(
            db,
            action_name=SCAN_ACTION,
            server_id=server_id,
            target_type="server",
            target_id=None,
            params={},
            priority="default",
            created_by=user.id,
        )
    except RenderError as exc:  # pragma: no cover - the template takes no params
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    db.refresh(job)
    return JobDetail.from_model(job)


def _install_params(db: Session, server_id: int, tool) -> dict[str, str]:
    """Server-side parameters for one tool's install template.

    Only Node needs one, and it is resolved from the matrix here — the major is
    never accepted from the request body, so a client cannot ask for a Node the
    matrix does not sanction (the template's enum is the second gate).
    """
    if tool.tool_id != "node":
        return {}
    ctx = build_context(db, server_id)
    if ctx.major is None:
        raise HTTPException(
            status_code=422,
            detail=(
                "Cannot choose a Node version: this server has no bench, so there "
                "is no Frappe version to resolve against. Discover or create a "
                "bench first."
            ),
        )
    return {"node_major": str(get_entry(ctx.major).node_max_major)}


@router.post(
    "/servers/{server_id}/tools/{tool_id}/install",
    status_code=202,
    response_model=JobDetail,
)
def install_tool(
    server_id: int, tool_id: str, db: DbSession, runner: Runner, user: CurrentUser
):
    """Install or upgrade one tool. Serialised per server by the `tools` lock."""
    _require(user, SERVER_MANAGE)
    _get_server_or_404(db, server_id)

    tool = TOOLS_BY_ID.get(tool_id)
    if tool is None:
        raise HTTPException(status_code=404, detail=f"Unknown tool {tool_id!r}.")
    if tool.install_action is None:
        raise HTTPException(
            status_code=422,
            detail=(
                f"{tool.display_name} is detect-only and is not installed by the "
                f"platform. {tool.note}"
            ).strip(),
        )

    try:
        job = runner.create(
            db,
            action_name=tool.install_action,
            server_id=server_id,
            target_type="server",
            target_id=None,
            params=_install_params(db, server_id, tool),
            priority="default",
            created_by=user.id,
        )
    except RenderError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except LockConflict as exc:
        return _conflict(
            exc, f"Another tool job is already running on server {server_id}."
        )
    db.refresh(job)
    return JobDetail.from_model(job)
