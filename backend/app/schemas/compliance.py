"""Request/response models for the backup-compliance API (session 2.3)."""

from datetime import datetime

from pydantic import BaseModel, Field

from app.models.compliance import BackupPolicy, ComplianceStatus


class PolicyOut(BaseModel):
    """A site's backup policy (null fields when a dimension isn't policed)."""

    site_id: int
    rpo_hours: int
    retention_days: int | None
    require_offsite: bool
    require_restore_test: bool
    enabled: bool
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_model(cls, policy: BackupPolicy) -> "PolicyOut":
        return cls(
            site_id=policy.site_id,
            rpo_hours=policy.rpo_hours,
            retention_days=policy.retention_days,
            require_offsite=policy.require_offsite,
            require_restore_test=policy.require_restore_test,
            enabled=policy.enabled,
            created_at=policy.created_at,
            updated_at=policy.updated_at,
        )


class UpsertPolicyRequest(BaseModel):
    """Create or replace a site's backup policy (PUT semantics — the full policy
    is supplied; omitted optional fields take their documented defaults)."""

    rpo_hours: int = Field(default=24, ge=1, le=8760)  # 1h .. 1y
    retention_days: int | None = Field(default=None, ge=1, le=3650)
    require_offsite: bool = False
    require_restore_test: bool = False
    enabled: bool = True


class ComplianceStatusOut(BaseModel):
    """One site's evaluated compliance state (drives the UI ticks)."""

    site_id: int
    site_name: str | None = None
    state: str
    last_backup_at: datetime | None
    breaches: list[dict]
    evaluated_at: datetime | None

    @classmethod
    def from_model(
        cls, status: ComplianceStatus, *, site_name: str | None = None
    ) -> "ComplianceStatusOut":
        return cls(
            site_id=status.site_id,
            site_name=site_name,
            state=status.state,
            last_backup_at=status.last_backup_at,
            breaches=list(status.breaches or []),
            evaluated_at=status.evaluated_at,
        )


class ComplianceSummaryOut(BaseModel):
    """Fleet-wide compliance rollup for the dashboard + Backups Policies tab.

    `compliance_pct` = round(compliant / policied * 100); 100 when nothing is
    policied (vacuously compliant — nothing to fail).
    """

    policied: int
    compliant: int
    breached: int
    compliance_pct: int
    statuses: list[ComplianceStatusOut]
