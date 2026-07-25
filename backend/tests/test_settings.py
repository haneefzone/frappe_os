"""Settings (sessions 1.12 + 6.6): white-label brand layer, logo/favicon upload,
/api/branding public endpoint, environment info, and RBAC."""

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


# --------------------------------------------------------------------------- #
# Session 6.6: brand token layer                                               #
# --------------------------------------------------------------------------- #

def test_branding_endpoint_unauthenticated(client):
    """GET /api/branding must succeed without any auth cookie."""
    resp = client.get("/api/branding")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # Returns only the public bundle — not bench paths, ports, etc.
    assert body["product_name"] == "FDM Platform"
    assert "logo_url" in body
    assert "favicon_url" in body
    assert "accent_hex" in body
    assert "bench_base_path" not in body
    assert "port_range_start" not in body


def test_branding_exposes_no_private_fields(client):
    """Confirm sensitive defaults are absent from /api/branding."""
    resp = client.get("/api/branding")
    data = resp.json()
    for forbidden in ("bench_base_path", "port_range_start", "port_range_end", "default_tz"):
        assert forbidden not in data, f"Private field '{forbidden}' leaked into /api/branding"


def test_update_brand_tokens(client):
    login(client, "admin@example.com")
    resp = client.put(
        "/api/settings",
        json={
            "accent_hex": "#3b82f6",
            "support_link": "https://support.example.com",
            "footer_line": "Powered by Acme Corp",
        },
        headers=csrf_headers(client),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["accent_hex"] == "#3b82f6"
    assert body["support_link"] == "https://support.example.com"
    assert body["footer_line"] == "Powered by Acme Corp"


def test_update_rejects_invalid_accent_hex(client):
    login(client, "admin@example.com")
    resp = client.put(
        "/api/settings",
        json={"accent_hex": "not-a-colour"},
        headers=csrf_headers(client),
    )
    assert resp.status_code == 422, resp.text


def test_update_rejects_non_https_support_link(client):
    login(client, "admin@example.com")
    resp = client.put(
        "/api/settings",
        json={"support_link": "ftp://nope.example.com"},
        headers=csrf_headers(client),
    )
    assert resp.status_code == 422, resp.text


def test_dark_logo_upload_and_fetch(client, tmp_path, monkeypatch):
    from app.config import get_settings

    monkeypatch.setenv("UPLOADS_DIR", str(tmp_path))
    get_settings.cache_clear()
    try:
        login(client, "admin@example.com")
        resp = client.post(
            "/api/settings/logo-dark",
            json={
                "content_type": "image/png",
                "content_base64": base64.b64encode(PNG_1X1).decode(),
            },
            headers=csrf_headers(client),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["logo_dark_path"] == "/api/settings/logo-dark"

        got = client.get("/api/settings/logo-dark")
        assert got.status_code == 200
        assert got.content == PNG_1X1
        assert "default-src 'none'" in got.headers.get("content-security-policy", "")
    finally:
        get_settings.cache_clear()


def test_favicon_upload_and_fetch(client, tmp_path, monkeypatch):
    from app.config import get_settings

    monkeypatch.setenv("UPLOADS_DIR", str(tmp_path))
    get_settings.cache_clear()
    try:
        login(client, "admin@example.com")
        resp = client.post(
            "/api/settings/favicon",
            json={
                "content_type": "image/png",
                "content_base64": base64.b64encode(PNG_1X1).decode(),
            },
            headers=csrf_headers(client),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["favicon_path"] == "/api/settings/favicon"

        got = client.get("/api/settings/favicon")
        assert got.status_code == 200
        assert got.content == PNG_1X1
        assert got.headers.get("x-content-type-options") == "nosniff"
    finally:
        get_settings.cache_clear()


def test_favicon_rejects_bad_type(client):
    login(client, "admin@example.com")
    resp = client.post(
        "/api/settings/favicon",
        json={"content_type": "image/svg+xml", "content_base64": "AAAA"},
        headers=csrf_headers(client),
    )
    assert resp.status_code == 422, resp.text


def test_branding_reflects_updated_product_name(client):
    login(client, "admin@example.com")
    client.put(
        "/api/settings",
        json={"product_name": "Nebula Panel"},
        headers=csrf_headers(client),
    )
    resp = client.get("/api/branding")
    assert resp.json()["product_name"] == "Nebula Panel"
