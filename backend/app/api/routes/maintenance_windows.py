"""Maintenance windows API (session 3.5).

- GET    /api/maintenance-windows            list (Read-only+, optionally ?server_id=N)
- GET    /api/maintenance-windows/danger-classes   list all danger class names (Read-only+)
- POST   /api/maintenance-windows            create (Admin/Developer)
- GET    /api/maintenance-windows/{id}       one window (Read-only+)
- PATCH  /api/maintenance-windows/{id}       update (Admin/Developer)
- DELETE /api/maintenance-windows/{id}       delete (Admin/Developer)

A maintenance window blocks dangerous action-classes (update / restore /
production_setup) from being dispatched to a server while the window is active.
The enforcement guard lives in JobRunner.create — this API only manages the rows.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, require
from app.audit import Audit
from app.core.permissions import MAINTENANCE_MANAGE, READ
from app.db import get_db
from app.models.maintenance_window import DANGER_CLASSES, MaintenanceWindow
from app.schemas.maintenance_window import (
    CreateMaintenanceWindowRequest,
    MaintenanceWindowOut,
    UpdateMaintenanceWindowRequest,
)

router = APIRouter(prefix="/api/maintenance-windows", tags=["maintenance-windows"])
DbSession = Annotated[Session, Depends(get_db)]
ReadPerm = Annotated[object, Depends(require(READ))]
ManagePerm = Annotated[object, Depends(require(MAINTENANCE_MANAGE))]


def _out(mw: MaintenanceWindow) -> MaintenanceWindowOut:
    return MaintenanceWindowOut.from_model(mw)


@router.get("")
def list_windows(
    db: DbSession,
    _: ReadPerm,
    server_id: int | None = None,
) -> list[MaintenanceWindowOut]:
    q = select(MaintenanceWindow).order_by(MaintenanceWindow.id)
    if server_id is not None:
        q = q.where(MaintenanceWindow.server_id == server_id)
    rows = db.execute(q).scalars().all()
    return [_out(r) for r in rows]


@router.get("/danger-classes")
def list_danger_classes(_: ReadPerm) -> list[str]:
    return list(DANGER_CLASSES)


@router.post("", status_code=201)
def create_window(
    body: CreateMaintenanceWindowRequest,
    db: DbSession,
    user: CurrentUser,
    audit: Audit,
    _: ManagePerm,
) -> MaintenanceWindowOut:
    mw = MaintenanceWindow(
        name=body.name,
        server_id=body.server_id,
        cron=body.cron,
        duration_minutes=body.duration_minutes,
        timezone=body.timezone,
        blocked_danger_classes=body.blocked_danger_classes,
        enabled=body.enabled,
        created_by=user.id,
    )
    db.add(mw)
    db.commit()
    db.refresh(mw)
    audit.record(
        action="maintenance_window.create",
        summary=f"Created maintenance window '{mw.name}' on server {mw.server_id}",
        entity_type="maintenance_window",
        entity_id=str(mw.id),
        params={"server_id": mw.server_id, "cron": mw.cron, "blocked": mw.blocked_danger_classes},
        result="ok",
    )
    return _out(mw)


@router.get("/{window_id}")
def get_window(
    window_id: int,
    db: DbSession,
    _: ReadPerm,
) -> MaintenanceWindowOut:
    mw = db.get(MaintenanceWindow, window_id)
    if mw is None:
        raise HTTPException(status_code=404, detail="Maintenance window not found")
    return _out(mw)


@router.patch("/{window_id}")
def update_window(
    window_id: int,
    body: UpdateMaintenanceWindowRequest,
    db: DbSession,
    user: CurrentUser,
    audit: Audit,
    _: ManagePerm,
) -> MaintenanceWindowOut:
    mw = db.get(MaintenanceWindow, window_id)
    if mw is None:
        raise HTTPException(status_code=404, detail="Maintenance window not found")
    if body.name is not None:
        mw.name = body.name
    if body.cron is not None:
        mw.cron = body.cron
    if body.duration_minutes is not None:
        mw.duration_minutes = body.duration_minutes
    if body.timezone is not None:
        mw.timezone = body.timezone
    if body.blocked_danger_classes is not None:
        mw.blocked_danger_classes = body.blocked_danger_classes
    if body.enabled is not None:
        mw.enabled = body.enabled
    db.commit()
    db.refresh(mw)
    audit.record(
        action="maintenance_window.update",
        summary=f"Updated maintenance window '{mw.name}' (id={mw.id})",
        entity_type="maintenance_window",
        entity_id=str(mw.id),
        params=body.model_dump(exclude_none=True),
        result="ok",
    )
    return _out(mw)


@router.delete("/{window_id}", status_code=204)
def delete_window(
    window_id: int,
    db: DbSession,
    audit: Audit,
    _: ManagePerm,
) -> None:
    mw = db.get(MaintenanceWindow, window_id)
    if mw is None:
        raise HTTPException(status_code=404, detail="Maintenance window not found")
    name = mw.name
    server_id = mw.server_id
    db.delete(mw)
    db.commit()
    audit.record(
        action="maintenance_window.delete",
        summary=f"Deleted maintenance window '{name}' (server {server_id})",
        entity_type="maintenance_window",
        entity_id=str(window_id),
        params={"server_id": server_id},
        result="ok",
    )
