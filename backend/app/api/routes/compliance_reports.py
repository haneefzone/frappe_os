"""Compliance report export endpoints (session 4.4 — FDM 4.4).

POST /api/compliance-reports/generate
  Generate an ISO-friendly compliance/audit report as PDF or CSV, record the
  content hash in AuditLog, and stream the file as a download.

RBAC: report:generate (Admin + Developer). Read-only users get 403.
"""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import require
from app.audit import Audit
from app.core.compliance_export import REPORT_FORMATS, REPORT_TYPES, generate_report
from app.core.permissions import REPORT_GENERATE
from app.db import get_db

router = APIRouter(prefix="/api/compliance-reports", tags=["compliance-reports"])

DbSession = Annotated[Session, Depends(get_db)]


class GenerateReportRequest(BaseModel):
    report_type: str  # access | backup_evidence | access_review
    format: str       # csv | pdf
    since: datetime | None = None
    until: datetime | None = None


@router.post("/generate")
def generate_compliance_report(
    body: GenerateReportRequest,
    db: DbSession,
    audit: Audit,
    _: Annotated[object, Depends(require(REPORT_GENERATE))],
) -> Response:
    """Generate a compliance report, record its SHA-256 hash, and download it."""
    if body.report_type not in REPORT_TYPES:
        from fastapi import HTTPException
        raise HTTPException(
            status_code=422,
            detail=f"report_type must be one of: {', '.join(REPORT_TYPES)}",
        )
    if body.format not in REPORT_FORMATS:
        from fastapi import HTTPException
        raise HTTPException(
            status_code=422,
            detail=f"format must be one of: {', '.join(REPORT_FORMATS)}",
        )

    result = generate_report(
        db,
        report_type=body.report_type,
        fmt=body.format,
        since=body.since,
        until=body.until,
    )

    # Record in AuditLog — immutable tamper-evidence for each export (golden rule 2).
    audit.record(
        action="compliance.export_report",
        summary=(
            f"Generated {body.report_type} compliance report as {body.format.upper()} "
            f"({result.row_count} rows)"
        ),
        entity_type="compliance_report",
        params={
            "report_type": body.report_type,
            "format": body.format,
            "since": body.since.isoformat() if body.since else None,
            "until": body.until.isoformat() if body.until else None,
            "row_count": result.row_count,
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
