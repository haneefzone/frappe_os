"""Request/response models for the AI settings API (session 5.0).

The API key is Password-style: it is write-only (accepted on PUT, never echoed)
and surfaces as `api_key_set` + a fixed `••••` mask so the browser learns whether
a key exists without ever receiving it (rule 6).
"""

from datetime import datetime

from pydantic import BaseModel, Field

from app.models.ai_settings import AISettings

# Constant mask returned in place of the key, mirroring the job-engine ``••••``.
KEY_MASK = "••••"


class AISettingsOut(BaseModel):
    """The Admin-visible AI settings — no key, ever."""

    enabled: bool
    api_key_set: bool
    api_key_masked: str | None
    model_deep: str
    model_high_volume: str
    monthly_budget_usd: float | None
    audit_calls: bool
    usage_period: str | None
    tokens_input_month: int
    tokens_output_month: int
    cost_usd_month: float
    updated_at: datetime | None

    @classmethod
    def from_model(cls, row: AISettings) -> "AISettingsOut":
        has_key = bool(row.api_key_enc)
        return cls(
            enabled=row.enabled,
            api_key_set=has_key,
            api_key_masked=KEY_MASK if has_key else None,
            model_deep=row.model_deep,
            model_high_volume=row.model_high_volume,
            monthly_budget_usd=row.monthly_budget_usd,
            audit_calls=row.audit_calls,
            usage_period=row.usage_period,
            tokens_input_month=row.tokens_input_month,
            tokens_output_month=row.tokens_output_month,
            cost_usd_month=row.cost_usd_month,
            updated_at=row.updated_at,
        )


class AISettingsUpdate(BaseModel):
    """A partial update — only the provided fields change.

    `api_key` is write-only: a non-empty value sets/rotates the key; an explicit
    empty string clears it; omitting the field leaves the stored key untouched.
    """

    enabled: bool | None = None
    api_key: str | None = Field(default=None, max_length=500)
    model_deep: str | None = Field(default=None, min_length=1, max_length=80)
    model_high_volume: str | None = Field(default=None, min_length=1, max_length=80)
    monthly_budget_usd: float | None = Field(default=None, ge=0)
    audit_calls: bool | None = None


class AITestResult(BaseModel):
    """Outcome of the cheap key-validation ping (`POST /api/settings/ai/test`)."""

    ok: bool
    model: str
    text: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    error: str | None = None
