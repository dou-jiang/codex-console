import asyncio
from contextlib import contextmanager
from pathlib import Path

import pytest

from src.config.constants import EmailServiceType
from src.database.models import Base, EmailService
from src.database.session import DatabaseSessionManager
from src.services import YahooMailService
from src.services.base import EmailServiceFactory
from src.web.routes import email as email_routes
from src.web.routes import registration as registration_routes


class DummySettings:
    custom_domain_base_url = ""
    custom_domain_api_key = None
    tempmail_enabled = False
    yyds_mail_enabled = False
    yyds_mail_api_key = None
    yyds_mail_default_domain = ""


def test_yahoo_mail_service_registered():
    service_type = EmailServiceType("yahoo_mail")
    service_class = EmailServiceFactory.get_service_class(service_type)
    assert service_class is not None
    assert service_class.__name__ == "YahooMailService"


def test_email_service_types_include_yahoo_mail():
    result = asyncio.run(email_routes.get_service_types())
    yahoo_type = next(item for item in result["types"] if item["value"] == "yahoo_mail")

    assert yahoo_type["label"] == "Yahoo Mail"
    field_names = [field["name"] for field in yahoo_type["config_fields"]]
    assert "parent_email" in field_names
    assert "parent_app_password" in field_names
    assert "email" in field_names
    assert "password" in field_names
    assert "app_password" in field_names
    assert "birth_year" in field_names
    assert "headless" in field_names


def test_filter_sensitive_config_marks_yahoo_secret_fields():
    filtered = email_routes.filter_sensitive_config(
        {
            "email": "test@yahoo.com",
            "password": "secret",
            "app_password": "imap-secret",
            "parent_password": "parent-secret",
            "parent_app_password": "parent-imap-secret",
            "phone_number": "+10000000000",
            "recovery_email": "backup@example.com",
        }
    )

    assert filtered["email"] == "test@yahoo.com"
    assert filtered["has_password"] is True
    assert filtered["has_app_password"] is True
    assert filtered["has_parent_password"] is True
    assert filtered["has_parent_app_password"] is True
    assert filtered["has_phone_number"] is True
    assert filtered["has_recovery_email"] is True
    assert "password" not in filtered
    assert "app_password" not in filtered


def test_yahoo_mail_existing_account_create_email_reuses_config():
    service = YahooMailService(
        {
            "email": "sample@yahoo.com",
            "password": "mail-password",
            "app_password": "imap-password",
            "headless": True,
        }
    )

    info = service.create_email()

    assert info["email"] == "sample@yahoo.com"
    assert info["service_id"] == "sample@yahoo.com"
    assert info["password"] == "mail-password"
    assert info["app_password"] == "imap-password"
    assert info["mode"] == "existing"


def test_yahoo_mail_existing_account_create_email_accepts_app_password_only():
    service = YahooMailService(
        {
            "email": "sample@yahoo.com",
            "app_password": "imap-password",
            "headless": True,
        }
    )

    info = service.create_email()

    assert info["email"] == "sample@yahoo.com"
    assert info["app_password"] == "imap-password"
    assert info["mode"] == "existing"


def test_yahoo_mail_parent_alias_mode_creates_child_alias(monkeypatch):
    service = YahooMailService(
        {
            "parent_email": "parent@yahoo.com",
            "parent_password": "parent-password",
            "parent_app_password": "imap-parent-secret",
            "headless": True,
        }
    )

    monkeypatch.setattr(
        service,
        "_create_alias_with_parent_mailbox",
        lambda: {
            "email": "alexriver12-demo88@yahoo.com",
            "service_id": "alexriver12-demo88@yahoo.com",
            "id": "alexriver12-demo88@yahoo.com",
            "parent_email": "parent@yahoo.com",
            "mailbox_owner_email": "parent@yahoo.com",
            "mailbox_owner_app_password": "imap-parent-secret",
            "mode": "parent_alias",
        },
    )

    info = service.create_email()

    assert info["email"] == "alexriver12-demo88@yahoo.com"
    assert info["parent_email"] == "parent@yahoo.com"
    assert info["mode"] == "parent_alias"


def test_yahoo_mail_parent_alias_requires_parent_password():
    service = YahooMailService(
        {
            "parent_email": "parent@yahoo.com",
            "parent_app_password": "imap-parent-secret",
            "headless": True,
        }
    )

    with pytest.raises(Exception):
        service.create_email()


def test_yahoo_mail_child_profile_auto_generated_when_blank():
    service = YahooMailService(
        {
            "parent_email": "parent@yahoo.com",
            "parent_app_password": "imap-parent-secret",
        }
    )

    profile = service._build_child_profile()

    assert profile["first_name"]
    assert profile["last_name"]
    assert 1 <= profile["birth_month"] <= 12
    assert 1 <= profile["birth_day"] <= 31
    assert 1960 <= profile["birth_year"] <= 2008


