"""Disaster-recovery runbook endpoint (session 4.3 — FDM 4.3).

POST /api/dr-runbook/generate
  Generate a full-fleet (or single-server) DR runbook as Markdown or PDF,
  record its SHA-256 content hash in AuditLog (tamper-evidence), and stream the
  file as a download.

RBAC: report:generate (Admin + Developer). Read-only + Operator get 403 — a DR
runbook is a sensitive, escrow-adjacent document.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import require
from app.audit import Audit
from app.core.dr_runbook import RUNBOOK_FORMATS, generate_runbook
from app.core.permissions import REPORT_GENERATE
from app.db import get_db

router = APIRouter(prefix="/api/dr-runbook", tags=["dr-runbook"])

DbSession = Annotated[Session, Depends(get_db)]


class GenerateRunbookRequest(BaseModel):
    format: str = "md"  # md | pdf
    server_id: int | None = None  # None = whole fleet


@router.post("/generate")
def generate_dr_runbook(
    body: GenerateRunbookRequest,
    db: DbSession,
    audit: Audit,
    _: Annotated[object, Depends(require(REPORT_GENERATE))],
) -> Response:
    """Generate a DR runbook, record its content hash, and download it."""
    if body.format not in RUNBOOK_FORMATS:
        raise HTTPException(
            status_code=422,
            detail=f"format must be one of: {', '.join(RUNBOOK_FORMATS)}",
        )

    try:
        result = generate_runbook(
            db,
            fmt=body.format,
            server_id=body.server_id,
            generated_by=audit.user.email,
        )
    except ValueError as exc:  # unknown server_id / format
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    # Immutable tamper-evidence for each generated runbook (golden rule 2).
    audit.record(
        action="dr.generate_runbook",
        summary=(
            f"Generated DR runbook ({result.scope}) as {body.format.upper()} — "
            f"{result.server_count} servers, {result.site_count} sites"
        ),
        entity_type="dr_runbook",
        params={
            "format": body.format,
            "scope": result.scope,
            "server_id": body.server_id,
            "server_count": result.server_count,
            "site_count": result.site_count,
            "content_hash_sha256": result.content_hash,
        },
        result="ok",
    )

    return Response(
        content=result.content,
        media_type=result.media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{result.filename}"',
            "X-Content-Hash-SHA256": result.content_hash,
        },
    )
