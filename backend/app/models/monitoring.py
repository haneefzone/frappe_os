"""Lightweight per-server telemetry samples (session 1.12 monitoring).

A background poller SSHes each active server every ~60s (configurable) and
records one `MonitoringSample`: CPU %, RAM %, disk % for `/`, the 1-minute load
average, and the `systemctl is-active` state of the four managed services
(nginx, mariadb, redis-server, supervisor). The dashboard's server strip and the
server-detail Overview read the *latest* sample per server; the monitoring charts
read a time window.

These are telemetry, not command jobs — they never create `CommandJob`/`AuditLog`
rows (they are reads, and one row every 60s would drown both tables). The table
is a rolling ring buffer: the poller prunes rows older than the retention window
after each insert, so it never grows without bound.
"""

from datetime import datetime

from sqlalchemy import (
    JSON,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

# JSONB on Postgres, plain JSON on the SQLite test fallback (mirrors job.py).
ServicesJSON = JSON().with_variant(JSONB(), "postgresql")

# The four services the sudoers allowlist lets us query + restart.
MANAGED_SERVICES = ("nginx", "mariadb", "redis-server", "supervisor")


class MonitoringSample(Base):
    """One point-in-time resource + service snapshot for a server."""

    __tablename__ = "monitoring_samples"

    id: Mapped[int] = mapped_column(primary_key=True)
    server_id: Mapped[int] = mapped_column(
        ForeignKey("servers.id", ondelete="CASCADE"), index=True
    )

    # True if the poll completed and the numbers below are real; False when the
    # SSH poll failed (server down / unreachable) — the row is still recorded so
    # the dashboard can show "no data / unreachable" instead of a stale gauge.
    ok: Mapped[bool] = mapped_column(default=True)
    # Set when ok is False: a short reason ("connection refused", "timeout").
    error: Mapped[str | None] = mapped_column(String(300))

    # Percentages 0-100 (NULL on a failed poll).
    cpu_pct: Mapped[float | None] = mapped_column(Float)
    mem_pct: Mapped[float | None] = mapped_column(Float)
    disk_pct: Mapped[float | None] = mapped_column(Float)

    # Absolute figures for the tooltip ("6.2 / 16 GB").
    mem_used_mb: Mapped[int | None] = mapped_column(Integer)
    mem_total_mb: Mapped[int | None] = mapped_column(Integer)
    disk_used_gb: Mapped[float | None] = mapped_column(Float)
    disk_total_gb: Mapped[float | None] = mapped_column(Float)

    # 1-minute load average (NULL on a failed poll).
    load1: Mapped[float | None] = mapped_column(Float)

    # {service_name: "active"|"inactive"|"failed"|"unknown"} for MANAGED_SERVICES.
    services: Mapped[dict] = mapped_column(ServicesJSON, default=dict)

    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )

    __table_args__ = (
        # "latest sample per server" and "window for server X" both walk
        # (server_id, ts DESC); one composite index serves both.
        Index("ix_monitoring_samples_server_ts", "server_id", "ts"),
    )
