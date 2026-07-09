"""Discovered Frappe benches (session 1.6).

A `Bench` is a plain `bench init` install found on a managed server over SSH:
a directory holding `apps/frappe` and `sites/`. Discovery reads each bench's
`sites/common_site_config.json` for its port map and runs `bench version`
inside it, then upserts one row per bench keyed on `(server_id, path)`.

The row is the platform's cached view of the bench; a later discovery refreshes
it. A bench whose directory has vanished is not deleted — it is marked
`status="missing"` so history and any references survive, and so the UI can
show it went away rather than silently dropping it.
"""

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

# Lifecycle: active = seen in the last discovery; missing = its dir has vanished.
BENCH_STATUSES = ("active", "missing")


class Bench(Base):
    """One Frappe bench discovered on a server."""

    __tablename__ = "benches"
    __table_args__ = (
        # A bench is identified by its path on a server; discovery upserts on it.
        UniqueConstraint("server_id", "path", name="uq_benches_server_path"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    server_id: Mapped[int] = mapped_column(
        ForeignKey("servers.id", ondelete="CASCADE"), index=True
    )
    # Directory basename (e.g. "frappe-bench") — the display name.
    name: Mapped[str] = mapped_column(String(200))
    # Absolute path of the bench directory on the server.
    path: Mapped[str] = mapped_column(String(500))

    # Versions parsed from `bench version` and the bench's own interpreters.
    frappe_version: Mapped[str | None] = mapped_column(String(50))
    python_version: Mapped[str | None] = mapped_column(String(50))
    node_version: Mapped[str | None] = mapped_column(String(50))

    # Port map parsed from sites/common_site_config.json (rule: detect conflicts
    # before creating a new bench on the same server — CLAUDE.md gotcha 8).
    webserver_port: Mapped[int | None] = mapped_column(Integer)
    socketio_port: Mapped[int | None] = mapped_column(Integer)
    redis_cache_port: Mapped[int | None] = mapped_column(Integer)
    redis_queue_port: Mapped[int | None] = mapped_column(Integer)
    redis_socketio_port: Mapped[int | None] = mapped_column(Integer)
    file_watcher_port: Mapped[int | None] = mapped_column(Integer)

    # Production benches run under supervisor/systemd; dev benches use `bench start`.
    is_production: Mapped[bool] = mapped_column(Boolean, default=False)
    # active | missing (see BENCH_STATUSES).
    status: Mapped[str] = mapped_column(String(20), default="active", index=True)
    # When this bench was last seen by a discovery run (UTC).
    discovered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Sites discovered inside this bench (session 1.8). Cascade-delete so a bench
    # row going away takes its site rows with it.
    sites: Mapped[list["Site"]] = relationship(  # noqa: F821
        back_populates="bench",
        cascade="all, delete-orphan",
        order_by="Site.name",
    )
