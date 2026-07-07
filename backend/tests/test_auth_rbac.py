from tests.conftest import csrf_headers, login


def test_readonly_gets_403_on_mutating_route(client):
    login(client, "readonly@example.com")
    response = client.post("/api/test/mutate", headers=csrf_headers(client))
    assert response.status_code == 403
    assert "site:operate" in response.json()["error"]["message"]


def test_readonly_can_still_read(client):
    login(client, "readonly@example.com")
    assert client.get("/api/auth/me").status_code == 200


def test_developer_has_site_operate(client):
    login(client, "developer@example.com")
    response = client.post("/api/test/mutate", headers=csrf_headers(client))
    assert response.status_code == 200


def test_admin_wildcard_grants_everything(client):
    login(client, "admin@example.com")
    response = client.post("/api/test/mutate", headers=csrf_headers(client))
    assert response.status_code == 200


def test_unauthenticated_mutation_is_401_not_403(client):
    assert client.post("/api/test/mutate").status_code == 401
