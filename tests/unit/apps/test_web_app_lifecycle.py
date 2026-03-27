from src.web.app import create_app


def test_create_app_uses_lifespan_instead_of_on_event():
    app = create_app()

    assert app.router.on_startup == []
    assert app.router.on_shutdown == []
