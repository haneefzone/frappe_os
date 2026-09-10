"""AlertRule evaluation + channel dispatch (session 3.1) — the testable core.

Split cleanly from the process that drives it (the scheduler's recurring
``evaluate_alerts`` job): everything here runs against a plain DB session + an
injected clock and injectable senders, so the whole path is unit-testable with
an in-memory backend — no Redis, RQ, SMTP or wall-clock needed.

Flow:

    scheduler tick ─▶ evaluate_alerts(db, now, secrets)
                        │  for each enabled rule × in-scope server:
                        │    read latest MonitoringSample, compare to threshold
                        │    breach + cooldown elapsed ─▶ create AlertFiring,
                        │      commit, then DELIVER (enqueued off the sweep)
                        ▼
                     run_delivery(firing_id)  (RQ worker)
                        └─▶ deliver_firing: email (SMTP) + signed webhook,
                            record per-channel outcome; secret decrypted here,
                            in memory, only to sign — never persisted/logged.

Golden rules: secrets via Fernet, never logged (6); delivery enqueued after the
sweep's commit so a slow webhook never blocks it (3); one failing channel never
silences the other (errors captured on the firing, not raised).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.alert import (
    AlertFiring,
    AlertRule,
    AlertRuleState,
)
from app.models.auth import User
from app.models.monitoring import MonitoringSample
from app.models.server import Server

logger = logging.getLogger("app.alerts")

# Webhook delivery: retry a failed POST a few times with exponential backoff so a
# transient blip doesn't drop the alert, but bounded so a dead endpoint can't
# stall the delivery worker.
_WEBHOOK_TIMEOUT_SECONDS = 10.0
_WEBHOOK_MAX_ATTEMPTS = 3
_WEBHOOK_BACKOFF_BASE = 0.5  # seconds: 0.5, 1.0, 2.0 …

# POST /api/alerts/{id}/test runs synchronously in the request thread (the
# operator wants the channel outcomes back in the response), so it uses a
# tighter bound than the sweep's delivery: one attempt, short timeout — a dead
# endpoint fails in ~3s instead of holding the worker for the sweep's ~31s
# (3 attempts × 10s timeout + backoff).
TEST_WEBHOOK_TIMEOUT_SECONDS = 3.0
TEST_WEBHOOK_MAX_ATTEMPTS = 1


# --------------------------------------------------------------------------- #
# Comparison + signing (pure, unit-tested)                                    #
# --------------------------------------------------------------------------- #


def _as_utc(dt: datetime | None) -> datetime | None:
    """Normalise a stored timestamp to aware UTC.

    All timestamps are written in UTC, but SQLite (the test backend) drops the
    tzinfo on round-trip, so a re-read value comes back naive. Treat a naive
    value as already-UTC (``replace``) — never ``astimezone``, which would
    reinterpret it as machine-local time and skew the cooldown math on a bench
    whose local zone isn't UTC.
    """
    if dt is None:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def compare(value: float, comparator: str, threshold: float) -> bool:
    """True if ``value <comparator> threshold``. Unknown comparator -> False."""
    if comparator == ">":
        return value > threshold
    if comparator == ">=":
        return value >= threshold
    if comparator == "<":
        return value < threshold
    if comparator == "<=":
        return value <= threshold
    if comparator == "==":
        return value == threshold
    return False


def sign_payload(secret: str, body: bytes) -> str:
    """Return the ``sha256=<hex>`` HMAC of ``body`` under ``secret`` (the value of
    the ``X-FDM-Signature`` header the receiver verifies)."""
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def verify_signature(secret: str, body: bytes, header: str) -> bool:
    """Constant-time verify of an ``X-FDM-Signature`` header against ``body``.

    Provided so a receiver (and our tests) can validate a delivery: a tampered
    body or wrong secret yields a different digest and fails.
    """
    if not header:
        return False
    expected = sign_payload(secret, body)
    return hmac.compare_digest(expected, header)


# --------------------------------------------------------------------------- #
# Metric read                                                                 #
# --------------------------------------------------------------------------- #


def latest_sample(db: Session, server_id: int) -> MonitoringSample | None:
    """The most recent *successful* monitoring sample for a server (the source of
    truth for a numeric metric). A failed/absent sample yields None -> no eval."""
    return db.scalar(
        select(MonitoringSample)
        .where(MonitoringSample.server_id == server_id, MonitoringSample.ok.is_(True))
        .order_by(MonitoringSample.ts.desc())
        .limit(1)
    )


def _scope_servers(db: Session, rule: AlertRule) -> list[Server]:
    """Resolve the servers a rule evaluates against for its scope."""
    if rule.scope == "server":
        if rule.scope_server_id is None:
            return []
        srv = db.get(Server, rule.scope_server_id)
        return [srv] if srv is not None and srv.status != "archived" else []
    return list(
        db.scalars(select(Server).where(Server.status != "archived")).all()
    )


def _get_state(db: Session, rule_id: int, server_id: int) -> AlertRuleState:
    state = db.scalar(
        select(AlertRuleState).where(
            AlertRuleState.rule_id == rule_id,
            AlertRuleState.server_id == server_id,
        )
    )
    if state is None:
        state = AlertRuleState(rule_id=rule_id, server_id=server_id, in_breach=False)
        db.add(state)
        db.flush()
    return state


# --------------------------------------------------------------------------- #
# Evaluation sweep                                                            #
# --------------------------------------------------------------------------- #


def evaluate_alerts(
    db: Session,
    *,
    now: datetime | None = None,
    deliver: Callable[[int], None] | None = None,
) -> dict:
    """Sweep every enabled rule against its in-scope servers, firing a rule when
    it breaches and its cooldown window has elapsed. Returns a summary
    ``{"rules", "evaluated", "fired", "resolved"}``.

    ``deliver(firing_id)`` is called AFTER the firing row is committed (defaults
    to enqueuing an RQ delivery job) so a slow channel never blocks the sweep.
    Cooldown is gated purely on ``last_fired_at``: a sustained breach fires at
    most once per window; a re-breach after recovery + window fires again.
    """
    now = (now or datetime.now(UTC)).astimezone(UTC)
    deliver = deliver or enqueue_delivery
    rules = list(db.scalars(select(AlertRule).where(AlertRule.enabled.is_(True))).all())

    evaluated = fired = resolved = 0
    pending_delivery: list[int] = []

    for rule in rules:
        cooldown = timedelta(minutes=max(0, rule.cooldown_minutes))
        for server in _scope_servers(db, rule):
            sample = latest_sample(db, server.id)
            if sample is None:
                continue
            value = getattr(sample, rule.metric, None)
            if value is None:
                continue
            evaluated += 1
            state = _get_state(db, rule.id, server.id)
            breaching = compare(float(value), rule.comparator, rule.threshold)

            if breaching:
                last_fired = _as_utc(state.last_fired_at)
                due = last_fired is None or (now - last_fired) >= cooldown
                if due:
                    firing = AlertFiring(
                        rule_id=rule.id,
                        rule_name=rule.name,
                        server_id=server.id,
                        server_name=server.name,
                        metric=rule.metric,
                        comparator=rule.comparator,
                        threshold=rule.threshold,
                        value=float(value),
                        channels=[],
                    )
                    db.add(firing)
                    db.flush()
                    state.last_fired_at = now
                    pending_delivery.append(firing.id)
                    fired += 1
                state.in_breach = True
            else:
                if state.in_breach:
                    # Recovery: close any still-open incident for this rule/server.
                    _resolve_open_firings(db, rule.id, server.id, now=now)
                    resolved += 1
                state.in_breach = False

            state.last_value = float(value)
            state.last_evaluated_at = now

    db.commit()

    # Enqueue deliveries only after the commit so the firing rows are durable and
    # the delivery worker can load them (rule 3 — never dispatch inside the sweep).
    for firing_id in pending_delivery:
        try:
            deliver(firing_id)
        except Exception:  # noqa: BLE001 — a broker blip must not sink the sweep.
            logger.exception("failed to enqueue alert delivery for firing %s", firing_id)

    if fired or resolved:
        logger.info(
            "alert sweep: %d rule(s), %d evaluated, %d fired, %d resolved",
            len(rules),
            evaluated,
            fired,
            resolved,
        )
    return {"rules": len(rules), "evaluated": evaluated, "fired": fired, "resolved": resolved}


def _resolve_open_firings(db: Session, rule_id: int, server_id: int, *, now: datetime) -> None:
    open_rows = db.scalars(
        select(AlertFiring).where(
            AlertFiring.rule_id == rule_id,
            AlertFiring.server_id == server_id,
            AlertFiring.resolved_at.is_(None),
        )
    ).all()
    for row in open_rows:
        row.resolved_at = now


# --------------------------------------------------------------------------- #
# Channel senders (injectable for tests)                                      #
# --------------------------------------------------------------------------- #


def build_webhook_payload(firing: AlertFiring) -> bytes:
    """The canonical, compact JSON body signed and POSTed for a firing.

    Signed as raw bytes, so the receiver must verify the signature over the exact
    body received (not a re-serialisation).
    """
    return json.dumps(
        {
            "event": "alert.fired",
            "rule": firing.rule_name,
            "server": firing.server_name,
            "metric": firing.metric,
            "comparator": firing.comparator,
            "threshold": firing.threshold,
            "value": firing.value,
            "firing_id": firing.id,
            "ts": (firing.created_at or datetime.now(UTC)).astimezone(UTC).isoformat(),
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


def send_email(to_addresses: list[str], subject: str, body: str, db: Session) -> None:
    """Send one alert email per recipient, reusing the 2.8 SMTP sender (rule:
    reuse, do not fork the notification channel). No-op when SMTP is unconfigured.
    """
    from app.core.notifications import _send_email

    for addr in to_addresses:
        _send_email(addr, subject, body, db=db)


def post_webhook(
    url: str,
    body: bytes,
    signature: str,
    *,
    sleep: Callable[[float], None] = time.sleep,
    timeout_seconds: float = _WEBHOOK_TIMEOUT_SECONDS,
    max_attempts: int = _WEBHOOK_MAX_ATTEMPTS,
) -> None:
    """POST a signed body with timestamp + bounded exponential-backoff retry.

    ``timeout_seconds``/``max_attempts`` default to the sweep's bounds; the
    ``/test`` endpoint passes tighter ones so a dead operator-supplied URL
    can't hold the request thread for the sweep's full worst case.

    Raises the last error if every attempt fails so the caller records the
    channel as failed (the firing row carries the outcome; the sweep never sees
    the exception).
    """
    # X-FDM-Timestamp is send-time metadata, NOT part of the signed body (the
    # signature covers `body` only — see sign_payload), so a captured request
    # replays successfully against X-FDM-Signature verification alone.
    # Integrators that need replay protection should reject deliveries whose
    # X-FDM-Timestamp is older than a short max-age (e.g. 5 minutes) in
    # addition to verifying the signature (DOO-437).
    headers = {
        "Content-Type": "application/json",
        "X-FDM-Signature": signature,
        "X-FDM-Timestamp": datetime.now(UTC).isoformat(),
        "X-FDM-Event": "alert.fired",
    }
    last_exc: Exception | None = None
    for attempt in range(max_attempts):
        try:
            resp = httpx.post(url, content=body, headers=headers, timeout=timeout_seconds)
            resp.raise_for_status()
            return
        except Exception as exc:  # noqa: BLE001 — retry transient failures.
            last_exc = exc
            if attempt + 1 < max_attempts:
                sleep(_WEBHOOK_BACKOFF_BASE * (2**attempt))
    assert last_exc is not None
    raise last_exc


def _email_recipients(db: Session, rule: AlertRule) -> list[str]:
    """The email channel's recipients: the rule's explicit list, else every
    active user with an address."""
    if rule.email_to:
        return [a.strip() for a in rule.email_to.split(",") if a.strip()]
    return [
        u.email
        for u in db.scalars(select(User).where(User.is_active.is_(True))).all()
        if u.email
    ]


def deliver_firing(
    db: Session,
    firing: AlertFiring,
    *,
    secrets,
    webhook_timeout_seconds: float = _WEBHOOK_TIMEOUT_SECONDS,
    webhook_max_attempts: int = _WEBHOOK_MAX_ATTEMPTS,
) -> AlertFiring:
    """Send a firing over its rule's enabled channels and record the per-channel
    outcome on the row. One channel failing never blocks the other; the secret is
    decrypted here, in memory, only to sign — never persisted or logged.

    ``webhook_timeout_seconds``/``webhook_max_attempts`` default to the sweep's
    bounds (RQ-delivered, off the request thread); ``POST /test`` runs inline
    and passes the tighter ``_TEST_WEBHOOK_*`` bounds instead (DOO-437)."""
    rule = db.get(AlertRule, firing.rule_id) if firing.rule_id is not None else None
    outcomes: list[dict] = []

    subject = f"[Alert] {firing.rule_name}: {firing.metric} {firing.comparator} {firing.threshold}"
    body = (
        f"Alert rule '{firing.rule_name}' fired.\n\n"
        f"Server: {firing.server_name}\n"
        f"Metric: {firing.metric}\n"
        f"Value:  {firing.value}\n"
        f"Rule:   {firing.metric} {firing.comparator} {firing.threshold}\n"
    )

    if rule is not None and rule.channel_email:
        try:
            recipients = _email_recipients(db, rule)
            send_email(recipients, subject, body, db)
            outcomes.append(
                {"channel": "email", "ok": True, "error": None, "recipients": len(recipients)}
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("alert email delivery failed for firing %s", firing.id)
            outcomes.append({"channel": "email", "ok": False, "error": str(exc)[:200]})

    if rule is not None and rule.channel_webhook and rule.webhook_url:
        try:
            # webhook_url is operator-supplied with no egress allowlist (same
            # trust model as the 2.8 notification webhook — informational;
            # revisit if egress hardening to internal endpoints is later
            # desired, DOO-437).
            secret = secrets.decrypt(rule.webhook_secret_enc) if rule.webhook_secret_enc else ""
            # Fail closed: an empty secret keys the HMAC on b"", making the
            # X-FDM-Signature trivially forgeable — worse than sending none,
            # because a receiver validating in good faith gets a false
            # authenticity guarantee. Skip and record the misconfiguration
            # rather than emit an empty-key signature (DOO-1031).
            if not secret:
                logger.warning(
                    "alert rule %s webhook enabled but no secret configured — "
                    "skipping unsigned delivery for firing %s",
                    rule.id,
                    firing.id,
                )
                outcomes.append(
                    {
                        "channel": "webhook",
                        "ok": False,
                        "error": "webhook secret not configured — unsigned delivery refused",
                    }
                )
            else:
                payload = build_webhook_payload(firing)
                post_webhook(
                    rule.webhook_url,
                    payload,
                    sign_payload(secret, payload),
                    timeout_seconds=webhook_timeout_seconds,
                    max_attempts=webhook_max_attempts,
                )
                outcomes.append({"channel": "webhook", "ok": True, "error": None})
        except Exception as exc:  # noqa: BLE001
            logger.exception("alert webhook delivery failed for firing %s", firing.id)
            outcomes.append({"channel": "webhook", "ok": False, "error": str(exc)[:200]})

    firing.channels = outcomes
    firing.delivered_at = datetime.now(UTC)
    db.commit()
    db.refresh(firing)
    return firing


# --------------------------------------------------------------------------- #
# Delivery enqueue (RQ) with synchronous fallback                             #
# --------------------------------------------------------------------------- #


def enqueue_delivery(firing_id: int) -> None:
    """Enqueue the delivery of one firing onto the RQ ``default`` queue. Falls
    back to running it inline if Redis/RQ is unavailable (dev/tests) so a firing
    is never silently dropped."""
    try:
        from redis import Redis
        from rq import Queue

        from app.config import get_settings

        conn = Redis.from_url(get_settings().redis_url)
        conn.ping()
        Queue("default", connection=conn).enqueue(
            "app.core.alerts.run_delivery", firing_id, result_ttl=86400, failure_ttl=604800
        )
    except Exception:  # noqa: BLE001 — no broker: deliver inline.
        logger.info("no RQ broker for alert delivery; delivering firing %s inline", firing_id)
        run_delivery(firing_id)


def run_delivery(firing_id: int) -> None:  # pragma: no cover - exercised via deliver_firing
    """RQ entrypoint: load the firing + secrets and dispatch its channels."""
    from app.core.security import get_secrets_service
    from app.db import SessionLocal

    with SessionLocal() as db:
        firing = db.get(AlertFiring, firing_id)
        if firing is None:
            logger.warning("alert delivery: firing %s vanished", firing_id)
            return
        deliver_firing(db, firing, secrets=get_secrets_service())


# --------------------------------------------------------------------------- #
# Dashboard read helpers                                                       #
# --------------------------------------------------------------------------- #


def open_alerts_count(db: Session) -> int:
    """Number of currently-breaching (rule, server) pairs for enabled rules — the
    dashboard Open Alerts KPI."""
    from sqlalchemy import func as sfunc

    return int(
        db.scalar(
            select(sfunc.count(AlertRuleState.id))
            .join(AlertRule, AlertRule.id == AlertRuleState.rule_id)
            .where(AlertRuleState.in_breach.is_(True), AlertRule.enabled.is_(True))
        )
        or 0
    )


def recent_incidents(db: Session, *, limit: int = 20) -> list[AlertFiring]:
    """Most recent alert firings, newest first — the Incidents panel feed."""
    return list(
        db.scalars(
            select(AlertFiring).order_by(AlertFiring.created_at.desc()).limit(limit)
        ).all()
    )
