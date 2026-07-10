"""Dashboard API (session 1.12, B4.1). One read-only endpoint that returns the
whole "is everything okay?" payload (KPIs, morning brief, servers strip, 7-day
backup grid, running jobs, first-run flag). Aggregation lives in
`app.core.dashboard`; this is a thin read behind the `read` permission."""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import require
from app.core.dashboard import build_dashboard
from app.core.permissions import READ
from app.db import get_db

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])

DbSession = Annotated[Session, Depends(get_db)]


@router.get("")
def get_dashboard(
    db: DbSession, _: Annotated[object, Depends(require(READ))]
) -> dict:
    return build_dashboard(db)
