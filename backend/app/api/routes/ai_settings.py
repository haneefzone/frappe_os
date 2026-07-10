"""AI settings API (session 5.0): encrypted Claude API config + a key-test ping.

- GET  /api/settings/ai       read the config (Admin only; key never returned).
- PUT  /api/settings/ai       update config (Admin only; key write-only, masked).
- POST /api/settings/ai/test  cheap server-side key-validation ping, streamed.

Every endpoint is gated on `settings:manage`, which only Admin holds in the
default role matrix — so a non-Admin gets 403 (rule 7). The API key is accepted
on PUT, Fernet-encrypted immediately, and never crosses back to the browser; no
raw prompt ever crosses to the browser either — the test ping runs entirely
server-side and streams back only status + a short model reply.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.api.deps import require
from app.audit import Audit
from app.core.ai import AIError, AnthropicClient
from app.core.permissions import SETTINGS_MANAGE
from app.core.security import SecretsService, get_secrets_service
from app.core.streaming import sse_comment, sse_event
from app.db import get_db
from app.models.ai_settings import AISettings
from app.schemas.ai_settings import AISettingsOut, AISettingsUpdate

router = APIRouter(prefix="/api/settings/ai", tags=["ai-settings"])

DbSession = Annotated[Session, Depends(get_db)]
Secrets = Annotated[SecretsService, Depends(get_secrets_service)]


def get_ai_client(db: DbSession, secrets: Secrets) -> AnthropicClient:
    """Build an AnthropicClient bound to the singleton settings row. Overridden
    in tests to inject a mock client (no real SDK/network)."""
    row = AISettings.get_or_create(db)
    return AnthropicClient(row, secrets, db=db)


AIClient = Annotated[AnthropicClient, Depends(get_ai_client)]


@router.get("", response_model=AISettingsOut)
def get_ai_settings(
    db: DbSession, _: Annotated[object, Depends(require(SETTINGS_MANAGE))]
) -> AISettingsOut:
    return AISettingsOut.from_model(AISettings.get_or_create(db))


@router.put("", response_model=AISettingsOut)
def update_ai_settings(
    body: AISettingsUpdate,
    db: DbSession,
    secrets: Secrets,
    audit: Audit,
    _: Annotated[object, Depends(require(SETTINGS_MANAGE))],
) -> AISettingsOut:
    row = AISettings.get_or_create(db)
    fields = body.model_dump(exclude_unset=True)

    # The key is handled specially: encrypt on set, clear on empty, never store
    # or log the plaintext. Everything else is a plain scalar assignment.
    key_action: str | None = None
    if "api_key" in fields:
        raw = fields.pop("api_key")
        if raw:
            row.api_key_enc = secrets.encrypt(raw)
            key_action = "set"
        else:
            row.api_key_enc = None
            key_action = "cleared"

    for key, value in fields.items():
        setattr(row, key, value)
    db.commit()
    db.refresh(row)

    # Audit the mutation. The key itself is never in `fields` (popped above);
    # record only that it was set/cleared, under a neutral field name so the
    # audit funnel's defensive re-mask (any key containing "key") doesn't hide
    # the harmless set/cleared signal.
    audited = dict(fields)
    if key_action is not None:
        audited["credential_updated"] = key_action
    audit.record(
        action="ai.settings.update",
        summary="Updated AI integration settings",
        entity_type="ai_settings",
        entity_id=row.id,
        params=audited,
    )
    return AISettingsOut.from_model(row)


@router.post("/test")
def test_ai_key(
    db: DbSession,
    ai: AIClient,
    audit: Audit,
    _: Annotated[object, Depends(require(SETTINGS_MANAGE))],
) -> StreamingResponse:
    """Validate the configured key with a cheap deep-model ping, streamed as SSE.

    Refuses cleanly (409) when disabled or unkeyed — no call is attempted. On a
    successful ping the reply is produced by the deep model
    (`AISettings.model_deep`, default ``claude-opus-4-8``)."""
    row = AISettings.get_or_create(db)
    if not ai.ready:
        # Clean, non-streamed error so the caller can distinguish "not
        # configured" from an upstream failure.
        detail = (
            "AI integration is disabled."
            if not row.enabled
            else "No Anthropic API key is configured."
        )
        raise HTTPException(status_code=409, detail=detail)

    model = ai.model_for("deep")

    def _stream():
        yield sse_comment("ai-test")
        try:
            result = ai.complete(
                messages=[{"role": "user", "content": "Reply with just: pong"}],
                system="You are a health check. Reply with a single short word.",
                tier="deep",
                max_tokens=16,
            )
        except AIError as exc:
            yield sse_event("error", {"ok": False, "model": model, "error": str(exc)})
            return
        except Exception:  # noqa: BLE001 — never leak an SDK traceback to the wire.
            yield sse_event(
                "error",
                {"ok": False, "model": model, "error": "Claude API call failed."},
            )
            return
        if row.audit_calls:
            audit.record(
                action="ai.test",
                summary=f"Tested Claude API key against {result.model}",
                entity_type="ai_settings",
                entity_id=row.id,
                params={
                    "model": result.model,
                    "input_tokens": result.input_tokens,
                    "output_tokens": result.output_tokens,
                },
            )
        yield sse_event(
            "result",
            {
                "ok": True,
                "model": result.model,
                "text": result.text,
                "input_tokens": result.input_tokens,
                "output_tokens": result.output_tokens,
            },
        )

    return StreamingResponse(_stream(), media_type="text/event-stream")
