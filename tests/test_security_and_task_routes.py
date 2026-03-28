from fastapi.routing import APIRoute

from src.web.app import create_app
from src.web.auth import require_api_auth
from src.web.routes import tasks as tasks_routes


def _find_route(app, path: str, method: str) -> APIRoute:
    wanted_method = method.upper()
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        if route.path == path and wanted_method in route.methods:
            return route
    raise AssertionError(f"Route not found: {method} {path}")


def test_app_has_first_run_password_setup_route():
    app = create_app()
    _find_route(app, "/setup-password", "GET")
    _find_route(app, "/setup-password", "POST")


def test_api_accounts_route_has_auth_dependency():
    app = create_app()
    route = _find_route(app, "/api/accounts", "GET")
    dependency_calls = [dep.call for dep in route.dependant.dependencies]
    assert require_api_auth in dependency_calls


def test_unified_tasks_router_exposes_summary_and_cancel():
    paths = {route.path for route in tasks_routes.router.routes if hasattr(route, "path")}
    assert "/summary" in paths
    assert "/{domain}/{task_id}" in paths
    assert "/{domain}/{task_id}/cancel" in paths