def test_yahoo_mail_alias_reads_openai_code_from_parent_mailbox(monkeypatch):
    service = YahooMailService(
        {
            "parent_email": "parent@yahoo.com",
            "parent_app_password": "imap-parent-secret",
        }
    )

    service._cache_account(
        {
            "email": "alexriver12-demo88@yahoo.com",
            "service_id": "alexriver12-demo88@yahoo.com",
            "id": "alexriver12-demo88@yahoo.com",
            "parent_email": "parent@yahoo.com",
            "mailbox_owner_email": "parent@yahoo.com",
            "mailbox_owner_app_password": "imap-parent-secret",
            "mode": "parent_alias",
        }
    )

    captured = {}

    def fake_get_via_imap(mailbox_email, password_value, timeout, pattern, otp_sent_at, target_email=None):
        captured["mailbox_email"] = mailbox_email
        captured["password_value"] = password_value
        captured["target_email"] = target_email
        return "123456"

    monkeypatch.setattr(service, "_get_verification_code_via_imap", fake_get_via_imap)

    code = service.get_verification_code("alexriver12-demo88@yahoo.com", "alexriver12-demo88@yahoo.com", timeout=30)

    assert code == "123456"
    assert captured["mailbox_email"] == "parent@yahoo.com"
    assert captured["password_value"] == "imap-parent-secret"
    assert captured["target_email"] == "alexriver12-demo88@yahoo.com"


def test_yahoo_mail_prefers_roxy_otp_over_fresh_browser_login(monkeypatch):
    service = YahooMailService(
        {
            "parent_email": "parent@yahoo.com",
            "parent_password": "parent-password",
            "parent_app_password": "imap-parent-secret",
            "roxy_ws_endpoint": "ws://172.28.64.1:56338/devtools/browser/mock",
            "prefer_roxy_otp": True,
        }
    )

    service._cache_account(
        {
            "email": "alexriver12-demo88@yahoo.com",
            "service_id": "alexriver12-demo88@yahoo.com",
            "id": "alexriver12-demo88@yahoo.com",
            "parent_email": "parent@yahoo.com",
            "mailbox_owner_email": "parent@yahoo.com",
            "mailbox_owner_password": "parent-password",
            "mailbox_owner_app_password": "imap-parent-secret",
            "mode": "parent_alias",
            "roxy_ws_endpoint": "ws://172.28.64.1:56338/devtools/browser/mock",
            "prefer_roxy_otp": True,
        }
    )

    roxy_calls = {}

    monkeypatch.setattr(
        service,
        "_get_verification_code_via_imap",
        lambda mailbox_email, password_value, timeout, pattern, otp_sent_at, target_email=None: None,
    )

    def fake_roxy(ws_endpoint, timeout, pattern, otp_sent_at, target_email, mailbox_email):
        roxy_calls["ws_endpoint"] = ws_endpoint
        roxy_calls["timeout"] = timeout
        roxy_calls["target_email"] = target_email
        roxy_calls["mailbox_email"] = mailbox_email
        return "654321"

    monkeypatch.setattr(service, "_get_verification_code_via_roxy_browser", fake_roxy)
    monkeypatch.setattr(
        service,
        "_get_verification_code_via_browser",
        lambda *args, **kwargs: pytest.fail("should not launch a fresh Yahoo browser when Roxy OTP is available"),
    )

    code = service.get_verification_code(
        "alexriver12-demo88@yahoo.com",
        "alexriver12-demo88@yahoo.com",
        timeout=30,
    )

    assert code == "654321"
    assert roxy_calls["ws_endpoint"] == "ws://172.28.64.1:56338/devtools/browser/mock"
    assert roxy_calls["target_email"] == "alexriver12-demo88@yahoo.com"
    assert roxy_calls["mailbox_email"] == "parent@yahoo.com"


def test_extract_best_alias_from_text_prefers_matching_keyword():
    service = YahooMailService({"parent_email": "parent@yahoo.com", "parent_password": "pw"})
    text = """
    oldalias@yahoo.com
    foo@yahoo.com
    susanbrown50-susanbro2mmd@yahoo.com
    """
    alias = service._extract_best_alias_from_text(
        text,
        domain="yahoo.com",
        nickname="susanbrown50",
        keyword="susanbro2mmd",
        fallback_alias="fallback@yahoo.com",
    )
    assert alias == "susanbrown50-susanbro2mmd@yahoo.com"


def test_detect_yahoo_blocker_allows_password_challenge_path():
    service = YahooMailService(
        {
            "parent_email": "parent@yahoo.com",
            "parent_password": "parent-password",
        }
    )

    blocker = service._detect_yahoo_blocker(
        "Enter your password",
        "https://login.yahoo.com/account/challenge/password?src=ym",
    )

    assert blocker is None


