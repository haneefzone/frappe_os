"""AI integration layer (session 5.0): encrypted settings + AnthropicClient.

Covers the acceptance gates: Fernet round-trip; the key never appears in
responses/DB plaintext; the outbound model is read from settings (defaults
opus/sonnet, and editing settings changes it); the job-engine secret-masking
redacts an injected fake secret from a sample payload before the SDK call; a
disabled/unkeyed integration refuses cleanly; the monthly budget cap bites;
usage accounting is persisted; and RBAC is Admin-only.

Every test runs against a mocked AnthropicClient — no real SDK/network.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from app.api.routes.ai_settings import get_ai_client
from app.core.ai import (
    AIBudgetError,
    AIDisabledError,
    AnthropicClient,
    build_prompt_payload,
)
from app.core.security import get_secrets_service
from app.models import AuditLog
from app.models.ai_settings import (
    DEFAULT_MODEL_DEEP,
    DEFAULT_MODEL_HIGH_VOLUME,
    AISettings,
)
from tests.conftest import csrf_headers, login

FAKE_KEY = "sk-ant-fake-key-abc123"


# --------------------------------------------------------------------------- #
# A mock Anthropic SDK client. Captures the outbound request; returns a canned
# message with usage so accounting can be asserted.
# --------------------------------------------------------------------------- #


class _Block:
    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


class _Usage:
    def __init__(self, input_tokens: int, output_tokens: int) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class _Message:
    def __init__(self, text: str, model: str, i: int, o: int) -> None:
        self.content = [_Block(text)]
        self.model = model
        self.usage = _Usage(i, o)


class _Stream:
    def __init__(self, message: _Message) -> None:
        self._m = message

    def __enter__(self) -> _Stream:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def get_final_message(self) -> _Message:
        return self._m


class _FakeMessages:
    def __init__(self, sink: dict) -> None:
        self._sink = sink

    def stream(self, **kwargs):
        self._sink["request"] = kwargs
        return _Stream(self._sink["message"])

    def create(self, **kwargs):
        self._sink["request"] = kwargs
        return self._sink["message"]


class _FakeClient:
    def __init__(self, sink: dict) -> None:
        self.messages = _FakeMessages(sink)


def _fake_factory(sink: dict):
    sink.setdefault("message", _Message("pong", DEFAULT_MODEL_DEEP, 5, 2))

    def factory(*, api_key: str, timeout: float, max_retries: int):
        sink["api_key"] = api_key
        return _FakeClient(sink)

    return factory


def _make_client(db, sink, **kw) -> AnthropicClient:
    row = AISettings.get_or_create(db)
    return AnthropicClient(
        row, get_secrets_service(), db=db, client_factory=_fake_factory(sink), **kw
    )


# --------------------------------------------------------------------------- #
# build_prompt_payload — the redaction gate.
# --------------------------------------------------------------------------- #


def test_build_prompt_payload_redacts_secret():
    secret = "hunter2-super-secret"
    payload = build_prompt_payload(
        messages=[
            {"role": "user", "content": f"the db password is {secret} please help"},
            {
                "role": "user",
                "content": [{"type": "text", "text": f"again: {secret}"}],
            },
        ],
        system=f"system knows {secret} too",
        secrets=(secret,),
    )
    blob = json.dumps(payload.request_kwargs(), ensure_ascii=False)
    assert secret not in blob
    assert "••••" in blob
    assert secret not in (payload.system or "")


def test_complete_never_sends_secret_to_sdk(db_session):
    AISettings.get_or_create(db_session)
    secrets = get_secrets_service()
    row = db_session.get(AISettings, 1)
    row.enabled = True
    row.api_key_enc = secrets.encrypt(FAKE_KEY)
    db_session.commit()

    sink: dict = {}
    client = _make_client(db_session, sink)
    secret = "leaked-credential-xyz"
    client.complete(
        messages=[{"role": "user", "content": f"here is {secret}"}],
        secrets=(secret,),
    )
    assert secret not in json.dumps(sink["request"])


# --------------------------------------------------------------------------- #
# Model routing read from settings.
# --------------------------------------------------------------------------- #


def _enable(db_session):
    secrets = get_secrets_service()
    row = AISettings.get_or_create(db_session)
    row.enabled = True
    row.api_key_enc = secrets.encrypt(FAKE_KEY)
    db_session.commit()
    return row


def test_defaults_are_current_models(db_session):
    row = AISettings.get_or_create(db_session)
    assert row.model_deep == DEFAULT_MODEL_DEEP == "claude-opus-4-8"
    assert row.model_high_volume == DEFAULT_MODEL_HIGH_VOLUME == "claude-sonnet-4-6"


def test_outbound_model_read_from_settings(db_session):
    _enable(db_session)
    sink: dict = {}
    client = _make_client(db_session, sink)

    client.complete(messages=[{"role": "user", "content": "hi"}], tier="deep")
    assert sink["request"]["model"] == "claude-opus-4-8"

    client.complete(messages=[{"role": "user", "content": "hi"}], tier="high_volume")
    assert sink["request"]["model"] == "claude-sonnet-4-6"


def test_editing_settings_changes_outbound_model(db_session):
    row = _enable(db_session)
    row.model_deep = "claude-opus-4-7"
    db_session.commit()
    sink: dict = {}
    client = _make_client(db_session, sink)
    client.complete(messages=[{"role": "user", "content": "hi"}], tier="deep")
    assert sink["request"]["model"] == "claude-opus-4-7"


def test_request_uses_adaptive_thinking_and_schema(db_session):
    _enable(db_session)
    sink: dict = {}
    client = _make_client(db_session, sink)
    schema = {"type": "object", "properties": {"n": {"type": "integer"}}}
    sink["message"] = _Message('{"n": 3}', "claude-opus-4-8", 4, 3)
    result = client.complete(
        messages=[{"role": "user", "content": "give n"}], schema=schema
    )
    assert sink["request"]["thinking"] == {"type": "adaptive"}
    assert sink["request"]["output_config"]["format"]["schema"] == schema
    assert result.parsed == {"n": 3}


# --------------------------------------------------------------------------- #
# Refusal when disabled/unkeyed, budget cap, accounting.
# --------------------------------------------------------------------------- #


def test_refuses_when_disabled(db_session):
    AISettings.get_or_create(db_session)  # disabled by default
    sink: dict = {}
    client = _make_client(db_session, sink)
    try:
        client.complete(messages=[{"role": "user", "content": "hi"}])
        raise AssertionError("expected AIDisabledError")
    except AIDisabledError:
        pass
    assert "request" not in sink  # no call was attempted


def test_refuses_when_no_key(db_session):
    row = AISettings.get_or_create(db_session)
    row.enabled = True  # enabled but unkeyed
    db_session.commit()
    sink: dict = {}
    client = _make_client(db_session, sink)
    try:
        client.complete(messages=[{"role": "user", "content": "hi"}])
        raise AssertionError("expected AIDisabledError")
    except AIDisabledError:
        pass


def test_budget_cap_bites(db_session):
    row = _enable(db_session)
    row.monthly_budget_usd = 1.0
    row.usage_period = datetime.now(UTC).strftime("%Y-%m")
    row.cost_usd_month = 1.5  # already over
    db_session.commit()
    sink: dict = {}
    client = _make_client(db_session, sink)
    try:
        client.complete(messages=[{"role": "user", "content": "hi"}])
        raise AssertionError("expected AIBudgetError")
    except AIBudgetError:
        pass
    assert "request" not in sink


def test_usage_accounting_persisted(db_session):
    _enable(db_session)
    sink: dict = {"message": _Message("pong", "claude-opus-4-8", 1000, 500)}
    client = _make_client(db_session, sink)
    client.complete(messages=[{"role": "user", "content": "hi"}])
    row = db_session.get(AISettings, 1)
    assert row.tokens_input_month == 1000
    assert row.tokens_output_month == 500
    # opus 4.8: $5/1M in + $25/1M out = 0.005 + 0.0125 = 0.0175
    assert abs(row.cost_usd_month - 0.0175) < 1e-9


# --------------------------------------------------------------------------- #
# API: RBAC, key never returned/stored plaintext, /test.
# --------------------------------------------------------------------------- #


def test_get_requires_admin(client):
    login(client, "developer@example.com")
    assert client.get("/api/settings/ai").status_code == 403
    login(client, "readonly@example.com")
    assert client.get("/api/settings/ai").status_code == 403
    login(client, "admin@example.com")
    assert client.get("/api/settings/ai").status_code == 200


def test_put_sets_key_never_returned_or_stored_plaintext(client, db_session):
    login(client, "admin@example.com")
    resp = client.put(
        "/api/settings/ai",
        json={"enabled": True, "api_key": FAKE_KEY, "monthly_budget_usd": 50},
        headers=csrf_headers(client),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "api_key" not in body
    assert body["api_key_set"] is True
    assert body["api_key_masked"] == "••••"
    assert body["enabled"] is True

    # Fernet round-trip: DB stores an opaque token, never the plaintext.
    row = db_session.get(AISettings, 1)
    assert row.api_key_enc is not None
    assert FAKE_KEY not in row.api_key_enc
    assert get_secrets_service().decrypt(row.api_key_enc) == FAKE_KEY

    # The audit row records only that the key was set — never its value.
    audit = (
        db_session.query(AuditLog)
        .filter(AuditLog.action == "ai.settings.update")
        .one()
    )
    assert audit.params_masked.get("credential_updated") == "set"
    assert "api_key" not in audit.params_masked  # the key value is never recorded
    assert FAKE_KEY not in json.dumps(audit.params_masked)


def test_readonly_cannot_update(client):
    login(client, "readonly@example.com")
    resp = client.put(
        "/api/settings/ai",
        json={"enabled": True},
        headers=csrf_headers(client),
    )
    assert resp.status_code == 403


def test_test_endpoint_disabled_returns_409(client):
    login(client, "admin@example.com")
    resp = client.post("/api/settings/ai/test", headers=csrf_headers(client))
    assert resp.status_code == 409, resp.text


def test_test_endpoint_streams_pong(client, db_session):
    login(client, "admin@example.com")
    client.put(
        "/api/settings/ai",
        json={"enabled": True, "api_key": FAKE_KEY},
        headers=csrf_headers(client),
    )
    sink: dict = {"message": _Message("pong", "claude-opus-4-8", 5, 1)}
    client.app.dependency_overrides[get_ai_client] = lambda: _make_client(
        db_session, sink
    )
    try:
        resp = client.post("/api/settings/ai/test", headers=csrf_headers(client))
    finally:
        client.app.dependency_overrides.pop(get_ai_client, None)
    assert resp.status_code == 200, resp.text
    assert "event: result" in resp.text
    assert "pong" in resp.text
    # The ping used the deep model (claude-opus-4-8).
    assert sink["request"]["model"] == "claude-opus-4-8"
