"""Request/response models for the safe update pipeline API (session 3.3)."""

from datetime import datetime

from pydantic import BaseModel, Field

from app.models.update_pipeline import UpdatePipeline


class CreatePipelineRequest(BaseModel):
    """Start a safe-update run by cloning a source site to a staging bench."""

    source_site_id: int
    # Where the clone lands; defaults to the source bench when omitted.
    staging_bench_id: int | None = None
    # The staging site name to create (validated as a site name server-side).
    staging_site_name: str = Field(min_length=2, max_length=80)
    # Admin password for the freshly-created staging site.
    admin_password: str = Field(min_length=1, max_length=128)
    # Optional dotted bench method that masks PII on the clone (uiux §8).
    scrub_method: str | None = None
    priority: str = "default"


class VerifyRequest(BaseModel):
    priority: str = "high"


class PromoteRequest(BaseModel):
    """Promote the tested update to prod. For a `prod` source this is destructive
    and requires the typed site name + a per-task sign-off reference."""

    # Must equal the source site name to confirm a prod promote (rule 5).
    confirm_name: str | None = None
    # Free-text per-task client sign-off reference — required for prod
    # (CLAUDE.md/uiux: "never write a client's prod without per-task sign-off").
    signoff: str | None = None
    priority: str = "high"


class ChecklistItemOut(BaseModel):
    key: str
    label: str
    ok: bool
    detail: str | None = None


class PipelineOut(BaseModel):
    """One safe-update run for the wizard/timeline."""

    id: int
    source_site_id: int
    source_bench_id: int
    staging_bench_id: int
    staging_site_name: str
    staging_site_id: int | None
    phase: str
    scrub_method: str | None
    checklist_ok: bool
    checklist: list[ChecklistItemOut]
    pre_backup_id: int | None
    clone_job_id: int | None
    update_job_id: int | None
    verify_job_id: int | None
    promote_job_id: int | None
    rollback_job_id: int | None
    note: str | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_model(cls, p: UpdatePipeline) -> "PipelineOut":
        checks = [ChecklistItemOut(**c) for c in ((p.checklist or {}).get("checks") or [])]
        return cls(
            id=p.id,
            source_site_id=p.source_site_id,
            source_bench_id=p.source_bench_id,
            staging_bench_id=p.staging_bench_id,
            staging_site_name=p.staging_site_name,
            staging_site_id=p.staging_site_id,
            phase=p.phase,
            scrub_method=p.scrub_method,
            checklist_ok=p.checklist_ok,
            checklist=checks,
            pre_backup_id=p.pre_backup_id,
            clone_job_id=p.clone_job_id,
            update_job_id=p.update_job_id,
            verify_job_id=p.verify_job_id,
            promote_job_id=p.promote_job_id,
            rollback_job_id=p.rollback_job_id,
            note=p.note,
            created_at=p.created_at,
            updated_at=p.updated_at,
        )


class SetEnvironmentRequest(BaseModel):
    """Classify a site's deployment environment (drives the EnvironmentBadge and
    the prod-update guardrails)."""

    environment: str  # dev | staging | prod