def test_detect_yahoo_blocker_rejects_real_challenge_path():
    service = YahooMailService(
        {
            "parent_email": "parent@yahoo.com",
            "parent_password": "parent-password",
        }
    )

    blocker = service._detect_yahoo_blocker(
        "Please verify it's you",
        "https://login.yahoo.com/account/challenge/ts?done=https%3A%2F%2Fmail.yahoo.com",
    )

    assert blocker is not None
    assert "challenge" in blocker.lower()


def test_try_resolve_yahoo_challenge_selector_ignores_non_selector_page():
    service = YahooMailService(
        {
            "parent_email": "parent@yahoo.com",
            "parent_password": "parent-password",
        }
    )

    class DummyPage:
        url = "https://login.yahoo.com/account/challenge/password"

    assert service._try_resolve_yahoo_challenge_selector(DummyPage()) is False


def test_check_yahoo_blocker_raises_with_selector_snippet():
    service = YahooMailService(
        {
            "parent_email": "parent@yahoo.com",
            "parent_password": "parent-password",
        }
    )

    class DummyLocator:
        def count(self):
            return 0

    class DummyPage:
        url = "https://login.yahoo.com/account/challenge/challenge-selector"

        def wait_for_timeout(self, _ms):
            return None

        def locator(self, _selector):
            return DummyLocator()

    service._extract_visible_text = lambda page: "Choose a way to sign in Password Account Key"

    with pytest.raises(Exception) as exc_info:
        service._check_yahoo_blocker(DummyPage())

    assert "challenge-selector" in str(exc_info.value)
    assert "Choose a way to sign in" in str(exc_info.value)


def test_complete_yahoo_email_identity_challenge_waits_for_parent_code(monkeypatch):
    service = YahooMailService(
        {
            "parent_email": "parent@yahoo.com",
            "parent_password": "parent-password",
            "parent_app_password": "imap-parent-secret",
        }
    )

    clicks = []

    class DummyPage:
        url = "https://login.yahoo.com/account/challenge/challenge-selector"

        def wait_for_timeout(self, _ms):
            return None

    monkeypatch.setattr(
        service,
        "_extract_visible_text",
        lambda page: "Is it really you? To keep your account secure, quickly verify your identity. Email Get a code at parent@yahoo.com",
    )
    monkeypatch.setattr(service, "_click_first", lambda page, selectors: clicks.append(tuple(selectors)) or True)
    monkeypatch.setattr(service, "_wait_for_parent_signup_code", lambda timeout: "123456")
    monkeypatch.setattr(service, "_fill_signup_verification_code", lambda page, code: code == "123456")

    assert service._complete_yahoo_email_identity_challenge(DummyPage()) is True
    assert len(clicks) >= 2


def test_registration_available_services_include_yahoo_mail(monkeypatch):
    runtime_dir = Path("tests_runtime")
    runtime_dir.mkdir(exist_ok=True)
    db_path = runtime_dir / "yahoo_routes.db"
    if db_path.exists():
        db_path.unlink()

    manager = DatabaseSessionManager(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=manager.engine)

    with manager.session_scope() as session:
        session.add(
            EmailService(
                service_type="yahoo_mail",
                name="Yahoo 自动邮箱",
                config={
                    "email": "",
                    "parent_email": "parent@yahoo.com",
                    "domain": "yahoo.com",
                    "headless": True,
                    "first_name": "Alex",
                    "last_name": "River",
                    "birth_month": 1,
                    "birth_day": 1,
                    "birth_year": 1998,
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
    monkeypatch.setattr(registration_routes, "get_settings", lambda: DummySettings())

    result = asyncio.run(registration_routes.get_available_email_services())

    assert result["yahoo_mail"]["available"] is True
    assert result["yahoo_mail"]["count"] == 1
    assert result["yahoo_mail"]["services"][0]["name"] == "Yahoo 自动邮箱"
    assert result["yahoo_mail"]["services"][0]["type"] == "yahoo_mail"
    assert result["yahoo_mail"]["services"][0]["parent_email"] == "parent@yahoo.com"
    assert result["yahoo_mail"]["services"][0]["domain"] == "yahoo.com"
    assert result["yahoo_mail"]["services"][0]["auto_create"] is True


def test_validate_yahoo_mail_config_accepts_parent_alias_defaults():
    registration_routes._validate_yahoo_mail_config(
        {
            "parent_email": "parent@yahoo.com",
            "parent_password": "parent-password",
        }
    )


def test_validate_yahoo_mail_config_rejects_alias_without_parent_password():
    with pytest.raises(ValueError):
        registration_routes._validate_yahoo_mail_config(
            {
                "parent_email": "parent@yahoo.com",
                "parent_app_password": "imap-parent-secret",
            }
        )


def test_validate_yahoo_mail_config_accepts_fixed_inbox_with_app_password():
    registration_routes._validate_yahoo_mail_config(
        {
            "email": "child@yahoo.com",
            "app_password": "imap-child-secret",
        }
    )
