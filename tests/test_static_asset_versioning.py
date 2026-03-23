from pathlib import Path
import asyncio
import importlib

from starlette.requests import Request

web_app = importlib.import_module("src.web.app")


def test_static_asset_version_is_non_empty_string():
    version = web_app._build_static_asset_version(web_app.STATIC_DIR)

    assert isinstance(version, str)
    assert version
    assert version.isdigit()


def test_email_services_template_uses_versioned_static_assets():
    template = Path("templates/email_services.html").read_text(encoding="utf-8")

    assert '/static/css/style.css?v={{ static_version }}' in template
    assert '/static/js/utils.js?v={{ static_version }}' in template
    assert '/static/js/email_services.js?v={{ static_version }}' in template


def test_index_template_uses_versioned_static_assets():
    template = Path("templates/index.html").read_text(encoding="utf-8")

    assert '/static/css/style.css?v={{ static_version }}' in template
    assert '/static/js/utils.js?v={{ static_version }}' in template
    assert '/static/js/app.js?v={{ static_version }}' in template


def test_login_page_renders_without_template_cache_error():
    app = web_app.create_app()

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/login",
        "raw_path": b"/login",
        "query_string": b"",
        "headers": [],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
        "root_path": "",
    }
    request = Request(scope)

    login_route = next(route for route in app.routes if getattr(route, "path", None) == "/login" and "GET" in getattr(route, "methods", set()))

    response = asyncio.run(login_route.endpoint(request, next="/"))

    assert response.status_code == 200
    assert "密码" in response.body.decode("utf-8")
