"""Operator-editable platform settings (session 1.12 Settings page).

A single row (`id == 1`) holds the white-label + defaults layer that the UI reads
at runtime: the product name and logo that replace the sidebar branding, the
default timezone, and the defaults the Create-Bench wizard pre-fills (bench base
path, port range). These are distinct from `app.config.Settings` (env-driven
secrets/infra, immutable at runtime) — this row is the handful of values a
non-technical operator changes from the UI without touching the environment.

`get_or_create(db)` returns the singleton, materialising it with shipped defaults
on first read so the app works before anyone visits Settings.
"""

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.config import get_settings
from app.db import Base

# The single row's fixed primary key.
SINGLETON_ID = 1


class PlatformSettings(Base):
    """The one-row white-label + defaults record."""

    __tablename__ = "platform_settings"

    id: Mapped[int] = mapped_column(primary_key=True, default=SINGLETON_ID)

    # White-label layer (General tab). product_name replaces the sidebar title;
    # logo_path is a server-relative URL under the uploads dir (NULL = wordmark).
    product_name: Mapped[str] = mapped_column(String(80), default="FDM Platform")
    logo_path: Mapped[str | None] = mapped_column(String(300))

    # Default timezone for rendering timestamps (CLAUDE.md rule 8 default).
    default_tz: Mapped[str] = mapped_column(String(64), default="Asia/Dubai")

    # Defaults tab: what the Create-Bench wizard pre-fills.
    bench_base_path: Mapped[str] = mapped_column(String(300), default="/home/frappe")
    port_range_start: Mapped[int] = mapped_column(Integer, default=8000)
    port_range_end: Mapped[int] = mapped_column(Integer, default=8999)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    @classmethod
    def get_or_create(cls, db: Session) -> "PlatformSettings":
        row = db.get(cls, SINGLETON_ID)
        if row is None:
            row = cls(id=SINGLETON_ID, default_tz=get_settings().default_tz)
            db.add(row)
            db.commit()
            db.refresh(row)
        return row
