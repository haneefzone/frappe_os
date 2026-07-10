"""Settings (session 1.12, B4.17): read/update the white-label + defaults row,
logo upload (base64, type + size validation), environment info, and RBAC
(Read-only can view but never mutate — rule 7)."""

import base64

from tests.conftest import csrf_headers, login

# A 1x1 transparent PNG.
PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+P+/HgAFhAJ/wlseKgAAAABJRU5ErkJggg=="
)


def test_get_settings_materialises_defaults(client):
    login(client, "readonly@example.com")
    resp = client.get("/api/settings")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["product_name"] == "FDM Platform"
    assert body["default_tz"] == "Asia/Dubai"
    assert body["port_range_start"] == 8000


def test_update_settings(client):
    login(client, "admin@example.com")
    resp = client.put(
        "/api/settings",
        json={"product_name": "Acme Panel", "default_tz": "UTC"},
        headers=csrf_headers(client),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["product_name"] == "Acme Panel"
    assert resp.json()["default_tz"] == "UTC"
    # Untouched fields are preserved.
    assert resp.json()["bench_base_path"] == "/home/frappe"


def test_update_rejects_inverted_port_range(client):
    login(client, "admin@example.com")
    resp = client.put(
        "/api/settings",
        json={"port_range_start": 9000, "port_range_end": 8000},
        headers=csrf_headers(client),
    )
    assert resp.status_code == 422, resp.text


def test_update_rejects_relative_bench_path(client):
    login(client, "admin@example.com")
    resp = client.put(
        "/api/settings",
        json={"bench_base_path": "relative/path"},
        headers=csrf_headers(client),
    )
    assert resp.status_code == 422, resp.text


def test_readonly_cannot_update_settings(client):
    login(client, "readonly@example.com")
    resp = client.put(
        "/api/settings",
        json={"product_name": "Nope"},
        headers=csrf_headers(client),
    )
    assert resp.status_code == 403, resp.text


def test_logo_upload_and_fetch(client, tmp_path, monkeypatch):
    # Point uploads at a temp dir so the test doesn't write into the repo.
    from app.config import get_settings

    monkeypatch.setenv("UPLOADS_DIR", str(tmp_path))
    get_settings.cache_clear()
    try:
        login(client, "admin@example.com")
        resp = client.post(
            "/api/settings/logo",
            json={
                "content_type": "image/png",
                "content_base64": base64.b64encode(PNG_1X1).decode(),
            },
            headers=csrf_headers(client),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["logo_path"] == "/api/settings/logo"

        got = client.get("/api/settings/logo")
        assert got.status_code == 200
        assert got.content == PNG_1X1
        # Served locked down so an uploaded SVG can't execute script (TA follow-up).
        assert "default-src 'none'" in got.headers.get("content-security-policy", "")
        assert got.headers.get("x-content-type-options") == "nosniff"
    finally:
        get_settings.cache_clear()


def test_logo_rejects_bad_type(client):
    login(client, "admin@example.com")
    resp = client.post(
        "/api/settings/logo",
        json={"content_type": "application/pdf", "content_base64": "AAAA"},
        headers=csrf_headers(client),
    )
    assert resp.status_code == 422, resp.text


def test_environment_info(client):
    login(client, "admin@example.com")
    resp = client.get("/api/settings/environment")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # database_backend reflects the configured DATABASE_URL (postgresql by
    # default; tests override only the session, not the settings URL).
    assert body["database_backend"] in ("postgresql", "sqlite", "other")
    assert body["app_version"]
    assert "python_version" in body
