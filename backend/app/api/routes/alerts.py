"""AlertRule engine API (session 3.1, FDM 3.1).

- GET    /api/alerts                 list rules (read; no secret material)
- POST   /api/alerts                 create a rule (alert:manage; secret encrypted)
- GET    /api/alerts/{id}            one rule
- PATCH  /api/alerts/{id}            update (webhook secret write-only: set/clear/keep)
- DELETE /api/alerts/{id}            remove a rule (and its state/history via cascade)
- POST   /api/alerts/{id}/test       fire a synthetic firing to validate channels
- GET    /api/alerts/incidents       Open Alerts KPI + recent Incidents feed

Reads are gated on `read`; writes on `alert:manage` (Admin via `*`, Developer,
Operator). Read-only can never mutate (golden rule 7). The webhook signing secret
is accepted on create/update, Fernet-encrypted immediately, and never returned to
the browser (rule 6). Every mutation writes an audit row whose params never
include the secret.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import require
from app.audit import Audit
from app.core import alerts as al
from app.core.permissions import ALERT_MANAGE, READ
from app.core.security import SecretsService, get_secrets_service
from app.db import get_db
from app.models.alert import AlertFiring, AlertRule
from app.models.server import Server
from app.schemas.alert import (
    AlertFiringOut,
    AlertRuleCreate,
    AlertRuleOut,
    AlertRuleUpdate,
    IncidentsOut,
)

router = APIRouter(prefix="/api/alerts", tags=["alerts"])

DbSession = Annotated[Session, Depends(get_db)]
Secrets = Annotated[SecretsService, Depends(get_secrets_service)]
ManageAlerts = Annotated[object, Depends(require(ALERT_MANAGE))]
ReadAlerts = Annotated[object, Depends(require(READ))]


def _get_or_404(db: Session, rule_id: int) -> AlertRule:
    rule = db.get(AlertRule, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="Alert rule not found.")
    return rule


def _require_server(db: Session, server_id: int) -> None:
    if db.get(Server, server_id) is None:
        raise HTTPException(status_code=422, detail=f"Server {server_id} does not exist.")


@router.get("", response_model=list[AlertRuleOut])
def list_rules(db: DbSession, _: ReadAlerts) -> list[AlertRuleOut]:
    rows = db.scalars(select(AlertRule).order_by(AlertRule.name)).all()
    return [AlertRuleOut.from_model(r) for r in rows]


@router.get("/incidents", response_model=IncidentsOut)
def incidents(db: DbSession, _: ReadAlerts, limit: int = 20) -> IncidentsOut:
    limit = max(1, min(limit, 100))
    return IncidentsOut(
        open_alerts=al.open_alerts_count(db),
        incidents=[AlertFiringOut.from_model(f) for f in al.recent_incidents(db, limit=limit)],
    )


@router.get("/{rule_id}", response_model=AlertRuleOut)
def get_rule(rule_id: int, db: DbSession, _: ReadAlerts) -> AlertRuleOut:
    return AlertRuleOut.from_model(_get_or_404(db, rule_id))


@router.post("", status_code=201, response_model=AlertRuleOut)
def create_rule(
    body: AlertRuleCreate,
    db: DbSession,
    secrets: Secrets,
    audit: Audit,
    _: ManageAlerts,
) -> AlertRuleOut:
    if db.scalars(select(AlertRule).where(AlertRule.name == body.name)).first():
        raise HTTPException(
            status_code=409, detail=f"An alert rule named {body.name!r} already exists."
        )
    if body.scope == "server" and body.scope_server_id is not None:
        _require_server(db, body.scope_server_id)

    rule = AlertRule(
        name=body.name,
        metric=body.metric,
        comparator=body.comparator,
        threshold=body.threshold,
        scope=body.scope,
        scope_server_id=body.scope_server_id,
        cooldown_minutes=body.cooldown_minutes,
        channel_email=body.channel_email,
        channel_webhook=body.channel_webhook,
        email_to=(body.email_to or None),
        webhook_url=(body.webhook_url or None),
        webhook_secret_enc=(secrets.encrypt(body.webhook_secret) if body.webhook_secret else None),
        enabled=body.enabled,
    )
    db.add(rule)
    db.commit()
    db.refresh(rule)
    audit.record(
        action="alert.rule.create",
        summary=(
            f"Created alert rule {rule.name!r} "
            f"({rule.metric} {rule.comparator} {rule.threshold})"
        ),
        entity_type="alert_rule",
        entity_id=rule.id,
        params={"metric": rule.metric, "comparator": rule.comparator, "threshold": rule.threshold,
                "scope": rule.scope, "channels": _channels(rule)},
    )
    return AlertRuleOut.from_model(rule)


@router.patch("/{rule_id}", response_model=AlertRuleOut)
def update_rule(
    rule_id: int,
    body: AlertRuleUpdate,
    db: DbSession,
    secrets: Secrets,
    audit: Audit,
    _: ManageAlerts,
) -> AlertRuleOut:
    rule = _get_or_404(db, rule_id)
    fields = body.model_dump(exclude_unset=True)

    # Webhook secret is write-only: non-empty re-encrypts, empty clears, omitted keeps.
    secret_action: str | None = None
    if "webhook_secret" in fields:
        raw = fields.pop("webhook_secret")
        if raw:
            rule.webhook_secret_enc = secrets.encrypt(raw)
            secret_action = "set"
        else:
            rule.webhook_secret_enc = None
            secret_action = "cleared"

    if "name" in fields and fields["name"] != rule.name:
        if db.scalars(select(AlertRule).where(AlertRule.name == fields["name"])).first():
            raise HTTPException(
                status_code=409, detail=f"An alert rule named {fields['name']!r} already exists."
            )

    # Empty strings normalise to NULL for the optional text columns.
    for key in ("email_to", "webhook_url"):
        if key in fields and not fields[key]:
            fields[key] = None

    for key, value in fields.items():
        setattr(rule, key, value)

    # Post-update invariants: a global rule has no server; a webhook rule needs a URL.
    if rule.scope == "global":
        rule.scope_server_id = None
    elif rule.scope == "server":
        if rule.scope_server_id is None:
            raise HTTPException(
                status_code=422, detail="scope_server_id is required when scope is 'server'."
            )
        _require_server(db, rule.scope_server_id)
    if not (rule.channel_email or rule.channel_webhook):
        raise HTTPException(
            status_code=422, detail="at least one channel (email or webhook) must be enabled."
        )
    if rule.channel_webhook and not rule.webhook_url:
        raise HTTPException(
            status_code=422, detail="webhook_url is required when the webhook channel is enabled."
        )

    db.commit()
    db.refresh(rule)

    audited = {k: v for k, v in fields.items()}
    if secret_action:
        audited["webhook_secret"] = secret_action
    audit.record(
        action="alert.rule.update",
        summary=f"Updated alert rule {rule.name!r}",
        entity_type="alert_rule",
        entity_id=rule.id,
        params=audited,
    )
    return AlertRuleOut.from_model(rule)


@router.delete("/{rule_id}", status_code=204)
def delete_rule(rule_id: int, db: DbSession, audit: Audit, _: ManageAlerts) -> None:
    rule = _get_or_404(db, rule_id)
    name = rule.name
    db.delete(rule)
    db.commit()
    audit.record(
        action="alert.rule.delete",
        summary=f"Deleted alert rule {name!r}",
        entity_type="alert_rule",
        entity_id=rule_id,
    )


@router.post("/{rule_id}/test", response_model=AlertFiringOut)
def test_rule(
    rule_id: int,
    db: DbSession,
    secrets: Secrets,
    audit: Audit,
    _: ManageAlerts,
) -> AlertFiringOut:
    """Fire a synthetic firing over the rule's channels (value = threshold) so an
    operator can validate the email/webhook wiring without waiting for a breach."""
    rule = _get_or_404(db, rule_id)
    firing = AlertFiring(
        rule_id=rule.id,
        rule_name=f"{rule.name} (test)",
        server_id=rule.scope_server_id,
        server_name="test",
        metric=rule.metric,
        comparator=rule.comparator,
        threshold=rule.threshold,
        value=rule.threshold,
        channels=[],
    )
    db.add(firing)
    db.commit()
    db.refresh(firing)
    firing = al.deliver_firing(db, firing, secrets=secrets)
    audit.record(
        action="alert.rule.test",
        summary=f"Sent test alert for rule {rule.name!r}",
        entity_type="alert_rule",
        entity_id=rule.id,
        params={"channels": firing.channels},
    )
    return AlertFiringOut.from_model(firing)


def _channels(rule: AlertRule) -> list[str]:
    out = []
    if rule.channel_email:
        out.append("email")
    if rule.channel_webhook:
        out.append("webhook")
    return out
