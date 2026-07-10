"""Settings API (session 1.12, B4.17): white-label + defaults + environment.

- GET  /api/settings              the operator-editable settings (public-read).
- PUT  /api/settings              update General/Defaults (settings:manage).
- POST /api/settings/logo         upload the white-label logo (settings:manage).
- GET  /api/settings/logo         stream the current logo (for the sidebar).
- GET  /api/settings/environment  read-only environment facts.

The logo is stored under `uploads_dir/branding/` and referenced by the API URL
`GET /api/settings/logo` (so the SPA needs no separate static mount). Upload is a
base64 JSON body to avoid a python-multipart dependency. Only `settings:manage`
(Admin) may mutate; everyone with `read` can view (rule 7).
"""

import base64
import binascii
import platform as _platform
import sys
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app import __version__
from app.api.deps import require
from app.audit import Audit
from app.config import get_settings
from app.core.permissions import READ, SETTINGS_MANAGE
from app.db import get_db
from app.models.settings import PlatformSettings
from app.schemas.settings import (
    ALLOWED_LOGO_TYPES,
    MAX_LOGO_BYTES,
    EnvironmentInfo,
    LogoUpload,
    PlatformSettingsOut,
    PlatformSettingsUpdate,
)

router = APIRouter(prefix="/api/settings", tags=["settings"])

DbSession = Annotated[Session, Depends(get_db)]

# content_type -> file extension for the stored logo.
_LOGO_EXT = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/svg+xml": "svg",
    "image/webp": "webp",
}
LOGO_URL = "/api/settings/logo"


def _branding_dir() -> Path:
    d = Path(get_settings().uploads_dir) / "branding"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _stored_logo(row: PlatformSettings) -> Path | None:
    """The on-disk logo file for the current settings row, if any."""
    if not row.logo_path:
        return None
    ext = row.logo_path.rsplit(".", 1)[-1] if "." in row.logo_path else None
    if not ext:
        # Older rows stored the bare API URL; probe the known extensions.
        for candidate in _LOGO_EXT.values():
            p = _branding_dir() / f"logo.{candidate}"
            if p.exists():
                return p
        return None
    p = _branding_dir() / f"logo.{ext}"
    return p if p.exists() else None


@router.get("", response_model=PlatformSettingsOut)
def get_platform_settings(
    db: DbSession, _: Annotated[object, Depends(require(READ))]
) -> PlatformSettingsOut:
    return PlatformSettingsOut.from_model(PlatformSettings.get_or_create(db))


@router.put("", response_model=PlatformSettingsOut)
def update_platform_settings(
    body: PlatformSettingsUpdate,
    db: DbSession,
    audit: Audit,
    _: Annotated[object, Depends(require(SETTINGS_MANAGE))],
) -> PlatformSettingsOut:
    row = PlatformSettings.get_or_create(db)
    fields = body.model_dump(exclude_unset=True)
    if (
        "port_range_start" in fields
        or "port_range_end" in fields
    ):
        start = fields.get("port_range_start", row.port_range_start)
        end = fields.get("port_range_end", row.port_range_end)
        if start > end:
            raise HTTPException(
                status_code=422, detail="port_range_start must be ≤ port_range_end."
            )
    for key, value in fields.items():
        setattr(row, key, value)
    db.commit()
    db.refresh(row)
    audit.record(
        action="settings.update",
        summary="Updated platform settings",
        entity_type="settings",
        entity_id=row.id,
        params={k: v for k, v in fields.items()},
    )
    return PlatformSettingsOut.from_model(row)


@router.post("/logo", response_model=PlatformSettingsOut)
def upload_logo(
    body: LogoUpload,
    db: DbSession,
    audit: Audit,
    _: Annotated[object, Depends(require(SETTINGS_MANAGE))],
) -> PlatformSettingsOut:
    if body.content_type not in ALLOWED_LOGO_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"Unsupported image type {body.content_type!r}; expected one of "
            f"{list(ALLOWED_LOGO_TYPES)}.",
        )
    payload = body.content_base64.split(",", 1)[-1]  # tolerate a data: URL prefix.
    try:
        data = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=422, detail="Logo is not valid base64.") from exc
    if not data:
        raise HTTPException(status_code=422, detail="Logo is empty.")
    if len(data) > MAX_LOGO_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Logo exceeds the {MAX_LOGO_BYTES // 1024} KB limit.",
        )

    ext = _LOGO_EXT[body.content_type]
    # Remove any previously stored logo of a different type so only one remains.
    for other in _LOGO_EXT.values():
        stale = _branding_dir() / f"logo.{other}"
        if other != ext and stale.exists():
            stale.unlink()
    (_branding_dir() / f"logo.{ext}").write_bytes(data)

    row = PlatformSettings.get_or_create(db)
    # Store the extensionless API URL; GET /logo probes the stored file. The
    # frontend cache-busts with the settings' updated_at.
    row.logo_path = LOGO_URL
    db.commit()
    db.refresh(row)
    audit.record(
        action="settings.logo",
        summary="Uploaded white-label logo",
        entity_type="settings",
        entity_id=row.id,
        params={"content_type": body.content_type, "bytes": len(data)},
    )
    return PlatformSettingsOut.from_model(row)


@router.get("/logo")
def get_logo(db: DbSession, _: Annotated[object, Depends(require(READ))]) -> FileResponse:
    row = PlatformSettings.get_or_create(db)
    path = _stored_logo(row)
    if path is None:
        raise HTTPException(status_code=404, detail="No logo configured.")
    # An uploaded SVG can carry inline script. Only an Admin can upload one, but
    # serve every logo locked down so opening the URL directly can't execute it:
    # a restrictive CSP + nosniff neutralises script/embed even for image/svg+xml.
    return FileResponse(
        path,
        headers={
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/environment", response_model=EnvironmentInfo)
def environment(_: Annotated[object, Depends(require(READ))]) -> EnvironmentInfo:
    settings = get_settings()
    backend = "postgresql" if settings.database_url.startswith("postgres") else (
        "sqlite" if settings.database_url.startswith("sqlite") else "other"
    )
    return EnvironmentInfo(
        app_version=__version__,
        python_version=sys.version.split()[0],
        platform=_platform.platform(),
        database_backend=backend,
        redis_configured=bool(settings.redis_url),
        debug=settings.debug,
        default_tz=settings.default_tz,
        monitoring_enabled=settings.monitoring_enabled,
        monitoring_interval_seconds=settings.monitoring_interval_seconds,
    )
