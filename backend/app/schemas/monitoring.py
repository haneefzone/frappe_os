"""Response models for the monitoring API (session 1.12)."""

from datetime import datetime

from pydantic import BaseModel

from app.models.monitoring import MonitoringSample


class MonitoringSampleOut(BaseModel):
    """One point-in-time resource + service snapshot."""

    id: int
    server_id: int
    ts: datetime
    ok: bool
    error: str | None
    cpu_pct: float | None
    mem_pct: float | None
    mem_used_mb: int | None
    mem_total_mb: int | None
    disk_pct: float | None
    disk_used_gb: float | None
    disk_total_gb: float | None
    load1: float | None
    services: dict

    @classmethod
    def from_model(cls, s: MonitoringSample) -> "MonitoringSampleOut":
        return cls(
            id=s.id,
            server_id=s.server_id,
            ts=s.ts,
            ok=s.ok,
            error=s.error,
            cpu_pct=s.cpu_pct,
            mem_pct=s.mem_pct,
            mem_used_mb=s.mem_used_mb,
            mem_total_mb=s.mem_total_mb,
            disk_pct=s.disk_pct,
            disk_used_gb=s.disk_used_gb,
            disk_total_gb=s.disk_total_gb,
            load1=s.load1,
            services=s.services or {},
        )


class MonitoringSeries(BaseModel):
    """A time window of samples for one server plus its most recent point."""

    server_id: int
    latest: MonitoringSampleOut | None
    samples: list[MonitoringSampleOut]
