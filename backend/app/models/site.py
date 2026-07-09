"""Frappe sites living inside a discovered bench (session 1.8).

A `Site` is one Frappe site directory under a bench's `sites/` folder — the dir
that holds a `site_config.json`. Discovery lists those dirs and reads what is
cheap from the config (maintenance flag); scheduler state and health are filled
in as toggle jobs run or a later health-check session lands.

Like benches, a site whose directory has vanished is not deleted — it is marked
`status="missing"` so history and any job references survive.
"""

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

# Lifecycle: active = seen in the last discovery; missing = its dir has vanished.
SITE_STATUSES = ("active", "missing")
# Health is a placeholder until the monitoring session pings the site over HTTP.
SITE_HEALTH = ("unknown", "ok", "warn", "err")


class Site(Base):
    """One Frappe site discovered (or freshly created) inside a bench."""

    __tablename__ = "sites"
    __table_args__ = (
        # A site is identified by its name within a bench; discovery upserts on it.
        UniqueConstraint("bench_id", "name", name="uq_sites_bench_name"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    bench_id: Mapped[int] = mapped_column(
        ForeignKey("benches.id", ondelete="CASCADE"), index=True
    )
    # The site directory name, which is also the site's host name (e.g.
    # "test1.localhost"). Reached in a browser as http://<server-ip>:<port> with
    # this value sent as the Host header.
    name: Mapped[str] = mapped_column(String(200))

    # active | missing (see SITE_STATUSES).
    status: Mapped[str] = mapped_column(String(20), default="active", index=True)

    # Scheduler on/off. Nullable = not yet known: it lives in the site DB (System
    # Settings), which discovery does not read; a scheduler toggle job sets it.
    scheduler_enabled: Mapped[bool | None] = mapped_column(Boolean)
    # Maintenance mode, read cheaply from site_config.json by discovery.
    maintenance_mode: Mapped[bool] = mapped_column(Boolean, default=False)
    # unknown | ok | warn | err — a placeholder dot until HTTP health checks land.
    health: Mapped[str] = mapped_column(String(20), default="unknown")

    # When this site was last seen by a discovery run (UTC).
    discovered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    bench: Mapped["Bench"] = relationship(back_populates="sites")  # noqa: F821
