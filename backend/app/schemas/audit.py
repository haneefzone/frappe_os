"""Response models for the Audit API (session 1.12)."""

from datetime import datetime

from pydantic import BaseModel

from app.models.audit import AuditLog


class AuditEntryOut(BaseModel):
    """One immutable audit row for the Audit DataTable."""

    id: int
    ts: datetime
    user_id: int | None
    user_email: str | None
    action: str
    entity_type: str | None
    entity_id: str | None
    summary: str
    params_masked: dict
    result: str
    source_ip: str | None
    job_id: int | None

    @classmethod
    def from_model(cls, row: AuditLog, *, user_email: str | None) -> "AuditEntryOut":
        return cls(
            id=row.id,
            ts=row.ts,
            user_id=row.user_id,
            user_email=user_email,
            action=row.action,
            entity_type=row.entity_type,
            entity_id=row.entity_id,
            summary=row.summary,
            params_masked=row.params_masked or {},
            result=row.result,
            source_ip=row.source_ip,
            job_id=row.job_id,
        )


class AuditPage(BaseModel):
    """A page of audit entries plus the total for the pager."""

    entries: list[AuditEntryOut]
    total: int
    limit: int
    offset: int
