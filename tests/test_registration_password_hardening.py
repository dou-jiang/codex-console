from src.config.constants import PASSWORD_SPECIAL_CHARSET
from src.core import register as register_module
from src.core.anyauto.register_flow import AnyAutoRegistrationEngine
from src.core.register import RegistrationEngine
from src.core.utils import generate_password


def _assert_password_is_hardened(password: str) -> None:
    assert len(password) >= 8
    assert any(ch.islower() for ch in password)
    assert any(ch.isupper() for ch in password)
    assert any(ch.isdigit() for ch in password)
    assert any(ch in PASSWORD_SPECIAL_CHARSET for ch in password)


def test_generate_password_contains_special_characters():
    _assert_password_is_hardened(generate_password(12))


def test_registration_engine_generate_password_contains_special_characters():
    engine = RegistrationEngine.__new__(RegistrationEngine)
    _assert_password_is_hardened(RegistrationEngine._generate_password(engine, 12))


def test_anyauto_generate_password_contains_special_characters():
    _assert_password_is_hardened(AnyAutoRegistrationEngine._build_password(12))


def test_register_password_with_retry_retries_generic_400(monkeypatch):
    engine = RegistrationEngine.__new__(RegistrationEngine)
    attempts = []
    logs = []

    def fake_register_password(_did=None, _sen_token=None):
        attempts.append(1)
        if len(attempts) < 3:
            engine._last_register_password_error = "注册密码接口返回异常: Failed to create account. Please try again."
            return False, None
        return True, "Aa1!retryPwd"

    monkeypatch.setattr(register_module.time, "sleep", lambda _seconds: None)
    engine._register_password = fake_register_password
    engine._last_register_password_error = None
    engine._log = lambda message, level="info": logs.append((level, message))

    success, password = RegistrationEngine._register_password_with_retry(engine, None, None)

    assert success is True
    assert password == "Aa1!retryPwd"
    assert len(attempts) == 3
    assert any("可重试 400" in message for _level, message in logs)
