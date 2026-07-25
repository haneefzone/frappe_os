"""Configurable metric AlertRule engine (session 3.1, Phase 3 — Alerts).

This turns raw monitoring telemetry into *policy*: an operator declares "disk on
any server over 85%", picks a cooldown and one or more channels, and the
evaluator fires a notification once per breach window over email (SMTP) and/or a
signed webhook.

Three rows model the feature:

- **AlertRule** — the operator's promise: a `metric` (a numeric column on the
  latest `MonitoringSample`), a `comparator` / `threshold`, a `scope`
  (global = every server, or one `scope_server_id`), a `cooldown_minutes`
  dedup window, the channel toggles, and — for the webhook — a per-rule signing
  secret stored ONLY as a Fernet token (`webhook_secret_enc`, rule 6): never
  plaintext, never logged, never returned to the browser.

- **AlertRuleState** — one row per (rule, server): the cooldown/dedup state so a
  *sustained* breach fires once per window, and a recovery + re-breach after the
  window fires again. `in_breach` drives the dashboard Open Alerts KPI.

- **AlertFiring** — an immutable record of one dispatch: which rule/server/value
  tripped, which channels were attempted and their outcome, and when the breach
  later `resolved_at`. Feeds the Incidents panel. Channel *secrets* never appear
  on this row — only the delivery outcome.

Design constraints (CLAUDE.md golden rules): secrets via Fernet, never logged
(rule 6); the evaluator runs on the scheduler's RQ worker and enqueues each
delivery after commit so a slow webhook never blocks the sweep (rule 3);
migrations idempotent (rule 8); RBAC is enforced server-side in the router.
"""

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import expression

from app.db import Base

# JSONB on Postgres, plain JSON on the SQLite test fallback (mirrors monitoring.py).
_JSON = JSON().with_variant(JSONB(), "postgresql")

# The numeric metrics a rule can watch — each is a column on the latest
# MonitoringSample (session 1.12). All are per-server. Percentages are 0-100;
# load1 is the 1-minute load average (unbounded).
ALERT_METRICS = ("cpu_pct", "mem_pct", "disk_pct", "load1")

# Supported comparators. `==` is exact float equality (thresholds are operator
# integers/decimals, so this is deliberate, not fuzzy).
ALERT_COMPARATORS = (">", ">=", "<", "<=", "==")

# Rule scopes: evaluate every active server, or exactly one.
ALERT_SCOPES = ("global", "server")


class AlertRule(Base):
    """One metric/threshold rule dispatching over email and/or signed webhook."""

    __tablename__ = "alert_rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, index=True)

    # What to watch (see ALERT_METRICS) and how to compare against `threshold`.
    metric: Mapped[str] = mapped_column(String(20), nullable=False)
    comparator: Mapped[str] = mapped_column(String(2), nullable=False)
    threshold: Mapped[float] = mapped_column(Float, nullable=False)

    # global => every non-archived server; server => only `scope_server_id`.
    scope: Mapped[str] = mapped_column(String(10), nullable=False, default="global")
    scope_server_id: Mapped[int | None] = mapped_column(
        ForeignKey("servers.id", ondelete="CASCADE"), index=True
    )

    # Dedup window: a sustained breach fires at most once per this many minutes.
    cooldown_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=30)

    # Channel toggles. A rule may enable either or both.
    channel_email: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=expression.false(), default=False
    )
    channel_webhook: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=expression.false(), default=False
    )

    # Optional explicit recipient(s) for the email channel (comma-separated). When
    # empty the dispatcher falls back to every active user with an email address.
    email_to: Mapped[str | None] = mapped_column(String(500))

    # Webhook endpoint + per-rule HMAC signing secret. The secret lives ONLY as a
    # Fernet token (rule 6); the plaintext never touches the DB, logs, or the API.
    webhook_url: Mapped[str | None] = mapped_column(String(500))
    webhook_secret_enc: Mapped[str | None] = mapped_column(Text)

    # Disabling stops evaluation without deleting the rule (and its history).
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=expression.true(), default=True
    )

    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    @property
    def webhook_secret_set(self) -> bool:
        """True when a signing secret is configured (never exposes the value)."""
        return bool(self.webhook_secret_enc)


class AlertRuleState(Base):
    """Per-(rule, server) cooldown/dedup state.

    `last_fired_at` is the single gate: a breach fires only when it is None or the
    cooldown window has elapsed, so a sustained breach fires once per window and a
    re-breach after recovery+window fires again. `in_breach` records the current
    condition for the Open Alerts KPI and recovery detection.
    """

    __tablename__ = "alert_rule_states"
    __table_args__ = (
        UniqueConstraint("rule_id", "server_id", name="uq_alert_rule_state_rule_server"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    rule_id: Mapped[int] = mapped_column(
        ForeignKey("alert_rules.id", ondelete="CASCADE"), index=True
    )
    server_id: Mapped[int] = mapped_column(
        ForeignKey("servers.id", ondelete="CASCADE"), index=True
    )

    # Whether the metric was breaching threshold at the last evaluation.
    in_breach: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=expression.false(), default=False
    )
    # The metric value at the last evaluation (for the UI / debugging).
    last_value: Mapped[float | None] = mapped_column(Float)
    # When this rule last fired a notification for this server (the cooldown gate).
    last_fired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_evaluated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    rule: Mapped["AlertRule"] = relationship()


class AlertFiring(Base):
    """Immutable record of one alert dispatch (feeds the Incidents panel).

    Denormalised rule/server names so an incident renders even after the rule or
    server is deleted (`SET NULL` on both FKs). `channels` is the per-channel
    delivery outcome ([{channel, ok, error}]) — never any secret material.
    `resolved_at` is stamped when the metric later recovers below threshold.
    """

    __tablename__ = "alert_firings"

    id: Mapped[int] = mapped_column(primary_key=True)

    rule_id: Mapped[int | None] = mapped_column(
        ForeignKey("alert_rules.id", ondelete="SET NULL"), index=True
    )
    rule_name: Mapped[str | None] = mapped_column(String(120))
    server_id: Mapped[int | None] = mapped_column(
        ForeignKey("servers.id", ondelete="SET NULL"), index=True
    )
    server_name: Mapped[str | None] = mapped_column(String(200))

    # Snapshot of the condition that tripped.
    metric: Mapped[str] = mapped_column(String(20))
    comparator: Mapped[str] = mapped_column(String(2))
    threshold: Mapped[float] = mapped_column(Float)
    value: Mapped[float | None] = mapped_column(Float)

    # Per-channel delivery outcome: [{"channel": "webhook", "ok": true, "error": null}].
    channels: Mapped[list] = mapped_column(_JSON, default=list)

    # NULL until delivery has been attempted (row is created up front, then the
    # enqueued delivery job stamps the outcome — rule 3, never block the sweep).
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # NULL while the breach is active; stamped when the metric recovers.
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
