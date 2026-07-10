"""Response/request models for the site uptime API (session 2.7)."""

from datetime import datetime

from pydantic import BaseModel

from app.models import UptimeSample


class UptimeSampleOut(BaseModel):
    """One external HTTP probe point (for the response-time sparkline)."""

    ts: datetime
    up: bool
    status_code: int | None
    latency_ms: float | None
    error: str | None

    @classmethod
    def from_model(cls, s: UptimeSample) -> "UptimeSampleOut":
        return cls(
            ts=s.ts,
            up=s.up,
            status_code=s.status_code,
            latency_ms=s.latency_ms,
            error=s.error,
        )


class UptimeSummary(BaseModel):
    """Rolling uptime + last-response facts for a site's health card."""

    uptime_24h_pct: float | None
    uptime_30d_pct: float | None
    samples_24h: int
    samples_30d: int
    currently_up: bool | None
    last_status_code: int | None
    last_latency_ms: float | None
    last_checked_at: str | None


class UptimeSeries(BaseModel):
    """A window of uptime samples for one site plus the rolling summary."""

    site_id: int
    enabled: bool
    check_url: str | None
    summary: UptimeSummary
    samples: list[UptimeSampleOut]


class UptimeConfigRequest(BaseModel):
    """Toggle uptime checking / set a check-URL override for a site."""

    enabled: bool | None = None
    check_url: str | None = None
