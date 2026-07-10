"""Audit API (session 1.12): the immutable, filterable activity log + CSV export.

- GET /api/audit          paged, filterable list for the Audit DataTable.
- GET /api/audit.csv      the same filtered result as a CSV download (ISO-friendly
                          evidence — B4.15 "compliance-report-friendly export").

Read-only for anyone with `read`; the table itself is append-only (there is no
create/update/delete endpoint — rule 2). Filters: actor, action, entity_type,
result, free-text `q` over the summary, and a `since`/`until` time window.
"""

import csv
import io
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import require
from app.core.permissions import READ
from app.db import get_db
from app.models import User
from app.models.audit import AuditLog
from app.schemas.audit import AuditEntryOut, AuditPage

router = APIRouter(prefix="/api/audit", tags=["audit"])

DbSession = Annotated[Session, Depends(get_db)]

# A hard cap so a runaway CSV export can't stream the whole table unbounded.
MAX_EXPORT_ROWS = 50_000
CSV_COLUMNS = (
    "ts",
    "user_email",
    "action",
    "entity_type",
    "entity_id",
    "result",
    "source_ip",
    "job_id",
    "summary",
)


def _apply_filters(
    stmt,
    *,
    action: str | None,
    entity_type: str | None,
    result: str | None,
    user_id: int | None,
    since: datetime | None,
    until: datetime | None,
    q: str | None,
):
    if action:
        stmt = stmt.where(AuditLog.action == action)
    if entity_type:
        stmt = stmt.where(AuditLog.entity_type == entity_type)
    if result:
        stmt = stmt.where(AuditLog.result == result)
    if user_id is not None:
        stmt = stmt.where(AuditLog.user_id == user_id)
    if since is not None:
        stmt = stmt.where(AuditLog.ts >= since)
    if until is not None:
        stmt = stmt.where(AuditLog.ts <= until)
    if q:
        stmt = stmt.where(AuditLog.summary.ilike(f"%{q}%"))
    return stmt


def _email_map(db: Session, rows: list[AuditLog]) -> dict[int, str]:
    """One lookup for every distinct actor on the page (avoids N+1)."""
    ids = {r.user_id for r in rows if r.user_id is not None}
    if not ids:
        return {}
    return {
        uid: email
        for uid, email in db.execute(
            select(User.id, User.email).where(User.id.in_(ids))
        ).all()
    }


@router.get("", response_model=AuditPage)
def list_audit(
    db: DbSession,
    _: Annotated[object, Depends(require(READ))],
    action: str | None = None,
    entity_type: str | None = None,
    result: str | None = None,
    user_id: int | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    q: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> AuditPage:
    filtered = _apply_filters(
        select(AuditLog),
        action=action,
        entity_type=entity_type,
        result=result,
        user_id=user_id,
        since=since,
        until=until,
        q=q,
    )
    total = db.scalar(
        _apply_filters(
            select(func.count(AuditLog.id)),
            action=action,
            entity_type=entity_type,
            result=result,
            user_id=user_id,
            since=since,
            until=until,
            q=q,
        )
    )
    rows = list(
        db.scalars(
            filtered.order_by(AuditLog.ts.desc(), AuditLog.id.desc())
            .limit(limit)
            .offset(offset)
        ).all()
    )
    emails = _email_map(db, rows)
    return AuditPage(
        entries=[
            AuditEntryOut.from_model(r, user_email=emails.get(r.user_id)) for r in rows
        ],
        total=total or 0,
        limit=limit,
        offset=offset,
    )


@router.get(".csv")
def export_audit_csv(
    db: DbSession,
    _: Annotated[object, Depends(require(READ))],
    action: str | None = None,
    entity_type: str | None = None,
    result: str | None = None,
    user_id: int | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    q: str | None = None,
) -> StreamingResponse:
    """The filtered audit trail as a CSV download (newest first, capped)."""
    rows = list(
        db.scalars(
            _apply_filters(
                select(AuditLog),
                action=action,
                entity_type=entity_type,
                result=result,
                user_id=user_id,
                since=since,
                until=until,
                q=q,
            )
            .order_by(AuditLog.ts.desc(), AuditLog.id.desc())
            .limit(MAX_EXPORT_ROWS)
        ).all()
    )
    emails = _email_map(db, rows)

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(CSV_COLUMNS)
    for r in rows:
        writer.writerow(
            [
                r.ts.isoformat(),
                emails.get(r.user_id) or "",
                r.action,
                r.entity_type or "",
                r.entity_id or "",
                r.result,
                r.source_ip or "",
                r.job_id if r.job_id is not None else "",
                r.summary,
            ]
        )
    buffer.seek(0)
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="fdm-audit.csv"'},
    )
