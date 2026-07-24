"""Settings API (sessions 1.12 + 6.6): white-label brand layer + defaults + environment.

- GET  /api/branding              public brand bundle (unauthenticated-safe, session 6.6)
- GET  /api/settings              the operator-editable settings (read-role required)
- PUT  /api/settings              update General/Defaults (settings:manage)
- POST /api/settings/logo         upload the light-theme logo (settings:manage)
- GET  /api/settings/logo         stream the light logo (for the sidebar)
- POST /api/settings/logo-dark    upload the dark-theme logo variant (settings:manage)
- GET  /api/settings/logo-dark    stream the dark logo
- POST /api/settings/favicon      upload the favicon (settings:manage)
- GET  /api/settings/favicon      stream the favicon
- GET  /api/settings/environment  read-only environment facts

Uploads are base64 JSON bodies (avoids a python-multipart dependency). Content type
is validated server-side (not by extension). Only `settings:manage` (Admin) may
mutate; every authenticated user can view.

/api/branding intentionally exposes only the public brand bundle and requires no
auth so the login page and setup wizard can render branded before auth.
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
    ALLOWED_FAVICON_TYPES,
    ALLOWED_LOGO_TYPES,
    MAX_FAVICON_BYTES,
    MAX_LOGO_BYTES,
    BrandingOut,
    EnvironmentInfo,
    FaviconUpload,
    LogoUpload,
    PlatformSettingsOut,
    PlatformSettingsUpdate,
)

router = APIRouter(tags=["settings"])

DbSession = Annotated[Session, Depends(get_db)]

# content_type → file extension for stored logos.
_LOGO_EXT = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/svg+xml": "svg",
    "image/webp": "webp",
}

# content_type → file extension for stored favicons.
_FAVICON_EXT = {
    "image/png": "png",
    "image/x-icon": "ico",
    "image/vnd.microsoft.icon": "ico",
}

LOGO_URL = "/api/settings/logo"
LOGO_DARK_URL = "/api/settings/logo-dark"
FAVICON_URL = "/api/settings/favicon"

# Lock-down headers for user-uploaded images so a served SVG can't run script.
_UPLOAD_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox",
    "X-Content-Type-Options": "nosniff",
}


def _branding_dir() -> Path:
    d = Path(get_settings().uploads_dir) / "branding"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _stored_file(stem: str, ext_map: dict[str, str]) -> Path | None:
    """Return the stored file for `stem` (e.g. "logo", "favicon"), or None."""
    d = _branding_dir()
    for ext in ext_map.values():
        p = d / f"{stem}.{ext}"
        if p.exists():
            return p
    return None


def _decode_upload(content_base64: str, max_bytes: int, label: str) -> bytes:
    """Strip data-URL prefix, base64-decode, and enforce size cap."""
    payload = content_base64.split(",", 1)[-1]
    try:
        data = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=422, detail=f"{label} is not valid base64.") from exc
    if not data:
        raise HTTPException(status_code=422, detail=f"{label} is empty.")
    if len(data) > max_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"{label} exceeds the {max_bytes // 1024} KB limit.",
        )
    return data


def _save_upload(stem: str, ext: str, data: bytes, ext_map: dict[str, str]) -> None:
    """Atomically replace the stored file for `stem`, pruning old extensions."""
    d = _branding_dir()
    for other_ext in ext_map.values():
        stale = d / f"{stem}.{other_ext}"
        if other_ext != ext and stale.exists():
            stale.unlink()
    (d / f"{stem}.{ext}").write_bytes(data)


# --------------------------------------------------------------------------- #
# Public branding bundle (unauthenticated)                                    #
# --------------------------------------------------------------------------- #

@router.get("/api/branding", response_model=BrandingOut)
def get_branding(db: DbSession) -> BrandingOut:
    """Public brand bundle — safe to call without auth.

    Returns only the fields the login page and setup wizard need before
    authentication. No sensitive settings leak here.
    """
    return BrandingOut.from_model(PlatformSettings.get_or_create(db))


# --------------------------------------------------------------------------- #
# Authenticated settings CRUD                                                 #
# --------------------------------------------------------------------------- #

@router.get("/api/settings", response_model=PlatformSettingsOut)
def get_platform_settings(
    db: DbSession, _: Annotated[object, Depends(require(READ))]
) -> PlatformSettingsOut:
    return PlatformSettingsOut.from_model(PlatformSettings.get_or_create(db))


@router.put("/api/settings", response_model=PlatformSettingsOut)
def update_platform_settings(
    body: PlatformSettingsUpdate,
    db: DbSession,
    audit: Audit,
    _: Annotated[object, Depends(require(SETTINGS_MANAGE))],
) -> PlatformSettingsOut:
    row = PlatformSettings.get_or_create(db)
    fields = body.model_dump(exclude_unset=True)
    if "port_range_start" in fields or "port_range_end" in fields:
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


# --------------------------------------------------------------------------- #
# Logo (light theme)                                                          #
# --------------------------------------------------------------------------- #

@router.post("/api/settings/logo", response_model=PlatformSettingsOut)
def upload_logo(
    body: LogoUpload,
    db: DbSession,
    audit: Audit,
    _: Annotated[object, Depends(require(SETTINGS_MANAGE))],
) -> PlatformSettingsOut:
    if body.content_type not in ALLOWED_LOGO_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"Unsupported type {body.content_type!r}; "
            f"expected one of {list(ALLOWED_LOGO_TYPES)}.",
        )
    data = _decode_upload(body.content_base64, MAX_LOGO_BYTES, "Logo")
    ext = _LOGO_EXT[body.content_type]
    _save_upload("logo", ext, data, _LOGO_EXT)

    row = PlatformSettings.get_or_create(db)
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


@router.get("/api/settings/logo")
def get_logo(db: DbSession) -> FileResponse:
    PlatformSettings.get_or_create(db)
    path = _stored_file("logo", _LOGO_EXT)
    if path is None:
        raise HTTPException(status_code=404, detail="No logo configured.")
    return FileResponse(path, headers=_UPLOAD_HEADERS)


# --------------------------------------------------------------------------- #
# Logo dark variant                                                           #
# --------------------------------------------------------------------------- #

@router.post("/api/settings/logo-dark", response_model=PlatformSettingsOut)
def upload_logo_dark(
    body: LogoUpload,
    db: DbSession,
    audit: Audit,
    _: Annotated[object, Depends(require(SETTINGS_MANAGE))],
) -> PlatformSettingsOut:
    if body.content_type not in ALLOWED_LOGO_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"Unsupported type {body.content_type!r}; "
            f"expected one of {list(ALLOWED_LOGO_TYPES)}.",
        )
    data = _decode_upload(body.content_base64, MAX_LOGO_BYTES, "Dark logo")
    ext = _LOGO_EXT[body.content_type]
    _save_upload("logo-dark", ext, data, _LOGO_EXT)

    row = PlatformSettings.get_or_create(db)
    row.logo_dark_path = LOGO_DARK_URL
    db.commit()
    db.refresh(row)
    audit.record(
        action="settings.logo_dark",
        summary="Uploaded dark-theme logo variant",
        entity_type="settings",
        entity_id=row.id,
        params={"content_type": body.content_type, "bytes": len(data)},
    )
    return PlatformSettingsOut.from_model(row)


@router.get("/api/settings/logo-dark")
def get_logo_dark(db: DbSession) -> FileResponse:
    PlatformSettings.get_or_create(db)
    path = _stored_file("logo-dark", _LOGO_EXT)
    if path is None:
        raise HTTPException(status_code=404, detail="No dark logo configured.")
    return FileResponse(path, headers=_UPLOAD_HEADERS)


# --------------------------------------------------------------------------- #
# Favicon                                                                     #
# --------------------------------------------------------------------------- #

@router.post("/api/settings/favicon", response_model=PlatformSettingsOut)
def upload_favicon(
    body: FaviconUpload,
    db: DbSession,
    audit: Audit,
    _: Annotated[object, Depends(require(SETTINGS_MANAGE))],
) -> PlatformSettingsOut:
    if body.content_type not in ALLOWED_FAVICON_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"Unsupported type {body.content_type!r}; expected PNG or ICO.",
        )
    data = _decode_upload(body.content_base64, MAX_FAVICON_BYTES, "Favicon")
    ext = _FAVICON_EXT[body.content_type]
    _save_upload("favicon", ext, data, _FAVICON_EXT)

    row = PlatformSettings.get_or_create(db)
    row.favicon_path = FAVICON_URL
    db.commit()
    db.refresh(row)
    audit.record(
        action="settings.favicon",
        summary="Uploaded favicon",
        entity_type="settings",
        entity_id=row.id,
        params={"content_type": body.content_type, "bytes": len(data)},
    )
    return PlatformSettingsOut.from_model(row)


@router.get("/api/settings/favicon")
def get_favicon() -> FileResponse:
    path = _stored_file("favicon", _FAVICON_EXT)
    if path is None:
        raise HTTPException(status_code=404, detail="No favicon configured.")
    # Favicons are not SVG so no script risk, but still nosniff for hygiene.
    return FileResponse(path, headers={"X-Content-Type-Options": "nosniff"})


# --------------------------------------------------------------------------- #
# Environment (read-only)                                                     #
# --------------------------------------------------------------------------- #

@router.get("/api/settings/environment", response_model=EnvironmentInfo)
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
