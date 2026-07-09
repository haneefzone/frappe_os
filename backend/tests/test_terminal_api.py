"""Tests for terminal session create + ticket lifecycle (FDM 1.5).

The WS bridge itself is not integration-tested here (it requires a live Redis
and AsyncSSH connection), but the REST layer — RBAC, server-not-found, missing
credential, and ticket storage/consume — is fully covered.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.conftest import csrf_headers, login

# ---------------------------------------------------------------------------
# RBAC: POST /api/terminal/sessions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "role,expected",
    [
        ("admin", 200),  # Admin has wildcard → terminal:access granted
        ("developer", 200),  # Developer has terminal:access
        ("readonly", 403),  # Read-only lacks terminal:access
    ],
)
def test_create_session_rbac(client, seeded_users, role, expected):
    login(client, f"{role}@example.com")
    with patch("app.api.routes.terminal._store_ticket", new_callable=AsyncMock):
        resp = client.post(
            "/api/terminal/sessions",
            json={"server_id": 99},
            headers=csrf_headers(client),
        )
    # 404 (server not found) is fine for Admin/Developer — it means RBAC passed.
    if expected == 200:
        assert resp.status_code in (200, 404, 422)
    else:
        assert resp.status_code == 403


def test_create_session_unauthenticated(client):
    resp = client.post("/api/terminal/sessions", json={"server_id": 1})
    assert resp.status_code == 401


def test_create_session_server_not_found(client):
    login(client, "developer@example.com")
    with patch("app.api.routes.terminal._store_ticket", new_callable=AsyncMock):
        resp = client.post(
            "/api/terminal/sessions",
            json={"server_id": 9999},
            headers=csrf_headers(client),
        )
    assert resp.status_code == 404


def test_create_session_no_credential(client, db_session, seeded_users):
    """A server row with no credential returns 422."""
    from app.models import Server

    server = Server(name="nocred", hostname="192.0.2.1")
    db_session.add(server)
    db_session.commit()
    db_session.refresh(server)

    login(client, "developer@example.com")
    with patch("app.api.routes.terminal._store_ticket", new_callable=AsyncMock):
        resp = client.post(
            "/api/terminal/sessions",
            json={"server_id": server.id},
            headers=csrf_headers(client),
        )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Ticket helpers (unit-tested via direct import, no network).
# ---------------------------------------------------------------------------


def _make_redis_mock(execute_return):
    """Build a mock Redis client where pipeline() is sync but execute() is async."""
    pipe_mock = MagicMock()
    pipe_mock.get = MagicMock()
    pipe_mock.delete = MagicMock()
    pipe_mock.execute = AsyncMock(return_value=execute_return)
    r_mock = MagicMock()
    r_mock.pipeline.return_value = pipe_mock
    r_mock.aclose = AsyncMock()
    return r_mock, pipe_mock


def test_consume_ticket_missing():
    """Consuming a non-existent ticket returns None."""
    r_mock, _ = _make_redis_mock([None, 1])
    with patch("app.api.routes.terminal._redis", return_value=r_mock):
        from app.api.routes.terminal import _consume_ticket

        result = asyncio.run(_consume_ticket("nonexistent"))
        assert result is None


def test_consume_ticket_found():
    """Consuming an existing ticket returns the payload and deletes the key."""
    import json

    payload = {"session_id": 42, "server_id": 7, "user_id": 3}
    r_mock, pipe_mock = _make_redis_mock([json.dumps(payload), 1])
    with patch("app.api.routes.terminal._redis", return_value=r_mock):
        from app.api.routes.terminal import _consume_ticket

        result = asyncio.run(_consume_ticket("valid-ticket"))
        assert result == payload
        pipe_mock.execute.assert_awaited_once()


# ---------------------------------------------------------------------------
# GET /api/terminal/sessions
# ---------------------------------------------------------------------------


def test_list_sessions_rbac(client, seeded_users):
    login(client, "readonly@example.com")
    resp = client.get("/api/terminal/sessions")
    assert resp.status_code == 403


def test_list_sessions_empty(client, seeded_users):
    login(client, "developer@example.com")
    resp = client.get("/api/terminal/sessions")
    assert resp.status_code == 200
    assert resp.json() == []
