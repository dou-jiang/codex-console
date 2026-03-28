import asyncio

from src.config.constants import EmailServiceType
from src.web.routes.email import get_service_types
from src.web.routes.payment import _normalize_email_service_config_for_session_bootstrap
from src.web.routes.registration import _normalize_email_service_config


def test_registration_normalization_maps_cloudmail_default_domain_to_domain():
    normalized = _normalize_email_service_config(
        EmailServiceType.CLOUDMAIL,
        {
            "base_url": "https://mail.example.com",
            "default_domain": "example.com",
            "admin_password": "secret",
        },
        proxy_url="http://127.0.0.1:7890",
    )

    assert normalized["base_url"] == "https://mail.example.com"
    assert normalized["domain"] == "example.com"
    assert "default_domain" not in normalized
    assert normalized["proxy_url"] == "http://127.0.0.1:7890"


def test_payment_bootstrap_normalization_keeps_cloudmail_direct():
    normalized = _normalize_email_service_config_for_session_bootstrap(
        EmailServiceType.CLOUDMAIL,
        {
            "base_url": "https://mail.example.com",
            "default_domain": "example.com",
            "admin_password": "secret",
        },
        proxy_url="http://127.0.0.1:7890",
    )

    assert normalized["domain"] == "example.com"
    assert "default_domain" not in normalized
    assert "proxy_url" not in normalized


def test_email_service_types_includes_cloudmail():
    payload = asyncio.run(get_service_types())
    type_values = {item["value"] for item in payload["types"]}
    assert "cloudmail" in type_values
