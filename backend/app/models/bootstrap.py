"""Bootstrap state singleton (session 6.4).

A single row (id == 1) persists whether first-run setup has completed.
The bootstrap routes (GET /api/bootstrap/status, POST /api/bootstrap/complete)
flip setup_complete True exactly once — persisted in the DB so the 410 survives
a backend restart.  The routes are the only unauthenticated mutating surface.
"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

SINGLETON_ID = 1


class BootstrapState(Base):
    __tablename__ = "bootstrap_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=SINGLETON_ID)
    setup_complete: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_by_user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # Track who triggered setup for audit; set to request IP at completion time.
    completed_from_ip: Mapped[str | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    @classmethod
    def get_or_create(cls, db) -> "BootstrapState":
        row = db.get(cls, SINGLETON_ID)
        if row is None:
            row = cls(id=SINGLETON_ID, setup_complete=False)
            db.add(row)
            db.commit()
            db.refresh(row)
        return row
