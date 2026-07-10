"""The shared audit writer (CLAUDE.md golden rule 2, formalized in session 1.12).

`record_audit()` is the single funnel every mutating code path uses to append one
immutable `AuditLog` row. Two callers cover the whole surface:

- **Job-backed mutations** — `JobRunner.create()` calls this for every job it
  enqueues, so no command action can ever land un-audited. The params it passes
  are the job's `params_sanitized` (secrets already masked as ``••••``).
- **Non-job mutations** — server / app-source / settings CRUD, backup download,
  login/logout — call it directly via the `AuditContext` request dependency,
  which captures the actor + source IP for the endpoint.

The request's source IP is stashed in a `ContextVar` by `AuditContext` (a
per-request dependency), so `JobRunner.create()` — which has no `Request` — still
records the caller's IP without every job endpoint threading it through. Outside
a request (e.g. the monitoring poller) the var is empty and `source_ip` is NULL.

`record_audit()` never raises into the caller: an audit write must not turn a
successful mutation into a 500. A failure is logged and swallowed (the mutation
already happened); the audit trail losing one row is strictly better than the
operation appearing to fail after it ran.
"""

from __future__ import annotations

import logging
from contextvars import ContextVar
from typing import Annotated, Any

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser
from app.db import get_db
from app.models.audit import AuditLog

logger = logging.getLogger("app.audit")

# Set per request by AuditContext; read by record_audit for job-backed rows.
_source_ip: ContextVar[str | None] = ContextVar("audit_source_ip", default=None)

# Fields whose *values* may carry a secret regardless of the template's secret
# flags — masked defensively even when a caller passes a raw dict (rule 6).
_SENSITIVE_KEYS = {"password", "admin_password", "mariadb_root_password", "secret",
                   "token", "key", "deploy_key", "private_key"}
_MASK = "••••"


def set_request_source_ip(ip: str | None) -> None:
    """Record the current request's source IP for job-backed audit rows."""
    _source_ip.set(ip)


def redact_text(text: str, secrets: tuple[str, ...] | list[str] = ()) -> str:
    """Replace every known secret plaintext in ``text`` with ``••••``.

    This is the value-based masking the job engine's `LogWriter._redact` uses
    (rule 6), factored here so any funnel that emits free text — notably the AI
    layer's `build_prompt_payload` — scrubs the same way before the text can
    leave the platform. Longest secrets first so an overlapping shorter secret
    can't unmask part of a longer one.
    """
    for secret in sorted((s for s in secrets if s), key=len, reverse=True):
        text = text.replace(secret, _MASK)
    return text


def mask_params(params: dict[str, Any] | None) -> dict[str, Any]:
    """Best-effort mask of anything secret-looking.

    Params coming from a job are already sanitized (the template masked every
    `secret=True` value), so this is a defence-in-depth pass for direct callers:
    any key whose name looks sensitive is replaced with ``••••``.
    """
    if not params:
        return {}
    masked: dict[str, Any] = {}
    for key, value in params.items():
        if any(token in key.lower() for token in _SENSITIVE_KEYS):
            masked[key] = _MASK
        else:
            masked[key] = value
    return masked


def record_audit(
    db: Session,
    *,
    action: str,
    summary: str,
    user_id: int | None = None,
    entity_type: str | None = None,
    entity_id: str | int | None = None,
    params: dict[str, Any] | None = None,
    result: str = "ok",
    source_ip: str | None = None,
    job_id: int | None = None,
    already_masked: bool = False,
) -> AuditLog | None:
    """Append one audit row. Never raises into the caller (see module docstring).

    `already_masked=True` (set by JobRunner.create for a job's `params_sanitized`)
    skips the defensive re-mask. `source_ip` defaults to the current request's IP.
    """
    try:
        row = AuditLog(
            user_id=user_id,
            action=action,
            entity_type=entity_type,
            entity_id=None if entity_id is None else str(entity_id),
            summary=summary[:300],
            params_masked=(params or {}) if already_masked else mask_params(params),
            result=result,
            source_ip=source_ip if source_ip is not None else _source_ip.get(),
            job_id=job_id,
        )
        db.add(row)
        db.commit()
        return row
    except Exception:  # noqa: BLE001 — audit must never break the mutation.
        logger.exception("failed to write audit row for action %s", action)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return None


class AuditContext:
    """Request-scoped audit helper injected into non-job mutating endpoints.

    Captures the actor + source IP once, publishes the IP to the ContextVar (so
    any job created during this request inherits it), and exposes `.record(...)`
    so an endpoint writes its own row in one call.
    """

    def __init__(self, request: Request, user: CurrentUser, db: Session) -> None:
        self.user = user
        self.db = db
        self.source_ip = request.client.host if request.client else None
        set_request_source_ip(self.source_ip)

    def record(
        self,
        *,
        action: str,
        summary: str,
        entity_type: str | None = None,
        entity_id: str | int | None = None,
        params: dict[str, Any] | None = None,
        result: str = "ok",
    ) -> AuditLog | None:
        return record_audit(
            self.db,
            action=action,
            summary=summary,
            user_id=self.user.id,
            entity_type=entity_type,
            entity_id=entity_id,
            params=params,
            result=result,
            source_ip=self.source_ip,
        )


def _audit_context(
    request: Request,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
) -> AuditContext:
    return AuditContext(request, user, db)


# Inject into a non-job mutating endpoint: `audit: Audit` then `audit.record(...)`.
Audit = Annotated[AuditContext, Depends(_audit_context)]
