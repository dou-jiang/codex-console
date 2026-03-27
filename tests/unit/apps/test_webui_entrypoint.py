from pydantic.types import SecretStr
import pytest


from src.webui_entry import (
    _apply_legacy_env_aliases,
    _collect_runtime_overrides,
    _enforce_startup_safety,
)


def test_apply_legacy_env_aliases_maps_old_names_to_app_names():
    env = {
        "WEBUI_HOST": "0.0.0.0",
        "WEBUI_PORT": "1455",
        "WEBUI_ACCESS_PASSWORD": "strong-pass-1234",
    }

    _apply_legacy_env_aliases(env)

    assert env["APP_HOST"] == "0.0.0.0"
    assert env["APP_PORT"] == "1455"
    assert env["APP_ACCESS_PASSWORD"] == "strong-pass-1234"


def test_apply_legacy_env_aliases_keeps_existing_app_values():
    env = {
        "APP_HOST": "127.0.0.1",
        "WEBUI_HOST": "0.0.0.0",
    }

    _apply_legacy_env_aliases(env)

    assert env["APP_HOST"] == "127.0.0.1"


def test_collect_runtime_overrides_prefers_cli_then_app_env():
    class Args:
        host = None
        port = None
        debug = False
        log_level = None
        access_password = None

    overrides = _collect_runtime_overrides(
        Args(),
        {
            "APP_HOST": "127.0.0.1",
            "APP_PORT": "9000",
            "APP_ACCESS_PASSWORD": "strong-pass-1234",
            "LOG_LEVEL": "INFO",
        },
    )

    assert overrides["webui_host"] == "127.0.0.1"
    assert overrides["webui_port"] == 9000
    assert overrides["webui_access_password"] == "strong-pass-1234"
    assert overrides["log_level"] == "INFO"


def test_enforce_startup_safety_rejects_weak_password_in_production():
    class Settings:
        debug = False
        webui_access_password = SecretStr("admin123")

    with pytest.raises(SystemExit):
        _enforce_startup_safety(Settings())


def test_enforce_startup_safety_allows_strong_password():
    class Settings:
        debug = False
        webui_access_password = SecretStr("StrongPass123!")

    _enforce_startup_safety(Settings())
