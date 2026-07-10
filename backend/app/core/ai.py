"""Claude API integration layer (session 5.0, Phase 5 — AI foundation).

The shared server-side Anthropic client both 5.1 (copilot) and 5.2 (agents)
build on. Three pieces live here:

- `build_prompt_payload(...)` — the ONE place a prompt is assembled. It reuses
  the job engine's secret-masking (`app.audit.redact_text`, the same
  value-replacement `LogWriter._redact` uses) to scrub every known secret
  plaintext out of the prompt *before it is handed to the SDK*, so a credential
  or a masked log secret can never leave the platform (rule 6). This is the
  redaction the unit-test gate exercises.

- `AnthropicClient` — wraps the current Anthropic Python SDK
  (`client.messages.create` / `.stream`) with: model chosen from the editable
  `AISettings` row (deep vs high-volume — never a hard-coded id), adaptive
  thinking, streaming for long outputs, optional structured output via
  `output_config.format`, a timeout + bounded retries, a structured error
  surface, persisted token/cost accounting, a monthly budget cap, and a refusal
  to call at all when the integration is disabled or unkeyed.

- `estimate_cost` / `MODEL_PRICING` — best-effort USD accounting per the
  Anthropic price list (cached), falling back to zero cost (tokens still
  counted) for a model we don't have a rate for.

The SDK import is deliberately lazy (only the default client factory touches
`anthropic`), so the package need not be installed to import this module — unit
tests inject a mock client factory and never load the real SDK.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.audit import redact_text
from app.core.security import SecretsService
from app.models.ai_settings import AISettings

# Adaptive thinking is the recommended mode on Opus 4.6+/Sonnet 4.6 (skill:
# claude-api). One shape for every model we route to.
ADAPTIVE_THINKING = {"type": "adaptive"}

# USD per 1,000,000 tokens (input, output). Cached from the Anthropic price
# list; used only for the local budget/accounting estimate. An unpriced model
# still has its tokens counted, at zero estimated cost.
MODEL_PRICING: dict[str, tuple[float, float]] = {
    "claude-fable-5": (10.0, 50.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-opus-4-6": (5.0, 25.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
}

# Sensible ceilings for the foundation. Callers may override per request.
DEFAULT_MAX_TOKENS = 4096
DEFAULT_TIMEOUT_SECONDS = 60.0
DEFAULT_MAX_RETRIES = 2

Tier = str  # "deep" | "high_volume"


class AIError(RuntimeError):
    """Base for every AI-layer failure."""


class AIDisabledError(AIError):
    """The integration is switched off, or no API key is configured. The client
    refuses to call out (rule: external/AI call is never attempted when disabled
    or unkeyed). Maps to HTTP 409 at the API edge."""


class AIBudgetError(AIError):
    """The monthly cost cap has been reached. Maps to HTTP 429."""


class AIRequestError(AIError):
    """The upstream SDK/network call failed after the bounded retries. Carries a
    short, secret-free message. Maps to HTTP 502."""


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """Best-effort USD cost for a call. Unknown model -> 0.0 (tokens still counted)."""
    in_rate, out_rate = MODEL_PRICING.get(model, (0.0, 0.0))
    return (input_tokens / 1_000_000) * in_rate + (output_tokens / 1_000_000) * out_rate


@dataclass(frozen=True)
class PromptPayload:
    """A ready-to-send request body whose text has ALREADY been scrubbed of every
    known secret. Never construct one directly — go through `build_prompt_payload`
    so the redaction is guaranteed."""

    messages: list[dict[str, Any]]
    system: str | None = None

    def request_kwargs(self) -> dict[str, Any]:
        req: dict[str, Any] = {"messages": self.messages}
        if self.system:
            req["system"] = self.system
        return req


def _redact_content(content: Any, secrets: tuple[str, ...]) -> Any:
    """Redact secret plaintexts from a message's `content` (a string, or a list
    of content blocks with `text` fields)."""
    if isinstance(content, str):
        return redact_text(content, secrets)
    if isinstance(content, list):
        out = []
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                block = {**block, "text": redact_text(block["text"], secrets)}
            out.append(block)
        return out
    return content


def build_prompt_payload(
    *,
    messages: list[dict[str, Any]],
    system: str | None = None,
    secrets: tuple[str, ...] | list[str] = (),
) -> PromptPayload:
    """Assemble the outbound Claude request, scrubbing every known secret
    plaintext from the system prompt and every message BEFORE it is sent.

    `secrets` is the set of credential/log-secret plaintexts the caller knows are
    in play (e.g. values just resolved from the credential store). Reusing
    `app.audit.redact_text` — the same masking the job engine's log writer
    applies — means no credential or log secret can ride a prompt off the box.
    """
    secrets_t = tuple(s for s in secrets if s)
    redacted = [
        {**m, "content": _redact_content(m.get("content"), secrets_t)} for m in messages
    ]
    return PromptPayload(
        messages=redacted,
        system=redact_text(system, secrets_t) if system else None,
    )


@dataclass
class AIResult:
    """The outcome of one completion, safe to persist/return (no prompt echoed)."""

    text: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    parsed: Any | None = None
    raw_content: list[Any] = field(default_factory=list)


def _default_client_factory(*, api_key: str, timeout: float, max_retries: int):
    """Build a real Anthropic SDK client. Imported lazily so the package is only
    required when the platform actually calls out (tests inject a fake factory)."""
    import anthropic  # noqa: PLC0415 — lazy so the SDK is optional at import time.

    return anthropic.Anthropic(
        api_key=api_key, timeout=timeout, max_retries=max_retries
    )


class AnthropicClient:
    """Server-side Claude client bound to the singleton `AISettings` row.

    Construct per request/job (cheap). Reads the API key + model routing off the
    row; never accepts a key or a raw prompt from the browser.
    """

    def __init__(
        self,
        settings_row: AISettings,
        secrets: SecretsService,
        *,
        db: Session | None = None,
        client_factory: Callable[..., Any] = _default_client_factory,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._row = settings_row
        self._secrets = secrets
        self._db = db
        self._client_factory = client_factory
        self._timeout = timeout
        self._max_retries = max_retries
        self._clock = clock

    # -- readiness ---------------------------------------------------------- #

    @property
    def ready(self) -> bool:
        """True when the integration is enabled AND a key is configured."""
        return bool(self._row.enabled and self._row.api_key_enc)

    def _require_ready(self) -> str:
        if not self._row.enabled:
            raise AIDisabledError("AI integration is disabled.")
        if not self._row.api_key_enc:
            raise AIDisabledError("No Anthropic API key is configured.")
        return self._secrets.decrypt(self._row.api_key_enc)

    def model_for(self, tier: Tier) -> str:
        """The outbound model id for a routing tier, read from the editable row."""
        return (
            self._row.model_high_volume
            if tier == "high_volume"
            else self._row.model_deep
        )

    def _check_budget(self) -> None:
        cap = self._row.monthly_budget_usd
        if cap is None:
            return
        period = self._clock().strftime("%Y-%m")
        spent = self._row.cost_usd_month if self._row.usage_period == period else 0.0
        if spent >= cap:
            raise AIBudgetError(
                f"Monthly AI budget cap of ${cap:.2f} reached (spent ${spent:.2f})."
            )

    # -- the call ----------------------------------------------------------- #

    def complete(
        self,
        *,
        messages: list[dict[str, Any]],
        system: str | None = None,
        tier: Tier = "deep",
        schema: dict[str, Any] | None = None,
        secrets: tuple[str, ...] | list[str] = (),
        max_tokens: int = DEFAULT_MAX_TOKENS,
        stream: bool = True,
    ) -> AIResult:
        """Run one completion and return its text + usage.

        Refuses (AIDisabledError) when disabled/unkeyed; refuses (AIBudgetError)
        when the monthly cap is reached. The prompt is scrubbed of every value in
        `secrets` before it is sent. `schema` (a JSON Schema) constrains the reply
        via `output_config.format`. Streaming is the default so a large
        `max_tokens` can't hit an HTTP timeout.
        """
        api_key = self._require_ready()
        self._check_budget()

        model = self.model_for(tier)
        payload = build_prompt_payload(messages=messages, system=system, secrets=secrets)

        req: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "thinking": ADAPTIVE_THINKING,
            **payload.request_kwargs(),
        }
        if schema is not None:
            # Structured output constrains the response format (skill: claude-api).
            # Incompatible with assistant prefill — we never prefill.
            req["output_config"] = {"format": {"type": "json_schema", "schema": schema}}

        client = self._client_factory(
            api_key=api_key, timeout=self._timeout, max_retries=self._max_retries
        )
        try:
            message = self._invoke(client, req, stream=stream)
        except (AIError, KeyboardInterrupt):
            raise
        except Exception as exc:  # noqa: BLE001 — normalise SDK/network faults.
            # Never surface the SDK exception verbatim: it can echo request
            # material. Keep the type name only.
            raise AIRequestError(
                f"Claude API call failed ({type(exc).__name__})."
            ) from exc

        return self._account(message, model, schema)

    def _invoke(self, client: Any, req: dict[str, Any], *, stream: bool) -> Any:
        """Dispatch to the SDK. Streaming uses the context-manager helper and
        `.get_final_message()`; non-streaming uses `.create()`."""
        if stream:
            with client.messages.stream(**req) as s:
                return s.get_final_message()
        return client.messages.create(**req)

    def _account(
        self, message: Any, model: str, schema: dict[str, Any] | None
    ) -> AIResult:
        content = list(getattr(message, "content", []) or [])
        text = "".join(
            getattr(b, "text", "") for b in content if getattr(b, "type", None) == "text"
        )
        usage = getattr(message, "usage", None)
        input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
        output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
        cost = estimate_cost(model, input_tokens, output_tokens)

        parsed: Any | None = None
        if schema is not None and text:
            import json  # noqa: PLC0415

            try:
                parsed = json.loads(text)
            except ValueError:
                parsed = None

        # Persist accounting so the budget cap and the Settings usage panel stay
        # accurate. Never blocks on failure — accounting is best-effort.
        if self._db is not None:
            try:
                self._row.record_usage(
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cost_usd=cost,
                    now=self._clock(),
                )
                self._db.commit()
            except Exception:  # noqa: BLE001
                self._db.rollback()

        return AIResult(
            text=text,
            model=getattr(message, "model", model),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
            parsed=parsed,
            raw_content=content,
        )
