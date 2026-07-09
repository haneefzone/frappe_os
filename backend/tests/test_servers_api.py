"""Server registry API: RBAC, secrets-never-in-responses, and the streamed test."""


from app.core.security import generate_ed25519_keypair, get_secrets_service
from app.core.ssh import SSHService, get_ssh_service
from app.models import SSHCredential
from tests.conftest import csrf_headers, login
from tests.test_server_ssh import HAPPY_RESPONSES, STORED_KEY, _FakeConn


def _generate_body(name="vm-alpha", **cred):
    credential = {"username": "frappe", "auth_type": "key", "generate": True, **cred}
    return {"name": name, "hostname": "10.0.0.5", "ssh_port": 22, "credential": credential}


def _create(client, body):
    return client.post("/api/servers", json=body, headers=csrf_headers(client))


def test_create_returns_pubkey_and_stores_only_a_fernet_token(client, db_session):
    login(client, "admin@example.com")
    resp = _create(client, _generate_body())
    assert resp.status_code == 201, resp.text
    data = resp.json()

    # Public key handed back once; private key never echoed anywhere.
    assert data["generated_public_key"].startswith("ssh-ed25519 ")
    assert data["credential"]["has_private_key"] is True
    # The response exposes only booleans about the credential, no key material.
    assert set(data["credential"]) == {
        "username",
        "auth_type",
        "sudo_mode",
        "has_private_key",
        "has_password",
        "host_key_pinned",
    }
    assert "PRIVATE KEY" not in resp.text

    # The DB holds an unreadable Fernet token, not the key material.
    cred = db_session.query(SSHCredential).filter_by(server_id=data["id"]).one()
    assert cred.private_key_enc
    assert "PRIVATE KEY" not in cred.private_key_enc
    # It round-trips back to a real private key under the master key.
    assert "OPENSSH PRIVATE KEY" in get_secrets_service().decrypt(cred.private_key_enc)


def test_create_with_pasted_key(client, db_session):
    login(client, "admin@example.com")
    private_pem, _ = generate_ed25519_keypair()
    body = {
        "name": "vm-pasted",
        "hostname": "10.0.0.9",
        "credential": {"username": "frappe", "auth_type": "key", "private_key": private_pem},
    }
    resp = _create(client, body)
    assert resp.status_code == 201, resp.text
    assert resp.json()["generated_public_key"] is None


def test_password_auth_is_encrypted(client, db_session):
    login(client, "admin@example.com")
    body = {
        "name": "vm-pw",
        "hostname": "10.0.0.7",
        "credential": {"username": "ubuntu", "auth_type": "password", "password": "hunter2"},
    }
    resp = _create(client, body)
    assert resp.status_code == 201, resp.text
    assert "hunter2" not in resp.text
    cred = db_session.query(SSHCredential).filter_by(server_id=resp.json()["id"]).one()
    assert cred.password_enc and cred.password_enc != "hunter2"
    assert get_secrets_service().decrypt(cred.password_enc) == "hunter2"


def test_readonly_cannot_create_but_can_read(client):
    login(client, "readonly@example.com")
    assert _create(client, _generate_body(name="nope")).status_code == 403
    assert client.get("/api/servers").status_code == 200


def test_duplicate_name_conflicts(client):
    login(client, "admin@example.com")
    assert _create(client, _generate_body(name="dup")).status_code == 201
    assert _create(client, _generate_body(name="dup")).status_code == 409


def test_missing_secret_is_rejected(client):
    login(client, "admin@example.com")
    body = {
        "name": "vm-bad",
        "hostname": "10.0.0.1",
        "credential": {"username": "frappe", "auth_type": "key"},  # no key, no generate
    }
    assert _create(client, body).status_code == 422


def test_delete_server(client):
    login(client, "admin@example.com")
    server_id = _create(client, _generate_body(name="vm-del")).json()["id"]
    deleted = client.delete(f"/api/servers/{server_id}", headers=csrf_headers(client))
    assert deleted.status_code == 204
    assert client.get(f"/api/servers/{server_id}").status_code == 404


def test_streamed_test_connection_updates_status_and_pins_key(client, db_session):
    login(client, "admin@example.com")
    server_id = _create(client, _generate_body(name="vm-test")).json()["id"]

    # Inject a fake SSH connection so the test is deterministic and offline.
    conn = _FakeConn(STORED_KEY, HAPPY_RESPONSES)

    async def connector(**kwargs):
        return conn

    client.app.dependency_overrides[get_ssh_service] = lambda: SSHService(
        get_secrets_service(), connector=connector
    )
    try:
        resp = client.post(f"/api/servers/{server_id}/test", headers=csrf_headers(client))
    finally:
        client.app.dependency_overrides.pop(get_ssh_service, None)

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    body = resp.text
    assert '"check": "ssh"' in body
    assert '"whoami": "frappe"' in body
    assert '"check": "done"' in body

    # Side effects persisted: online, OS detected, host key pinned.
    server = client.get(f"/api/servers/{server_id}").json()
    assert server["status"] == "online"
    assert server["os_version"] == "Ubuntu 24.04.1 LTS"
    assert server["credential"]["host_key_pinned"] is True


def test_readonly_cannot_test(client):
    login(client, "admin@example.com")
    server_id = _create(client, _generate_body(name="vm-ro")).json()["id"]
    login(client, "readonly@example.com")
    resp = client.post(f"/api/servers/{server_id}/test", headers=csrf_headers(client))
    assert resp.status_code == 403
