from src.web.app import create_app
from fastapi.testclient import TestClient


def test_create_app_uses_lifespan_instead_of_on_event():
    app = create_app()

    assert app.router.on_startup == []
    assert app.router.on_shutdown == []


def test_healthz_route_is_available_without_auth():
    app = create_app()
    client = TestClient(app)

    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"ok": True}
