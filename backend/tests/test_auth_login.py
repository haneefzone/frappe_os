from tests.conftest import csrf_headers, login


def test_login_wrong_password_rejected(client):
    response = login(client, "admin@example.com", "nope-nope-nope")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_login_unknown_email_same_shape_as_wrong_password(client):
    response = login(client, "ghost@example.com")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_login_sets_session_cookies_and_returns_user(client):
    response = login(client, "admin@example.com")
    assert response.status_code == 200
    body = response.json()
    assert body["email"] == "admin@example.com"
    assert body["role"] == "Admin"
    assert body["permissions"] == ["*"]
    for cookie in ("fdm_access_token", "fdm_refresh_token", "fdm_csrf_token"):
        assert client.cookies.get(cookie)
    # Session cookies are httpOnly; the CSRF cookie must be JS-readable.
    set_cookies = response.headers.get_list("set-cookie")
    assert any("fdm_access_token" in c and "HttpOnly" in c for c in set_cookies)
    assert any("fdm_refresh_token" in c and "HttpOnly" in c for c in set_cookies)
    assert any("fdm_csrf_token" in c and "HttpOnly" not in c for c in set_cookies)


def test_me_requires_auth(client):
    assert client.get("/api/auth/me").status_code == 401


def test_me_returns_current_user(client):
    login(client, "developer@example.com")
    response = client.get("/api/auth/me")
    assert response.status_code == 200
    assert response.json()["email"] == "developer@example.com"
    assert response.json()["last_login"] is not None


def test_mutation_without_csrf_header_rejected(client):
    login(client, "admin@example.com")
    response = client.post("/api/test/mutate")
    assert response.status_code == 403
    assert "CSRF" in response.json()["error"]["message"]


def test_mutation_with_csrf_header_allowed(client):
    login(client, "admin@example.com")
    response = client.post("/api/test/mutate", headers=csrf_headers(client))
    assert response.status_code == 200


def test_refresh_rotates_session(client):
    login(client, "admin@example.com")
    old_access = client.cookies.get("fdm_access_token")
    response = client.post("/api/auth/refresh", headers=csrf_headers(client))
    assert response.status_code == 200
    assert client.cookies.get("fdm_access_token") != old_access
    assert client.get("/api/auth/me").status_code == 200


def test_refresh_without_csrf_header_rejected(client):
    login(client, "admin@example.com")
    assert client.post("/api/auth/refresh").status_code == 403


def test_refresh_without_cookie_rejected(client):
    assert client.post("/api/auth/refresh").status_code == 401


def test_logout_clears_session(client):
    login(client, "admin@example.com")
    response = client.post("/api/auth/logout")
    assert response.status_code == 204
    assert client.get("/api/auth/me").status_code == 401


def test_inactive_user_cannot_login(client):
    response = login(client, "inactive@example.com")
    assert response.status_code == 401
