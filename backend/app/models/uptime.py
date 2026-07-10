"""External HTTP(S) uptime samples per site (session 2.7).

A background checker makes one **external, read-only** HTTP(S) request to each
enabled site every ~60s and records a single `UptimeSample`: whether it was up
(a non-error status code), the status code, the response latency in ms, and a
short error string on failure. The site Overview reads a time window (uptime %
and a response-time sparkline); the Fleet Health uptime component reads a 30-day
window across the fleet.

Like `MonitoringSample`, these are telemetry — never a `CommandJob`/`AuditLog`
row (they are reads, and one row per site every 60s would drown both tables).
The table is a rolling ring buffer: the checker prunes rows older than the
retention window after each insert, so it never grows without bound.
"""

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class UptimeSample(Base):
    """One external HTTP(S) probe of a site: up/down, status, latency."""

    __tablename__ = "uptime_samples"

    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(
        ForeignKey("sites.id", ondelete="CASCADE"), index=True
    )

    # True when the site answered with a non-error status (< 400). False when the
    # request failed (connection refused / timeout / DNS) or returned >= 400.
    up: Mapped[bool] = mapped_column(Boolean, default=False)
    # HTTP status code when a response came back; NULL when the request never
    # completed (connection refused, timeout, DNS failure).
    status_code: Mapped[int | None] = mapped_column(Integer)
    # Wall-clock response time in milliseconds; NULL on a failed request.
    latency_ms: Mapped[float | None] = mapped_column(Float)
    # Set when up is False: a short reason ("connection refused", "timeout",
    # "HTTP 502"). Never NULL-vs-empty ambiguous — empty means "no error".
    error: Mapped[str | None] = mapped_column(String(300))

    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )

    __table_args__ = (
        # "latest sample per site" and "window for site X" both walk
        # (site_id, ts DESC); one composite index serves both.
        Index("ix_uptime_samples_site_ts", "site_id", "ts"),
    )
