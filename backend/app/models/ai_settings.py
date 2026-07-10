"""Encrypted AI integration settings (session 5.0, Phase 5 — AI foundation).

A single row (`id == 1`) holds the Claude/Anthropic integration config that both
5.1 (copilot) and 5.2 (agents) build on: the on/off toggle, the Fernet-encrypted
Anthropic API key (never returned to the browser — rule 6), the **editable**
model routing (deep-analysis vs cheap high-volume), a monthly token/cost budget
cap, a per-call audit toggle, and the persisted usage accounting.

Model routing is stored as configuration, NOT hard-coded constants: the outbound
`AnthropicClient` always reads `model_deep` / `model_high_volume` off this row, so
an operator can retarget a newer model from Settings without a code change. The
shipped DEFAULT_* values below seed the columns only — they are the current
Anthropic model ids (deep = ``claude-opus-4-8``, high-volume =
``claude-sonnet-4-6``), never referenced at call time.

`get_or_create(db)` returns the singleton, materialising it disabled on first
read so the platform runs fine before anyone provisions a key.
"""

from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text, func
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.db import Base

# The single row's fixed primary key.
SINGLETON_ID = 1

# Shipped default model routing. Seeds the columns only; the client reads the
# row, so editing settings — not editing this constant — changes the outbound
# model. Kept current with the Anthropic model catalogue (deep = Opus, cheap
# high-volume = Sonnet).
DEFAULT_MODEL_DEEP = "claude-opus-4-8"
DEFAULT_MODEL_HIGH_VOLUME = "claude-sonnet-4-6"


def _current_period(now: datetime | None = None) -> str:
    """The accounting bucket key for a moment, ``YYYY-MM`` in UTC."""
    return (now or datetime.now(UTC)).strftime("%Y-%m")


class AISettings(Base):
    """The one-row Claude API integration record."""

    __tablename__ = "ai_settings"

    id: Mapped[int] = mapped_column(primary_key=True, default=SINGLETON_ID)

    # Master switch. When false the AnthropicClient refuses every call.
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Anthropic API key, Fernet-encrypted via SecretsService/FDM_SECRET_KEY.
    # Password-style: write-only, masked as ``••••`` in responses, never logged.
    api_key_enc: Mapped[str | None] = mapped_column(Text)

    # Editable model routing (deep analysis vs cheap high-volume). Read by the
    # client at call time — NOT hard-coded there.
    model_deep: Mapped[str] = mapped_column(String(80), default=DEFAULT_MODEL_DEEP)
    model_high_volume: Mapped[str] = mapped_column(
        String(80), default=DEFAULT_MODEL_HIGH_VOLUME
    )

    # Monthly cost cap in USD (NULL = no cap). The client refuses a call once the
    # current month's estimated spend has reached this ceiling.
    monthly_budget_usd: Mapped[float | None] = mapped_column(Float)

    # Per-call audit toggle: when true every AI call writes an AuditLog row.
    audit_calls: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # Persisted usage accounting for the current period (`usage_period`). Reset
    # atomically when a call lands in a new month.
    usage_period: Mapped[str | None] = mapped_column(String(7))
    tokens_input_month: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    tokens_output_month: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cost_usd_month: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    @classmethod
    def get_or_create(cls, db: Session) -> "AISettings":
        row = db.get(cls, SINGLETON_ID)
        if row is None:
            row = cls(id=SINGLETON_ID)
            db.add(row)
            db.commit()
            db.refresh(row)
        return row

    def record_usage(
        self,
        *,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float,
        now: datetime | None = None,
    ) -> None:
        """Fold one call's tokens + estimated cost into the monthly accounting,
        rolling the bucket over (reset to zero) when the month changes."""
        period = _current_period(now)
        if self.usage_period != period:
            self.usage_period = period
            self.tokens_input_month = 0
            self.tokens_output_month = 0
            self.cost_usd_month = 0.0
        self.tokens_input_month += int(input_tokens)
        self.tokens_output_month += int(output_tokens)
        self.cost_usd_month += float(cost_usd)
