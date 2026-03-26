from packages.registration_core.models import RegistrationInput, RegistrationResult


def test_registration_result_failure_shape():
    result = RegistrationResult(success=False, error_message="boom")

    assert result.success is False
    assert result.error_message == "boom"
    assert result.logs == []


def test_registration_input_defaults():
    data = RegistrationInput(email_service_type="duck_mail")

    assert data.email_service_type == "duck_mail"
    assert data.proxy_url is None
