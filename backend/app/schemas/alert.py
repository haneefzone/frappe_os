"""Request/response models for the AlertRule engine API (session 3.1).

The webhook signing secret is write-only: accepted on create/update, Fernet-
encrypted immediately, and NEVER echoed back — the read model exposes only a
`webhook_secret_set` boolean (CLAUDE.md rule 6).
"""

from datetime import datetime

from pydantic import BaseModel, Field, model_validator

from app.models.alert import (
    ALERT_COMPARATORS,
    ALERT_METRICS,
    ALERT_SCOPES,
    AlertFiring,
    AlertRule,
)


class AlertRuleOut(BaseModel):
    """One alert rule for the Monitoring CRUD screen (no secret material)."""

    id: int
    name: str
    metric: str
    comparator: str
    threshold: float
    scope: str
    scope_server_id: int | None
    cooldown_minutes: int
    channel_email: bool
    channel_webhook: bool
    email_to: str | None
    webhook_url: str | None
    webhook_secret_set: bool
    enabled: bool
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_model(cls, rule: AlertRule) -> "AlertRuleOut":
        return cls(
            id=rule.id,
            name=rule.name,
            metric=rule.metric,
            comparator=rule.comparator,
            threshold=rule.threshold,
            scope=rule.scope,
            scope_server_id=rule.scope_server_id,
            cooldown_minutes=rule.cooldown_minutes,
            channel_email=rule.channel_email,
            channel_webhook=rule.channel_webhook,
            email_to=rule.email_to,
            webhook_url=rule.webhook_url,
            webhook_secret_set=rule.webhook_secret_set,
            enabled=rule.enabled,
            created_at=rule.created_at,
            updated_at=rule.updated_at,
        )


def _validate_metric(v: str) -> str:
    if v not in ALERT_METRICS:
        raise ValueError(f"metric must be one of {ALERT_METRICS}")
    return v


def _validate_comparator(v: str) -> str:
    if v not in ALERT_COMPARATORS:
        raise ValueError(f"comparator must be one of {ALERT_COMPARATORS}")
    return v


class AlertRuleCreate(BaseModel):
    """Create an alert rule. At least one channel must be enabled, and a webhook
    channel requires a URL."""

    name: str = Field(min_length=1, max_length=120)
    metric: str
    comparator: str
    threshold: float
    scope: str = "global"
    scope_server_id: int | None = None
    cooldown_minutes: int = Field(default=30, ge=0, le=10080)
    channel_email: bool = False
    channel_webhook: bool = False
    email_to: str | None = Field(default=None, max_length=500)
    webhook_url: str | None = Field(default=None, max_length=500)
    webhook_secret: str | None = Field(default=None, max_length=255)
    enabled: bool = True

    @model_validator(mode="after")
    def _check(self) -> "AlertRuleCreate":
        _validate_metric(self.metric)
        _validate_comparator(self.comparator)
        if self.scope not in ALERT_SCOPES:
            raise ValueError(f"scope must be one of {ALERT_SCOPES}")
        if self.scope == "server" and self.scope_server_id is None:
            raise ValueError("scope_server_id is required when scope is 'server'")
        if self.scope == "global":
            self.scope_server_id = None
        if not (self.channel_email or self.channel_webhook):
            raise ValueError("at least one channel (email or webhook) must be enabled")
        if self.channel_webhook and not self.webhook_url:
            raise ValueError("webhook_url is required when the webhook channel is enabled")
        return self


class AlertRuleUpdate(BaseModel):
    """Patch an alert rule. The webhook secret is optional: a non-empty value
    re-encrypts, an empty string clears it, and omitting it leaves it unchanged."""

    name: str | None = Field(default=None, min_length=1, max_length=120)
    metric: str | None = None
    comparator: str | None = None
    threshold: float | None = None
    scope: str | None = None
    scope_server_id: int | None = None
    cooldown_minutes: int | None = Field(default=None, ge=0, le=10080)
    channel_email: bool | None = None
    channel_webhook: bool | None = None
    email_to: str | None = Field(default=None, max_length=500)
    webhook_url: str | None = Field(default=None, max_length=500)
    webhook_secret: str | None = Field(default=None, max_length=255)
    enabled: bool | None = None

    @model_validator(mode="after")
    def _check(self) -> "AlertRuleUpdate":
        if self.metric is not None:
            _validate_metric(self.metric)
        if self.comparator is not None:
            _validate_comparator(self.comparator)
        if self.scope is not None and self.scope not in ALERT_SCOPES:
            raise ValueError(f"scope must be one of {ALERT_SCOPES}")
        return self


class AlertFiringOut(BaseModel):
    """One alert firing for the Incidents panel."""

    id: int
    rule_id: int | None
    rule_name: str | None
    server_id: int | None
    server_name: str | None
    metric: str
    comparator: str
    threshold: float
    value: float | None
    channels: list
    delivered_at: datetime | None
    resolved_at: datetime | None
    created_at: datetime

    @classmethod
    def from_model(cls, f: AlertFiring) -> "AlertFiringOut":
        return cls(
            id=f.id,
            rule_id=f.rule_id,
            rule_name=f.rule_name,
            server_id=f.server_id,
            server_name=f.server_name,
            metric=f.metric,
            comparator=f.comparator,
            threshold=f.threshold,
            value=f.value,
            channels=f.channels or [],
            delivered_at=f.delivered_at,
            resolved_at=f.resolved_at,
            created_at=f.created_at,
        )


class IncidentsOut(BaseModel):
    """The Incidents panel + Open Alerts KPI payload."""

    open_alerts: int
    incidents: list[AlertFiringOut]
