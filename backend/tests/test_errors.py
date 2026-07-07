from fastapi import Query
from fastapi.testclient import TestClient

from app.main import create_app


def make_client() -> TestClient:
    app = create_app()

    @app.get("/api/_test/validated")
    def validated(count: int = Query(...)) -> dict[str, int]:
        return {"count": count}

    @app.get("/api/_test/boom")
    def boom() -> None:
        raise RuntimeError("kaboom")

    return TestClient(app, raise_server_exceptions=False)


def test_404_uses_error_envelope():
    response = make_client().get("/api/does-not-exist")
    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "not_found"
    assert body["error"]["message"]


def test_validation_error_uses_error_envelope():
    response = make_client().get("/api/_test/validated", params={"count": "not-a-number"})
    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "validation_error"
    assert "count" in body["error"]["message"]


def test_unhandled_exception_masks_internals():
    response = make_client().get("/api/_test/boom")
    assert response.status_code == 500
    body = response.json()
    assert body["error"]["code"] == "internal_error"
    assert "kaboom" not in body["error"]["message"]
