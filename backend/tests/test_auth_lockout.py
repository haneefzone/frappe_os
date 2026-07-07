from app.config import get_settings
from tests.conftest import login

EMAIL = "admin@example.com"


def _fail_times(client, n):
    for _ in range(n):
        response = login(client, EMAIL, "wrong-password")
        assert response.status_code == 401


def test_six_wrong_passwords_lock_temporarily(client):
    threshold = get_settings().login_lockout_threshold
    _fail_times(client, threshold)
    # Even the CORRECT password is rejected while locked.
    response = login(client, EMAIL)
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "rate_limited"
    assert int(response.headers["Retry-After"]) > 0


def test_attempts_below_threshold_do_not_lock(client):
    _fail_times(client, get_settings().login_lockout_threshold - 1)
    assert login(client, EMAIL).status_code == 200


def test_lockout_expires(client, fake_clock):
    settings = get_settings()
    _fail_times(client, settings.login_lockout_threshold)
    assert login(client, EMAIL).status_code == 429

    fake_clock.advance(settings.login_lockout_seconds + 1)
    assert login(client, EMAIL).status_code == 200


def test_success_resets_failure_counter(client):
    threshold = get_settings().login_lockout_threshold
    _fail_times(client, threshold - 1)
    assert login(client, EMAIL).status_code == 200
    # Counter restarted: threshold-1 more failures still don't lock.
    _fail_times(client, threshold - 1)
    assert login(client, EMAIL).status_code == 200


def test_lockout_is_per_email(client):
    _fail_times(client, get_settings().login_lockout_threshold)
    # A different account from the same client is unaffected.
    assert login(client, "developer@example.com").status_code == 200
