import asyncio
from contextlib import contextmanager
from pathlib import Path

from src.config.constants import EmailServiceType
from src.database.models import Base, EmailService
from src.database.session import DatabaseSessionManager
from src.services.base import EmailServiceFactory
from src.web.routes import email as email_routes
from src.web.routes import registration as registration_routes


class DummySettings:
    custom_domain_base_url = ""
    custom_domain_api_key = None


def test_cloudmail_service_registered():
    service_type = EmailServiceType("cloudmail")
    service_class = EmailServiceFactory.get_service_class(service_type)
    assert service_class is not None
    assert service_class.__name__ == "CloudMailService"


def test_email_service_types_include_cloudmail():
    result = asyncio.run(email_routes.get_service_types())
    cloudmail_type = next(item for item in result["types"] if item["value"] == "cloudmail")

    assert cloudmail_type["label"] == "CloudMail"
    field_names = [field["name"] for field in cloudmail_type["config_fields"]]
    assert "base_url" in field_names
    assert "login_email" in field_names
    assert "login_password" in field_names
    assert "default_domain" in field_names


def test_filter_sensitive_config_marks_cloudmail_login_password():
    filtered = email_routes.filter_sensitive_config({
        "base_url": "https://cloudmail.example.test",
        "login_email": "admin@example.test",
        "login_password": "super-secret",
        "default_domain": "mail.example.test",
    })

    assert filtered["base_url"] == "https://cloudmail.example.test"
    assert filtered["login_email"] == "admin@example.test"
    assert filtered["default_domain"] == "mail.example.test"
    assert filtered["has_login_password"] is True
    assert "login_password" not in filtered


def test_registration_available_services_include_cloudmail(monkeypatch):
    runtime_dir = Path("tests_runtime")
    runtime_dir.mkdir(exist_ok=True)
    db_path = runtime_dir / "cloudmail_routes.db"
    if db_path.exists():
        db_path.unlink()

    manager = DatabaseSessionManager(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=manager.engine)

    with manager.session_scope() as session:
        session.add(
            EmailService(
                service_type="cloudmail",
                name="CloudMail Primary",
                config={
                    "base_url": "https://cloudmail.example.test",
                    "login_email": "admin@example.test",
                    "login_password": "super-secret",
                    "default_domain": "mail.example.test",
                    "poll_interval": 3,
                },
                enabled=True,
                priority=0,
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

    result = asyncio.run(registration_routes.get_available_email_services())

    assert result["cloudmail"]["available"] is True
    assert result["cloudmail"]["count"] == 1
    assert result["cloudmail"]["services"][0]["name"] == "CloudMail Primary"
    assert result["cloudmail"]["services"][0]["type"] == "cloudmail"
    assert result["cloudmail"]["services"][0]["default_domain"] == "mail.example.test"
    assert result["cloudmail"]["services"][0]["login_email"] == "admin@example.test"
