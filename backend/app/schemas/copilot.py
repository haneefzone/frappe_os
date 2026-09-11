"""Request/response schemas for the panel copilot (session 5.2)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.models.job_analysis import JobAnalysis


class JobAnalysisOut(BaseModel):
    """One AI analysis of a failed job. Rendered by the "Ask AI to analyze"
    panel; polled until `status` is terminal."""

    id: int
    job_id: int
    status: str
    model: str | None = None
    root_cause: str | None = None
    suggested_fix: str | None = None
    summary: str | None = None
    error: str | None = None
    requested_by: int | None = None
    created_at: datetime
    completed_at: datetime | None = None

    @classmethod
    def from_model(cls, a: JobAnalysis) -> JobAnalysisOut:
        return cls(
            id=a.id,
            job_id=a.job_id,
            status=a.status,
            model=a.model,
            root_cause=a.root_cause,
            suggested_fix=a.suggested_fix,
            summary=a.summary,
            error=a.error,
            requested_by=a.requested_by,
            created_at=a.created_at,
            completed_at=a.completed_at,
        )


class NLResolveRequest(BaseModel):
    q: str = Field(default="", max_length=400)


class NLProposalOut(BaseModel):
    """A confirm-before-run action mapped to an existing registered template."""

    title: str
    action_name: str
    summary: str
    site_id: int
    site_name: str
    params: dict
    allowed: bool
    confirm: bool
    run: dict


class NLResolveResponse(BaseModel):
    resolved: bool
    intent: str | None = None
    reason: str | None = None
    proposals: list[NLProposalOut] = []
