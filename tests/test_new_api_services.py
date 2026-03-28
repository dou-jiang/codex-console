from fastapi import FastAPI

from src.web.routes.upload.new_api_services import router


def test_new_api_router_registers_expected_paths():
    app = FastAPI()
    app.include_router(router, prefix="/new-api-services")

    paths = {route.path for route in app.routes}

    assert "/new-api-services" in paths
    assert "/new-api-services/{service_id}" in paths
    assert "/new-api-services/{service_id}/full" in paths
    assert "/new-api-services/{service_id}/test" in paths
    assert "/new-api-services/test-connection" in paths
    assert "/new-api-services/upload" in paths
