import asyncio
from contextlib import contextmanager
from pathlib import Path

from pydantic import SecretStr

from src.config.constants import EmailServiceType
from src.database.models import Base, EmailService
from src.database.session import DatabaseSessionManager
from src.services.base import EmailServiceFactory
from src.web.routes import email as email_routes
from src.web.routes import registration as registration_routes


class DummySettings:
    tempmail_enabled = True
    yyds_mail_enabled = True
    yyds_mail_base_url = "https://maliapi.215.im/v1"
    yyds_mail_api_key = SecretStr("AC-test-key")
    yyds_mail_default_domain = "public.example.com"
    custom_domain_base_url = ""
    custom_domain_api_key = None


def test_yyds_mail_service_registered():
    service_type = EmailServiceType("yyds_mail")
    service_class = EmailServiceFactory.get_service_class(service_type)
    assert service_class is not None
    assert service_class.__name__ == "YYDSMailService"


def test_email_service_types_include_yyds_mail():
    result = asyncio.run(email_routes.get_service_types())
    yyds_type = next(item for item in result["types"] if item["value"] == "yyds_mail")

    assert yyds_type["label"] == "YYDS Mail"
    field_names = [field["name"] for field in yyds_type["config_fields"]]
    assert "base_url" in field_names
    assert "api_key" in field_names
    assert "default_domain" in field_names


def test_filter_sensitive_config_marks_yyds_api_key():
    filtered = email_routes.filter_sensitive_config({
        "base_url": "https://maliapi.215.im/v1",
        "api_key": "AC-test-key",
        "default_domain": "public.example.com",
    })

    assert filtered["base_url"] == "https://maliapi.215.im/v1"
    assert filtered["default_domain"] == "public.example.com"
    assert filtered["has_api_key"] is True
    assert "api_key" not in filtered


def test_registration_available_services_include_yyds_mail(monkeypatch):
    runtime_dir = Path("tests_runtime")
    runtime_dir.mkdir(exist_ok=True)
    db_path = runtime_dir / "yyds_routes.db"
    if db_path.exists():
        db_path.unlink()

    manager = DatabaseSessionManager(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=manager.engine)

    @contextmanager
    def fake_get_db():
        session = manager.SessionLocal()
        try:
            yield session
        finally:
            session.close()

    monkeypatch.setattr(registration_routes, "get_db", fake_get_db)

    import src.config.settings as settings_module

    monkeypatch.setattr(settings_module, "get_settings", lambda: DummySettings())
    monkeypatch.setattr(registration_routes, "get_settings", lambda: DummySettings())

    result = asyncio.run(registration_routes.get_available_email_services())

    assert result["yyds_mail"]["available"] is True
    assert result["yyds_mail"]["count"] == 1
    assert result["yyds_mail"]["services"][0]["name"] == "YYDS Mail"
    assert result["yyds_mail"]["services"][0]["type"] == "yyds_mail"
    assert result["yyds_mail"]["services"][0]["default_domain"] == "public.example.com"


def test_registration_available_services_include_custom_yyds_mail(monkeypatch):
    runtime_dir = Path("tests_runtime")
    runtime_dir.mkdir(exist_ok=True)
    db_path = runtime_dir / "custom_yyds_routes.db"
    if db_path.exists():
        db_path.unlink()

    manager = DatabaseSessionManager(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=manager.engine)

    with manager.session_scope() as session:
        session.add(
            EmailService(
                service_type="yyds_mail",
                name="YYDS Mail Custom",
                config={
                    "base_url": "https://maliapi.custom.test/v1",
                    "api_key": "AC-custom-key",
                    "default_domain": "custom.example.com",
                },
                enabled=True,
                priority=1,
            )
        )

    @contextmanager
    def fake_get_db():
        session = manager.SessionLocal()
        try:
            yield session
        finally:
            session.close()

    monkeypatch.setattr(registration_routes, "get_db", fake_get_db)

    import src.config.settings as settings_module

    monkeypatch.setattr(settings_module, "get_settings", lambda: DummySettings())
    monkeypatch.setattr(registration_routes, "get_settings", lambda: DummySettings())

    result = asyncio.run(registration_routes.get_available_email_services())

    assert result["yyds_mail"]["available"] is True
    assert result["yyds_mail"]["count"] == 2
    assert result["yyds_mail"]["services"][0]["name"] == "YYDS Mail"
    assert result["yyds_mail"]["services"][1]["name"] == "YYDS Mail Custom"
    assert result["yyds_mail"]["services"][1]["id"] is not None
    assert result["yyds_mail"]["services"][1]["default_domain"] == "custom.example.com"
