"""SEC-M1: proxy-aware throttle keys + per-email cross-IP backstop.

The throttle keys on request.client.host. X-Forwarded-For may only influence
that value when the socket peer is a configured trusted proxy; the per-email
backstop then catches attackers who rotate (real or forwarded) source IPs.
"""

from app.config import get_settings
from tests.conftest import PASSWORD

EMAIL = "admin@example.com"


def _attempt(client, password, xff=None, email=EMAIL):
    headers = {"X-Forwarded-For": xff} if xff else {}
    return client.post(
        "/api/auth/login", json={"email": email, "password": password}, headers=headers
    )


def _fail_with_rotating_xff(client, n, start=1):
    for i in range(start, start + n):
        response = _attempt(client, "wrong-password", xff=f"198.51.100.{i}")
        assert response.status_code == 401


def test_spoofed_xff_ignored_when_no_proxy_configured(client):
    """Default deployment (TRUSTED_PROXY_IPS empty): rotating X-Forwarded-For
    does not change the throttle key — the socket peer still locks."""
    _fail_with_rotating_xff(client, get_settings().login_lockout_threshold)
    response = _attempt(client, PASSWORD, xff="198.51.100.250")
    assert response.status_code == 429


def test_spoofed_xff_ignored_from_untrusted_peer(proxy_client_factory):
    """Middleware active, but the socket peer is NOT the trusted proxy:
    X-Forwarded-For is still ignored."""
    client = proxy_client_factory("203.0.113.9")
    _fail_with_rotating_xff(client, get_settings().login_lockout_threshold)
    response = _attempt(client, PASSWORD, xff="198.51.100.250")
    assert response.status_code == 429


def test_trusted_proxy_xff_becomes_throttle_key(proxy_client_factory):
    """Behind the trusted proxy the forwarded client address is the key:
    locking one forwarded IP does not lock the account for another."""
    client = proxy_client_factory("testclient")
    threshold = get_settings().login_lockout_threshold
    for _ in range(threshold):
        assert _attempt(client, "wrong-password", xff="198.51.100.1").status_code == 401
    assert _attempt(client, PASSWORD, xff="198.51.100.1").status_code == 429
    assert _attempt(client, PASSWORD, xff="198.51.100.2").status_code == 200


def test_rotating_xff_hits_email_backstop(proxy_client_factory):
    """Distributed spraying: one failure from each of N distinct forwarded IPs
    never trips a pair lock, but the per-email backstop locks the account."""
    client = proxy_client_factory("testclient")
    _fail_with_rotating_xff(client, get_settings().login_email_failure_limit)
    response = _attempt(client, PASSWORD, xff="203.0.113.77")
    assert response.status_code == 429
    assert int(response.headers["Retry-After"]) > 0


def test_email_backstop_lock_expires(proxy_client_factory, fake_clock):
    client = proxy_client_factory("testclient")
    settings = get_settings()
    _fail_with_rotating_xff(client, settings.login_email_failure_limit)
    assert _attempt(client, PASSWORD, xff="203.0.113.77").status_code == 429

    fake_clock.advance(settings.login_lockout_seconds + 1)
    assert _attempt(client, PASSWORD, xff="203.0.113.77").status_code == 200


def test_email_backstop_window_slides(throttle, fake_clock):
    settings = get_settings()
    limit = settings.login_email_failure_limit
    for i in range(limit - 1):
        throttle.register_failure(EMAIL, f"10.0.0.{i}")
    fake_clock.advance(settings.login_email_failure_window_seconds + 1)
    # Old failures aged out: one more failure is 1-in-window, not limit-th.
    throttle.register_failure(EMAIL, "10.0.1.1")
    assert throttle.retry_after(EMAIL, "10.0.2.2") == 0
