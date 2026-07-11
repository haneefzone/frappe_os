"""S3-compatible storage-targets API (session 2.2).

- GET    /api/storage-targets            list targets (no secret material)
- POST   /api/storage-targets            register a target (Admin; keys encrypted)
- GET    /api/storage-targets/{id}       one target
- PATCH  /api/storage-targets/{id}       update (keys write-only: set/clear/keep)
- DELETE /api/storage-targets/{id}       remove a target
- POST   /api/storage-targets/{id}/test-connection   probe reachable/writable/latency

Config endpoints are gated on `settings:manage` (Admin only in the default role
matrix) because a target carries S3 credentials. The keys are accepted on
create/update, Fernet-encrypted immediately, and never returned to the browser
(rule 6). Every mutation writes an audit row; the audited params never include
the key material.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import require
from app.audit import Audit
from app.core import storage as st
from app.core.permissions import SETTINGS_MANAGE
from app.core.security import SecretsService, get_secrets_service
from app.db import get_db
from app.models.storage import STORAGE_PROVIDERS, StorageTarget
from app.schemas.storage import (
    StorageTargetCreate,
    StorageTargetOut,
    StorageTargetUpdate,
    TestConnectionOut,
)

router = APIRouter(prefix="/api/storage-targets", tags=["storage"])

DbSession = Annotated[Session, Depends(get_db)]
Secrets = Annotated[SecretsService, Depends(get_secrets_service)]
ManageStorage = Annotated[object, Depends(require(SETTINGS_MANAGE))]


def _get_or_404(db: Session, target_id: int) -> StorageTarget:
    target = db.get(StorageTarget, target_id)
    if target is None:
        raise HTTPException(status_code=404, detail="Storage target not found.")
    return target


@router.get("", response_model=list[StorageTargetOut])
def list_targets(db: DbSession, _: ManageStorage) -> list[StorageTargetOut]:
    rows = db.scalars(select(StorageTarget).order_by(StorageTarget.name)).all()
    return [StorageTargetOut.from_model(t) for t in rows]


@router.get("/{target_id}", response_model=StorageTargetOut)
def get_target(target_id: int, db: DbSession, _: ManageStorage) -> StorageTargetOut:
    return StorageTargetOut.from_model(_get_or_404(db, target_id))


@router.post("", status_code=201, response_model=StorageTargetOut)
def create_target(
    body: StorageTargetCreate,
    db: DbSession,
    secrets: Secrets,
    audit: Audit,
    _: ManageStorage,
) -> StorageTargetOut:
    if db.scalars(
        select(StorageTarget).where(StorageTarget.name == body.name)
    ).first():
        raise HTTPException(
            status_code=409, detail=f"A storage target named {body.name!r} already exists."
        )
    target = StorageTarget(
        name=body.name,
        provider=body.provider_normalized(),
        endpoint_url=body.endpoint_url or None,
        region=body.region or None,
        bucket=body.bucket,
        path_prefix=(body.path_prefix or None),
        access_key_enc=secrets.encrypt(body.access_key),
        secret_key_enc=secrets.encrypt(body.secret_key),
        use_ssl=body.use_ssl,
        enabled=body.enabled,
    )
    db.add(target)
    db.commit()
    db.refresh(target)
    audit.record(
        action="storage.target.create",
        summary=f"Registered storage target {target.name!r} ({target.provider})",
        entity_type="storage_target",
        entity_id=target.id,
        params={"bucket": target.bucket, "provider": target.provider},
    )
    return StorageTargetOut.from_model(target)


@router.patch("/{target_id}", response_model=StorageTargetOut)
def update_target(
    target_id: int,
    body: StorageTargetUpdate,
    db: DbSession,
    secrets: Secrets,
    audit: Audit,
    _: ManageStorage,
) -> StorageTargetOut:
    target = _get_or_404(db, target_id)
    fields = body.model_dump(exclude_unset=True)

    # Keys are write-only: a non-empty value re-encrypts, an empty string clears,
    # omitting them leaves them untouched. Never store/log the plaintext.
    key_actions: dict[str, str] = {}
    for enc_col, field in (("access_key_enc", "access_key"), ("secret_key_enc", "secret_key")):
        if field in fields:
            raw = fields.pop(field)
            if raw:
                setattr(target, enc_col, secrets.encrypt(raw))
                key_actions[field] = "set"
            else:
                setattr(target, enc_col, None)
                key_actions[field] = "cleared"

    if "name" in fields and fields["name"] != target.name:
        if db.scalars(
            select(StorageTarget).where(StorageTarget.name == fields["name"])
        ).first():
            raise HTTPException(
                status_code=409,
                detail=f"A storage target named {fields['name']!r} already exists.",
            )
    if "provider" in fields and fields["provider"] not in STORAGE_PROVIDERS:
        fields["provider"] = "other"

    for key, value in fields.items():
        setattr(target, key, value)
    db.commit()
    db.refresh(target)

    audited = {k: v for k, v in fields.items()}
    for field, act in key_actions.items():
        audited[f"{field}_updated"] = act
    audit.record(
        action="storage.target.update",
        summary=f"Updated storage target {target.name!r}",
        entity_type="storage_target",
        entity_id=target.id,
        params=audited,
    )
    return StorageTargetOut.from_model(target)


@router.delete("/{target_id}", status_code=204)
def delete_target(
    target_id: int, db: DbSession, audit: Audit, _: ManageStorage
) -> None:
    target = _get_or_404(db, target_id)
    name = target.name
    db.delete(target)
    db.commit()
    audit.record(
        action="storage.target.delete",
        summary=f"Deleted storage target {name!r}",
        entity_type="storage_target",
        entity_id=target_id,
    )


@router.post("/{target_id}/test-connection", response_model=TestConnectionOut)
def test_connection(
    target_id: int,
    db: DbSession,
    secrets: Secrets,
    audit: Audit,
    _: ManageStorage,
) -> TestConnectionOut:
    """Probe a target: HEAD the bucket (reachable) + write/delete a marker
    (writable), with the round-trip latency. Never raises on an S3 failure — the
    red state is carried in the response so the UI renders it cleanly."""
    target = _get_or_404(db, target_id)
    result = st.test_connection(target, secrets=secrets)
    audit.record(
        action="storage.target.test",
        summary=f"Tested storage target {target.name!r}",
        entity_type="storage_target",
        entity_id=target.id,
        params={"reachable": result.reachable, "writable": result.writable},
        result="ok" if result.reachable else "error",
    )
    return TestConnectionOut(
        reachable=result.reachable,
        writable=result.writable,
        latency_ms=result.latency_ms,
        error=result.error,
    )
